"""Run a V2 notebook in Fabric from VS Code and get back everything it printed.

Fabric's API starts a notebook and reports Completed/Failed, but it doesn't return the cell output. This runner:
  1. wraps the notebook: one driver cell runs the original cells in order, copies all printed output, tables and the
     error (with traceback) into a transcript, and writes it to ADLS after every cell;
  2. creates or updates the wrapped copy in a runner workspace as "AGENT_<notebook name>";
  3. runs it as a Fabric job, waits, downloads the transcript and prints it.

Safety (enforced here AND inside the wrapped notebook, so editing one side isn't enough):
  * V2_1, V2_4, V2_5 and V2_6 change Fabric (link, delete, restore, load): they always run with DRY_RUN = True and
    every CONFIRM_* setting emptied. Their real runs are done by a person in Fabric.
  * V2_0, V2_2 and V2_3 can do a real run only with --allow-real (the person must ask for it).
  * V2_T (test items) creates items whenever it runs: --allow-real only; its cleanup (DELETE_TEST_ITEMS) never.
  * CONFIRM_* and FIX_STALE_REFERENCES can't be set from here.

Sign-in: `az login` with the account that runs the notebooks (Fabric: Contributor on the runner workspace; ADLS:
Storage Blob Data Reader on the container for the transcript). Uses azure-identity if installed, else the az CLI.
The CLI fallback needs Azure CLI 2.54+ and caches tokens only until their reported expires_on timestamp.

Examples:
  python agent/fabric_agent_run.py run out_lab/V2_2_Backup_Data.ipynb --workspace <runner workspace id>
  python agent/fabric_agent_run.py run out_lab/V2_4_Delete_Fabric_Items.ipynb --workspace <id> \\
      --set 'SNAPSHOT_ID="20261117T0800Z-ab12"'
  python agent/fabric_agent_run.py transcript <run id> V2_2_Backup_Data
"""
import argparse, ast, base64, datetime as dt, json, os, re, shutil, subprocess, sys, time, uuid
from urllib.parse import quote

import requests

FABRIC = "https://api.fabric.microsoft.com/v1"
DESTRUCTIVE = ("V2_1_", "V2_4_", "V2_5_", "V2_6_")
REAL_RUN_OK = ("V2_0_", "V2_2_", "V2_3_")
CREATES_ITEMS = ("V2_T_",)        # creates test items whenever it runs (no dry run)
NEVER_SET = re.compile(r"^(CONFIRM_\w*|FIX_STALE_REFERENCES|DELETE_TEST_ITEMS|HARD_DELETE|DELETE_DATAFLOW_STAGING)$")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "common"))
from redaction import redact_sensitive_data, redact_sensitive_text, register_sensitive_value

REDACTION_SOURCE = open(os.path.join(os.path.dirname(HERE), "common", "redaction.py"), encoding="utf-8").read()


# ---------------------------------------------------------------- sign-in and HTTP
_tokens = {}


def token(resource):
    if resource in _tokens and _tokens[resource][1] > time.time() + 120:
        return _tokens[resource][0]
    try:
        from azure.identity import AzureCliCredential
        t = AzureCliCredential().get_token(resource + "/.default")
        _tokens[resource] = (t.token, t.expires_on)
    except ImportError:
        az = shutil.which("az") or shutil.which("az.cmd")
        if not az:
            sys.exit("Sign-in: install the Azure CLI and run `az login` (or pip install azure-identity).")
        out = subprocess.run([az, "account", "get-access-token", "--resource", resource, "-o", "json"],
                             capture_output=True, text=True)
        if out.returncode:
            sys.exit(redact_sensitive_text("Sign-in failed (run `az login`): " + out.stderr.strip()))
        j = json.loads(out.stdout)
        try:
            expires_on = int(j["expires_on"])
        except (KeyError, TypeError, ValueError):
            sys.exit("Sign-in: Azure CLI didn't return a valid expires_on timestamp. Update Azure CLI "
                     "(2.54 or newer) and run `az login` again.")
        if expires_on <= time.time():
            sys.exit("Sign-in: Azure CLI returned an expired access token. Run `az login` again.")
        _tokens[resource] = (j["accessToken"], expires_on)
    register_sensitive_value(_tokens[resource][0])
    return _tokens[resource][0]


