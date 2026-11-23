import base64, json, os, sys, types, io, contextlib
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))
import fabric_agent_run as far
FAIL = []
import tempfile
TMP = tempfile.mkdtemp()
def check(c, m):
    print(("ok   " if c else "FAIL ") + m); c or FAIL.append(m)

def mk(name, settings, body):
    nb = {"cells": [{"cell_type": "markdown", "source": ["# x"], "metadata": {}},
                    {"cell_type": "code", "source": [settings], "metadata": {"tags": ["parameters"]}},
                    {"cell_type": "code", "source": [body], "metadata": {}}],
          "metadata": {"kernelspec": {"name": "jupyter"}}, "nbformat": 4, "nbformat_minor": 5}
    p = os.path.join(TMP, f"{name}.ipynb"); json.dump(nb, open(p, "w")); return p

STORE = {}
class R:
    def __init__(s, code=200): s.status_code = code
    def raise_for_status(s): pass
def fake_rq():
    m = types.ModuleType("requests"); pend = {}
    def put(u, **k): pend[u.split("?")[0]] = b""; return R(201)
    def patch(u, data=None, **k):
        b = u.split("?")[0]
        if "append" in u: pend[b] += data
        else: STORE[b] = pend.pop(b)
        return R()
    m.put, m.patch = put, patch; return m

def execute(nb, display_fn=None):
    code = "".join(nb["cells"][1]["source"])
    sys.modules["requests"] = fake_rq()
    g = {"notebookutils": types.SimpleNamespace(credentials=types.SimpleNamespace(getToken=lambda a: "t")),
         "__name__": "__main__"}
    if display_fn: g["display"] = display_fn
    err = None
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf): exec(compile(code, "drv", "exec"), g)
    except BaseException as e: err = e
    sys.modules.pop("requests")
    import requests  # restore real
    return g, err

clock = {"now": 1000}
with patch.dict(sys.modules, {"azure.identity": None}), patch.object(far, "_tokens", {}), \
        patch.object(far.shutil, "which", return_value="az"), \
        patch.object(far.time, "time", side_effect=lambda: clock["now"]), \
        patch.object(far.subprocess, "run") as cli:
    cli.side_effect = [
        types.SimpleNamespace(returncode=0, stdout=json.dumps({"accessToken": "near-expiry", "expires_on": 1060})),
        types.SimpleNamespace(returncode=0, stdout=json.dumps({"accessToken": "renewed", "expires_on": 4600})),
    ]
    first = far.token(far.FABRIC)
    clock["now"] = 1061
    refreshed = far.token(far.FABRIC)
    check(first == "near-expiry" and refreshed == "renewed" and cli.call_count == 2,
          "CLI token refresh uses the actual expiry, not a new lifetime for a cached CLI token")
    check(far.token(far.FABRIC) == "renewed" and cli.call_count == 2, "valid CLI token is reused")
    for body, message in (({"accessToken": "missing-expiry"}, "expires_on"),
                          ({"accessToken": "already-expired", "expires_on": 1000}, "expired")):
        far._tokens.clear()
        cli.side_effect = [types.SimpleNamespace(returncode=0, stdout=json.dumps(body))]
        try:
            far.token(far.FABRIC)
            check(False, f"CLI token with {message} metadata is refused")
        except SystemExit as exc:
            check(message in str(exc), f"CLI token with {message} metadata is refused with a fix")

ADLS = ["acc", "cont", "root"]
# 1 completed run, display captured, transcript + status saved
p = mk("V2_2_Backup_Data", "DRY_RUN = True\nMODE = 'x'", "import pandas as pd\nprint('hello', MODE)\ndisplay(pd.DataFrame({'a':[1,2]}))\n"
       "SEED_METRICS = {'Lakehouse': {'rows': 200000, 'storedBytes': 1073741824, 'sizeMetric': 'active_parquet_bytes'}}")
