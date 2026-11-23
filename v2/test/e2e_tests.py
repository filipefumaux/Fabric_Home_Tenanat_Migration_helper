"""Tests for agent/e2e_rehearsal.py and the rehearsal mode of the agent wrapper (fake Fabric, no network)."""
import base64, contextlib, io, json, os, re, shutil, sys, tempfile, types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))
import fabric_agent_run as far
import e2e_rehearsal as e2e

# CLI default tests use the fake lab_config runner, not the terminal's live workspace.
os.environ.pop("FABRIC_RUNNER_WORKSPACE_ID", None)

FAIL = []


def check(c, m):
    m = str(m)
    print(("ok   " + m[:160]) if c else ("FAIL " + m[-3000:]))
    c or FAIL.append(m)


TMP = tempfile.mkdtemp()
NBDIR = os.path.join(TMP, "nb")
os.makedirs(NBDIR)
SETTINGS = {"V2_T": 'TARGET_WORKSPACE_ID = ""', "V2_0": 'WORKSPACE_IDS = ["old"]',
            "V2_1": 'WORKSPACE_IDS = ["old"]\nDRY_RUN = True\nCONFIRM_CONNECT_ID = ""',
            "V2_2": 'WORKSPACE_IDS = ["old"]\nDRY_RUN = True\nFREEZE_STARTED_UTC = ""',
            "V2_3": 'WORKSPACE_IDS = ["old"]\nDRY_RUN = True\nSECONDARY_BACKUP_RUN_ID = ""\nFREEZE_STARTED_UTC = ""',
            "V2_4": 'SNAPSHOT_ID = ""\nWORKSPACE_IDS = []\nDRY_RUN = True\nCONFIRM_DELETE_ID = ""',
            "V2_5": 'SNAPSHOT_ID = ""\nDRY_RUN = True\nCONFIRM_PLAN_ID = ""\nFIX_STALE_REFERENCES = False\n'
                    'CONFIRM_FIX_ID = ""\nWORKSPACE_MAP = {}',
            "V2_6": 'SNAPSHOT_ID = ""\nDRY_RUN = True\nCONFIRM_DATA_ID = ""\nWORKSPACE_MAP = {}'}


def mk(path, settings, body="print('body')"):
    nb = {"cells": [{"cell_type": "code", "source": [settings], "metadata": {"tags": ["parameters"]}},
                    {"cell_type": "code", "source": [body], "metadata": {}}],
          "metadata": {"kernelspec": {"name": "jupyter"}}, "nbformat": 4, "nbformat_minor": 5}
    json.dump(nb, open(path, "w"))


for k, n in e2e.NOTEBOOKS.items():
    mk(os.path.join(NBDIR, n + ".ipynb"), SETTINGS[k])

RUNNER = "99999999-0000-4000-8000-000000000000"
CAP = "cccccccc-0000-4000-8000-000000000000"
SP = "ServicePrincipal:aaaaaaaa-0000-4000-8000-000000000000:Contributor"