def call(method, url, resource="https://api.fabric.microsoft.com", ok=(200, 201, 202), **kw):
    url = url if url.startswith("https://") else FABRIC + url
    for attempt in range(8):
        try:
            r = requests.request(method, url, headers={"Authorization": "Bearer " + token(resource),
                                                       **kw.pop("headers", {})}, timeout=120, **kw)
        except requests.RequestException as exc:
            if redact_sensitive_text(str(exc)) != str(exc):
                raise SystemExit(redact_sensitive_text(f"{method} {url}: {exc}")) from None
            raise
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(int(r.headers.get("Retry-After", "10") or 10), 60) if r.status_code == 429 else 5 * (attempt + 1))
            continue
        if r.status_code not in ok:
            raise SystemExit(redact_sensitive_text(f"{method} {url} -> HTTP {r.status_code}: {r.text}"))
        return r
    raise SystemExit(redact_sensitive_text(f"{method} {url} kept failing: HTTP {r.status_code} {r.text}"))


def wait_operation(r):
    """Fabric long-running operation: 202 + Location, poll until Succeeded."""
    if r.status_code != 202:
        return r.json() if r.content else {}
    loc = r.headers["Location"]
    while True:
        time.sleep(int(r.headers.get("Retry-After", "3") or 3))
        r = call("GET", loc)
        body = r.json() if r.content else {}
        status = body.get("status")
        if status in ("Succeeded", None) and r.status_code == 200:
            if status is None:
                return body
            try:
                return call("GET", loc.rstrip("/") + "/result", ok=(200,)).json()
            except SystemExit:
                return body
        if status in ("Failed", "Cancelled"):
            raise SystemExit(redact_sensitive_text(f"Fabric operation {status}: {json.dumps(body.get('error') or body)}"))


