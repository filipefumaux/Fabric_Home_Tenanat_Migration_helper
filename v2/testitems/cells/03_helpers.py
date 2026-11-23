import base64, json, re, time, uuid
import pandas as pd
import requests

try:
    notebookutils  # preloaded in Fabric notebooks
except NameError:
    import notebookutils

FABRIC_API = "https://api.fabric.microsoft.com/v1"
ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
STEPS = []
_session = requests.Session()


class HttpError(Exception):
    def __init__(self, status, code, message, url=""):
        self.status, self.code, self.message, self.url = status, code or "", redact_sensitive_text(str(message or "")), url
        super().__init__(f"HTTP {status} {code}: {self.message}" if status else f"{code}: {self.message}")


def _details(resp):
    try:
        body = resp.json()
    except ValueError:
        return "", resp.text or ""
    err = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else body
    if not isinstance(err, dict):
        return "", str(err)
    return str(err.get("errorCode") or err.get("code") or ""), str(err.get("message") or "")


def http(method, url, audience, *, params=None, json_body=None, data=None, headers=None, ok404=False):
    for attempt in range(1, 7):
        hdrs = {"Authorization": "Bearer " + notebookutils.credentials.getToken(audience), **(headers or {})}
        register_sensitive_value(hdrs["Authorization"].split(" ", 1)[-1])
        try:
            resp = _session.request(method, url, params=params, json=json_body, data=data, headers=hdrs, timeout=300)
        except requests.RequestException as exc:
            if attempt == 6:
                raise HttpError(None, type(exc).__name__, str(exc), url)
            time.sleep(5 * attempt)
            continue
        if resp.status_code in (429, 408) or resp.status_code >= 500:
            if attempt < 6:
                try:
                    wait = int(float(resp.headers.get("Retry-After", "")))
                except ValueError:
                    wait = 10 * attempt
                time.sleep(min(max(wait, 1), 300))
                continue
        if ok404 and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise HttpError(resp.status_code, *_details(resp), url)
        return resp


def fabric(method, path, body=None):
    resp = http(method, FABRIC_API + path, "pbi", json_body=body)
    if resp.status_code == 202 and (resp.headers.get("Location") or resp.headers.get("x-ms-operation-id")):
        location = resp.headers.get("Location") or f"{FABRIC_API}/operations/{resp.headers['x-ms-operation-id']}"
        deadline = time.monotonic() + 30 * 60
        while True:
            time.sleep(3)
            state = http("GET", location, "pbi").json()
            status = str(state.get("status", "")).lower()
            if status == "succeeded":
                result = http("GET", location.rstrip("/") + "/result", "pbi", ok404=True)
                return result.json() if result is not None and result.content else {}
            if status in ("failed", "cancelled", "canceled"):
                err = state.get("error") or {}
                raise HttpError(None, err.get("errorCode") or status, err.get("message"), location)
            if time.monotonic() > deadline:
                raise HttpError(None, "Timeout", "operation didn't finish in 30 minutes", location)
    return resp.json() if resp.content else {}


def fabric_list(path):
    url, out = FABRIC_API + path, []
    while url:
        body = http("GET", url, "pbi").json()
        out += body.get("value") or []
        url = body.get("continuationUri")
    return out


def b64(data):
    if not isinstance(data, (bytes, bytearray)):
        data = data if isinstance(data, str) else json.dumps(data, indent=2)
        data = data.encode("utf-8")
    return base64.b64encode(data).decode()


def find_item(item_type, name):
    for item in fabric_list(f"/workspaces/{WS}/items?type={item_type}"):
        if item.get("displayName", "").lower() == name.lower() and item.get("type") == item_type:
            return item
    return None


def ensure_item(item_type, name, definition=None, description="Test item for the Fabric migration notebooks (V2_T)"):
    """Return (item id, 'Exists' or 'Created'). Creates the item only if no item of that type has this name."""
    found = find_item(item_type, name)
    if found:
        if (LARGE_DATA_MIB or MEASURE_DATA_SIZE) and item_type in ("Lakehouse", "Warehouse", "Eventhouse"):
            if found.get("description") != description:
                raise RuntimeError(f"Ownership collision: {item_type} {name} isn't a V2_T-owned test item. "
                                   "Use a fresh PREFIX; don't adopt or overwrite existing user data.")
        return found["id"], "Exists"
    body = {"displayName": name, "type": item_type, "description": description}
    if definition:
        body["definition"] = definition
    result = fabric("POST", f"/workspaces/{WS}/items", body)
    item_id = result.get("id") if isinstance(result, dict) else None
    if not item_id:
        found = find_item(item_type, name)
        item_id = found["id"] if found else None
    if not item_id:
        raise HttpError(None, "NotFound", f"{item_type} {name} was accepted but can't be found")
    return item_id, "Created"