class Fabric:
    """Fake Fabric + fake notebook runs: reads the wrapped notebook's payload and acts like the real notebook."""

    def __init__(self):
        self.ws, self.items, self.adls, self.runs, self.role = {}, {}, {}, [], []
        self.deleted_ws = []
        self.refs_fixed = False
        self.v26_attention = 0
        self.extra_manifest_ws = False
        self.rename_ws = False
        self.n = 0
        self.linked, self.disconnects, self.hard_deleted = set(), [], []
        self.build = None
        self.dirmap = None
        self.seed_metrics_change = None
        self.seed_ids_change = None

    # Fabric REST
    def call(self, method, url, resource=None, ok=(200,), json=None, **kw):
        p = url.replace(far.FABRIC, "")
        R = types.SimpleNamespace
        if method == "POST" and p == "/workspaces":
            wid = f"{len(self.ws) + 1:08d}-0000-4000-8000-000000000000"
            self.ws[wid] = dict(json, id=wid)
            self.items[wid] = []
            return R(json=lambda: self.ws[wid], status_code=201)
        m = re.fullmatch(r"/workspaces/([^/]+)", p)
        if m and method == "GET":
            w = dict(self.ws[m.group(1)])
            if self.rename_ws:
                w["displayName"] = "Sales"
            return R(json=lambda: w, status_code=200)
        if m and method == "DELETE":
            self.deleted_ws.append(m.group(1))
            return R(status_code=200)
        m = re.fullmatch(r"/workspaces/([^/]+)/git/connection", p)
        if m:
            st_ = "ConnectedAndInitialized" if m.group(1) in self.linked else "NotConnected"
            return R(json=lambda: {"gitConnectionState": st_}, status_code=200)
        m = re.fullmatch(r"/workspaces/([^/]+)/git/disconnect", p)
        if m:
            self.linked.discard(m.group(1)); self.disconnects.append(m.group(1))
            return R(status_code=200)
        m = re.fullmatch(r"/workspaces/([^/]+)/items/([^/?]+)\?hardDelete=true", p)
        if m and method == "DELETE":
            self.hard_deleted.append(m.group(2))
            self.items[m.group(1)] = [i for i in self.items[m.group(1)] if i["id"] != m.group(2)]
            return R(status_code=200)
        m = re.fullmatch(r"/workspaces/([^/]+)/roleAssignments", p)
        if m:
            self.role.append(json)
            return R(status_code=201)
        m = re.fullmatch(r"/workspaces/([^/]+)/items", p)
        if m:
            return R(json=lambda: {"value": list(self.items[m.group(1)])}, status_code=200)
        raise AssertionError((method, url))

    def upsert(self, ws, name, nb):
        assert ws == RUNNER, ws
        self.last_nb = nb
        return "agent-nb", "updated"

    def run_job(self, ws, item_id, timeout):
        code = "".join(self.last_nb["cells"][1]["source"])
        payload = json.loads(base64.b64decode(re.search(r'b64decode\("([^"]+)"\)', code).group(1)))
        self.runs.append(payload)
        ok, facts = self.act(payload)
        if self.build:
            facts["BUILD_ID"] = self.build
        self.adls[(payload["run"], payload["notebook"])] = {"status": "completed" if ok else "failed", "facts": facts}
        return {"status": "Completed" if ok else "Failed"}

    def fetch(self, adls, run_id, name):
        local = os.path.join(TMP, "runs", run_id)
        os.makedirs(local, exist_ok=True)
        return "transcript", self.adls.get((run_id, name)), local

    def read_adls(self, adls, path):
        m = re.search(r"_migration/([^/]+)/manifest.json$", path)
        return self.adls.get(("manifest", m.group(1))) if m else None

    # what each notebook does
    def act(self, p):
        o, e = p["overrides"], p["e2e"]
        ws, conf, nb = e["workspace"], e["confirm"], p["notebook"][:4]
        dry = o.get("DRY_RUN", True)
        ok_log = {"rows": 2, "statusColumn": "Status", "statusCounts": {"OK": 2}, "problems": [], "records": []}
        if nb == "V2_T":
            assert o["TARGET_WORKSPACE_ID"] == ws
            if o.get("MEASURE_DATA_SIZE"):
                prefix = o["PREFIX"]
                ids = {}
                for t in ("Lakehouse", "SQLEndpoint", "Warehouse", "Eventhouse", "KQLDatabase", "Notebook",
                          "DataPipeline", "Dataflow", "DataAgent", "GraphModel", "Ontology"):
                    name = f"{prefix}_{t.lower()}"
                    item = next((i for i in self.items[ws] if i["type"] == t and i["displayName"] == name), None)
                    if not item:
                        item = {"id": f"{prefix}-{t}", "type": t, "displayName": name}
                        self.items[ws].append(item)
                    ids[t] = item["id"]
                if self.seed_ids_change:
                    self.seed_ids_change(o, ids)
                target = o.get("LARGE_DATA_MIB", 0) * 1024 ** 2
                metrics = {t: {"itemId": ids["KQLDatabase"] if t == "Eventhouse" else ids[t],
                               "table": f"{prefix}_stress", "rows": 200000 if target else 1000,
                               "storedBytes": target or 2048, "targetBytes": target, "status": "OK",
                               "sizeMetric": "compressed_extent_bytes" if t == "Eventhouse" else "active_parquet_bytes"}
                           for t in ("Lakehouse", "Warehouse", "Eventhouse")}
                if self.seed_metrics_change:
                    self.seed_metrics_change(o, metrics)
                return True, {"failed": {"rows": 0, "records": []}, "WS": ws, "IDS": ids, "SEED_METRICS": metrics}
            self.items[ws] += [{"id": f"i{i}", "type": t, "displayName": n} for i, (t, n) in enumerate([
                ("Lakehouse", "bkp_test_lh"), ("SQLEndpoint", "bkp_test_lh"), ("Warehouse", "bkp_test_wh"),
                ("Eventhouse", "bkp_test_eh"), ("KQLDatabase", "bkp_test_eh"), ("Notebook", "bkp_test_nb"),
                ("SemanticModel", "bkp_test_sm"), ("Lakehouse", "StagingLakehouseForDataflows_20261113112943")])]
            return True, {"failed": {"rows": 0, "records": []}, "WS": ws}
        assert o.get("WORKSPACE_IDS", [ws]) == [ws], o
        if nb == "V2_0":
            return True, {"CHECK_DF": {"rows": 5, "statusColumn": "Result", "statusCounts": {"PASS": 4, "INFO": 1},
                                       "problems": []}}
        if nb == "V2_1":
            self.dirmap = o.get("DIRECTORY_MAP")
            if dry:
                return True, {"CONNECT_ID": "c-1", "DRY_RUN": True}
            assert conf == {"CONFIRM_CONNECT_ID": "c-1"}, conf
            return True, {"RESULTS": ok_log}
        if nb == "V2_2":
            assert o["FREEZE_STARTED_UTC"].endswith("Z") and dry is False
            return True, {"RUN_ID": "20261117T100000Z", "RESULTS": ok_log}
        if nb == "V2_3":
            assert o["SECONDARY_BACKUP_RUN_ID"] == "20261117T100000Z" and dry is False
            ids = [{"id": ws}] + ([{"id": "other"}] if self.extra_manifest_ws else [])
            self.adls[("manifest", "S-1")] = json.dumps({"workspaces": ids})
            return True, {"SNAPSHOT_ID": "S-1", "ready": [self.ws[ws]["displayName"]], "blocked": [],
                          "RESULTS": ok_log}
        if nb == "V2_4":
            if dry:
                return True, {"DELETE_ID": "d-1"}
            assert conf == {"CONFIRM_DELETE_ID": "d-1"}
            self.saved = [i for i in self.items[ws]]
            self.items[ws] = [i for i in self.items[ws] if i["type"] in e2e.KEPT_BY_V2_4]
            return True, {"RESULTS": ok_log}
        if nb == "V2_5":
            missing = [i for i in self.saved if i not in self.items[ws] and not e2e.STAGING_RE.match(i["displayName"])]
            refs = [] if self.refs_fixed else [{"Item": "bkp_test_nb", "Fix": "automatic (with confirmation)"}]
            ref_df = {"rows": len(refs), "records": refs}
            if dry:
                return True, {"PLAN_ID": f"p-{len(missing)}", "TODO_count": len(missing),
                              "REF_DF": ref_df if not missing else {"rows": 0, "records": []},
                              "FIX_ID": "f-1" if refs and not missing else ""}
            if conf.get("FIX_STALE_REFERENCES"):
                assert conf["CONFIRM_FIX_ID"] == "f-1"
                self.refs_fixed = True
                return True, {"RESULTS": ok_log}
            assert conf == {"CONFIRM_PLAN_ID": f"p-{len(missing)}"}, conf
            self.items[ws] += missing
            return True, {"RESULTS": ok_log}
        if nb == "V2_6":
            if dry:
                return True, {"DATA_ID": "x-1"}
            assert conf == {"CONFIRM_DATA_ID": "x-1"}
            att = {"rows": self.v26_attention, "problems": [{"Item": "bkp_test_wh", "Step": "Verify",
                                                             "Status": "Failed", "Detail": "rows differ"}]
                   if self.v26_attention else []}
            return True, {"attention": att, "blocked": [], "RESULTS": ok_log}
        raise AssertionError(nb)


