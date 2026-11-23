import base64, datetime as dt, hashlib, json, re, threading, time, uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import pandas as pd

pd.set_option("display.max_colwidth", None)   # never cut error messages or fixes in tables
import requests

try:
    notebookutils  # preloaded in Fabric notebooks
except NameError:
    import notebookutils

# ---------------------------------------------------------------- V2 settings -> backup switches
_mode = str(globals().get("KQL_BACKUP_MODE", "parquet")).strip().lower()
if _mode not in ("parquet", "adx", "both"):
    raise ValueError(f'KQL_BACKUP_MODE must be "parquet", "adx" or "both", not "{KQL_BACKUP_MODE}"')
KQL_BACKUP_MODE = _mode
EXPORT_KQL_TABLES = KQL_BACKUP_MODE in ("parquet", "both")
COPY_KQL_TO_ADX = KQL_BACKUP_MODE in ("adx", "both")
KC_TENANT_ID = str(globals().get("KC_TENANT_ID") or "").strip() or str(globals().get("ADO_SP_TENANT_ID") or "").strip() or "<tenant-id>"
KC_CLIENT_ID = str(globals().get("KC_CLIENT_ID") or "").strip() or str(globals().get("ADO_SP_CLIENT_ID") or "").strip() or "<app-client-id>"
KC_CLIENT_SECRET_NAME = (str(globals().get("KC_CLIENT_SECRET_NAME") or "").strip()
                         or str(globals().get("ADO_SP_SECRET_NAME") or "").strip() or "<secret-name>")
BACKUP_DEFINITIONS_TO_ADLS = True   # V2_3 checks the definition copy of every item
PUSH_DEFINITIONS_TO_DEVOPS = False  # V2: the workspace's own Git link (V2_1/V2_3) is the Git copy
ADO_ORGANIZATION = ADO_PROJECT = ADO_REPOSITORY = ADO_PAT_SECRET_NAME = ""
ADO_BRANCH, ADO_ROOT_FOLDER = "", ""
FREEZE_STARTED_UTC = str(globals().get("FREEZE_STARTED_UTC") or "").strip()

FABRIC_API = "https://api.fabric.microsoft.com/v1"
ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
STORAGE_VERSION = "2023-11-03"
ADO_API = "api-version=7.1"
MAX_ATTEMPTS = 6
MAX_RETRY_WAIT_SECONDS = 15 * 60
LRO_TIMEOUT_SECONDS = 60 * 60
KQL_EXPORT_TIMEOUT_SECONDS = 4 * 60 * 60
CHUNK_BYTES = 8 * 1024 * 1024
ADO_PUSH_MAX_BYTES = 15 * 1024 * 1024
ADO_PUSH_MAX_CHANGES = 500
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RUN_ID = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
NO_DEFINITION_TYPES = {"SQLEndpoint"}           # no definition API: scripted with T-SQL instead
ONELAKE_DATA_TYPES = {"Lakehouse", "Warehouse"}
# Dataflow Gen2 staging items: Fabric creates them for Dataflows Gen2 (one Lakehouse and one Warehouse per workspace,
# named StagingLakehouseForDataflows_<timestamp> / StagingWarehouseForDataflows_<timestamp>) and creates them again
# when a Dataflow Gen2 runs. Not saved in Git, not backed up, not restored; V2_4 still deletes them (a Fabric item
# left behind blocks a tenant migration).
STAGING_RE = re.compile(r"^Staging(Lakehouse|Warehouse)ForDataflows(_\d+)?$", re.IGNORECASE)


def is_dataflow_staging(item):
    return bool(item.get("staging")) or (item.get("type") in ("Lakehouse", "Warehouse", "SQLEndpoint")
                                         and bool(STAGING_RE.match(str(item.get("displayName") or ""))))

RESULTS = []           # one row per item and step: the backup log
_lock = threading.Lock()
CALLS, RETRIES = Counter(), Counter()


def norm_id(value):
    text = str(value or "").strip().strip("{}").lower()
    return text if GUID_RE.match(text) else None


def safe_name(name, max_len=80):
    """A folder-safe version of a Fabric display name."""
    text = re.sub(r'[\\/:*?"<>|#%\x00-\x1f]', "_", str(name or "")).strip().strip(".")
    return (text[:max_len].rstrip(" .") or "unnamed")


