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

FABRIC_API = "https://api.fabric.microsoft.com/v1"
ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
STORAGE_VERSION = "2023-11-03"
MAX_ATTEMPTS = 6
MAX_RETRY_WAIT_SECONDS = 15 * 60
LRO_TIMEOUT_SECONDS = 60 * 60
CHUNK_BYTES = 8 * 1024 * 1024
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NOW_ID = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

RESULTS = []           # one row per item and step: this notebook's log
_lock = threading.Lock()
CALLS, RETRIES = Counter(), Counter()


def norm_id(value):
    text = str(value or "").strip().strip("{}").lower()
    return text if GUID_RE.match(text) else None


def safe_name(name, max_len=80):
    """A folder-safe version of a Fabric display name."""
    text = re.sub(r'[\\/:*?"<>|#%\x00-\x1f]', "_", str(name or "")).strip().strip(".")
    return (text[:max_len].rstrip(" .") or "unnamed")


def log(workspace, item, step, status, detail=""):
    """One row of this notebook's log. workspace: name; item: dict with type/displayName/id (or {})."""
    detail = redact_sensitive_text(str(detail))
    with _lock:
        RESULTS.append({"Workspace": workspace, "Item": item.get("displayName", ""), "Type": item.get("type", ""),
                        "ItemId": item.get("id", ""), "Step": step, "Status": status,
                        "Detail": str(detail)})
    if status not in ("OK", "Dry run", "Skipped", "Unchanged", "Info", "Ready"):
        print(f"  ! {workspace} / {item.get('type', '')} {item.get('displayName', '')} / {step}: {status} - "
              f"{detail}")


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
    text = re.sub(r"<[^>]+>", " ", text or "")              # HTML page -> its text
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
        return "", json.dumps(body)
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    code = err.get("errorCode") or err.get("code") or err.get("typeKey") or err.get("error") or ""
    msg = err.get("message") or err.get("@message") or err.get("error_description") or ""
    if not msg:
        msg = json.dumps(body)                                  # unknown error shape: keep all of it
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
def _wait_operation(resp, want_result=True):
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
            if not want_result:
                return state
            try:
                result = http("GET", location.rstrip("/") + "/result", "pbi", label="fabric")
            except HttpError as exc:
                if exc.status in (400, 404):      # operations without a result (commit, update from Git)
                    return state
                raise
            return result.json() if result.content else {}
        if status in ("failed", "cancelled", "canceled"):
            err = state.get("error") or {}
            raise HttpError(None, err.get("errorCode") or status, err.get("message"), location)
        if time.monotonic() > deadline:
            raise HttpError(None, "Timeout", "the long-running operation didn't finish", location)
        wait = 3


def fabric(method, path, body=None, params=None, want_result=True):
    resp = http(method, FABRIC_API + path, "pbi", json_body=body, params=params, label="fabric")
    if resp.status_code == 202:
        return _wait_operation(resp, want_result)
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


# ---------------------------------------------------------------- ADLS Gen2 (secondary backup + migration records)
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