def setup(world):
    far.call, far.upsert_notebook, far.run_job = world.call, world.upsert, world.run_job
    far.fetch_transcript, far.read_adls = world.fetch, world.read_adls
    e2e.HERE = TMP
    e2e.subprocess = types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="...\nRESULT: ALL PASSED\n", stderr=""))


def start(extra=()):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = e2e.main(["start", "--capacity", CAP, "--runner-workspace", RUNNER, "--member", SP,
                           "--notebooks", NBDIR, "--adls", "acc/cont/root", *extra])
        except SystemExit as exc:
            rc = exc
    return rc, buf.getvalue()


def latest():
    rid = sorted(os.listdir(os.path.join(TMP, "e2e")))[-1]
    return rid, e2e.load(rid)


print("E1 happy path: whole chain, confirmations from the dry runs, references fixed, GREEN report")
w = Fabric(); setup(w)
rc, out = start()
rid, st = latest()
rep = open(os.path.join(TMP, "e2e", rid, "E2E_REPORT.md")).read()
check(rc == 0 and "GREEN" in rep.splitlines()[0], out[-2500:])
check(all(st["steps"][s]["ok"] for s in e2e.STEPS), [s for s in e2e.STEPS if not st["steps"].get(s, {}).get("ok")])
check(w.ws[st["workspace"]]["displayName"].startswith("E2E-REHEARSAL-") and rid in w.ws[st["workspace"]]["description"]
      and w.role == [{"principal": {"id": SP.split(":")[1], "type": "ServicePrincipal"}, "role": "Contributor"}],
      "workspace created with marker and service principal member")