# ---------------------------------------------------------------- wrapping
DRIVER = r'''
# AGENT WRAPPER: runs the original notebook's cells and saves a transcript to ADLS. Generated by
# agent/fabric_agent_run.py; don't edit by hand. Delete AGENT_* notebooks when testing is finished.
import base64 as _b64, json as _json, sys as _sys, traceback as _tb, datetime as _dt, requests as _rq
_AG = _json.loads(_b64.b64decode("@@PAYLOAD@@").decode("utf-8"))
_AG_BUF = []


class _AgTee:
    def __init__(self, real):
        self.real = real
        self.pending = ""

    def write(self, s):
        _AG_BUF.append(s)
        self.pending += s
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.real.write(redact_sensitive_text(line) + "\n")
        return len(s)

    def flush(self):
        self.real.flush()

    def finish(self):
        if self.pending:
            self.real.write(redact_sensitive_text(self.pending))
            self.pending = ""
        self.real.flush()

    def __getattr__(self, n):
        return getattr(self.real, n)


def _ag_save(status, extra=None):
    acc, cont, root = _AG["adls"]
    base = f"https://{acc}.dfs.core.windows.net/{cont}/{root}/_agent_runs/{_AG['run']}/{_AG['notebook']}"
    storage_token = notebookutils.credentials.getToken("storage")
    register_sensitive_value(storage_token)
    h = {"Authorization": "Bearer " + storage_token, "x-ms-version": "2021-08-06"}
    for suffix, text in ((".txt", redact_sensitive_text("".join(_AG_BUF))),
                         (".json", _json.dumps(redact_sensitive_data({"status": status, **(extra or {}),
                                                "savedUtc": _dt.datetime.now(_dt.timezone.utc).isoformat()})))):
        data = text.encode("utf-8")
        u = base + suffix
        _rq.put(u + "?resource=file", headers=h, timeout=60).raise_for_status()
        if data:
            _rq.patch(u + "?action=append&position=0", headers=h, data=data, timeout=120).raise_for_status()
        _rq.patch(u + f"?action=flush&position={len(data)}", headers=h, timeout=60).raise_for_status()


try:
    _ag_real_display = display
except NameError:
    _ag_real_display = None


def display(obj, *a, **k):
    if hasattr(obj, "columns") and hasattr(obj, "copy"):
        obj = obj.copy()
        for column in obj.columns:
            obj[column] = obj[column].map(lambda value: redact_sensitive_data({column: value})[column])
    elif isinstance(obj, (dict, list, tuple, str, int, float, bool)) or obj is None:
        obj = redact_sensitive_data(obj)
    else:
        obj = redact_sensitive_text(repr(obj))
    try:
        text = obj.to_string(max_rows=200, max_colwidth=None) if hasattr(obj, "to_string") else repr(obj)
    except Exception:
        text = repr(obj)
    _AG_BUF.append(redact_sensitive_text(text) + "\n")
    if _ag_real_display:
        _ag_real_display(obj, *a, **k)


def _ag_guard():
    g = globals()
    for k, v in _AG["overrides"].items():
        g[k] = v
    _e2e = _AG.get("e2e")
    if _e2e:   # end-to-end rehearsal: only the disposable workspace the rehearsal created itself
        _ws = _e2e["workspace"].lower()
        if "WORKSPACE_IDS" in g and [str(x).strip().lower() for x in g["WORKSPACE_IDS"]] != [_ws]:
            raise RuntimeError(f"AGENT GUARD (rehearsal): WORKSPACE_IDS must be exactly [{_ws}], got {g['WORKSPACE_IDS']}")
        if "TARGET_WORKSPACE_ID" in g and str(g["TARGET_WORKSPACE_ID"]).strip().lower() != _ws:
            raise RuntimeError("AGENT GUARD (rehearsal): TARGET_WORKSPACE_ID must be the rehearsal workspace")
        if g.get("WORKSPACE_MAP"):
            raise RuntimeError("AGENT GUARD (rehearsal): WORKSPACE_MAP must be empty (restore into the same workspace)")
        for k, v in _e2e.get("confirm", {}).items():
            g[k] = v
        print(f"[agent] rehearsal in workspace {_ws}: DRY_RUN={g.get('DRY_RUN')!r}; confirmations {sorted(_e2e.get('confirm', {}))}")
        return
    if _AG["dry_run_only"]:
        for k in list(g):
            if k.startswith("CONFIRM_"):
                g[k] = ""
        if "FIX_STALE_REFERENCES" in g:
            g["FIX_STALE_REFERENCES"] = False
        if "DRY_RUN" in g and g["DRY_RUN"] is not True:
            raise RuntimeError("AGENT GUARD: this notebook only runs as a dry run from the agent runner")
    elif "DRY_RUN" in g and g["DRY_RUN"] is not True and not _AG["allow_real"]:
        raise RuntimeError("AGENT GUARD: a real run needs --allow-real")
    print(f"[agent] settings after overrides: DRY_RUN={g.get('DRY_RUN')!r}; overrides={sorted(_AG['overrides'])}")


_AG_FACTS = ("BUILD_ID", "RUN_ID", "NOW_ID", "SNAPSHOT_ID", "CONNECT_ID", "DELETE_ID", "PLAN_ID", "FIX_ID", "DATA_ID", "WS", "IDS",
             "DRY_RUN", "RESULTS", "CHECK_DF", "ready", "blocked", "attention", "REF_DF", "PLAN_DF", "result", "failed",
             "SEED_METRICS")
_AG_LEN_ONLY = ("TODO", "ITEMS")
_AG_GOOD = ("OK", "Dry run", "Skipped", "Unchanged", "Info", "Ready", "Not applicable", "LEFT ALONE", "PASS", "INFO")


def _ag_small(v):
    v = redact_sensitive_data(v)
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return {str(k): _ag_small(x) for k, x in list(v.items())[:60]}
    if isinstance(v, (list, tuple)):
        return [_ag_small(x) for x in list(v)[:300]]
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return redact_sensitive_text(str(v))


def _ag_facts():
    """Values the end-to-end rehearsal reads (IDs, verdicts, log rows), as JSON."""
    g, out = globals(), {}
    for n in _AG_FACTS:
        if n not in g:
            continue
        v = g[n]
        try:
            recs = None
            if hasattr(v, "columns") and hasattr(v, "to_json"):
                recs = _json.loads(v.to_json(orient="records", default_handler=str))
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                recs = v
            if recs is not None:
                col = next((c for c in ("Status", "Result") if recs and c in recs[0]), None)
                counts = {}
                for r in recs:
                    counts[str(r.get(col))] = counts.get(str(r.get(col)), 0) + 1
                out[n] = {"rows": len(recs), "statusColumn": col, "statusCounts": counts if col else {},
                          "problems": _ag_small([r for r in recs if col and str(r.get(col)) not in _AG_GOOD])[:150],
                          "records": _ag_small(recs[:300])}
            else:
                out[n] = _ag_small(v)
        except Exception as _e:
            out[n] = f"<not readable: {_e}>"
    for n in _AG_LEN_ONLY:
        if n in g and hasattr(g[n], "__len__"):
            out[n + "_count"] = len(g[n])
    return out


_ag_out, _ag_err = _sys.stdout, _sys.stderr
_ag_stdout, _ag_stderr = _AgTee(_ag_out), _AgTee(_ag_err)
_sys.stdout, _sys.stderr = _ag_stdout, _ag_stderr
_ag_failed, _ag_i = None, -1
try:
    print(f"[agent] run {_AG['run']} notebook {_AG['notebook']} started {_dt.datetime.now(_dt.timezone.utc).isoformat()}")
    for _ag_i, _ag_cell in enumerate(_AG["cells"]):
        print(f"\n[agent] ---- cell {_ag_i + 1}/{len(_AG['cells'])}: {_ag_cell['name']}")
        exec(compile(_ag_cell["src"], f"<{_AG['notebook']} cell {_ag_i + 1}>", "exec"), globals())
        if _ag_cell["params"]:
            _ag_guard()
        try:
            _ag_save("running", {"cell": _ag_i + 1})
        except Exception as _ag_save_exc:
            print("[agent] transcript save failed:", _ag_save_exc)
    print("\n[agent] COMPLETED")
except BaseException as _ag_exc:
    _ag_failed = _ag_exc
    print("\n[agent] FAILED in cell", _ag_i + 1, "\n" + _tb.format_exc())
finally:
    _ag_stdout.finish()
    _ag_stderr.finish()
    _sys.stdout, _sys.stderr = _ag_out, _ag_err
    try:
        try:
            _ag_f = _ag_facts()
        except Exception as _ag_fe:
            _ag_f = {"_error": repr(_ag_fe)}
        _ag_save("failed" if _ag_failed else "completed",
                 {"cell": _ag_i + 1, "error": redact_sensitive_text(repr(_ag_failed)) if _ag_failed else None, "facts": _ag_f})
    except Exception as _ag_save_exc:
        print("[agent] couldn't save the transcript to ADLS (Storage Blob Data Contributor on the container?):",
              redact_sensitive_text(str(_ag_save_exc)))
if _ag_failed:
    if (redact_sensitive_text(repr(_ag_failed)) != repr(_ag_failed)
            or redact_sensitive_text(str(_ag_failed)) != str(_ag_failed)):
        raise RuntimeError(type(_ag_failed).__name__ + ": " + redact_sensitive_text(str(_ag_failed))) from None
    raise _ag_failed
'''