def log(item, step, status, detail="", files=0, size=0, rows=None):
    detail = redact_sensitive_text(str(detail))
    with _lock:
        RESULTS.append({"Workspace": item.get("workspaceName", ""), "WorkspaceId": item.get("workspaceId", ""),
                        "Item": item.get("displayName", ""), "Type": item.get("type", ""),
                        "ItemId": item.get("id", ""), "Step": step, "Status": status, "Files": files,
                        "Bytes": size, "Rows": rows, "Detail": str(detail)})


def show(df, rows=200):
    try:
        display(df.head(rows))
    except NameError:
        print(df.head(rows).to_string())


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024


class HttpError(Exception):
    def __init__(self, status, code, message, url):
        self.status, self.code, self.message, self.url = status, code or "", redact_sensitive_text(str(message or "")), url
        super().__init__(self.short())

    def short(self):
        head = f"HTTP {self.status} {self.code}".strip() if self.status else str(self.code)
        return f"{head}: {self.message}" if self.message else head


RAW_BODY_LIMIT = 4000   # only for non-JSON bodies (for example a whole HTML page); JSON error messages are kept whole


def _raw_text(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= RAW_BODY_LIMIT:
        return text
    return text[:RAW_BODY_LIMIT] + f" ... [{len(text) - RAW_BODY_LIMIT} more characters of the response body]"


def _error_details(resp):
    try:
        body = resp.json()
    except ValueError:
        return "", _raw_text(resp.text)
    if not isinstance(body, dict):
        return "", str(body)
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    code = err.get("errorCode") or err.get("code") or err.get("typeKey") or err.get("error") or ""
    msg = err.get("message") or err.get("@message") or err.get("error_description") or ""
    return str(code), str(msg)


def _retry_after(resp, attempt):
    try:
        return max(1, int(float(resp.headers.get("Retry-After", ""))))
    except (TypeError, ValueError):
        return min(300, 10 * 2 ** (attempt - 1))


_local = threading.local()


def _session():
    if getattr(_local, "session", None) is None:
        _local.session = requests.Session()
    return _local.session


def http(method, url, audience=None, *, auth=None, headers=None, params=None, json_body=None, data=None,
         timeout=300, retry=True, label="other"):
    """One HTTP call with a fresh Entra token. Retries 408/429/5xx (honouring Retry-After) and network errors."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        hdrs = dict(headers or {})
        if auth:
            hdrs["Authorization"] = auth
        elif audience:
            hdrs["Authorization"] = "Bearer " + notebookutils.credentials.getToken(audience)
        if hdrs.get("Authorization"):
            register_sensitive_value(hdrs["Authorization"].split(" ", 1)[-1])
        try:
            resp = _session().request(method, url, params=params, json=json_body, data=data,
                                      headers=hdrs, timeout=timeout)
        except requests.RequestException as exc:
            if not retry or attempt == MAX_ATTEMPTS:
                raise HttpError(None, type(exc).__name__, str(exc), url)
            time.sleep(min(60, 5 * attempt))
            continue
        with _lock:
            CALLS[label] += 1
        if retry and (resp.status_code in (408, 429) or resp.status_code >= 500):
            wait = _retry_after(resp, attempt)
            if attempt < MAX_ATTEMPTS and wait <= MAX_RETRY_WAIT_SECONDS:
                with _lock:
                    RETRIES[label] += 1
                time.sleep(wait)
                continue
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, *_error_details(resp), url)
        return resp


# ---------------------------------------------------------------- Fabric REST API
def _wait_operation(resp):
    """Follow a Fabric long-running operation (202 Accepted) to its result."""
    location = resp.headers.get("Location")
    op_id = resp.headers.get("x-ms-operation-id")
    if not location and op_id:
        location = f"{FABRIC_API}/operations/{op_id}"
    if not location:
        return {}
    wait = _retry_after(resp, 1) if resp.headers.get("Retry-After") else 2
    deadline = time.monotonic() + LRO_TIMEOUT_SECONDS
    while True:
        time.sleep(min(max(wait, 1), 30))
        state = http("GET", location, "pbi", label="fabric").json()
        status = str(state.get("status", "")).lower()
        if status == "succeeded":
            result = http("GET", location.rstrip("/") + "/result", "pbi", label="fabric")
            return result.json() if result.content else {}
        if status in ("failed", "cancelled", "canceled"):
            err = state.get("error") or {}
            raise HttpError(None, err.get("errorCode") or status, err.get("message"), location)
        if time.monotonic() > deadline:
            raise HttpError(None, "Timeout", "the long-running operation didn't finish", location)
        wait = 3


def fabric(method, path, body=None, params=None):
    resp = http(method, FABRIC_API + path, "pbi", json_body=body, params=params, label="fabric")
    if resp.status_code == 202:
        return _wait_operation(resp)
    return resp.json() if resp.content else {}


def fabric_pages(path, key="value", params=None):
    url, prm, seen = FABRIC_API + path, dict(params or {}), set()
    while url:
        body = http("GET", url, "pbi", params=prm or None, label="fabric").json()
        yield from body.get(key) or []
        if body.get("continuationUri"):
            url, prm = body["continuationUri"], {}
        elif body.get("continuationToken"):
            url, prm = FABRIC_API + path, {**(params or {}), "continuationToken": body["continuationToken"]}
        else:
            break
        marker = (url, json.dumps(prm, sort_keys=True))
        if marker in seen:
            break
        seen.add(marker)


# ---------------------------------------------------------------- ADLS Gen2 (destination)
ADLS_HEADERS = {"x-ms-version": STORAGE_VERSION}


def adls_url(path):
    return f"https://{ADLS_ACCOUNT}.dfs.core.windows.net/{ADLS_CONTAINER}/{quote(path.strip('/'), safe='/')}"


def adls_create(path):
    http("PUT", adls_url(path), "storage", headers=ADLS_HEADERS, params={"resource": "file"}, label="adls")


def adls_append(path, position, data):
    http("PATCH", adls_url(path), "storage", headers=ADLS_HEADERS, data=data,
         params={"action": "append", "position": str(position)}, label="adls")


def adls_flush(path, position):
    http("PATCH", adls_url(path), "storage", headers=ADLS_HEADERS, data=b"",
         params={"action": "flush", "position": str(position)}, label="adls")


def adls_write(path, data):
    """Write a whole (small) file, replacing any existing one."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    adls_create(path)
    for pos in range(0, len(data), CHUNK_BYTES):
        adls_append(path, pos, data[pos:pos + CHUNK_BYTES])
    adls_flush(path, len(data))
    return len(data)


# ---------------------------------------------------------------- OneLake (source)
def onelake_list(workspace_id, item_id, folder):
    """Every file under <item>/<folder> as (relative path, size, last modified). Empty if the folder is missing."""
    params = {"resource": "filesystem", "recursive": "true", "directory": f"{item_id}/{folder}"}
    url = f"{ONELAKE_DFS}/{workspace_id}"
    while True:
        try:
            resp = http("GET", url, "storage", headers=ADLS_HEADERS, params=params, label="onelake")
        except HttpError as exc:
            if exc.status == 404:
                return
            raise
        for entry in resp.json().get("paths", []):
            if str(entry.get("isDirectory", "false")).lower() == "true":
                continue
            rel = entry["name"].split("/", 1)[1] if "/" in entry["name"] else entry["name"]
            yield rel, int(entry.get("contentLength") or 0), entry.get("lastModified", "")
        token = resp.headers.get("x-ms-continuation")
        if not token:
            return
        params = {**params, "continuation": token}


def onelake_read(workspace_id, item_id, rel_path, start, end):
    url = f"{ONELAKE_DFS}/{workspace_id}/{item_id}/{quote(rel_path, safe='/')}"
    resp = http("GET", url, "storage", headers={**ADLS_HEADERS, "Range": f"bytes={start}-{end}"}, label="onelake")
    return resp.content


def copy_onelake_file(workspace_id, item_id, rel_path, size, dest_path):
    """Copy one OneLake file to ADLS in chunks. Retries the whole file once if a chunk fails."""
    for attempt in (1, 2):
        try:
            adls_create(dest_path)
            pos = 0
            while pos < size:
                chunk = onelake_read(workspace_id, item_id, rel_path, pos, min(pos + CHUNK_BYTES, size) - 1)
                if not chunk:
                    raise HttpError(None, "ShortRead", f"no bytes returned at offset {pos}", rel_path)
                adls_append(dest_path, pos, chunk)
                pos += len(chunk)
            adls_flush(dest_path, pos)
            return pos
        except HttpError as exc:
            if attempt == 2 or exc.status == 404:
                raise
            time.sleep(5)


# ---------------------------------------------------------------- Kusto (Eventhouse)
def kusto_mgmt(query_uri, database, command, retry=True, auth=None, kind="mgmt"):
    """Run a Kusto control command (kind="mgmt") or query (kind="query"). auth overrides the runner's token."""
    resp = http("POST", query_uri.rstrip("/") + f"/v1/rest/{kind}", "kusto", auth=auth,
                json_body={"db": database, "csl": command}, label="kusto", retry=retry)
    tables = resp.json().get("Tables") or []
    if not tables:
        return []
    cols = [c["ColumnName"] for c in tables[0].get("Columns", [])]
    return [dict(zip(cols, row)) for row in tables[0].get("Rows", [])]


def kql_string(text):
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


# ---------------------------------------------------------------- Kusto Copy service principal
_kc_tokens = {}


def kc_secret():
    if "secret" not in _kc_tokens:
        _kc_tokens["secret"] = notebookutils.credentials.getSecret(KEY_VAULT_URL, KC_CLIENT_SECRET_NAME)
        register_sensitive_value(_kc_tokens["secret"])
    return _kc_tokens["secret"]


def kc_auth(resource):
    """Authorization header for the Kusto Copy service principal (client credentials), cached until near expiry."""
    scope = resource.rstrip("/") + "/.default"
    token, expires = _kc_tokens.get(scope, (None, 0))
    if not token or time.time() > expires - 300:
        resp = http("POST", f"https://login.microsoftonline.com/{quote(KC_TENANT_ID)}/oauth2/v2.0/token",
                    data={"grant_type": "client_credentials", "client_id": KC_CLIENT_ID,
                          "client_secret": kc_secret(), "scope": scope}, label="entra")
        body = resp.json()
        token, expires = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        register_sensitive_value(token)
        _kc_tokens[scope] = (token, expires)
    return "Bearer " + token


# ---------------------------------------------------------------- ADX databases (Azure Resource Manager)
ARM = "https://management.azure.com"
KUSTO_ARM_API = "2023-08-15"
ADX_ARM = {}            # cluster: id, name, location
ADX_DB_NAMES = {}       # lower-case name -> name, databases that exist on the cluster
ADX_CREATED = {}        # database -> "created" / "would create"


def adx_uri():
    return ADX_CLUSTER_URI.rstrip("/").lower()


def arm(method, path, body=None, api=KUSTO_ARM_API):
    url = path if path.startswith("http") else ARM + path
    if "api-version=" not in url:
        url += ("&" if "?" in url else "?") + "api-version=" + api
    return http(method, url, auth=kc_auth(ARM), json_body=body, label="azure")


def arm_list(path, api=KUSTO_ARM_API):
    out, url = [], path
    while url:
        body = arm("GET", url, api=api).json()
        out += body.get("value", [])
        url = body.get("nextLink")
    return out


def adx_cluster_arm():
    """The ADX cluster's Azure resource (found from ADX_CLUSTER_URI unless subscription and group are set)."""
    if ADX_ARM:
        return ADX_ARM
    name = adx_uri().split("//", 1)[-1].split(".")[0]
    if ADX_SUBSCRIPTION_ID and ADX_RESOURCE_GROUP:
        c = arm("GET", f"/subscriptions/{quote(ADX_SUBSCRIPTION_ID)}/resourceGroups/{quote(ADX_RESOURCE_GROUP)}"
                       f"/providers/Microsoft.Kusto/clusters/{quote(name)}").json()
    else:
        subs = [ADX_SUBSCRIPTION_ID] if ADX_SUBSCRIPTION_ID else \
            [x["subscriptionId"] for x in arm_list("/subscriptions", api="2022-12-01")]
        c = None
        for sub in subs:
            for cl in arm_list(f"/subscriptions/{quote(sub)}/providers/Microsoft.Kusto/clusters"):
                if str((cl.get("properties") or {}).get("uri", "")).rstrip("/").lower() == adx_uri():
                    c = cl
                    break
            if c:
                break
        if not c:
            raise RuntimeError(
                f"The Kusto Copy service principal can't see the ADX cluster {ADX_CLUSTER_URI} in Azure "
                f"(searched {len(subs)} subscription(s)). Give it Contributor on the cluster (Azure portal > the "
                "cluster > Access control (IAM)), or set ADX_SUBSCRIPTION_ID and ADX_RESOURCE_GROUP, or set "
                "ADX_CREATE_DATABASES = False and create the databases yourself.")
    ADX_ARM.update(id=c["id"], name=c["name"], location=c["location"])
    return ADX_ARM


def adx_can_create():
    """True if the service principal's Azure roles on the cluster allow creating databases."""
    import fnmatch
    perms = arm_list(adx_cluster_arm()["id"] + "/providers/Microsoft.Authorization/permissions", api="2022-04-01")
    want = "microsoft.kusto/clusters/databases/write"
    ok = lambda pats: any(fnmatch.fnmatch(want, str(x).lower()) for x in pats or [])
    return any(ok(p.get("actions")) and not ok(p.get("notActions")) for p in perms)


def adx_existing_databases():
    if not ADX_DB_NAMES:
        for d in arm_list(adx_cluster_arm()["id"] + "/databases"):
            n = d["name"].split("/")[-1]
            ADX_DB_NAMES[n.lower()] = n
    return ADX_DB_NAMES


def adx_safe_name(text):
    """Letters, digits, _ . - only; long names keep their start plus a hash, so they stay unique."""
    t = re.sub(r"_{3,}", "__", re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text))).strip("_.-") or "db"
    return t if len(t) <= 200 else t[:150] + "_" + hashlib.sha1(t.encode()).hexdigest()[:8]