check(w.refs_fixed and "reference fix" in rep and "Not covered by this rehearsal" in rep and "Build ID" in rep,
      "reference fix run, report lists what isn't covered and the build")
check(not w.deleted_ws, "rehearsal workspace kept until cleanup")
check(all(r["e2e"]["workspace"] == st["workspace"] for r in w.runs) and not any(
      k.startswith("CONFIRM_") for r in w.runs for k in r["overrides"]), "confirmations only via the rehearsal channel")

print("E2 data not verified: RED, stops at V2_6; resume after the fix only reruns V2_6 onwards -> GREEN")
w = Fabric(); w.v26_attention = 1; setup(w)
rc, out = start()
rid, st = latest()
check(rc == 1 and not st["steps"]["V2_6"]["ok"] and "compare" not in st["steps"] and "STOPPED at V2_6" in out,
      out[-1500:])
check("RED" in open(os.path.join(TMP, "e2e", rid, "E2E_REPORT.md")).read().splitlines()[0], "RED report")
w.v26_attention = 0
n_before = len(w.runs)
with contextlib.redirect_stdout(io.StringIO()):
    rc = e2e.main(["resume", rid])
names = [r["notebook"][:4] for r in w.runs[n_before:]]
check(rc == 0 and set(names) == {"V2_6"}, names)

print("E3 guard: a workspace that isn't the rehearsal's (name changed) -> nothing real runs")
w = Fabric(); setup(w)
orig = e2e.step_ws
def ws_then_rename(st):
    r = orig(st); w.rename_ws = True; return r
e2e.RUNNERS["ws"] = ws_then_rename
rc, out = start()
e2e.RUNNERS["ws"] = orig
check(isinstance(rc, SystemExit) and "REHEARSAL GUARD" in str(rc) and not w.runs, str(rc))

print("E4 guard: snapshot manifest with another workspace -> stops before V2_4, nothing deleted")
w = Fabric(); w.extra_manifest_ws = True; setup(w)
rc, out = start()
rid, st = latest()
check(isinstance(rc, SystemExit) and "holds workspaces" in str(rc)
      and not any(r["notebook"].startswith("V2_4") for r in w.runs), str(rc))

print("E5 runner workspace can't be the rehearsal workspace; bad --member refused")
w = Fabric(); setup(w)
st = {"id": "x", "workspace": RUNNER, "runner": RUNNER}
try:
    e2e.assert_rehearsal_workspace(st); check(False, "same workspace refused")
except SystemExit as exc:
    check("runner workspace" in str(exc), str(exc))
try:
    e2e.main(["start", "--capacity", CAP, "--runner-workspace", RUNNER, "--member", "ServicePrincipal:x:Owner",
              "--notebooks", NBDIR, "--adls", "a/b/c"]); check(False, "bad member")
except SystemExit as exc:
    check("TYPE:OBJECT-ID:ROLE" in str(exc), str(exc))

print("E6 --skip-local-tests can't give GREEN; a failing local suite stops before Fabric")
w = Fabric(); setup(w)
rc, out = start(["--skip-local-tests"])
check(rc == 1 and not w.ws and not w.runs, "skipped local tests: RED, nothing created")
e2e.subprocess = types.SimpleNamespace(run=lambda *a, **k: types.SimpleNamespace(
    returncode=1, stdout="RESULT: 2 FAILED\n", stderr=""))
rc, out = start()
check(rc == 1 and not w.ws, "failing suite: RED, nothing created")

print("E7 notebooks changed after the rehearsal started: report RED")
w = Fabric(); setup(w)
rc, out = start()
rid, st = latest()
p = os.path.join(NBDIR, "V2_2_Backup_Data.ipynb")
mk(p, SETTINGS["V2_2"] + "\n# edited")
with contextlib.redirect_stdout(io.StringIO()):
    rc2 = e2e.main(["report", rid])
check(rc == 0 and rc2 == 1 and "changed since the rehearsal started" in
      open(os.path.join(TMP, "e2e", rid, "E2E_REPORT.md")).read(), "changed build -> RED")
mk(p, SETTINGS["V2_2"])

print("E8 cleanup deletes only the rehearsal workspace, only with --yes")
try:
    e2e.main(["cleanup", rid]); check(False, "needs --yes")