def wrap(nb_path, overrides, allow_real, run_id, adls, e2e=None):
    """e2e: {"workspace": id, "confirm": {...}} only from agent/e2e_rehearsal.py (never from the command line)."""
    nb = json.load(open(nb_path, encoding="utf-8"))
    name = os.path.splitext(os.path.basename(nb_path))[0]
    cells, n = [], 0
    for c in nb["cells"]:
        if c["cell_type"] != "code":
            continue
        n += 1
        src = "".join(c["source"])
        params = "parameters" in (c.get("metadata", {}).get("tags") or [])
        cells.append({"name": "settings (parameters)" if params else f"code {n}", "src": src, "params": params})
    if not any(c["params"] for c in cells):
        raise SystemExit(f"{name}: no cell tagged 'parameters'; the guard needs one")
    payload = {"cells": cells, "overrides": overrides, "notebook": name, "run": run_id, "adls": adls,
               "dry_run_only": name.startswith(DESTRUCTIVE) and not e2e, "allow_real": bool(allow_real or e2e),
               "e2e": e2e}
    code = REDACTION_SOURCE + "\n" + DRIVER.replace("@@PAYLOAD@@", base64.b64encode(json.dumps(payload).encode()).decode())
    ast.parse(code)
    out = {"cells": [{"cell_type": "markdown", "metadata": {}, "source": [
        f"# AGENT copy of {name}\n", "Generated by `agent/fabric_agent_run.py` for test runs from VS Code. "
        "Run the original notebook for real work. Delete AGENT_* notebooks when testing is finished.\n"]},
        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
         "source": code.splitlines(keepends=True)}],
        "metadata": nb["metadata"], "nbformat": 4, "nbformat_minor": 5}
    return name, out