name, nb = far.wrap(p, far.parse_overrides(["MODE='parquet'"], "V2_2_Backup_Data", False), False, "R1", ADLS)
g, err = execute(nb, display_fn=lambda *a, **k: None)
t = STORE.get("https://acc.dfs.core.windows.net/cont/root/_agent_runs/R1/V2_2_Backup_Data.txt", b"").decode()
st = json.loads(STORE.get("https://acc.dfs.core.windows.net/cont/root/_agent_runs/R1/V2_2_Backup_Data.json", b"{}"))
check(err is None and "hello parquet" in t and "a" in t and "[agent] COMPLETED" in t and st.get("status") == "completed", "completed run: output, table and status saved")
check(st.get("facts", {}).get("SEED_METRICS") == g["SEED_METRICS"], "measured seed bytes and metric provenance survive fact capture")
# 2 failure: traceback in transcript, job raises
STORE.clear()
p = mk("V2_3_Git_Snapshot", "DRY_RUN = True", "x = None\nx.itertuples()")
name, nb = far.wrap(p, {}, False, "R2", ADLS)
g, err = execute(nb)
t = STORE.get("https://acc.dfs.core.windows.net/cont/root/_agent_runs/R2/V2_3_Git_Snapshot.txt", b"").decode()
st = json.loads(STORE.get("https://acc.dfs.core.windows.net/cont/root/_agent_runs/R2/V2_3_Git_Snapshot.json", b"{}"))
check(isinstance(err, AttributeError) and "itertuples" in t and "Traceback" in t and st.get("status") == "failed" and st.get("cell") == 2, "failure: traceback saved, job fails")
# Credential canaries are synthetic; never contact a service with them.
STORE.clear()
shown = []
body = """import pandas as pd, sys
opaque = "opaque-canary-" + "not-a-real-secret"
signature = "synthetic-" + "signature-canary"
github_token = "ghp_" + "A" * 36
if "register_sensitive_value" in globals():
    register_sensitive_value(opaque)
url = "https://example.invalid/file?sv=1&sig=" + signature
sys.stdout.write(url[:45])
sys.stdout.flush()
sys.stdout.write(url[45:] + "\\n")
print("Authorization: Bearer " + github_token)
table = pd.DataFrame({"client_secret": [opaque], "Detail": [url], "Rows": [7]})
display(table)
diagnostic = "long diagnostic " + "x" * 600 + " exact fix retained"
RESULTS = [{"Status": "Failed", "Detail": opaque + " " + diagnostic}]
raise ValueError(opaque + " " + diagnostic)
"""
p = mk("V2_2_Backup_Data", "DRY_RUN = True", body)
name, nb = far.wrap(p, {}, False, "R2secrets", ADLS)
g, err = execute(nb, display_fn=lambda obj, *a, **k: shown.append(obj))
t = STORE.get("https://acc.dfs.core.windows.net/cont/root/_agent_runs/R2secrets/V2_2_Backup_Data.txt", b"").decode()
st = json.loads(STORE.get("https://acc.dfs.core.windows.net/cont/root/_agent_runs/R2secrets/V2_2_Backup_Data.json", b"{}"))
captured = t + json.dumps(st) + g["_ag_out"].getvalue() + "".join(str(obj) for obj in shown) + str(err)
check(all(value not in captured for value in (g["opaque"], g["signature"], g["github_token"])),
      "stdout, rich tables, transcripts, facts and terminal exception redact credential canaries")
check(err is not None and st.get("status") == "failed" and "Traceback" in t,
      "redaction preserves job failure and traceback")
check(g["diagnostic"] in t and g["diagnostic"] in st.get("error", "")
      and g["diagnostic"] in json.dumps(st.get("facts", {})),
      "full diagnostic and fix remain readable after redaction")
check(g["table"].iloc[0]["client_secret"] == g["opaque"] and shown[0].iloc[0]["Rows"] == 7,
      "redacting displayed output never changes source data or numeric evidence")
# 3 destructive notebook: CONFIRM emptied even if baked in; DRY_RUN False baked -> guard
STORE.clear()
p = mk("V2_4_Delete_Fabric_Items", "DRY_RUN = True\nCONFIRM_DELETE_ID = 'abc'", "print('confirm is', repr(CONFIRM_DELETE_ID))")
name, nb = far.wrap(p, {}, False, "R3", ADLS); g, err = execute(nb)
check(err is None and g["CONFIRM_DELETE_ID"] == "", "destructive: baked confirmation emptied")
p = mk("V2_4_Delete_Fabric_Items", "DRY_RUN = False", "print('should not run')")
name, nb = far.wrap(p, {}, True, "R4", ADLS); g, err = execute(nb)
check(isinstance(err, RuntimeError) and "AGENT GUARD" in str(err), "destructive: real run refused inside the notebook, even with allow_real")
# 4 CLI refusals
for args, nbname, real, msg in [(["CONFIRM_DELETE_ID='x'"], "V2_4_Delete_Fabric_Items", False, "CONFIRM refused"),
                                (["FIX_STALE_REFERENCES=True"], "V2_5_Restore_From_Git", False, "FIX_STALE refused"),
                                (["DRY_RUN=False"], "V2_6_Restore_Data", True, "destructive real run refused"),
                                (["DRY_RUN=False"], "V2_2_Backup_Data", False, "real run needs --allow-real"),
                                (["MODE=parquet"], "V2_2_Backup_Data", False, "non-literal refused"),
                                ([], "V2_T_Create_Test_Items", False, "V2_T needs --allow-real"),
                                (["DELETE_TEST_ITEMS=True"], "V2_T_Create_Test_Items", True, "V2_T cleanup refused"),
                                (["HARD_DELETE=False"], "V2_T_Create_Test_Items", True, "V2_T delete mode refused")]:
    try: far.parse_overrides(args, nbname, real); check(False, msg)
    except SystemExit: check(True, msg)