except SystemExit as exc:
    check("--yes" in str(exc), str(exc))
with contextlib.redirect_stdout(io.StringIO()):
    e2e.main(["cleanup", rid, "--yes"])
check(w.deleted_ws == [st["workspace"]], w.deleted_ws)

print("E9 run_nb refuses confirmation or cleanup settings as overrides")
for ov in ({"CONFIRM_DELETE_ID": "x"}, {"DELETE_TEST_ITEMS": True}, {"HARD_DELETE": False}):
    try:
        e2e.run_nb(st, "V2_4", ov, real=False); check(False, str(ov))
    except SystemExit:
        check(True, f"refused {ov}")

print("E10 wrapper in rehearsal mode: workspace scope enforced inside the notebook, confirmations applied, facts saved")
STORE = {}
class R:
    status_code = 200
    def raise_for_status(self): pass
def fake_requests():
    m = types.ModuleType("requests"); pend = {}
    def put(u, **k): pend[u.split("?")[0]] = b""; return R()
    def patch(u, data=None, **k):
        b = u.split("?")[0]
        if "append" in u: pend[b] += data
        else: STORE[b] = pend.pop(b)
        return R()
    m.put, m.patch = put, patch
    return m
def execute(nb):
    code = "".join(nb["cells"][1]["source"])
    real = sys.modules.get("requests")
    sys.modules["requests"] = fake_requests()
    g = {"notebookutils": types.SimpleNamespace(credentials=types.SimpleNamespace(getToken=lambda a: "t")),
         "__name__": "__main__", "display": lambda *a, **k: None}
    err = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            exec(compile(code, "drv", "exec"), g)
    except BaseException as exc:
        err = exc
    sys.modules["requests"] = real
    return g, err
WS = "11111111-0000-4000-8000-000000000000"
body = ("import pandas as pd\nRESULTS = [{'Status': 'OK'}, {'Status': 'Failed', 'Item': 'x', 'Detail': 'boom'}]\n"
        "DELETE_ID = 'd-9'\nattention = pd.DataFrame([{'Status': 'Warning'}])\nprint('confirm', CONFIRM_DELETE_ID)")
p = os.path.join(TMP, "V2_4_Delete_Fabric_Items.ipynb")
mk(p, 'WORKSPACE_IDS = []\nDRY_RUN = True\nCONFIRM_DELETE_ID = ""', body)
_, nb = far.wrap(p, {"WORKSPACE_IDS": [WS], "DRY_RUN": False}, True, "E10", ["acc", "cont", "root"],
                 e2e={"workspace": WS, "confirm": {"CONFIRM_DELETE_ID": "d-9"}})
g, err = execute(nb)
st10 = json.loads(STORE.get("https://acc.dfs.core.windows.net/cont/root/_agent_runs/E10/V2_4_Delete_Fabric_Items.json", b"{}"))
f = st10.get("facts", {})
check(err is None and g["CONFIRM_DELETE_ID"] == "d-9" and g["DRY_RUN"] is False, repr(err))
check(f.get("DELETE_ID") == "d-9" and f["RESULTS"]["statusCounts"] == {"OK": 1, "Failed": 1}
      and f["RESULTS"]["problems"][0]["Detail"] == "boom" and f["attention"]["rows"] == 1, f)
_, nb = far.wrap(p, {"WORKSPACE_IDS": ["22222222-0000-4000-8000-000000000000"], "DRY_RUN": False}, True, "E10b",
                 ["acc", "cont", "root"], e2e={"workspace": WS, "confirm": {"CONFIRM_DELETE_ID": "d-9"}})
g, err = execute(nb)
check(isinstance(err, RuntimeError) and "AGENT GUARD (rehearsal)" in str(err), repr(err))
p6 = os.path.join(TMP, "V2_6_Restore_Data.ipynb")
mk(p6, 'DRY_RUN = True\nWORKSPACE_MAP = {"a": "b"}')
_, nb = far.wrap(p6, {}, True, "E10c", ["acc", "cont", "root"], e2e={"workspace": WS, "confirm": {}})
g, err = execute(nb)
check(isinstance(err, RuntimeError) and "WORKSPACE_MAP" in str(err), repr(err))
_, nb = far.wrap(p, {"DRY_RUN": False}, True, "E10d", ["acc", "cont", "root"])     # no rehearsal: still refused
g, err = execute(nb)
check(isinstance(err, RuntimeError) and "only runs as a dry run" in str(err), "outside a rehearsal V2_4 stays dry-run only")