def runner_principal():
    """Kusto principal of the person (or app) running the notebook, for ADX_VIEWERS = ["me"]."""
    try:
        part = notebookutils.credentials.getToken("pbi").split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return ""
    if c.get("idtyp") == "app" or ("appid" in c and "upn" not in c and "unique_name" not in c):
        return f"aadapp={c.get('appid', '')};{c.get('tid', '')}"
    upn = c.get("upn") or c.get("unique_name") or ""
    return f"aaduser={upn}" if upn else ""


def create_adx_database(name):
    """Create a ReadWrite database (the service principal becomes its Admin) and wait until it is ready."""
    props = {}
    if ADX_SOFT_DELETE_PERIOD:
        props["softDeletePeriod"] = ADX_SOFT_DELETE_PERIOD
    if ADX_HOT_CACHE_PERIOD:
        props["hotCachePeriod"] = ADX_HOT_CACHE_PERIOD
    path = adx_cluster_arm()["id"] + "/databases/" + quote(name, safe="")
    resp = arm("PUT", path, body={"kind": "ReadWrite", "location": adx_cluster_arm()["location"],
                                  "properties": props})
    op = resp.headers.get("Azure-AsyncOperation") or resp.headers.get("azure-asyncoperation")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        if op:
            status = arm("GET", op).json().get("status", "")
        else:
            status = (arm("GET", path).json().get("properties") or {}).get("provisioningState", "")
        if status.lower() == "succeeded":
            break
        if status.lower() in ("failed", "canceled"):
            raise RuntimeError(f"creating ADX database {name} ended as {status}")
        time.sleep(10)
    else:
        raise RuntimeError(f"ADX database {name} wasn't ready after 15 minutes")
    ADX_DB_NAMES[name.lower()] = name
    ADX_CREATED[name] = "created"
    notes = []
    for v in ADX_VIEWERS:
        principal = runner_principal() if v == "me" else v
        if not principal:
            notes.append("couldn't tell who runs the notebook; add yourself as Viewer by hand")
            continue
        try:
            kusto_mgmt(ADX_CLUSTER_URI, name, f".add database [{kql_string(name)}] viewers ({kql_string(principal)}) "
                       "'Fabric migration backup (V2_2)'", auth=kc_auth(ADX_CLUSTER_URI))
            notes.append(f"Viewer: {principal}")
        except HttpError as exc:
            notes.append(f"Viewer {principal} not added ({exc.short()}); add it by hand")
    return notes


# ---------------------------------------------------------------- Azure DevOps
def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def ado_base():
    return (f"https://dev.azure.com/{quote(ADO_ORGANIZATION)}/{quote(ADO_PROJECT)}"
            f"/_apis/git/repositories/{quote(ADO_REPOSITORY)}")


_ado_auth = {}


def ado(method, path, params=None, body=None, retry=True):
    if "header" not in _ado_auth:
        pat = notebookutils.credentials.getSecret(KEY_VAULT_URL, ADO_PAT_SECRET_NAME)
        register_sensitive_value(pat)
        _ado_auth["header"] = "Basic " + base64.b64encode((":" + pat).encode()).decode()
    url = ado_base() + path + ("&" if "?" in path else "?") + ADO_API
    resp = http(method, url, auth=_ado_auth["header"], params=params, json_body=body, retry=retry, label="devops")
    if "json" not in resp.headers.get("Content-Type", "json"):
        raise HttpError(resp.status_code, "NotJson", "Azure DevOps didn't return JSON; check the organization, "
                        "project and token (an expired token is redirected to a sign-in page)", url)
    return resp.json() if resp.content else {}


print(f"Run ID: {RUN_ID}")