def parse_overrides(pairs, name, allow_real):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--set {p!r}: use NAME=python-literal, e.g. DRY_RUN=True or MODE=\"parquet\"")
        k, v = p.split("=", 1)
        k = k.strip()
        if not re.match(r"^[A-Z][A-Z0-9_]*$", k):
            raise SystemExit(f"--set {k!r}: only UPPER_CASE settings")
        if NEVER_SET.match(k):
            raise SystemExit(f"--set {k}: confirmation settings are filled in by a person in Fabric, never by the agent")
        try:
            val = ast.literal_eval(v.strip())
        except (ValueError, SyntaxError):
            raise SystemExit(redact_sensitive_text(f"--set {k}: {v!r} isn't a Python literal (strings need quotes)"))
        out[k] = val
    if name.startswith(CREATES_ITEMS) and not allow_real:
        raise SystemExit(f"{name} creates test items (it has no dry run): needs --allow-real, only when the person asked "
                         "for it, with TARGET_WORKSPACE_ID set to their test workspace. Its cleanup is never run by the agent.")
    if out.get("DRY_RUN", True) is not True:
        if name.startswith(DESTRUCTIVE):
            raise SystemExit(f"{name} changes Fabric: the agent runs it only as a dry run. A person does the real run.")
        if not allow_real:
            raise SystemExit("DRY_RUN=False needs --allow-real (only when the person asked for a real run)")
    return out


# ---------------------------------------------------------------- Fabric side
def upsert_notebook(ws, display_name, nb):
    payload = base64.b64encode(json.dumps(nb).encode()).decode()
    definition = {"format": "ipynb", "parts": [{"path": "artifact.content.ipynb", "payload": payload,
                                                "payloadType": "InlineBase64"}]}
    existing, url = None, f"/workspaces/{ws}/items?type=Notebook"
    while url and not existing:
        body = call("GET", url).json()
        existing = next((i for i in body.get("value", []) if i["displayName"] == display_name), None)
        url = body.get("continuationUri")
    if existing:
        wait_operation(call("POST", f"/workspaces/{ws}/items/{existing['id']}/updateDefinition", json={"definition": definition}))
        return existing["id"], "updated"
    res = wait_operation(call("POST", f"/workspaces/{ws}/items", json={"displayName": display_name, "type": "Notebook",
                                                                    "definition": definition}))
    if res.get("id"):
        return res["id"], "created"
    body = call("GET", f"/workspaces/{ws}/items?type=Notebook").json()
    return next(i["id"] for i in body["value"] if i["displayName"] == display_name), "created"


def run_job(ws, item_id, timeout_s):
    r = call("POST", f"/workspaces/{ws}/items/{item_id}/jobs/instances?jobType=RunNotebook", json={})
    loc = r.headers.get("Location")
    if not loc:
        raise SystemExit("Fabric didn't return a job location")
    start, last = time.time(), None
    while True:
        time.sleep(15)
        job = call("GET", loc).json()
        status = job.get("status")
        if status != last:
            print(f"  job {status} ({int(time.time() - start)} s)", flush=True)
            last = status
        if status in ("Completed", "Failed", "Cancelled", "Deduped"):
            return job
        if time.time() - start > timeout_s:
            print(f"  still {status} after {timeout_s} s; stopped waiting (the job keeps running). "
                  f"Fetch the transcript later with the 'transcript' command.")
            return job