LABWS = "77777777-0000-4000-8000-000000000000"
e2e.lab_e2e = lambda: {"RUNNER_WORKSPACE_ID": RUNNER, "REHEARSAL_WORKSPACE_IDS": [LABWS], "MEMBERS": []}


def reuse_world(dirty=False):
    w = Fabric(); setup(w)
    w.ws[LABWS] = {"id": LABWS, "displayName": "rehearsal-test-ws", "description": ""}
    w.items[LABWS] = [{"id": "pbi", "type": "SemanticModel", "displayName": "kept"}]
    if dirty:
        w.linked.add(LABWS)
        w.items[LABWS] += [{"id": "old1", "type": "Lakehouse", "displayName": "bkp_test_lh"},
                           {"id": "old2", "type": "SQLEndpoint", "displayName": "bkp_test_lh"},
                           {"id": "old3", "type": "Notebook", "displayName": "bkp_test_nb"}]
    return w


def start_reuse(extra=()):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            rc = e2e.main(["start", "--workspace", LABWS, "--notebooks", NBDIR, "--adls", "acc/cont/root", *extra])
        except SystemExit as exc:
            rc = exc
    return rc, buf.getvalue()


print("E11 reuse the listed lab workspace (runner from lab_config), fresh Git folder per rehearsal -> GREEN")
w = reuse_world()
rc, out = start_reuse()
rid, st = latest()
check(rc == 0 and st["runner"] == RUNNER and st["reused"] and not w.ws.get("00000001-0000-4000-8000-000000000000"),
      out[-1500:])
check(w.dirmap == {LABWS: f"/e2e-rehearsals/{rid}/rehearsal-test-ws"}, w.dirmap)
try:
    e2e.main(["cleanup", rid, "--yes"]); check(False, "reused workspace not deleted")
except SystemExit as exc:
    check("isn't deleted" in str(exc) and LABWS not in w.deleted_ws, str(exc))
check("reused (lab list)" in open(os.path.join(TMP, "e2e", rid, "E2E_REPORT.md")).read(), "report says reused")

print("E12 reused workspace not clean, no --reset: stops at ws, lists what would be deleted, touches nothing")
w = reuse_world(dirty=True)
rc, out = start_reuse()
rid, st = latest()
check(rc == 1 and not st["steps"]["ws"]["ok"] and "bkp_test_nb" in out and "--reset" in out
      and not w.hard_deleted and not w.disconnects and not w.runs, out[-1500:])

print("E13 --reset: Git disconnected, every non-Power BI item hard-deleted (SQL endpoint with its Lakehouse), then GREEN")
w = reuse_world(dirty=True)
rc, out = start_reuse(["--reset"])
check(rc == 0 and w.disconnects == [LABWS] and sorted(w.hard_deleted) == ["old1", "old3"]
      and w.hard_deleted.index("old3") < w.hard_deleted.index("old1") and "pbi" not in w.hard_deleted, out[-1500:])

print("E14 --workspace must be listed in lab_config; --reset only with --workspace; not the runner")
for args, msg in ([["--workspace", "11111111-0000-4000-8000-000000000000"], "isn't in lab_config"],
                  [["--capacity", CAP, "--reset"], "--reset only with --workspace"]):
    try:
        e2e.main(["start", *args, "--notebooks", NBDIR, "--adls", "a/b/c"]); check(False, msg)
    except SystemExit as exc:
        check(msg in str(exc), str(exc))
e2e.lab_e2e = lambda: {"RUNNER_WORKSPACE_ID": LABWS, "REHEARSAL_WORKSPACE_IDS": [LABWS]}
try:
    e2e.main(["start", "--workspace", LABWS, "--notebooks", NBDIR, "--adls", "a/b/c"]); check(False, "runner")
except SystemExit as exc:
    check("runner workspace" in str(exc), str(exc))
e2e.lab_e2e = lambda: {"RUNNER_WORKSPACE_ID": RUNNER, "REHEARSAL_WORKSPACE_IDS": [LABWS], "MEMBERS": []}

print("E15 the notebook that ran must be the expected build (BUILD_ID it prints)")
w = reuse_world(); w.build = "000000000000"
rc, out = start_reuse()
rid, st = latest()
check(rc == 1 and "expected" in out and not st["steps"]["V2_T"]["ok"], out[-800:])

print("E16 verify-build: settings-only differences match, code differences don't")
d1, d2 = os.path.join(TMP, "b1"), os.path.join(TMP, "b2")
for d, settings, code in ((d1, "X = 1", "print(1)"), (d2, "X = 2", "print(1)")):
    os.makedirs(d, exist_ok=True)
    for n in e2e.NOTEBOOKS.values():
        mk(os.path.join(d, n + ".ipynb"), settings, 'BUILD_ID = "abcdefabcdef"\n' + code)