check(far.parse_overrides(["DRY_RUN=False"], "V2_2_Backup_Data", True) == {"DRY_RUN": False}, "V2_2 real run with --allow-real")
check(far.parse_overrides(['TARGET_WORKSPACE_ID="w"'], "V2_T_Create_Test_Items", True) == {"TARGET_WORKSPACE_ID": "w"}, "V2_T with --allow-real")
# 5 non-destructive real run without allow_real baked -> guard
p = mk("V2_2_Backup_Data", "DRY_RUN = False", "print(1)")
name, nb = far.wrap(p, {}, False, "R5", ADLS); g, err = execute(nb)
check(isinstance(err, RuntimeError), "baked DRY_RUN=False needs --allow-real")
# 6 end-to-end CLI against a fake Fabric
calls = []
class FR:
    def __init__(s, code, body=None, headers=None): s.status_code, s._b, s.headers = code, body, headers or {}; s.content = b"x" if body is not None else b""; s.text = json.dumps(body)
    def json(s): return s._b
signature = "synthetic-" + "signature-canary"
diagnostic = "x" * 2000 + " full fix retained"
with patch.object(far, "token", return_value="t"), patch.object(
        far.requests, "request", return_value=FR(403, {"message": diagnostic})):
    try:
        far.call("GET", "https://example.invalid/data?sig=" + signature)
        check(False, "runner HTTP failure remains an explicit failure")
    except SystemExit as exc:
        check(signature not in str(exc) and diagnostic in str(exc),
              "runner HTTP failures redact signed URLs and preserve full error text")
with patch.object(far, "token", return_value="t"), patch.object(
        far.requests, "request", side_effect=far.requests.ConnectionError(
            "connection failed: https://example.invalid/data?sig=" + signature)):
    try:
        far.call("GET", "https://example.invalid/data?sig=" + signature)
        check(False, "runner network failure remains explicit")
    except SystemExit as exc:
        check(signature not in str(exc) and "connection failed" in str(exc),
              "runner network errors redact signed URLs")
    except far.requests.RequestException:
        check(False, "runner network errors redact signed URLs")
items = []
def fake_request(method, url, headers=None, json=None, **k):
    calls.append((method, url))
    if url.endswith("items?type=Notebook"): return FR(200, {"value": items})
    if method == "POST" and (url.endswith("/items") or url.endswith("/updateDefinition")):
        definition = json.get("definition", {})
        parts = definition.get("parts", [])
        if (definition.get("format") != "ipynb" or len(parts) != 1
                or parts[0].get("path") != "artifact.content.ipynb"
                or parts[0].get("payloadType") != "InlineBase64"):
            return FR(400, {"errorCode": "InvalidItemDefinition",
                            "message": "ipynb requires artifact.content.ipynb as InlineBase64"})
    if method == "POST" and url.endswith("/items"):
        items.append({"id": "nb1", "displayName": json["displayName"]}); calls.append(("payload", json["definition"]["parts"][0]["path"]))
        return FR(202, None, {"Location": "https://api.fabric.microsoft.com/v1/operations/op1", "Retry-After": "0"})
    if url.endswith("/updateDefinition"): return FR(200, {})
    if url.endswith("operations/op1"): return FR(200, {"status": "Succeeded"})
    if url.endswith("operations/op1/result"): return FR(200, {"id": "nb1"})
    if "jobs/instances" in url and method == "POST": return FR(202, None, {"Location": "https://api.fabric.microsoft.com/v1/job/1"})
    if url.endswith("job/1"): return FR(200, {"status": "Completed", "failureReason": None})
    raise AssertionError(url)
def fake_get(url, headers=None, **k):
    calls.append(("GET", url))
    body = b"transcript text" if url.endswith(".txt") else b'{"status": "completed"}'
    r = FR(200); r.content = body; return r
far.requests.request, far.requests.get = fake_request, fake_get
far.token = lambda res: "tok"; far.time.sleep = lambda s: None; far.HERE = TMP
p = mk("V2_0_Permission_Check", "DRY_RUN = True", "print(1)")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = far.main(["run", p, "--workspace", "11111111-2222-3333-4444-555555555555", "--adls", "acc/cont/root"])
o = buf.getvalue()
check(rc == 0 and "AGENT_V2_0_Permission_Check created" in o and "transcript text" in o
      and ("payload", "artifact.content.ipynb") in calls, "CLI: create, run, transcript printed")
with contextlib.redirect_stdout(io.StringIO()):
    rc = far.main(["run", p, "--workspace", "11111111-2222-3333-4444-555555555555", "--adls", "acc/cont/root"])
check(rc == 0 and any(u.endswith("/updateDefinition") for m, u in calls if m == "POST"), "CLI: second run updates the same notebook")
print("\nRESULT:", "ALL PASSED" if not FAIL else f"{len(FAIL)} FAILED")