def read_adls(adls, path):
    acc, cont, _ = adls
    r = requests.get(f"https://{acc}.dfs.core.windows.net/{cont}/{quote(path)}",
                     headers={"Authorization": "Bearer " + token("https://storage.azure.com"),
                              "x-ms-version": "2021-08-06"}, timeout=120)
    return r.content.decode("utf-8", "replace") if r.status_code == 200 else None


def fetch_transcript(adls, run_id, name):
    base = f"{adls[2]}/_agent_runs/{run_id}/{name}"
    text, status = read_adls(adls, base + ".txt"), read_adls(adls, base + ".json")
    local = os.path.join(HERE, "runs", run_id)
    os.makedirs(local, exist_ok=True)
    if text is not None:
        text = redact_sensitive_text(text)
        open(os.path.join(local, name + ".txt"), "w", encoding="utf-8").write(text)
    return text, redact_sensitive_data(json.loads(status)) if status else None, local


def lab_adls():
    sys.path.insert(0, os.path.dirname(HERE))
    try:
        from lab_config import LAB
        return LAB["ADLS_ACCOUNT"], LAB["ADLS_CONTAINER"], LAB["ADLS_ROOT"]
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="wrap, upload, run and print the transcript")
    r.add_argument("notebook", help="path to a built notebook, e.g. out_lab/V2_2_Backup_Data.ipynb")
    r.add_argument("--workspace", default=os.environ.get("FABRIC_RUNNER_WORKSPACE_ID"),
                   help="runner workspace ID (not a workspace being migrated); or FABRIC_RUNNER_WORKSPACE_ID")
    r.add_argument("--set", action="append", metavar="NAME=VALUE", help="override a setting (Python literal)")
    r.add_argument("--allow-real", action="store_true", help="V2_0/V2_2/V2_3 only, when the person asked for it")
    r.add_argument("--timeout", type=int, default=5400, help="seconds to wait for the job (default 90 min)")
    r.add_argument("--adls", help="account/container/root for transcripts (default: lab_config.py)")
    t = sub.add_parser("transcript", help="download a transcript again")
    t.add_argument("run_id")
    t.add_argument("notebook_name")
    t.add_argument("--adls")
    a = ap.parse_args(argv)

    adls = tuple(a.adls.split("/", 2)) if a.adls else lab_adls()
    if not adls or len(adls) != 3:
        raise SystemExit("Transcript location unknown: pass --adls account/container/root")
    if a.cmd == "transcript":
        text, status, local = fetch_transcript(adls, a.run_id, a.notebook_name)
        print(text if text is not None else "(no transcript)")
        print(f"\n[runner] status: {status}; saved under {local}")
        return 0 if status and status.get("status") == "completed" else 1

    if not a.workspace or not re.match(r"^[0-9a-fA-F-]{36}$", a.workspace):
        raise SystemExit("--workspace: the runner workspace ID (GUID), or set FABRIC_RUNNER_WORKSPACE_ID")
    name = os.path.splitext(os.path.basename(a.notebook))[0]
    overrides = parse_overrides(a.set, name, a.allow_real)
    run_id = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:4]
    name, nb = wrap(a.notebook, overrides, a.allow_real, run_id, list(adls))
    print(f"[runner] run {run_id}: {name} (dry-run only: {name.startswith(DESTRUCTIVE)}; overrides {redact_sensitive_data(overrides)})")
    item_id, how = upsert_notebook(a.workspace, "AGENT_" + name, nb)
    print(f"[runner] notebook AGENT_{name} {how} ({item_id}); starting job", flush=True)
    job = run_job(a.workspace, item_id, a.timeout)
    text, status, local = fetch_transcript(adls, run_id, name)
    print("\n" + "=" * 30 + " TRANSCRIPT " + "=" * 30)
    print(text if text is not None else "(no transcript: the job failed before the first cell finished; see below)")
    print("=" * 72)
    print(redact_sensitive_text(f"[runner] job: {job.get('status')}; failure: {json.dumps(job.get('failureReason'))}"))
    print(f"[runner] notebook status: {status}; transcript saved under {local}")
    return 0 if job.get("status") == "Completed" and status and status.get("status") == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