check(e2e.build_fingerprint(d1) == e2e.build_fingerprint(d2)
      and set(e2e.build_fingerprint(d1).values()) == {"abcdefabcdef"}, e2e.build_fingerprint(d1))
st = e2e.load(latest()[0]); st["fingerprint"] = e2e.build_fingerprint(d1); st["notebooks"] = d1; e2e.save(st)
with contextlib.redirect_stdout(io.StringIO()) as buf:
    rc = e2e.main(["verify-build", st["id"], "--notebooks", d2])
check(rc == 1 and "same code as rehearsed" in buf.getvalue(), buf.getvalue())   # not GREEN (E15 run), code same
mk(os.path.join(d2, "V2_4_Delete_Fabric_Items.ipynb"), "X = 2", 'BUILD_ID = "111111111111"\nprint(2)')
with contextlib.redirect_stdout(io.StringIO()) as buf:
    rc = e2e.main(["verify-build", st["id"], "--notebooks", d2])
check(rc == 1 and "DIFFERENT" in buf.getvalue() and "V2_4_Delete_Fabric_Items" in buf.getvalue(), buf.getvalue())
st["steps"] = {k: {"ok": True, "checks": [("x", "PASS", "")]} for k in e2e.STEPS}; e2e.save(st)
mk(os.path.join(d2, "V2_4_Delete_Fabric_Items.ipynb"), "X = 2", 'BUILD_ID = "abcdefabcdef"\nprint(1)')
e2e.lab_e2e = lambda: {"RUNNER_WORKSPACE_ID": RUNNER, "REHEARSAL_WORKSPACE_IDS": [LABWS], "LAB_MARKERS": ["private-test-marker"]}
with contextlib.redirect_stdout(io.StringIO()) as buf:
    rc = e2e.main(["verify-build", st["id"], "--notebooks", d2])
check(rc == 0 and "GREEN; " in buf.getvalue() and "no lab values" in buf.getvalue(), buf.getvalue())
open(os.path.join(d2, "README_v2.md"), "w").write("user@private-test-marker.net")
with contextlib.redirect_stdout(io.StringIO()) as buf:
    rc = e2e.main(["verify-build", st["id"], "--notebooks", d2])
check(rc == 1 and "LAB VALUES FOUND" in buf.getvalue() and "README_v2.md: private-test-marker" in buf.getvalue(), buf.getvalue())

print("E17 stress profile: four independent sets, one measured large copy and three measured small copies")
w = reuse_world()
rc, out = start_reuse(["--copies", "4", "--large-mib", "1024", "--seed-prefix", "bkp_stress",
                       "--isolated-stress-runtime"])
rid, st = latest()
seeds = [r for r in w.runs if r["notebook"].startswith("V2_T")]
check(rc == 0 and len(seeds) == 4, out[-1500:])
check([r["overrides"].get("PREFIX") for r in seeds] == [f"bkp_stress_{i:02d}" for i in range(1, 5)]
      and [r["overrides"].get("LARGE_DATA_MIB") for r in seeds] == [1024, 0, 0, 0],
      "distinct prefixes; only the first copy requests the large dataset")
check(seeds and all(r["overrides"].get("MEASURE_DATA_SIZE") is True
      and r["e2e"]["workspace"] == LABWS for r in seeds), "every copy is measured and stays inside the same guarded workspace")
check(seeds and all(r["overrides"].get("ISOLATED_STRESS_RUNTIME") is True for r in seeds),
      "approved stress-only runtime option reaches each seed job")
check(len(st.get("seedCopies", {})) == 4 and len({i for c in st.get("seedCopies", {}).values()
      for i in c.get("ids", {}).values()}) == 44, "all eleven explicit item types have four distinct instances")
profile_report = open(os.path.join(TMP, "e2e", rid, "E2E_REPORT.md")).read()
check(rc == 0 and "Seed profile" in profile_report and "active_parquet_bytes" in profile_report
      and "compressed_extent_bytes" in profile_report, "report preserves actual bytes and the physical-size metric")

print("E18 oversized small copy stops before backup; resume does not rerun the successful large seed")
def oversized_small(o, metrics):
    if o["PREFIX"].endswith("_02"):
        metrics["Warehouse"]["storedBytes"] = 1024 ** 3