def step(item_type, name, item_id, status, detail=""):
    STEPS.append({"Type": item_type, "Name": name, "Id": item_id or "", "Status": status, "Detail": str(detail)})
    print(f"  {item_type:<12} {name:<28} {status}  {detail}")


def wait_until(check, what, timeout=15 * 60, every=10):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise HttpError(None, "Timeout", f"{what} wasn't ready after {timeout // 60} minutes")
        time.sleep(every)


def onelake_write(item_id, path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    url = f"{ONELAKE_DFS}/{WS}/{item_id}/{path}"
    hdr = {"x-ms-version": "2023-11-03"}
    http("PUT", url, "storage", params={"resource": "file"}, headers=hdr)
    if data:
        http("PATCH", url, "storage", params={"action": "append", "position": "0"}, data=data, headers=hdr)
    http("PATCH", url, "storage", params={"action": "flush", "position": str(len(data))}, data=b"", headers=hdr)


def sql_connect(item_id, item_type):
    data_utils = getattr(notebookutils, "data", None)
    if data_utils is None or not hasattr(data_utils, "connect_to_artifact"):
        raise RuntimeError("notebookutils.data isn't available: set the notebook language to Python, not PySpark")
    return data_utils.connect_to_artifact(item_id, WS, item_type)


def sql_run(conn, statement):
    """Run a T-SQL statement that returns no rows (wrapped in EXEC so it can be followed by a SELECT)."""
    escaped = statement.replace("'", "''")
    conn.query(f"EXEC(N'{escaped}'); SELECT 1 AS done;")
    if hasattr(conn, "commit"):
        try:
            conn.commit()
        except Exception:
            pass


def sql_scalar(conn, query):
    df = conn.query(query)
    return None if df is None or len(df) == 0 else df.iloc[0, 0]


def kusto(uri, database, command, kind="mgmt"):
    resp = http("POST", uri.rstrip("/") + f"/v1/rest/{kind}", "kusto", json_body={"db": database, "csl": command})
    body = resp.json()
    if body.get("error") or body.get("Exceptions"):
        raise HttpError(None, "KustoError", json.dumps(body))
    tables = body.get("Tables") or []
    if not tables:
        return []
    cols = [c["ColumnName"] for c in tables[0].get("Columns", [])]
    return [dict(zip(cols, row)) for row in tables[0].get("Rows", [])]


if type(LARGE_DATA_MIB) is not int or LARGE_DATA_MIB < 0:
    raise ValueError("LARGE_DATA_MIB must be a non-negative integer number of MiB (0 keeps the small fixtures).")
if type(MEASURE_DATA_SIZE) is not bool:
    raise ValueError("MEASURE_DATA_SIZE must be True or False.")
if type(ISOLATED_STRESS_RUNTIME) is not bool:
    raise ValueError("ISOLATED_STRESS_RUNTIME must be True or False.")
if (LARGE_DATA_MIB or MEASURE_DATA_SIZE) and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", PREFIX):
    raise ValueError("Measured/stress PREFIX must start with a letter and contain at most 80 letters, digits or _.")
if LARGE_DATA_MIB and "Lakehouse" not in CREATE_TYPES:
    raise ValueError("LARGE_DATA_MIB requires Lakehouse in CREATE_TYPES: its owned source is reused by the bulk loaders.")

ctx = notebookutils.runtime.context
WS = (TARGET_WORKSPACE_ID or ctx.get("currentWorkspaceId") or "").strip().lower()
if not WS:
    raise ValueError("Set TARGET_WORKSPACE_ID: the notebook's own workspace couldn't be read.")
ws_info = fabric("GET", f"/workspaces/{WS}")
print(f"Target workspace: {ws_info.get('displayName')} ({WS})")
if not ws_info.get("capacityId"):
    print("WARNING: this workspace has no capacity. Lakehouse, Warehouse, Eventhouse and most other items "
          "need a Fabric capacity (F SKU or trial).")
NAMES = {t: f"{PREFIX}_{t.lower()}" for t in CREATE_TYPES}
IDS = {}