w = reuse_world(); w.seed_metrics_change = oversized_small
rc, out = start_reuse(["--copies", "4", "--large-mib", "1024", "--seed-prefix", "bkp_stress"])
rid, st = latest()
check(rc == 1 and not st.get("steps", {}).get("V2_T", {}).get("ok")
      and not any(r["notebook"].startswith("V2_2") for r in w.runs),
      "an accidentally large second copy cannot reach backup or deletion")
if rc == 1 and "seedCopies" in st:
    w.seed_metrics_change = None
    before = len(w.runs)
    with contextlib.redirect_stdout(io.StringIO()):
        resumed = e2e.main(["resume", rid])
    repeated = [r["overrides"].get("PREFIX") for r in w.runs[before:] if r["notebook"].startswith("V2_T")]
    check(resumed == 0 and repeated == ["bkp_stress_02", "bkp_stress_03", "bkp_stress_04"],
          f"only incomplete copies are retried: {repeated}")
else:
    check(False, "stress resume state was not recorded")

print("E19 logical-only metrics, boolean byte counts and metrics for the wrong item are rejected")
for change in (lambda o, m: m["Eventhouse"].update(sizeMetric="logical_bytes"),
               lambda o, m: m["Warehouse"].update(storedBytes=True),
               lambda o, m: m["Warehouse"].update(itemId=m["Lakehouse"]["itemId"]),
               lambda o, m: m["Lakehouse"].update(storedBytes=m["Lakehouse"]["targetBytes"] // 2),
               lambda o, m: m["Warehouse"].update(storedBytes=m["Warehouse"]["targetBytes"] * 2)):
    w = reuse_world(); w.seed_metrics_change = change
    rc, out = start_reuse(["--copies", "4", "--large-mib", "1024"])
    check(rc == 1 and not any(r["notebook"].startswith("V2_2") for r in w.runs),
          "unproven or mismatched stored-byte measurements fail before migration")

print("E20 invalid seed profiles are refused before creating a workspace")
for extra, message in ((["--copies", "0"], "copies"),
                       (["--large-mib", "-1"], "large-mib"),
                       (["--seed-prefix", "AGENT_hidden"], "seed-prefix")):
    w = Fabric(); setup(w)
    rc, out = start(extra)
    check(isinstance(rc, SystemExit) and message in str(rc) and not w.ws and not w.runs, str(rc))

print("E21 missing or reused explicit item IDs cannot masquerade as four copies of every type")
def missing_ontology(o, ids):
    if o["PREFIX"].endswith("_02"):
        ids.pop("Ontology")

def reused_notebook(o, ids):
    if o["PREFIX"].endswith("_02"):
        ids["Notebook"] = "bkp_stress_01-Notebook"

for change in (missing_ontology, reused_notebook):
    w = reuse_world(); w.seed_ids_change = change
    rc, out = start_reuse(["--copies", "4", "--large-mib", "1024", "--seed-prefix", "bkp_stress"])
    rid, st = latest()
    check(rc == 1 and not st.get("steps", {}).get("baseline", {}).get("ok")
          and not any(r["notebook"].startswith("V2_2") for r in w.runs),
          "unproven object multiplicity blocks backup/delete rather than counting system extras")

print("E22 reports keep transcript locations when Windows drives differ")
if os.name == "nt":
    rid, st = latest()
    transcript = r"Z:\rehearsal-transcripts\run.txt"
    st["transcripts"] = [{"step": "V2_T", "mode": "REAL", "minutes": 1, "file": transcript}]
    expected_verdict = e2e.verdict(st)[0]
    try:
        green, md = e2e.report(st)
    except ValueError:
        check(False, "cross-drive transcript paths must not stop report generation")
    else:
        check(transcript in md and green == expected_verdict,
              "absolute transcript location preserved without changing the verdict")

print("E23 rehearsal reports redact diagnostic URLs without changing state or verdict")
rid, st = latest()
signature = "synthetic-" + "signature-canary"
detail = "https://example.invalid/data?sig=" + signature + " full fix retained"
st["steps"]["local"]["checks"].append(("diagnostic canary", "PASS", detail))
expected_verdict = e2e.verdict(st)[0]
green, md = e2e.report(st)
json_report = open(os.path.join(TMP, "e2e", rid, "E2E_REPORT.json")).read()
check(signature not in md + json_report and "full fix retained" in md,
      "Markdown and JSON report diagnostics redact credentials but keep fix text")
check(st["steps"]["local"]["checks"][-1][2] == detail and green == expected_verdict,
      "redaction does not change workflow state or verification outcomes")

shutil.rmtree(TMP, ignore_errors=True)
print("\nRESULT:", "ALL PASSED" if not FAIL else f"{len(FAIL)} FAILED")
