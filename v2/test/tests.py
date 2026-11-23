import contextlib, io, json, re, sys, traceback, types
from unittest.mock import patch
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import *

fails = 0
def expect(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + str(msg))
    fails += not cond

def go(world, nb, settings=""):
    out = io.StringIO(); err = None; ns = None
    with contextlib.redirect_stdout(out):
        try:
            ns = run(world, nb, settings)
        except Exception as exc:
            err = exc
            traceback.print_exc(file=out)
    return ns, err, out.getvalue()

def writes(w, hosts=("api.fabric.microsoft.com", "dev.azure.com", "acct.dfs.core.windows.net")):
    return [c[:3] for c in w.calls if c[1] in hosts and c[0] in ("POST", "PUT", "PATCH", "DELETE")
            and not c[2].endswith("/getDefinition")]

SNAP, DEL, RES = "V2_3_Git_Snapshot.ipynb", "V2_4_Delete_Fabric_Items.ipynb", "V2_5_Restore_From_Git.ipynb"
PERM, CONN = "V2_0_Permission_Check.ipynb", "V2_1_Connect_Workspaces_To_Git.ipynb"

def snapshot(w, extra=""):
    ns, err, out = go(w, SNAP, "DRY_RUN = False\n" + extra)
    assert err is None, out[-3000:]
    return ns["SNAPSHOT_ID"], ns, out

print("1 snapshot dry run: reads only, pending change reported")
w = World(); w.defs[w.NB] += " edited"
ns, err, out = go(w, SNAP, "COMMIT_PENDING_CHANGES = True")
expect(err is None, f"no error {out[-2000:] if err else ''}")
expect(not writes(w), f"no writes {writes(w)[:3]}")
expect(ns["WS"][W1]["status"] == "READY after commit", ns["WS"][W1]["status"])
expect("+1 change(s) to commit" in out, "pending commit reported")

print("1b HTTP errors and log rows redact secrets without changing diagnostic context")
canary = "opaque-canary-" + "not-a-real-secret"
signature = "synthetic-" + "signature-canary"
if "register_sensitive_value" in ns:
    ns["register_sensitive_value"](canary)
detail = "retry with the approved setting " + "x" * 600 + " exact fix retained"
error = ns["HttpError"](403, "Forbidden", canary + " " + detail,
                        "https://example.invalid/data?sig=" + signature)
with contextlib.redirect_stdout(io.StringIO()) as captured:
    ns["log"]("Test", {}, "Check", "Failed", canary + " " + detail + " ?sig=" + signature)
expect(canary not in str(error) + captured.getvalue() + ns["RESULTS"][-1]["Detail"]
       and signature not in captured.getvalue() + ns["RESULTS"][-1]["Detail"],
       "HTTP errors, console and recorded log details hide credential canaries")
expect(error.status == 403 and error.code == "Forbidden" and detail in str(error)
       and detail in ns["RESULTS"][-1]["Detail"], "status, full error and fix are preserved")

print("2 snapshot real: commit, locked branch at the commit, manifest with hash")
w = World(); w.defs[w.NB] += " edited"
sid, ns, out = snapshot(w, "COMMIT_PENDING_CHANGES = True")
ws = ns["WS"][W1]
expect(ws["ready"] and ws["status"] == "READY", ws["blockers"])
expect(w.refs["main"] == ws["head"] and "edited" in w.commits[ws["head"]][f"{DIR}/NB.Notebook/content.txt"].decode(),
       "workspace change committed")
br = ws["snapshotBranch"]
expect(w.refs.get(br) == ws["head"] and br in w.locked, f"branch {br} locked at head")
m = json.loads(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])
expect(hashlib.sha256(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"]).hexdigest() ==
       w.adls[f"fabric-backups/_migration/{sid}/manifest.sha256"].decode(), "hash stored")
it = {i["displayName"] + "." + i["type"]: i for i in m["workspaces"][0]["items"]}
expect(it["LH.SQLEndpoint"]["recovery"].startswith("recreated") and it["LH.Lakehouse"]["recovery"] == "Git; data from ADLS",
       {k: v["recovery"] for k, v in it.items()})
expect(len(m["workspaces"][0]["powerBiItems"]) == 2 and "SM.SemanticModel" not in it, "Power BI items separate")

print("3 deletion pending in workspace blocks; nothing committed")
w = World(); w.ws[W1]["items"] = [i for i in w.ws[W1]["items"] if i["id"] != w.PL]
sid, ns, out = snapshot(w)
expect(not ns["WS"][W1]["ready"] and "committing would" in " ".join(ns["WS"][W1]["blockers"]).lower(), ns["WS"][W1]["blockers"])
expect(not [c for c in w.calls if c[2].endswith("commitToGit")] and len(w.refs) == 1, "no commit, no branch")

print("4 incoming Git change blocks")
w = World(); files = {w.path(k): c for k, c in w.tree_view(w.refs["main"]).items()}
files[f"{DIR}/PL.DataPipeline"] = "changed in git|" + w.tree_view(w.refs["main"])["DataPipeline|PL"].rsplit("|", 1)[1]
w.refs["main"] = w.commit(files, w.refs["main"])
sid, ns, out = snapshot(w)
expect(not ns["WS"][W1]["ready"] and "incoming" in " ".join(ns["WS"][W1]["blockers"]).lower(), ns["WS"][W1]["blockers"])

print("5 partial data backup and unacknowledged type block")
w = World(); w.make_backup(data_ok=False); w.add_item(W1, "Environment", "ENV", "env")
w.commits[w.refs["main"]] = w.commits[w.refs["main"]]  # Environment not committed -> pending Added
sid, ns, out = snapshot(w, "COMMIT_PENDING_CHANGES = True")
g = ns["GATE_DF"].set_index(["Item", "Type"])
expect(g.loc[("LH", "Lakehouse"), "Status"] == "BLOCKED" and "Partial" in g.loc[("LH", "Lakehouse"), "Why blocked"],
       "partial data blocks the Lakehouse")
expect("isn't in ITEM_TYPES" in g.loc[("ENV", "Environment"), "Why blocked"], "Environment needs acknowledgement")
expect(not ns["WS"][W1]["ready"] and not any(b.startswith("migration-snapshot") for b in w.refs), "no snapshot branch")
w2 = World(); w2.add_item(W1, "Environment", "ENV", "env")
sid2, ns2, _ = snapshot(w2, 'ACKNOWLEDGED_ITEM_TYPES = ["Environment"]\nCOMMIT_PENDING_CHANGES = True')
expect(ns2["WS"][W1]["ready"], f"acknowledged + committed -> READY {ns2['WS'][W1]['blockers']}")

print("6 freeze after the backup run blocks")
w = World()
sid, ns, out = snapshot(w, 'FREEZE_STARTED_UTC = "2026-11-21T00:00:00Z"')
expect(not ns["WS"][W1]["ready"] and "before the freeze" in " ".join(ns["WS"][W1]["blockers"]), "freeze check")

print("7 delete: dry run, wrong id, new item since snapshot")
w = World(); sid, _, _ = snapshot(w)
n0 = len(w.calls)
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and not writes(type("x", (), {"calls": w.calls[n0:]})), f"dry run, no writes {err}")
did = ns["DELETE_ID"]
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action))
expect(acts["NB.Notebook"] == "delete" and acts["LH.SQLEndpoint"] == "goes with its parent" and "SM.SemanticModel" not in acts,
       acts)
order = list(ns["PLAN_DF"][ns["PLAN_DF"].Action == "delete"].Type)
expect(order.index("DataAgent") < order.index("Notebook") < order.index("Warehouse") < order.index("Lakehouse"), order)
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "000000000000"')
expect(isinstance(err, RuntimeError) and len(w.ws[W1]["items"]) == 10, f"wrong id refused {err}")
w.add_item(W1, "Notebook", "NEW", "late")
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect("created after the snapshot" in out and set(ns["PLAN_DF"].Action) == {"blocked"}, "new item blocks")

print("8 delete confirmed: Git disconnected first, Fabric items gone, Power BI kept")
w = World(); sid, _, _ = snapshot(w)
ns, _, _ = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
expect(err is None, f"no error {out[-2000:] if err else ''}")
expect(sorted(i["type"] for i in w.ws[W1]["items"]) == ["Report", "SemanticModel"], [i["type"] for i in w.ws[W1]["items"]])
expect(not getattr(w, "deleted_while_connected", False) and w.ws[W1]["git"]["state"] == "NotConnected",
       "disconnected before any delete")
expect(len(w.commits[w.refs[[b for b in w.refs if b.startswith("migration-snapshot")][0]]]) == 18,
       "snapshot branch still has all 9 items")
expect(w.delete_modes and set(w.delete_modes) == {"true"} and "hard-deleted" in out,
       f"items hard-deleted (ATM blocks on soft-deleted items) {getattr(w, 'delete_modes', None)}")
w = World(); sid, _, _ = snapshot(w)
ns, _, _ = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
ns2, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nHARD_DELETE = False\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
expect(isinstance(err, RuntimeError) and len(w.ws[W1]["items"]) == 10, "switching to soft delete needs a new DELETE ID")

def migrated(new_ws=None):
    w = World(); sid, _, _ = snapshot(w)
    ns, _, _ = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
    go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
    old = {i["displayName"] + "." + i["type"]: i["id"] for i in json.loads(
        w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])["workspaces"][0]["items"]}
    w.migrate(new_ws)
    return w, sid, old

print("9 restore dry run after the move")
w, sid, old = migrated()
n0 = len(w.calls)
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and not writes(type("x", (), {"calls": w.calls[n0:]})), f"dry run, no writes {out[-1500:] if err else ''}")
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action))
expect(acts["NB.Notebook"] == "restore from Git" and acts["SM.SemanticModel"].startswith("left out"), acts)
pid = ns["PLAN_ID"]

print("10 restore confirmed: restore branch without Power BI folders, add-only update, references reported")
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{pid}"')
expect(err is None, f"no error {out[-2500:] if err else ''}")
names = sorted(f"{i['displayName']}.{i['type']}" for i in w.ws[W1]["items"])
expect(names == sorted(["LH.Lakehouse", "LH.SQLEndpoint", "WH.Warehouse", "EH.Eventhouse", "EH.KQLDatabase",
                        "NB.Notebook", "PL.DataPipeline", "DA.DataAgent", "SM.SemanticModel", "RP.Report"]), names)
expect(w.last_update["options"] == {"allowOverrideItems": False} and "conflictResolution" not in w.last_update,
       "update can't overwrite")
rb = [b for b in w.refs if b.startswith("migration-restore")][0]
expect(not any("SemanticModel" in p for p in w.commits[w.refs[rb]]) and rb in w.locked, "restore branch locked, PBI left out")
expect(set(ns["RESULTS"][i]["Status"] for i in range(len(ns["RESULTS"])) if ns["RESULTS"][i]["Step"].startswith("Verify"))
       == {"OK"}, "verify OK")
ref = ns["REF_DF"]
expect(set(ref.Item) == {"NB", "SM"} and set(ref[ref.Item == "SM"].Fix) == {"manual"}, ref[["Item", "Fix"]].values.tolist())
nb_new = next(i["id"] for i in w.ws[W1]["items"] if i["type"] == "Notebook")
lh_new = next(i["id"] for i in w.ws[W1]["items"] if i["type"] == "Lakehouse")
expect(old["LH.Lakehouse"] in w.defs[nb_new], "nothing fixed without confirmation")
fid = ns["FIX_ID"]
expect("V2_6_Restore_Data" in out and f'SNAPSHOT_ID = "{sid}"' in out and "restore_state.json" in out, "data step settings printed")

print("11 fix references with confirmation; semantic model untouched")
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nFIX_STALE_REFERENCES = True\nCONFIRM_FIX_ID = "{fid}"')
expect(err is None, f"no error {out[-2500:] if err else ''}")
expect(lh_new in w.defs[nb_new] and old["LH.Lakehouse"] not in w.defs[nb_new], w.defs[nb_new])
sm = next(i["id"] for i in w.ws[W1]["items"] if i["type"] == "SemanticModel")
expect(w.update_bodies == [nb_new], f"only the notebook updated {w.update_bodies}")
expect("nothing left to restore" in out and not [c for c in w.calls if c[2].endswith("/git/connect")][1:], "no second restore")

print("12 restore into a new workspace ID with an existing Fabric item -> blocked; tampered branch -> blocked")
w, sid, old = migrated(W2)
w.add_item(W2, "Notebook", "NB", "someone made this")
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nWORKSPACE_MAP = {{"{W1}": "{W2}"}}')
expect(err is None and "already exists in the target" in out and not ns["TODO"], "existing item blocks")
w, sid, old = migrated()
sb = [b for b in w.refs if b.startswith("migration-snapshot")][0]
w.refs[sb] = w.commit({}, None)
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
expect("no longer at" in out and not ns["TODO"], "moved snapshot branch blocks")

print("13 manifest edited after the snapshot -> refused")
w, sid, old = migrated()
p = f"fabric-backups/_migration/{sid}/manifest.json"
w.adls[p] = w.adls[p].replace(b'"ready": true', b'"ready": true ')
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
expect(isinstance(err, RuntimeError) and "hash" in str(err), err)

print("14 new workspace ID; update fails once, the rerun resumes")
w, sid, old = migrated(W2)
w.fail_update = 1
cfg = f'SNAPSHOT_ID = "{sid}"\nWORKSPACE_MAP = {{"{W1}": "{W2}"}}'
ns, _, _ = go(w, RES, cfg)
ns, err, out = go(w, RES, cfg + f'\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
expect(err is None and "stopped" in out and len(w.ws[W2]["items"]) == 2, "first run stops cleanly")
ns, err, out = go(w, RES, cfg)
expect(err is None and set(ns["PLAN_DF"].Action) >= {"restore from Git"} and ns["TODO"], f"replanned {err}")
ns, err, out = go(w, RES, cfg + f'\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
expect(err is None and len(w.ws[W2]["items"]) == 10, f"resumed: {len(w.ws[W2]['items'])} items {out[-1500:] if err else ''}")
expect(len([c for c in w.calls if c[2].endswith("/git/connect")]) == 1 and
       len([b for b in w.refs if b.startswith("migration-restore")]) == 1, "no second connect or branch")
expect(ns["ID_MAP"].get(W1) == W2 and f"Sales: {W1} -> {W2}" in out, "workspace mapped")
nb_new = next(i["id"] for i in w.ws[W2]["items"] if i["type"] == "Notebook")
expect(set(ns["REF_DF"][ns["REF_DF"].ItemId == nb_new]["Old ID"]) == {W1, old["LH.Lakehouse"]}, "old ws + lakehouse IDs found")

print("15 raw copy: backup layout + file copy of the commit (V2_5 reads it: scenarios 43-45)")
w = World(); sid, ns, out = snapshot(w)
raw = f"fabric-backups/_migration/{sid}/raw"
expect(f"{raw}/_run_completed.json" in w.adls and f"{raw}/backup_log.csv" in w.adls, "raw run markers")
kq = json.loads(w.adls[f"{raw}/Sales__{W1}/EH.KQLDatabase__{w.KQ}/item.json"])
lh = json.loads(w.adls[f"{raw}/Sales__{W1}/LH.Lakehouse__{w.LH}/item.json"])
expect(kq["properties"] == {"parentEventhouseItemId": w.EH} and lh["properties"]["sqlEndpointProperties"]["id"] == w.ep(w.LH),
       "item.json properties kept with the raw copy")
expect(w.adls[f"{raw}/Sales__{W1}/NB.Notebook__{w.NB}/definition/content.txt"].decode() == w.defs[w.NB], "raw part bytes")
head = ns["WS"][W1]["head"]
mirror = [p for p in w.adls if p.startswith(f"fabric-backups/_migration/{sid}/git/Sales__{W1}/files/")]
expect(len(mirror) == len(w.commits[head]), f"git mirror {len(mirror)} files")
m = json.loads(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])
expect(m["rawCopy"]["definitionsRunId"] == f"_migration/{sid}/raw" and all(i.get("rawCopy") == "OK" for i in
       m["workspaces"][0]["items"] if i["type"] != "SQLEndpoint"), "manifest records the raw copy")

print("16 raw copy fails for an in-scope item -> workspace blocked, no snapshot branch")
w = World(); w.no_definition = {"DataAgent"}
sid, ns, out = snapshot(w)
expect(not ns["WS"][W1]["ready"] and not any(b.startswith("migration-snapshot") for b in w.refs), ns["WS"][W1]["blockers"])
g = ns["GATE_DF"].set_index(["Item", "Type"])
expect(g.loc[("DA", "DataAgent"), "Status"] == "BLOCKED" and g.loc[("DA", "DataAgent"), "Raw copy"].startswith("Failed"),
       "gate row updated")

print("17 delete refuses when the raw copy disappeared")
w = World(); sid, _, _ = snapshot(w)
del w.adls[f"fabric-backups/_migration/{sid}/raw/_run_completed.json"]
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect("raw copy" in out and set(ns["PLAN_DF"].Action) == {"blocked"}, "blocked")

print("18 permission check")
w = World(); n0 = len(w.calls)
ns, err, out = go(w, PERM, 'GIT_LINK_AUTH = "connection_id"\nGIT_CONNECTION_ID = "c1"')
r = ns["CHECK_DF"] if ns else None
expect(err is None and set(r.Result) <= {"PASS", "INFO"}, f"all pass {out[-2000:]}")
expect(not any(b.startswith("permission-check") for b in w.refs) and not any("_permission_check" in p for p in w.adls),
       "test branch and file cleaned up")
expect(len(r) >= 20, f"{len(r)} checks")
d = set(r[r.Area.str.startswith("Data restore")].Check)
expect({"write files into a Lakehouse (OneLake)", "read a KQL database", "Eventhouse reads the backup with your identity",
        "Warehouse reads the backup with your identity", "SQL execution through Fabric connector (Warehouse)",
        "SQL execution through Fabric connector (SQLEndpoint)"} <= d, d)
expect(not any(k.endswith(".txt") and "_permission_check_" in k for k in w.onelake), "OneLake probe removed")
w = World(); w.is_admin = False; w.cred_source = "None"; w.broken_creds = True
ns, err, out = go(w, PERM, "WRITE_TESTS = False")
f = set(ns["CHECK_DF"][ns["CHECK_DF"].Result == "FAIL"].Check)
expect({"Fabric administrator (admin APIs)", "Git status (Contributor + your Git sign-in work)"} <= f, f)

print("19 connect workspaces: dry run, confirm, first commit into an empty folder")
w = World(); W3 = G(77); w.add_ws(W3, "Ops"); w.add_item(W3, "Notebook", "ON", "ops nb"); w.add_item(W3, "Lakehouse", "OL", "x")
cfg = f'WORKSPACE_IDS = ["{W1}", "{W3}"]\nADO_BRANCH = "fabric-workspaces"'
n0 = len(w.calls)
ns, err, out = go(w, CONN, cfg)
acts = dict(zip(ns["CHECK_DF"].Workspace, ns["CHECK_DF"].Action))
expect(err is None and acts == {"Sales": "already connected (left as is)", "Ops": "create Git folder, connect + first commit"}, acts)
expect(not writes(type("x", (), {"calls": w.calls[n0:]})), "dry run: no writes")
ns, err, out = go(w, CONN, cfg + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
expect(err is None and w.ws[W3]["git"]["state"] == "ConnectedAndInitialized" and w.ws[W3]["git"]["dir"] == "/workspaces/Ops",
       f"connected {out[-1500:] if err else ''}")
expect(w.init_bodies[-1] == {"initializationStrategy": "PreferWorkspace"} and "fabric-workspaces" in w.refs, "PreferWorkspace, branch created")
v = ns["VERIFY_DF"].set_index("Workspace")
expect(v.loc["Ops", "Items in Git"] == 2 and v.loc["Ops", "Uncommitted changes"] == 0 and not v.loc["Ops", "Not in Git"],
       v.to_dict())
expect(w.ws[W1]["git"]["branch"] == "main", "already connected workspace untouched")
W4 = G(78); w.add_ws(W4, "Ops"); w.add_item(W4, "Notebook", "X", "y")
ns, err, out = go(w, CONN, f'WORKSPACE_IDS = ["{W4}"]\nADO_BRANCH = "fabric-workspaces"\nDIRECTORY_MAP = {{"{W4}": "/workspaces/Ops"}}')
expect("isn't empty" in ns["CHECK_DF"].Detail.iloc[0] and ns["CONNECT_ID"] and not ns["PLAN"], "non-empty folder blocked")

DATA = "V2_6_Restore_Data.ipynb"

def restored(new_ws=None):
    w, sid, old = migrated(new_ws)
    cfg = f'SNAPSHOT_ID = "{sid}"' + (f'\nWORKSPACE_MAP = {{"{W1}": "{new_ws}"}}' if new_ws else "")
    ns, _, _ = go(w, RES, cfg)
    ns, err, out = go(w, RES, cfg + f'\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
    assert err is None, out[-2000:]
    return w, sid, cfg

def new_id(w, ws, t):
    return next(i["id"] for i in w.ws[ws]["items"] if i["type"] == t)

print("20 data restore blocked before step 5; dry run after step 5 writes nothing")
w, sid, old = migrated()
ns, err, out = go(w, DATA, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and set(ns["PLAN_DF"].Action) == {"BLOCKED"} and "step 5" in ns["PLAN_DF"].Detail.iloc[0],
       f"blocked before step 5 {out[-1500:] if err else ''}")
w, sid, cfg = restored()
n0 = len(w.calls)
ns, err, out = go(w, DATA, cfg)
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action)) if ns else {}
expect(err is None and acts == {"LH.Lakehouse": "load", "WH.Warehouse": "COPY INTO (script)", "EH.KQLDatabase": "ingest",
                                "LH.SQLEndpoint": "SQL objects (script)"}, f"{acts} {out[-1500:] if err else ''}")
expect(not [c for c in w.calls[n0:] if c[0] in ("PUT", "PATCH", "DELETE")] and not any(
       x.startswith(".ingest") for x in w.kusto_log), "dry run: no writes")

print("20b SQL execution uses Fabric's connector, not nonexistent SQL endpoint connection properties")
ep = new_id(w, W1, "SQLEndpoint")
before = (dict(w.wh_rows), dict(w.sql_objects))
try:
    ns["sql_execute"](W1, ep, "SQLEndpoint", ["SELECT 1"])
    expect((w.wh_rows, w.sql_objects) == before, "SQL endpoint execution probe is read-only")
except Exception as exc:
    expect(False, f"SQL endpoint execution probe failed: {exc}")

print("21 data restore confirmed (run modes): files, Delta logs last, KQL rows, Warehouse rows, view; all verified")
runcfg = cfg + '\nWAREHOUSE_LOAD = "run"\nSQL_OBJECTS = "run"'
ns, _, _ = go(w, DATA, runcfg)
did = ns["DATA_ID"]
ns, err, out = go(w, DATA, runcfg + '\nDRY_RUN = False\nCONFIRM_DATA_ID = "000000000000"')
expect(isinstance(err, RuntimeError) and not w.onelake, "wrong DATA ID refused")
ns, err, out = go(w, DATA, runcfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{did}"')
expect(err is None, f"no error {out[-2500:] if err else ''}")
lh, wh, kq, ep = (new_id(w, W1, t) for t in ("Lakehouse", "Warehouse", "KQLDatabase", "SQLEndpoint"))
got = {k.split("/", 2)[2]: v for k, v in w.onelake.items() if k.startswith(f"{W1}/{lh}/")}
expect(got == DATA_FILES["Lakehouse"], f"Lakehouse files identical {sorted(got)}")
order = [p for p in w.onelake_order if p.startswith(f"{W1}/{lh}/")]
expect(order[-1].endswith(".json") and "_delta_log" in order[-1], "Delta log written last")
expect(w.kusto[kq]["Events"] == 12 and w.wh_rows[(wh, "dbo.orders")] == 7 and "dbo/v_sales" in w.sql_objects[ep],
       f"KQL {w.kusto.get(kq)}, WH {w.wh_rows}, SQL {w.sql_objects}")
v = {r["Item"] + "." + r["Type"]: r["Status"] for r in ns["RESULTS"] if r["Step"] == "Verify"}
expect(v == {"LH.Lakehouse": "OK", "EH.KQLDatabase": "OK", "WH.Warehouse": "OK"}, v)
expect("ALL DATA VERIFIED" in out and any(k.startswith(f"fabric-backups/_migration/{sid}/data_log_") for k in w.adls),
       "verified and logged")

print("22 rerun: nothing loaded twice")
n_ingest = sum(x.startswith(".ingest") for x in w.kusto_log); n_put = len(w.onelake_order)
ns, _, _ = go(w, DATA, runcfg)
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action))
expect(acts["LH.Lakehouse"] == "nothing to load" and acts["EH.KQLDatabase"] == "nothing to load", acts)
ns, err, out = go(w, DATA, runcfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
expect(err is None and sum(x.startswith(".ingest") for x in w.kusto_log) == n_ingest and len(w.onelake_order) == n_put
       and w.wh_rows[(wh, "dbo.orders")] == 7 and len(w.sql_objects[ep]) == 1, f"no second load {out[-1500:] if err else ''}")

print("22b Warehouse with only column-mapped tables: manual work cannot disappear or report ALL DATA VERIFIED")
delta_path = "Tables/dbo/orders/_delta_log/00000000000000000000.json"
actions = [json.loads(line) for line in DATA_FILES["Warehouse"][delta_path].splitlines()]
next(a["metaData"] for a in actions if "metaData" in a)["configuration"]["delta.columnMapping.mode"] = "id"
mapped_log = ("\n".join(json.dumps(a) for a in actions) + "\n").encode()
with patch.dict(DATA_FILES["Warehouse"], {delta_path: mapped_log}):
    w, sid, cfg = restored()
settings = cfg + '\nSQL_OBJECTS = "run"'
ns, err, out = go(w, DATA, settings)
wh_plan = ns["PLAN_DF"][ns["PLAN_DF"].Type == "Warehouse"].iloc[0]
expect(err is None and wh_plan.Action == "manual load" and "column mapping" in wh_plan.Detail,
       "a manual-only Warehouse remains actionable")
ns, err, out = go(w, DATA, settings + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
wh_attention = [r for r in ns["RESULTS"] if r["Type"] == "Warehouse" and r["Status"] not in ("OK", "Info")]
expect(err is None and wh_attention and "ALL DATA VERIFIED" not in out,
       out[-2000:] if err else f"manual Warehouse work prevents a false success: {wh_attention}")
expect(not w.wh_rows.get((new_id(w, W1, "Warehouse"), "dbo.orders")), "unsupported mapped data was not loaded unsafely")

def mapped_warehouse(change=None):
    root = "Tables/dbo/orders/"
    fields = [{"name": n, "type": t, "nullable": True,
               "metadata": {"delta.columnMapping.id": j + 1, "delta.columnMapping.physicalName": p}}
              for j, (n, t, p) in enumerate((("OrderId", "integer", "c_order_id"),
                                              ("Customer", "string", "c_customer"),
                                              ("Amount", "decimal(10,2)", "c_amount"),
                                              ("OrderDate", "date", "c_date")))]
    meta = {"configuration": {"delta.columnMapping.mode": "name"}, "partitionColumns": [],
            "schemaString": json.dumps({"type": "struct", "fields": fields})}
    latest = json.loads(json.dumps(meta))
    schema = json.loads(latest["schemaString"])
    schema["fields"][1]["name"] = "Customer] label"
    latest["schemaString"] = json.dumps(schema)
    stats = lambda n, nulls: json.dumps({"numRecords": n, "nullCount": {
        "c_order_id": 0, "c_customer": nulls, "c_amount": 0, "c_date": 0}})
    commits = [[{"metaData": meta},
                {"add": {"path": "part-0.parquet", "stats": stats(4, 1)}},
                {"add": {"path": "retired.parquet", "stats": stats(2, 0)}}],
               [{"metaData": latest}, {"remove": {"path": "retired.parquet"}},
                {"add": {"path": "part-1.parquet", "stats": stats(3, 0)}}]]
    if change:
        change(commits)
    files = {root + p: b"parquet" for p in ("part-0.parquet", "part-1.parquet", "retired.parquet")}
    files.update({root + f"_delta_log/{j:020d}.json": ("\n".join(json.dumps(a) for a in c) + "\n").encode()
                  for j, c in enumerate(commits)})
    with patch.dict(DATA_FILES["Warehouse"], files, clear=True):
        w, sid, cfg = restored()
    folder = f"fabric-backups/{RUN}/Sales__{W1}/WH.Warehouse__{w.WH}/data/{root}"
    data = [{"c_order_id": j, "c_customer": None if j == 2 else f"customer-{j}",
             "c_amount": j * 3.75, "c_date": f"2026-01-{j:02d}"} for j in range(1, 8)]
    w.parquet_rows = {folder + "part-0.parquet": data[:4], folder + "part-1.parquet": data[4:],
                      folder + "retired.parquet": data[:2]}
    return w, sid, cfg + '\nWAREHOUSE_LOAD = "run"\nSQL_OBJECTS = "run"', data


print("22c mapped Warehouse: latest logical names, physical bindings, active files, values and resumability")
w, sid, cfg, source_rows = mapped_warehouse()
wh = new_id(w, W1, "Warehouse")
ns, err, out = go(w, DATA, cfg)
expect(err is None and not w.sql_batches, "mapped Warehouse dry run writes no SQL data")
ns, err, out = go(w, DATA, cfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
expected = [{"OrderId": r["c_order_id"], "Customer] label": r["c_customer"],
             "Amount": r["c_amount"], "OrderDate": r["c_date"]} for r in source_rows]
expect(err is None and w.wh_data.get((wh, "dbo.orders")) == expected,
       out[-2500:] if err else "physical fields restored to the latest logical columns with all seven values intact")
wh_sql = [b for i, b in w.sql_batches if i == wh]
expect(wh_sql and all("retired.parquet" not in b and "COPY INTO" not in b for b in wh_sql)
       and "UNION ALL" in wh_sql[0] and "[c_amount] decimal(10,2)" in wh_sql[0],
       "only active files are read through typed physical names, not an unsafe COPY or wildcard")
expect("ALL DATA VERIFIED" in out and any(r["Type"] == "Warehouse" and r["Step"] == "Verify" and
       r["Status"] == "OK" for r in ns["RESULTS"]), "mapped row and null counts verified")
n_sql = len(w.sql_batches)
ns, _, _ = go(w, DATA, cfg)
ns, err, out = go(w, DATA, cfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
expect(err is None and len(w.sql_batches) == n_sql and w.wh_data.get((wh, "dbo.orders")) == expected,
       "a mapped-table rerun does not append or execute SQL twice")

print("22d mapped Warehouse safety: nonempty targets, changed mappings and NULL binding failures")
w, sid, cfg, _ = mapped_warehouse()
wh = new_id(w, W1, "Warehouse")
w.wh_data[(wh, "dbo.orders")], w.wh_rows[(wh, "dbo.orders")] = [{"OrderId": 99}], 1
ns, _, _ = go(w, DATA, cfg)
ns, err, out = go(w, DATA, cfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
expect(err is None and w.wh_data[(wh, "dbo.orders")] == [{"OrderId": 99}]
       and not [b for i, b in w.sql_batches if i == wh], "nonempty mapped target is never overwritten or appended")
w, sid, cfg, _ = mapped_warehouse()
ns, _, _ = go(w, DATA, cfg)
old_id = ns["DATA_ID"]
key = next(k for k in w.adls if k.startswith(f"fabric-backups/{RUN}/") and "/WH.Warehouse__" in k
           and k.endswith("_delta_log/00000000000000000001.json"))
w.adls[key] = w.adls[key].replace(b"c_order_id", b"c_order_ix")
ns, err, out = go(w, DATA, cfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{old_id}"')
expect(isinstance(err, RuntimeError) and not w.sql_batches, "a same-size physical mapping change invalidates the DATA ID")
w, sid, cfg, _ = mapped_warehouse()
for rows_ in w.parquet_rows.values():
    for r in rows_:
        r["c_customer"] = None
ns, _, _ = go(w, DATA, cfg)
ns, err, out = go(w, DATA, cfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
expect(err is None and "ALL DATA VERIFIED" not in out and any(
       r["Type"] == "Warehouse" and r["Step"] == "Verify" and r["Status"] == "Failed" for r in ns["RESULTS"]),
       "correct row count with wrong NULL bindings is not a successful restore")

print("22e unsafe mapped layouts remain manual: missing physical name, deletion vectors, partitions, invalid statistics")
def bad_physical(commits):
    meta = commits[1][0]["metaData"]
    schema = json.loads(meta["schemaString"])
    schema["fields"][0]["metadata"].pop("delta.columnMapping.physicalName")
    meta["schemaString"] = json.dumps(schema)

def bad_null_count(commits):
    add = commits[1][-1]["add"]
    stats = json.loads(add["stats"])
    stats["nullCount"]["c_customer"] = -1
    add["stats"] = json.dumps(stats)

for change in (bad_physical,
               lambda c: c[1][-1]["add"].update(deletionVector={"storageType": "u", "pathOrInlineDv": "dv"}),
               lambda c: c[1][0]["metaData"].update(partitionColumns=["OrderId"]),
               bad_null_count):
    w, sid, cfg, _ = mapped_warehouse(change)
    ns, err, out = go(w, DATA, cfg)
    row = ns["PLAN_DF"][ns["PLAN_DF"].Type == "Warehouse"].iloc[0]
    expect(err is None and row.Action == "manual load" and not w.sql_batches, row.Detail)

print("22f mapped Delta checkpoint: recover schema metadata as well as active files")
w, sid, cfg, _ = mapped_warehouse()
folder = f"fabric-backups/{RUN}/Sales__{W1}/WH.Warehouse__{w.WH}"
log_prefix = folder + "/data/Tables/dbo/orders/_delta_log/"
commits = [json.loads(line) for k, body in sorted(w.adls.items()) if k.startswith(log_prefix)
           for line in body.splitlines()]
meta = next(a["metaData"] for a in reversed(commits) if "metaData" in a)
meta["configuration"] = list(meta["configuration"].items())
adds = [a for a in commits if a.get("add", {}).get("path") in ("part-0.parquet", "part-1.parquet")]
for k in [k for k in w.adls if k.startswith(log_prefix)]:
    del w.adls[k]
w.adls[log_prefix + "_last_checkpoint"] = b'{"version":1}'
w.adls[log_prefix + "00000000000000000001.checkpoint.parquet"] = b"checkpoint"
manifest = [{"path": k[len(folder + "/data/"):], "size": len(v), "lastModified": "", "status": "copied"}
            for k, v in w.adls.items() if k.startswith(folder + "/data/")]
w.adls[folder + "/data_manifest.csv"] = pd.DataFrame(manifest).to_csv(index=False).encode()
arrow = types.ModuleType("pyarrow")
pq = types.ModuleType("pyarrow.parquet")
pq.read_table = lambda stream: types.SimpleNamespace(to_pylist=lambda: [{"metaData": meta}, *adds])
arrow.parquet = pq
with patch.dict(sys.modules, {"pyarrow": arrow, "pyarrow.parquet": pq}):
    ns, err, out = go(w, DATA, cfg)
wh_item = next(i for i in ns["ITEMS"] if i["type"] == "Warehouse") if ns else {}
res = wh_item.get("tables", {}).get("Tables/dbo/orders", {})
expect(err is None and len(res.get("columns", [])) == 4 and res.get("rows") == 7 and not wh_item.get("manual"),
       out[-2000:] if err else "checkpoint metadata preserves physical bindings and row-count verification")

print("23 data already in the target is never touched; KQL update policy paused and put back")
w, sid, cfg = restored()
lh, kq = new_id(w, W1, "Lakehouse"), new_id(w, W1, "KQLDatabase")
w.onelake[f"{W1}/{lh}/Tables/sales/_delta_log/00000000000000000000.json"] = b"theirs"
w.onelake[f"{W1}/{lh}/Files/raw/a.csv"] = b"different"
w.kusto[kq] = {"Events": 3}
ns, _, _ = go(w, DATA, cfg)
d = dict(zip(ns["PLAN_DF"].Type, ns["PLAN_DF"].Detail))
expect("left alone: Tables/sales" in d["Lakehouse"] and "different content" in d["Lakehouse"] and
       "left alone: Events" in d["KQLDatabase"], d)
ns, err, out = go(w, DATA, cfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
expect(w.onelake[f"{W1}/{lh}/Files/raw/a.csv"] == b"different" and
       w.onelake[f"{W1}/{lh}/Tables/sales/_delta_log/00000000000000000000.json"] == b"theirs" and
       w.kusto[kq]["Events"] == 3, "existing data untouched")
w2, sid2, cfg2 = restored()
kq2 = new_id(w2, W1, "KQLDatabase")
w2.policies[kq2] = {"Events": '[{"Source":"Raw"}]'}
ns, _, _ = go(w2, DATA, cfg2)
go(w2, DATA, cfg2 + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
log_ = w2.kusto_log
i_del = next(i for i, x in enumerate(log_) if x.startswith(".delete table"))
i_ing = next(i for i, x in enumerate(log_) if x.startswith(".ingest"))
i_alt = next(i for i, x in enumerate(log_) if x.startswith(".alter table"))
expect(i_del < i_ing < i_alt and w2.kusto[kq2]["Events"] == 12, "policy paused during ingest, then restored")

print("24 backup damaged: data restore and delete both refuse")
w, sid, cfg = restored()
lhf = next(k for k in w.adls if k.startswith(f"fabric-backups/{RUN}/") and k.endswith("Tables/sales/part-0.parquet"))
w.adls[lhf] = b"short"
ns, err, out = go(w, DATA, cfg)
d = dict(zip(ns["PLAN_DF"].Type, zip(ns["PLAN_DF"].Action, ns["PLAN_DF"].Detail)))
expect(d["Lakehouse"][0] == "BLOCKED" and "data_manifest.csv" in d["Lakehouse"][1], d["Lakehouse"])
w = World(); sid, _, _ = snapshot(w)
del w.adls[next(k for k in w.adls if k.startswith(f"fabric-backups/{RUN}/") and "WH.Warehouse" in k
                and k.endswith("data_manifest.csv"))]
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect("WH (Warehouse): data_manifest.csv is missing" in out and set(ns["PLAN_DF"].Action) == {"blocked"}, "delete blocked")

print("25 workspace with a new ID after the move")
w, sid, cfg = restored(G(55))
ns, _, _ = go(w, DATA, cfg)
ns, err, out = go(w, DATA, cfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
lh = new_id(w, G(55), "Lakehouse")
expect(err is None and len([k for k in w.onelake if k.startswith(f"{G(55)}/{lh}/")]) == 3, f"loaded into the new workspace {out[-1500:] if err else ''}")
v = {r["Type"]: r["Status"] for r in ns["RESULTS"] if r["Step"] == "Verify"}
expect(v["Warehouse"] == "Manual step", v)
wh = new_id(w, G(55), "Warehouse"); w.wh_rows[(wh, "dbo.orders")] = 7          # the script was run by hand
ns, _, _ = go(w, DATA, cfg)
ns, err, out = go(w, DATA, cfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
v = {r["Type"]: r["Status"] for r in ns["RESULTS"] if r["Step"] == "Verify"}
expect(v.get("Warehouse") == "OK", f"script run by hand is then verified {v}")

print("26 DevOps sign-in page (HTTP 203): token rejected -> diagnose rows; spaces in the secret and URL org accepted")
w = World(); w.bad_pat = True
ns, err, out = go(w, PERM, "WRITE_TESTS = False")
r = ns["CHECK_DF"].set_index("Check")
expect(r.loc["read repository", "Result"] == "FAIL" and "sign-in page" in r.loc["read repository", "Detail"], r.loc["read repository"].to_dict())
expect(r.loc["diagnose: token format", "Result"] == "PASS" and
       r.loc["diagnose: organization accepts the token", "Result"] == "FAIL" and
       "expired" in r.loc["diagnose: organization accepts the token", "How to fix"], "organization rejects the token")
w = World(); w.bad_pat = True; w.pat = "abc" + "8Q~" + "x" * 34          # 40-character Entra client secret
ns, err, out = go(w, PERM, "WRITE_TESTS = False")
r = ns["CHECK_DF"].set_index("Check")
expect(r.loc["diagnose: token format", "Result"] == "FAIL" and "client secret" in r.loc["diagnose: token format", "Detail"]
       and "Personal access tokens" in r.loc["diagnose: token format", "How to fix"], r.loc["diagnose: token format"].to_dict())
expect("client secret" in r.loc["read repository", "Detail"] and "8Q~x" not in out and "8Q~x" not in r.to_string(),
       "error names the cause; the secret is never shown")
expect("diagnose: organization accepts the token" not in r.index, "stops after the token row")
w = World(); w.pat = "  " + "p" * 52 + "\n"
ns, err, out = go(w, PERM, 'WRITE_TESTS = False\nADO_ORGANIZATION = "https://dev.azure.com/org/"')
r = ns["CHECK_DF"].set_index("Check")
expect(r.loc["read repository", "Result"] == "PASS", "whitespace stripped, organization URL accepted")
hdr = w.calls  # header not recorded; check the stored token length instead
expect(ns["_ado_auth"]["pat_length"] == 52, ns["_ado_auth"]["pat_length"])
w = World()
ns, err, out = go(w, PERM, 'WRITE_TESTS = False\nADO_REPOSITORY = "Repo2"')
r = ns["CHECK_DF"].set_index("Check")
expect(r.loc["diagnose: repository", "Result"] == "FAIL" and "other, repo" in r.loc["diagnose: repository", "Detail"],
       "wrong repository named")

print("27 linking: role check, add me as Admin, credentials error with fix + manual values, resume, service principal")
def fresh():
    w = World(); W5 = G(90); w.add_ws(W5, "Ops"); w.add_item(W5, "Notebook", "ON", "ops nb")
    return w, W5, f'WORKSPACE_IDS = ["{W5}"]\nADO_BRANCH = "fabric-workspaces"'
w, W5, cfg = fresh(); w.roles[W5] = {"me-oid": "Contributor"}
ns, err, out = go(w, CONN, cfg)
row = ns["CHECK_DF"].iloc[0]
expect(row.Action == "blocked" and "Contributor" in row.Detail and row["Your role"] == "Contributor", row.to_dict())
ns, err, out = go(w, CONN, cfg + "\nADD_ME_AS_WORKSPACE_ADMIN = True")
expect(ns["CHECK_DF"].iloc[0].Action == "add you as Admin, create Git folder, connect + first commit", ns["CHECK_DF"].iloc[0].Action)
ns, err, out = go(w, CONN, cfg + f'\nADD_ME_AS_WORKSPACE_ADMIN = True\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
expect(err is None and w.roles[W5]["me-oid"] == "Admin" and w.ws[W5]["git"]["state"] == "ConnectedAndInitialized",
       f"admin added, linked {out[-1500:] if err else ''}")
w, W5, cfg = fresh(); w.no_creds = True
ns, _, _ = go(w, CONN, cfg)
ns, err, out = go(w, CONN, cfg + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
fail = [r for r in ns["RESULTS"] if r["Step"] == "Connect"][0]
expect(fail["Status"] == "Failed" and "Source control once and sign in" in fail["Detail"] and
       "Workspace settings > Git integration" in out and "/workspaces/Ops" in out, fail["Detail"])
w.no_creds = False
w.ws[W5]["git"] = {"state": "Connected", "branch": "fabric-workspaces", "dir": "/workspaces/Ops", "head": None}
ns, _, _ = go(w, CONN, cfg)
expect(ns["CHECK_DF"].iloc[0].Action == "finish link (first commit)", ns["CHECK_DF"].iloc[0].Action)
ns, err, out = go(w, CONN, cfg + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
expect(err is None and w.ws[W5]["git"]["state"] == "ConnectedAndInitialized" and
       not [c for c in w.calls if c[2].endswith("/git/connect") and W5 in c[2]][1:], "resumed without a second connect")
w, W5, cfg = fresh(); w.claims = {"oid": "sp", "appid": "app-1", "idtyp": "app"}
ns, err, out = go(w, CONN, cfg)
expect(isinstance(err, RuntimeError) and 'GIT_LINK_AUTH = "service_principal"' in str(err), f"service principal refused {err}")

print("28 snapshot: same branch name for all workspaces + tag; folder changed after commit -> blocked")
w = World(); sid, ns, out = snapshot(w)
ws = ns["WS"][W1]
expect(ws["snapshotBranch"] == f"migration-snapshot/{sid}" and w.refs[ws["snapshotBranch"]] == ws["head"] and
       w.tags.get(f"migration-snapshot-{sid}") == ws["head"] and ws["snapshotBranch"] in w.locked, "shared branch + tag")
m = json.loads(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])
expect(m["workspaces"][0]["git"]["snapshotTag"] == f"migration-snapshot-{sid}", "tag in the manifest")
w = World(); w.move_main = True
sid, ns, out = snapshot(w)
expect(not ns["WS"][W1]["ready"] and "changed after" in " ".join(ns["WS"][W1]["blockers"]) and
       not any(b.startswith("migration-snapshot") for b in w.refs), ns["WS"][W1]["blockers"])
print("29 Azure DevOps sign-in with a service principal (client secret in Key Vault), PAT still the default")
SP = ('WRITE_TESTS = False\nADO_AUTH = "service_principal"\nADO_SP_TENANT_ID = "tenant-example"\n'
      'ADO_SP_CLIENT_ID = "app-1"\nADO_SP_SECRET_NAME = "sp-secret"')
w = World(); w.secrets = {"sp-secret": " abc8Q~" + "s" * 34 + "\n"}
ns, err, out = go(w, PERM, SP)
r = ns["CHECK_DF"].set_index("Check")
expect(r.loc["service principal sign-in", "Result"] == "PASS" and r.loc["read repository", "Result"] == "PASS",
       r.loc[["service principal sign-in", "read repository"]].to_dict())
expect("s" * 34 not in out and "s" * 34 not in r.to_string(), "client secret never shown")
w = World(); w.secrets = {"sp-secret": "wrong~" + "x" * 34}
ns, err, out = go(w, PERM, SP)
r = ns["CHECK_DF"].set_index("Check")
expect(r.loc["service principal sign-in", "Result"] == "FAIL" and "Secret ID" in r.loc["service principal sign-in", "How to fix"]
       and "read repository" not in r.index, r.loc["service principal sign-in"].to_dict())
w = World(); w.secrets = {"sp-secret": "abc8Q~" + "s" * 34}; w.sp_not_in_org = True
ns, err, out = go(w, PERM, SP)
r = ns["CHECK_DF"].set_index("Check")
expect(r.loc["read repository", "Result"] == "FAIL" and "Organization settings > Users" in r.loc["read repository", "Detail"]
       and "diagnose: token format" not in r.index
       and "Organization settings > Users" in r.loc["diagnose: organization accepts the token", "How to fix"],
       "service principal not in the organization")
ns, err, out = go(World(), PERM, 'WRITE_TESTS = False\nADO_AUTH = "service_principal"')
expect(err is not None and "ADO_SP_TENANT_ID" in str(err), f"missing service principal settings named: {err}")
# a whole snapshot with the service principal; the Entra token is renewed when it is close to expiry
w = World(); w.secrets = {"sp-secret": "abc8Q~" + "s" * 34}; w.sp_expires_in = 200
sid, ns, out = snapshot(w, SP.replace("WRITE_TESTS = False\n", ""))
expect(w.sp_logins > 1,
       f"snapshot ran with the service principal ({w.sp_logins} sign-ins)")


print("30 cross-geography Git: tenant setting reported by V2_0, connect error gives the admin fix")
w = World(); w.geo_setting = {"enabled": False, "delegateToWorkspace": False}
ns, err, out = go(w, PERM, "WRITE_TESTS = False")
r = ns["CHECK_DF"].set_index("Check")
geo = [i for i in r.index if "cross-geography" in i][0]
expect(r.loc[geo, "Result"] == "WARN" and "Tenant settings > Git integration" in r.loc[geo, "How to fix"], r.loc[geo].to_dict())
expect(r.loc[[i for i in r.index if "synchronize" in i][0], "Result"] == "PASS", "sync setting on")
expect("UAE North" in r.loc["capacity assigned", "Detail"], r.loc["capacity assigned", "Detail"])
w = World(); w.geo_setting = {"enabled": True, "enabledSecurityGroups": [{"graphId": "g1", "name": "Fabric-Migration"}]}
ns, err, out = go(w, PERM, "WRITE_TESTS = False")
r = ns["CHECK_DF"].set_index("Check")
expect(r.loc[geo, "Result"] == "WARN" and "Fabric-Migration" in r.loc[geo, "Detail"], r.loc[geo].to_dict())
w, W5, cfg = fresh(); w.cross_geo = True
ns, _, _ = go(w, CONN, cfg)
ns, err, out = go(w, CONN, cfg + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
fail = [r for r in ns["RESULTS"] if r["Step"] == "Connect"][0]
expect(fail["Status"] == "Failed" and fail["Detail"].endswith(ns["CROSS_GEO_FIX"]) and ns["CROSS_GEO_FIX"] in out,
       "whole fix in the log and the printed output")
long = "x" * 1500 + " END"
e = ns["HttpError"](400, "Long", long, "u")
expect(e.short().endswith(" END") and str(e).endswith(" END"), "long error messages are never cut")
w.cross_geo = False
ns, _, _ = go(w, CONN, cfg)
ns, err, out = go(w, CONN, cfg + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
expect(err is None and w.ws[W5]["git"]["state"] == "ConnectedAndInitialized", "linked after the setting is on")


print("31 GitProviderResourceNotFound: specific fix; GIT_LINK_AUTH = service_principal creates a service principal connection and links")
w, W5, cfg = fresh(); w.fabric_cant_see = True
ns, _, _ = go(w, CONN, cfg)
ns, err, out = go(w, CONN, cfg + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
fail = [r for r in ns["RESULTS"] if r["Step"] == "Connect"][0]
expect(fail["Status"] == "Failed" and 'GIT_LINK_AUTH = "service_principal"' in fail["Detail"]
       and "no working Azure DevOps credentials" not in fail["Detail"], fail["Detail"])
SPC = ('\nGIT_LINK_AUTH = "service_principal"\nADO_SP_TENANT_ID = "tenant-example"\nADO_SP_CLIENT_ID = "app-1"'
       '\nADO_SP_SECRET_NAME = "sp-secret"')
w.secrets = {"sp-secret": "abc8Q~" + "s" * 34}
ns, err, out = go(w, CONN, cfg + SPC)
expect(err is None and "created when you confirm the plan" in out and not getattr(w, "connections", []), "dry run creates nothing")
ns, err, out = go(w, CONN, cfg + SPC + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
expect(err is None and w.ws[W5]["git"]["state"] == "ConnectedAndInitialized"
       and w.ws[W5]["git"]["creds"] == {"source": "ConfiguredConnection", "connectionId": "conn-new"}
       and 'GIT_CONNECTION_ID = "conn-new"' in out and "s" * 34 not in out, out[-2000:])
ns, err, out = go(w, CONN, cfg + SPC)
expect("existing Fabric connection fabric-migration-org-proj-repo (conn-new)" in out and len(w.connections) == 1, "second run reuses the connection")
w, W5, cfg = fresh()
ns, err, out = go(w, CONN, cfg + '\nGIT_LINK_AUTH = "service_principal"')
expect(err is not None and "ADO_SP_TENANT_ID" in str(err), str(err))


print("32 V2_1/V2_0 say who links and whether the DevOps organization is in Fabric's tenant")
w, W5, cfg = fresh()
ns, err, out = go(w, CONN, cfg)
expect("same Entra tenant as Fabric" in out and "your own Azure DevOps sign-in" in out and "This will fail" not in out
       and "branch fabric-workspaces" in out, out[:1500])
w, W5, cfg = fresh(); w.org_tenant = "t-other"
ns, err, out = go(w, CONN, cfg)
expect(err is not None and "Stopped" in str(err) and "Connect directory" in str(err)
       and not [c for c in w.calls if c[2].endswith("/git/connect")], f"other tenant stops before linking: {err}")
ns, err, out = go(w, CONN, cfg + "\nALLOW_CROSS_TENANT = True")
expect(err is None and "uses Entra tenant t-other" in out, "cross tenant only when explicitly allowed")
w, W5, cfg = fresh(); w.secrets = {"sp-secret": "abc8Q~" + "s" * 34}
ns, err, out = go(w, CONN, cfg + SPC.replace('ADO_SP_TENANT_ID = "tenant-example"', 'ADO_SP_TENANT_ID = "t-other"'))
expect(err is not None and "service principal's tenant" in str(err), f"service principal from another tenant: {err}")
w = World(); w.org_tenant = "00000000-0000-0000-0000-000000000000"
ns, err, out = go(w, PERM, "WRITE_TESTS = False")
r = ns["CHECK_DF"].set_index("Check")
row = [i for i in r.index if i.startswith("organization in the same Entra tenant")][0]
expect(r.loc[row, "Result"] == "FAIL" and "not connected to Entra" in r.loc[row, "Detail"]
       and "Connect directory" in r.loc[row, "How to fix"], r.loc[row].to_dict())


print("33 GIT_LINK_AUTH: service principal link in V2_0/V2_1/V2_5; tenant confirmed through the service principal")
w, W5, cfg = fresh(); w.org_tenant_hidden = True; w.secrets = {"sp-secret": "abc8Q~" + "s" * 34}
ns, err, out = go(w, CONN, cfg)
expect(err is None and "couldn't tell which Entra tenant" in out and "Organization settings > Microsoft Entra" in out, out[:1500])
ns, err, out = go(w, CONN, cfg + SPC)
expect(err is None and "service principal app-1 can read repository repo" in out
       and "yes, same Entra tenant as Fabric (tenant-example; the organization accepts service principal app-1" in out
       and 'GIT_LINK_AUTH = "service_principal"' in out, out[:2500])
ns, err, out = go(w, CONN, cfg + SPC + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
expect(err is None and w.ws[W5]["git"]["creds"] == {"source": "ConfiguredConnection", "connectionId": "conn-new"},
       f"linked with the service principal connection {out[-1500:]}")
ns, err, out = go(w, PERM, "WRITE_TESTS = False" + SPC)
r = ns["CHECK_DF"].set_index("Check")
expect(r.loc['Git link sign-in (GIT_LINK_AUTH = "service_principal")', "Result"] == "PASS"
       and r.loc["service principal app-1 can read the repository (Fabric links with it)", "Result"] == "PASS"
       and r.loc["organization in the same Entra tenant as Fabric", "Result"] == "PASS", r[["Result", "Detail"]].to_string())
w, W5, cfg = fresh(); w.secrets = {"sp-secret": "abc8Q~" + "s" * 34}; w.sp_not_in_org = True
ns, err, out = go(w, CONN, cfg + SPC)
expect(err is not None and "can't read repository" in str(err) and "Basic access" in str(err)
       and not getattr(w, "connections", []), f"service principal without access stops first: {err}")
ns, err, out = go(fresh()[0], CONN, cfg + '\nGIT_LINK_AUTH = "bogus"')
expect(isinstance(err, ValueError) and "my_account" in str(err), str(err))
ns, err, out = go(fresh()[0], CONN, cfg + '\nGIT_CONNECTION_ID = "c1"')
expect(isinstance(err, ValueError) and "connection_id" in str(err), f"ambiguous choice refused: {err}")
w, sid, old = migrated(); w.secrets = {"sp-secret": "abc8Q~" + "s" * 34}
RSP = f'SNAPSHOT_ID = "{sid}"' + SPC
ns, err, out = go(w, RES, RSP)
expect(err is None and "created when you confirm the plan" in out and not getattr(w, "connections", []), out[-1500:])
ns, err, out = go(w, RES, RSP + f'\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
expect(err is None and w.ws[W1]["git"]["creds"] == {"source": "ConfiguredConnection", "connectionId": "conn-new"},
       f"restore links with the service principal {out[-1500:]}")


print("34 the connect API needs the Git folder to exist: V2_1 creates it first; marker-only folder counts as empty")
w, W5, cfg = fresh(); w.secrets = {"sp-secret": "abc8Q~" + "s" * 34}
ns, err, out = go(w, CONN, cfg + SPC)
expect(ns["CHECK_DF"].iloc[0].Action == "create Git folder, connect + first commit", ns["CHECK_DF"].iloc[0].Action)
ns, err, out = go(w, CONN, cfg + SPC + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
files = w.commits[w.refs["fabric-workspaces"]]
expect(err is None and w.ws[W5]["git"]["state"] == "ConnectedAndInitialized"
       and "/workspaces/Ops/README.md" in files and any(p.endswith("/.platform") and p.startswith("/workspaces/Ops/")
                                                      for p in files), f"folder created, linked, committed {out[-1500:]}")
w2, W6, cfg2 = fresh(); w2.secrets = {"sp-secret": "abc8Q~" + "s" * 34}
ns, _, _ = go(w2, CONN, cfg2 + SPC)
go(w2, CONN, cfg2 + SPC + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
w2.ws[W6]["git"] = {"state": "NotConnected"}
head = w2.refs["fabric-workspaces"]
w2.commits[head] = {p: b for p, b in w2.commits[head].items() if not p.startswith("/workspaces/Ops/") or p.endswith("README.md")}
ns, err, out = go(w2, CONN, cfg2 + SPC)
expect(ns["CHECK_DF"].iloc[0].Action == "connect + first commit", f"marker-only folder is empty: {ns['CHECK_DF'].iloc[0].to_dict()}")

print("35 linked with the service principal, own Git sign-in Automatic -> V2_1 only reports it; switch is opt-in")
w, W5, cfg = fresh(); w.secrets = {"sp-secret": "abc8Q~" + "s" * 34}
ns, _, _ = go(w, CONN, cfg + SPC)
ns, err, out = go(w, CONN, cfg + SPC + f'\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
expect(not [c for c in w.calls if c[0] == "PATCH" and "myGitCredentials" in c[2]]
       and "fabric-migration-org-proj-repo" in out, "new link: no forced switch, connection name given")
w.ws[W5]["git"]["creds"] = {"source": "Automatic"}
ns, err, out = go(w, CONN, cfg + SPC)
row = ns["CHECK_DF"].iloc[0]
expect(row.Action == "already connected (left as is)" and "Can't fetch branches" in row.Detail and not ns["PLAN"]
       and "fabric-migration-org-proj-repo" in row.Detail, row.to_dict())
ns, err, out = go(w, CONN, cfg + SPC + "\nSWITCH_MY_GIT_CREDENTIALS = True")
row = ns["CHECK_DF"].iloc[0]
expect(row.Action == "already connected; switch your Git sign-in to the connection", row.to_dict())
ns, err, out = go(w, CONN, cfg + SPC + f'\nSWITCH_MY_GIT_CREDENTIALS = True\nDRY_RUN = False\nCONFIRM_CONNECT_ID = "{ns["CONNECT_ID"]}"')
expect(err is None and w.ws[W5]["git"]["creds"] == {"source": "ConfiguredConnection", "connectionId": "conn-new"}
       and w.ws[W5]["git"]["state"] == "ConnectedAndInitialized", f"opt-in switch, link untouched {out[-1200:]}")
ns, err, out = go(w, CONN, cfg + SPC)
expect(ns["CHECK_DF"].iloc[0].Action == "already connected (left as is)" and not ns["PLAN"]
       and not ns["CHECK_DF"].iloc[0].Detail, "nothing left to do")
ns, err, out = go(w, CONN, cfg + '\nGIT_LINK_AUTH = "my_account"')
expect(ns["CHECK_DF"].iloc[0].Action == "already connected (left as is)", "my_account never switches")

print("36 V2_4 refuses to run inside a workspace it would clear; V2_1 warns")
w = World(); sid, _, _ = snapshot(w); w.here_ws = W1
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect(isinstance(err, RuntimeError) and "separate workspace" in str(err) and "DELETE_ID" not in (ns or {}), str(err))
w, W5, cfg = fresh(); w.here_ws = W5
ns, err, out = go(w, CONN, cfg)
expect("runs inside a workspace it links" in out, out[:800])

print("37 own Git sign-in rejected: V2_0 flags it, V2_3/V2_4 block with the Source control fix, nothing changed")
w = World(); w.broken_creds = True
ns, err, out = go(w, PERM, "WRITE_TESTS = False")
r = ns["CHECK_DF"].set_index("Check")
g = r.loc["Git status (Contributor + your Git sign-in work)"]
expect(g["Result"] == "FAIL" and "Source control" in g["How to fix"] and "GitCredentialsUnauthorized" in g["Detail"], g.to_dict())
refs = dict(w.refs)
ns, err, out = go(w, SNAP, "DRY_RUN = False")
expect(not ns["WS"][W1].get("ready") and "Source control" in " ".join(ns["WS"][W1]["blockers"]), ns["WS"][W1]["blockers"])
expect(w.refs == refs and not [c for c in w.calls if "commitToGit" in c[2]], "nothing committed, no branch")
w2 = World(); sid, _, _ = snapshot(w2); w2.broken_creds = True
ns, err, out = go(w2, DEL, f'SNAPSHOT_ID = "{sid}"')
expect("Source control" in out and w2.ws[W1]["git"]["state"] == "ConnectedAndInitialized", out[-1500:] if err else "V2_4 blocked")
w.ws[W1]["git"]["creds"] = {"source": "ConfiguredConnection", "connectionId": "c1"}      # picked in Source control
ns, err, out = go(w, SNAP)
expect(ns["WS"][W1]["status"].startswith("READY"), ns["WS"][W1]["status"])

print("38 Eventhouse rows backed up to ADX (V2_2 KQL_BACKUP_MODE = adx): snapshot READY, delete allowed, V2_6 copies back")
w = World(); w.kql_to_adx()
sid, ns, out = snapshot(w)
m = json.loads(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])
kq = next(i for i in m["workspaces"][0]["items"] if i["type"] == "KQLDatabase")
expect(ns["WS"][W1]["ready"] and "data from ADX" in kq["recovery"], (ns["WS"][W1]["blockers"], kq["recovery"]))
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and ns.get("DELETE_ID") and not [r for r in ns["RESULTS"] if r["Status"] == "Blocked"],
       out[-1500:] if err else [r for r in ns["RESULTS"] if r["Status"] == "Blocked"])
go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
w.migrate()
cfg = f'SNAPSHOT_ID = "{sid}"'
ns, _, _ = go(w, RES, cfg)
ns, err, out = go(w, RES, cfg + f'\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
expect(err is None, out[-1500:] if err else "restored from Git")
ns, err, out = go(w, DATA, cfg + '\nKQL_RESTORE_SOURCE = "parquet"')
row = ns["PLAN_DF"].set_index("Type").loc["KQLDatabase"]
expect(row.Action == "BLOCKED" and "KQL export" in row.Detail, row.to_dict())
ns, err, out = go(w, DATA, cfg)
row = ns["PLAN_DF"].set_index("Type").loc["KQLDatabase"]
expect(row.Action == "ingest" and "from ADX https://adx.example / K1db" in row.Detail and row.Backup == "OK", row.to_dict())
ns, err, out = go(w, DATA, cfg + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
v = {r["Type"]: (r["Status"], r["Detail"]) for r in ns["RESULTS"] if r["Step"] == "Verify"}
expect(getattr(w, "adx_copies", []) == [("Events", "https://adx.example", "K1db", "Events")]
       and v.get("KQLDatabase", ("",))[0] == "OK", (getattr(w, "adx_copies", None), v.get("KQLDatabase"), out[-1500:] if err else ""))
w = World(); w.kql_to_adx()
sid, _, _ = snapshot(w)
del w.adls[next(k for k in w.adls if k.endswith("/kusto_copy/_destination.json"))]
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect(any("_destination.json" in r["Detail"] for r in ns["RESULTS"] if r["Status"] == "Blocked"), "delete blocked without the ADX record")
w = World(); w.kql_to_adx(keep_parquet=True)
sid, ns, out = snapshot(w)
kq = next(i for i in json.loads(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])["workspaces"][0]["items"]
          if i["type"] == "KQLDatabase")
expect("data from ADLS" in kq["recovery"], f"both: Parquet preferred {kq['recovery']}")

print("39 Git sync check (V2_3): default never commits; all workspaces must be synced; shared branch moved elsewhere is OK")
w = World(); w.defs[w.NB] += " edited"
ns, err, out = go(w, SNAP, "DRY_RUN = False")
sync = ns["SYNC_DF"].set_index("Workspace")
expect(err is None and sync.loc["Sales", "Sync"] == "Uncommitted changes" and "Source control > Changes > Commit"
       in sync.loc["Sales", "Fix"], (out[-1500:] if err else sync.to_dict("records")))
expect(not writes(w) and "NO SNAPSHOT TAKEN" in out and len(w.refs) == 1, f"nothing created {writes(w)[:3]}")
w = World()                                   # another workspace's folder committed to the shared branch
files = dict(w.commits[w.refs["main"]]); files["/other/X.Notebook/content.txt"] = b"x"
w.commits["c" * 40] = files; w.refs["main"] = "c" * 40
sid, ns, out = snapshot(w)
row = ns["SYNC_DF"].iloc[0]
expect(row.Sync == "Synced" and "other folders" in row.Detail and ns["WS"][W1]["ready"], (row.to_dict(), ns["WS"][W1]["blockers"]))
w = World()                                   # Sales synced, Ops not connected
w.add_ws(W2, "Ops")
ns, err, out = go(w, SNAP, f'DRY_RUN = False\nWORKSPACE_IDS = ["{W1}", "{W2}"]')
sync = ns["SYNC_DF"].set_index("Workspace")
expect(err is None and sync.loc["Ops", "Sync"] == "Not connected" and sync.loc["Sales", "Sync"] == "Synced"
       and not ns["WS"][W1]["ready"] and "not every workspace is synced" in " ".join(ns["WS"][W1]["blockers"]),
       out[-1500:] if err else (sync.to_dict("records"), ns["WS"][W1]["blockers"]))
expect(not writes(w) and not any(k.endswith("manifest.json") for k in w.adls), f"no manifest {writes(w)[:3]}")
ns, err, out = go(w, SNAP, f'DRY_RUN = False\nREQUIRE_ALL_WORKSPACES_SYNCED = False\nWORKSPACE_IDS = ["{W1}", "{W2}"]')
expect(err is None and ns["WS"][W1]["ready"] and not ns["WS"][W2]["ready"]
       and any(k.endswith("manifest.json") for k in w.adls), out[-1500:] if err else ns["WS"][W1]["blockers"])

print("40 workspace ID changed in the move, no WORKSPACE_MAP: V2_5 finds it by name, V2_6 follows V2_5")
w, sid, old = migrated(W2)
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
p = ns["PLAN_DF"]
expect(err is None and set(p.Target) == {W2} and p["Found by"].iloc[0].startswith("found by name"),
       out[-1500:] if err else p[["Target", "Found by"]].drop_duplicates().to_dict("records"))
go(w, RES, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
st = json.loads(w.adls[f"fabric-backups/_migration/{sid}/restore_state.json"])["workspaces"][W1]
expect(st.get("updated") and st.get("target") == W2, st)
ns, err, out = go(w, DATA, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and f"target workspace {W2} (same as V2_5 (ID changed))" in out and "BLOCKED" not in set(ns["PLAN_DF"].Action),
       out[-1500:] if err else ns["PLAN_DF"][["Item", "Action", "Detail"]].to_dict("records"))
w2 = World(); sid2, _, _ = snapshot(w2); w2.migrate(W2); w2.ws[W2]["name"] = "Sales renamed"
ns, err, out = go(w2, RES, f'SNAPSHOT_ID = "{sid2}"')
expect(err is None and "BLOCKED" in set(ns["PLAN_DF"].Action) and "set WORKSPACE_MAP" in " ".join(ns["PLAN_DF"]["Found by"].dropna()),
       out[-1500:] if err else ns["PLAN_DF"].to_dict("records"))

print("41 schema-enabled Lakehouse: Tables/<schema>/<table> restored only into a schema-enabled Lakehouse")
def schema_backup(w, with_json=True):
    for k in [k for k in w.adls if "/LH.Lakehouse__" in k and "/data/Tables/sales/" in k]:
        w.adls[k.replace("/data/Tables/sales/", "/data/Tables/dbo/sales/")] = w.adls.pop(k)
    man = next(k for k in w.adls if "/LH.Lakehouse__" in k and k.endswith("data_manifest.csv"))
    w.adls[man] = w.adls[man].replace(b"Tables/sales/", b"Tables/dbo/sales/")
    if with_json:
        w.adls[man.replace("data_manifest.csv", "lakehouse.json")] = json.dumps({
            "schemaEnabled": True, "defaultSchema": "dbo", "schemas": ["dbo"], "tables": ["Tables/dbo/sales"]}).encode()
for with_json in (True, False):
    w = World(); schema_backup(w, with_json)
    sid, _, _ = snapshot(w)
    ns, _, _ = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
    go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
    w.migrate()
    ns, _, _ = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
    go(w, RES, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
    ns, err, out = go(w, DATA, f'SNAPSHOT_ID = "{sid}"')
    row = ns["PLAN_DF"].set_index("Type").loc["Lakehouse"]
    expect(err is None and row.Action == "BLOCKED" and "has no schemas" in row.Detail,
           f"json={with_json}: blocked into a Lakehouse without schemas " + (out[-1500:] if err else str(row.to_dict())))
    w.lh_schema = "dbo"
    ns, err, out = go(w, DATA, f'SNAPSHOT_ID = "{sid}"')
    row = ns["PLAN_DF"].set_index("Type").loc["Lakehouse"]
    expect(err is None and row.Action == "load" and "schemas: dbo" in row.Detail, f"json={with_json}: " + str(row.to_dict()))
    ns, err, out = go(w, DATA, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
    lh_new = next(i["id"] for i in w.ws[W1]["items"] if i["type"] == "Lakehouse")
    got = [k for k in w.onelake if lh_new in str(k) and "Tables/dbo/sales/" in str(k)]
    v = {r["Type"]: r["Status"] for r in ns["RESULTS"] if r["Step"] == "Verify"}
    expect(err is None and got and v.get("Lakehouse") == "OK", (got[:2], v, out[-1500:] if err else ""))
w = World()                                   # a Lakehouse without schemas can't go into a schema-enabled one
sid, _, _ = snapshot(w)
ns, _, _ = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
w.migrate(); ns, _, _ = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
go(w, RES, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
w.lh_schema = "dbo"
ns, err, out = go(w, DATA, f'SNAPSHOT_ID = "{sid}"')
row = ns["PLAN_DF"].set_index("Type").loc["Lakehouse"]
expect(row.Action == "BLOCKED" and "is schema-enabled" in row.Detail, row.to_dict())

print("42 V2_3: schema-enabled Lakehouse must keep defaultSchema in its Git definition")
w = World(); w.lh_schema = "dbo"
ns, err, out = go(w, SNAP)
g = ns["GATE_DF"].set_index(["Item", "Type"])
expect(err is None and g.loc[("LH", "Lakehouse"), "Status"] == "BLOCKED" and "defaultSchema" in g.loc[("LH", "Lakehouse"), "Why blocked"],
       out[-1500:] if err else g.loc[("LH", "Lakehouse")].to_dict())
w.commits[w.refs["main"]][f"{DIR}/LH.Lakehouse/lakehouse.metadata.json"] = b'{"defaultSchema": "dbo"}'
sid, ns, out = snapshot(w)
lh = next(i for i in json.loads(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])["workspaces"][0]["items"]
          if i["type"] == "Lakehouse")
expect(ns["WS"][W1]["ready"] and lh["defaultSchema"] == "dbo", (ns["WS"][W1]["blockers"], lh.get("defaultSchema")))

print("43 workspace without Git: V2_3 READY from the raw copy, V2_4 proves nothing changed, V2_5 recreates from ADLS")
w = World(); w.ws[W1]["git"] = {"state": "NotConnected"}
NOGIT = f'NO_GIT_WORKSPACE_IDS = ["{W1}"]'
ns, err, out = go(w, SNAP, NOGIT)
expect(err is None and ns["WS"][W1]["status"] == "READY" and ns["SYNC_DF"].Sync.iloc[0] == "No Git (ADLS route)",
       out[-2000:] if err else ns["WS"][W1]["blockers"])
g = ns["GATE_DF"].set_index(["Item", "Type"])
expect(g.loc[("NB", "Notebook"), "Recovery"] == "ADLS definition (workspace without Git)", g.loc[("NB", "Notebook"), "Recovery"])
ns, err, out = go(w, SNAP, NOGIT + "\nSAVE_RAW_COPY_TO_ADLS = False")
expect(isinstance(err, ValueError) and "SAVE_RAW_COPY_TO_ADLS" in str(err), str(err))
sid, ns, out = snapshot(w, NOGIT)
m = json.loads(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])["workspaces"][0]
expect(m["gitRoute"] == "none" and m["ready"] and all(i.get("rawHash") for i in m["items"] if i["type"] != "SQLEndpoint"),
       (m["gitRoute"], m["blockers"]))
expect(not any(c[1] == "dev.azure.com" and c[0] != "GET" for c in w.calls) and not any(
    b.startswith("migration-snapshot") for b in w.refs), "no branch, nothing written to DevOps")
# a definition changed after the snapshot -> V2_4 blocks
w.defs[w.NB] += " changed later"
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and set(ns["PLAN_DF"].Action) == {"blocked"} and "NB (Notebook): its definition changed" in out,
       out[-1500:] if err else "blocked")
w.defs[w.NB] = w.defs[w.NB].replace(" changed later", "")
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
expect(err is None and sorted(i["type"] for i in w.ws[W1]["items"]) == ["Report", "SemanticModel"], out[-1500:] if err else "deleted")
old = {i["displayName"] + "." + i["type"]: i["id"] for i in m["items"]}
w.migrate()
n0 = len(w.calls)
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action)) if ns else {}
expect(err is None and acts.get("NB.Notebook") == "create from ADLS definition" and acts.get("LH.SQLEndpoint") ==
       "recreated with its parent", out[-2000:] if err else acts)
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
names = sorted(f"{i['displayName']}.{i['type']}" for i in w.ws[W1]["items"])
expect(err is None and names == sorted(["LH.Lakehouse", "LH.SQLEndpoint", "WH.Warehouse", "EH.Eventhouse", "EH.KQLDatabase",
                                        "NB.Notebook", "PL.DataPipeline", "DA.DataAgent", "SM.SemanticModel", "RP.Report"]),
       out[-2500:] if err else names)
order = [b["type"] for b in w.created_bodies]
expect(order.index("Lakehouse") < order.index("Notebook") and "KQLDatabase" not in order, order)
nb_new, lh_new = new_id(w, W1, "Notebook"), new_id(w, W1, "Lakehouse")
expect(lh_new in w.defs[nb_new] and old["LH.Lakehouse"] not in w.defs[nb_new], w.defs[nb_new])
kq_new = new_id(w, W1, "KQLDatabase")
expect(w.defs[kq_new] == "kql", "auto-created KQL database got its saved definition")
expect(not any(c[1] == "dev.azure.com" for c in w.calls[n0:]) and w.ws[W1]["git"]["state"] == "NotConnected",
       "no DevOps, no Git link")
st = json.loads(w.adls[f"fabric-backups/_migration/{sid}/restore_state.json"])["workspaces"][W1]
expect(st.get("updated") and st.get("route") == "adls", st)
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and set(ns["PLAN_DF"].Action) <= {"already restored", "recreated with its parent"}, "rerun: nothing to do")
ns, err, out = go(w, DATA, f'SNAPSHOT_ID = "{sid}"')
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action)) if ns else {}
expect(err is None and acts.get("LH.Lakehouse") == "load" and acts.get("EH.KQLDatabase") == "ingest", out[-1500:] if err else acts)

print("44 Azure DevOps unavailable: RESTORE_SOURCE = adls recreates a Git workspace from the raw copy, no DevOps calls")
w, sid, old = migrated()
w.create_no_definition = {"Warehouse"}; w.create_lro = True
n0 = len(w.calls)
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nRESTORE_SOURCE = "adls"')
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nRESTORE_SOURCE = "adls"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
expect(err is None and not any(c[1] == "dev.azure.com" for c in w.calls[n0:]), out[-2500:] if err else "no DevOps")
wh = [r for r in ns["RESULTS"] if r["Step"] == "Create (ADLS)" and r["Type"] == "Warehouse"]
expect(wh and wh[0]["Status"] == "Warning" and "WITHOUT its definition" in wh[0]["Detail"], wh)
expect(len([i for i in w.ws[W1]["items"] if i["type"] not in ("Report", "SemanticModel")]) == 8, [i["type"] for i in w.ws[W1]["items"]])
expect('RESTORE_SOURCE = "adls"' not in out or "If Azure DevOps is unavailable" not in out, "fallback hint only on the git route")

print("45 Git workspace with an item Git doesn't hold: ALLOW_SECONDARY_ONLY_ITEMS -> V2_4 checks it, V2_5 recreates it from ADLS")
def git_without(types):
    w = World()
    w.git_unsupported = set(types)
    w.defs[w.DA] = f"agent uses {w.LH}"
    sha = w.commit({w.path(k): c for k, c in w.git_view(W1).items()}, None)
    w.refs["main"] = sha
    w.ws[W1]["git"]["head"] = sha
    return w
w = git_without({"DataAgent"})
ns, err, out = go(w, SNAP)
g = ns["GATE_DF"].set_index(["Item", "Type"]) if ns else None
expect(err is None and g.loc[("DA", "DataAgent"), "Status"] == "BLOCKED" and "not saved in Git" in
       g.loc[("DA", "DataAgent"), "Why blocked"], out[-1500:] if err else g.loc[("DA", "DataAgent")].to_dict())
ns, err, out = go(w, SNAP, "ALLOW_SECONDARY_ONLY_ITEMS = True\nSAVE_RAW_COPY_TO_ADLS = False")
expect(isinstance(err, ValueError) and "SAVE_RAW_COPY_TO_ADLS" in str(err), str(err))
ALLOW = "ALLOW_SECONDARY_ONLY_ITEMS = True"
sid, ns, out = snapshot(w, ALLOW)
g = ns["GATE_DF"].set_index(["Item", "Type"])
expect(ns["WS"][W1]["status"] == "READY" and g.loc[("DA", "DataAgent"), "Recovery"] == "ADLS definition (V2_5, not in Git)",
       (ns["WS"][W1]["status"], g.loc[("DA", "DataAgent"), "Recovery"]))
expect("v1" not in out.lower().replace("/v1/", ""), "no v1 wording in V2_3 output")
# V2_4: the item outside Git changed after the snapshot -> blocked (Git status can't see it)
w.defs[w.DA] += " changed"
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and set(ns["PLAN_DF"].Action) == {"blocked"} and "DA (DataAgent): its definition changed" in out,
       out[-1500:] if err else set(ns["PLAN_DF"].Action))
w.defs[w.DA] = w.defs[w.DA].replace(" changed", "")
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
expect(err is None and sorted(i["type"] for i in w.ws[W1]["items"]) == ["Report", "SemanticModel"], out[-1500:] if err else "deleted")
w.migrate()
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action)) if ns else {}
expect(err is None and acts.get("DA.DataAgent") == "create from ADLS definition (not in Git), after the Git update"
       and acts.get("NB.Notebook") == "restore from Git", out[-2000:] if err else acts)
w.created_bodies = []
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
names = sorted(f"{i['displayName']}.{i['type']}" for i in w.ws[W1]["items"])
expect(err is None and "DA.DataAgent" in names and "NB.Notebook" in names and "LH.Lakehouse" in names,
       out[-2500:] if err else names)
expect([b["type"] for b in w.created_bodies] == ["DataAgent"], [b["type"] for b in w.created_bodies])
da_new, lh_new = new_id(w, W1, "DataAgent"), new_id(w, W1, "Lakehouse")
expect(lh_new in w.defs[da_new] and w.LH not in w.defs[da_new], f"DataAgent points to the restored Lakehouse: {w.defs[da_new]}")
st = json.loads(w.adls[f"fabric-backups/_migration/{sid}/restore_state.json"])["workspaces"][W1]
expect(st.get("updated") and w.DA in st.get("created", {}), st)
v = [r for r in ns["RESULTS"] if r["Step"] == "Verify"]
expect(v and all(r["Status"] == "OK" for r in v), [r for r in v if r["Status"] != "OK"])
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action)) if ns else {}
expect(err is None and acts.get("DA.DataAgent") == "already restored" and not ns["TODO"], out[-1500:] if err else acts)
# Git update done but the ADLS creation was interrupted: the rerun creates only the missing item
w2 = git_without({"DataAgent"}); sid2, _, _ = snapshot(w2, ALLOW)
ns, _, _ = go(w2, DEL, f'SNAPSHOT_ID = "{sid2}"')
go(w2, DEL, f'SNAPSHOT_ID = "{sid2}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
w2.migrate()
w2.create_no_definition = {"DataAgent"}            # first attempt: Fabric refuses the create
ns, _, _ = go(w2, RES, f'SNAPSHOT_ID = "{sid2}"')
ns, err, out = go(w2, RES, f'SNAPSHOT_ID = "{sid2}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
expect(not any(i["type"] == "DataAgent" for i in w2.ws[W1]["items"]) and
       any(r["Step"] == "Restore" and r["Status"] == "Failed" for r in ns["RESULTS"]), "first attempt fails on the DataAgent")
w2.create_no_definition = set()
n_upd = len([c for c in w2.calls if c[2].endswith("/git/updateFromGit")])
ns, err, out = go(w2, RES, f'SNAPSHOT_ID = "{sid2}"')
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action)) if ns else {}
expect(err is None and acts.get("NB.Notebook") == "already restored" and
       acts.get("DA.DataAgent", "").startswith("create from ADLS"), out[-1500:] if err else acts)
ns, err, out = go(w2, RES, f'SNAPSHOT_ID = "{sid2}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
expect(err is None and any(i["type"] == "DataAgent" for i in w2.ws[W1]["items"]) and
       len([i for i in w2.ws[W1]["items"] if i["type"] == "Notebook"]) == 1 and
       len([c for c in w2.calls if c[2].endswith("/git/updateFromGit")]) == n_upd,
       out[-1500:] if err else "DataAgent created, Git update not repeated, nothing duplicated")

print("45b Ontology backing types outside Git: required backups, raw fingerprints and ADLS restore")
w = World()
ontology_id = w.add_item(W1, "Ontology", "O", "ontology")
suffix = ontology_id.replace("-", "")
graph_id = w.add_item(W1, "GraphModel", f"O_graph_{suffix}", "ontology graph")
eventhouse_id = w.add_item(W1, "Eventhouse", f"O_eh_{suffix}", "ontology eventhouse")
database_id = w.add_item(W1, "KQLDatabase", f"O_eh_{suffix}", "ontology database")
backing_ids = {graph_id, eventhouse_id, database_id}
backing_keys = {(i["displayName"], i["type"]) for i in w.ws[W1]["items"] if i["id"] in backing_ids}
w.git_unsupported_items = {f"{t}|{n}" for n, t in backing_keys}
sha = w.commit({w.path(k): c for k, c in w.git_view(W1).items()}, None)
w.refs["main"] = sha
w.ws[W1]["git"]["head"] = sha
w.make_backup()
ns, err, out = go(w, SNAP)
g = ns["GATE_DF"].set_index(["Item", "Type"])
expect(err is None and all(g.loc[k, "Status"] == "BLOCKED" for k in backing_keys)
       and g.loc[("EH", "Eventhouse"), "In Git"], "only the backing items are outside Git; default stays blocked")

backup_key = f"fabric-backups/{RUN}/backup_log.csv"
backup_log = w.adls[backup_key]
log = pd.read_csv(io.BytesIO(backup_log), dtype=str).fillna("")
w.adls[backup_key] = log[~((log.ItemId == database_id) & (log.Step == "KQL export"))].to_csv(index=False).encode()
ns, err, out = go(w, SNAP, ALLOW)
g = ns["GATE_DF"].set_index(["Item", "Type"])
expect(err is None and g.loc[(f"O_eh_{suffix}", "KQLDatabase"), "Status"] == "BLOCKED",
       "the ADLS opt-in does not waive the KQL data backup")
w.adls[backup_key] = backup_log

sid, ns, out = snapshot(w, ALLOW)
mws = json.loads(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])["workspaces"][0]
backing = [i for i in mws["items"] if i["id"] in backing_ids]
expect(mws["ready"] and len(backing) == 3 and all(not i["inGit"] and i["rawCopy"] == "OK"
       and i["rawHash"] and i["recovery"].startswith("ADLS definition (V2_5") for i in backing),
       "all three backing types require saved raw definitions and fingerprints; none is skipped")
w.defs[graph_id] += " changed"
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and set(ns["PLAN_DF"].Action) == {"blocked"} and "its definition changed" in out,
       "a changed backing GraphModel blocks deletion")
w.defs[graph_id] = w.defs[graph_id].replace(" changed", "")
ns, _, _ = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
expect(err is None and not any(i["id"] in backing_ids for i in w.ws[W1]["items"]), "backing items deleted only after confirmation")
w.migrate()
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action))
expect(err is None and all(acts[f"{n}.{t}"].startswith("create from ADLS") for n, t in backing_keys)
       and acts["O.Ontology"] == "restore from Git", "Ontology stays on Git; its backing items use ADLS")
w.created_bodies = []
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
restored_backing = [i for i in w.ws[W1]["items"] if (i["displayName"], i["type"]) in backing_keys]
expect(err is None and len(restored_backing) == 3 and
       sorted(b["type"] for b in w.created_bodies) == ["Eventhouse", "GraphModel"] and
       any(i["type"] == "KQLDatabase" and i["id"] in w.update_bodies for i in restored_backing),
       out[-2500:] if err else "three backing items restored; the Eventhouse's auto-created KQL database is reused")

print("46 Dataflow Gen2 staging items: never a blocker, not saved or restored, still hard-deleted by V2_4")
w = World()
SWH = w.add_item(W1, "Warehouse", "StagingWarehouseForDataflows_20261113112943", "staging wh")
SLH = w.add_item(W1, "Lakehouse", "StagingLakehouseForDataflows_20261113112943", "staging lh")
sid, ns, out = snapshot(w)
g = ns["GATE_DF"].set_index(["Item", "Type"])
st_rows = g[g.index.get_level_values(0).str.startswith("Staging")]
expect(ns["WS"][W1]["status"] == "READY" and len(st_rows) == 3 and set(st_rows.Status) == {"READY"} and
       all(r.startswith("Dataflow Gen2 staging") for r in st_rows.Recovery), st_rows[["Status", "Recovery"]].to_dict())
expect(not [p for p in w.adls if "/raw/" in p and "Staging" in p], "no raw copy of staging items")
m = json.loads(w.adls[f"fabric-backups/_migration/{sid}/manifest.json"])["workspaces"][0]
expect(sum(1 for i in m["items"] if i.get("staging")) == 3, "manifest marks them")
LATE = w.add_item(W1, "Warehouse", "StagingWarehouseForDataflows_20261117090000", "staging later")   # a Dataflow ran
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"')
acts = dict(zip(ns["PLAN_DF"].Item, ns["PLAN_DF"].Action)) if ns else {}
expect(err is None and acts.get("StagingWarehouseForDataflows_20261117090000") == "delete" and
       acts.get("StagingWarehouseForDataflows_20261113112943") == "delete" and "blocked" not in acts.values(),
       out[-1500:] if err else acts)
w.delete_modes = []
ns, err, out = go(w, DEL, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_DELETE_ID = "{ns["DELETE_ID"]}"')
expect(err is None and sorted(i["type"] for i in w.ws[W1]["items"]) == ["Report", "SemanticModel"] and
       set(w.delete_modes) == {"true"}, out[-1500:] if err else [i["displayName"] for i in w.ws[W1]["items"]])
w.migrate()
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"')
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action)) if ns else {}
expect(err is None and acts.get("StagingWarehouseForDataflows_20261113112943.Warehouse", "").startswith("not restored: Dataflow Gen2")
       and acts.get("StagingLakehouseForDataflows_20261113112943.SQLEndpoint", "").startswith("not restored"),
       out[-1500:] if err else acts)
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nDRY_RUN = False\nCONFIRM_PLAN_ID = "{ns["PLAN_ID"]}"')
v = [r for r in ns["RESULTS"] if r["Step"] == "Verify"] if ns else []
expect(err is None and v and all(r["Status"] == "OK" for r in v) and
       not any(i["displayName"].startswith("Staging") for i in w.ws[W1]["items"]), out[-1500:] if err else v)
ns, err, out = go(w, RES, f'SNAPSHOT_ID = "{sid}"\nRESTORE_SOURCE = "adls"')
acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action)) if ns else {}
expect(err is None and acts.get("StagingWarehouseForDataflows_20261113112943.Warehouse", "").startswith("not restored"), acts)
ns, err, out = go(w, DATA, f'SNAPSHOT_ID = "{sid}"')
expect(err is None and not any(str(x).startswith("Staging") for x in ns["PLAN_DF"].Item), out[-1500:] if err else list(ns["PLAN_DF"].Item))

print("47 after the restore Fabric rewrites a KQL database (uncommitted): Info row, not a failure; V2_6 not blocked")
w, sid, cfg = restored()
kid = new_id(w, W1, "KQLDatabase")
w.defs[kid] = w.defs[kid] + " rewritten with new IDs"
ns, err, out = go(w, RES, cfg)
info = [r for r in ns["RESULTS"] if r["Step"] == "Verify Git sync" and r["Status"] == "Info"] if ns else []
sync = [r for r in ns["RESULTS"] if r["Step"] == "Verify Git sync" and r["Status"] != "Info"] if ns else []
expect(err is None and len(info) == 1 and "uncommitted" in info[0]["Detail"] and sync and
       all(r["Status"] == "OK" for r in sync), out[-1500:] if err else (info, sync))
ns, err, out = go(w, DATA, cfg)
expect(err is None and "BLOCKED" not in set(ns["PLAN_DF"].Action), out[-1500:] if err else set(ns["PLAN_DF"].Action))

print("48 Lakehouse copy with fastcp: probe, tables data first then _delta_log, both fastcp layouts, fallbacks")
def lh_load(setup=None, extra=""):
    w, sid, cfg = restored()
    if setup:
        setup(w)
    ns, _, _ = go(w, DATA, cfg + extra)
    ns, err, out = go(w, DATA, cfg + extra + f'\nDRY_RUN = False\nCONFIRM_DATA_ID = "{ns["DATA_ID"]}"')
    lh = new_id(w, W1, "Lakehouse")
    got = {k.split("/", 2)[2]: v for k, v in w.onelake.items() if k.startswith(f"{W1}/{lh}/")}
    row = next((r for r in ns["RESULTS"] if r["Step"] == "Lakehouse data" and r["Status"] != "Info"), {}) if ns else {}
    info = [r["Detail"] for r in ns["RESULTS"] if r["Step"] == "Lakehouse data" and r["Status"] == "Info"] if ns else []
    ver = next((r["Status"] for r in ns["RESULTS"] if r["Step"] == "Verify" and r["Type"] == "Lakehouse"), None) if ns else None
    order = [p for p in getattr(w, "onelake_order", []) if p.startswith(f"{W1}/{lh}/")]
    return w, err, out, got, row, info, ver, order, lh
for mode in ("into", "contents"):
    w, err, out, got, row, info, ver, order, lh = lh_load(lambda w: setattr(w, "fastcp_mode", mode))
    calls = [c for c in getattr(w, "fastcp_calls", []) if "onelake" in c[1]]
    expect(err is None and got == DATA_FILES["Lakehouse"] and ver == "OK" and "3 by fastcp, 0 file by file" in row.get("Detail", "")
           and "_delta_log" in order[-1] and not [k for k in w.adls if "_fastcp_probe_" in k]
           and all("--overwrite=false" in c[2]["flags"] for c in calls)
           and any("--exclude-path=_delta_log" in c[2]["flags"] for c in calls),
           f"{mode}: {row} {info} {sorted(got)} {out[-1500:] if err else ''}")
w, err, out, got, row, info, ver, order, lh = lh_load(lambda w: setattr(w, "fastcp_fail", True))
expect(err is None and got == DATA_FILES["Lakehouse"] and ver == "OK" and info and "not usable" in info[0]
       and "3 copied" in row.get("Detail", ""), f"fastcp fails -> per file {row} {info}")
w, err, out, got, row, info, ver, order, lh = lh_load(lambda w: setattr(w, "fastcp_ignore_exclude", True))
expect(err is None and got == DATA_FILES["Lakehouse"] and ver == "OK" and info and "exclusion" in info[0],
       f"exclude flag ignored -> per file {row} {info}")
w, err, out, got, row, info, ver, order, lh = lh_load(lambda w: setattr(w, "fastcp_drop", {"part-0.parquet"}))
expect(err is None and got == DATA_FILES["Lakehouse"] and ver == "OK" and "2 by fastcp, 1 file by file" in row.get("Detail", "")
       and "_delta_log" in order[-1], f"file fastcp missed is copied one by one, still before the log {row} {order}")
w, err, out, got, row, info, ver, order, lh = lh_load(extra='\nLAKEHOUSE_COPY_METHOD = "per_file"')
expect(err is None and got == DATA_FILES["Lakehouse"] and not getattr(w, "fastcp_calls", None) and "3 copied" in row.get("Detail", ""),
       f"per_file setting {row}")
w, sid, cfg = restored()
ns, err, out = go(w, DATA, cfg + '\nLAKEHOUSE_COPY_METHOD = "bulk"')
expect(isinstance(err, ValueError) and "LAKEHOUSE_COPY_METHOD" in str(err), "bad setting refused")

print("49 V2_6 reads data_manifest.csv in both formats (old unquoted dates with commas, new quoted); size change caught")
for fmt in ("old", "new"):
    w, sid, cfg = restored()
    for k in [k for k in w.adls if k.endswith("/data_manifest.csv")]:
        rows = w.adls[k].decode().splitlines()
        if fmt == "new":
            rows = [rows[0]] + ['"' + '","'.join([r.split(",")[0], r.split(",")[1], "Tue, 17 Nov 2026 07:35:24 GMT", "copied"]) + '"'
                                for r in rows[1:]]
        w.adls[k] = ("\n".join(rows) + "\n").encode()
    ns, err, out = go(w, DATA, cfg)
    acts = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Action)) if ns else {}
    expect(err is None and acts.get("LH.Lakehouse") == "load" and acts.get("WH.Warehouse") != "BLOCKED",
           f"{fmt}: {acts} {out[-1500:] if err else ''}")
    k = next(k for k in w.adls if "/LH.Lakehouse__" in k and "/data/Files/raw/a.csv" in k)
    w.adls[k] = w.adls[k] + b"changed"
    ns, err, out = go(w, DATA, cfg)
    d = dict(zip(ns["PLAN_DF"].Item + "." + ns["PLAN_DF"].Type, ns["PLAN_DF"].Detail)) if ns else {}
    expect(err is None and "missing or changed in ADLS" in d.get("LH.Lakehouse", ""), f"{fmt}: size change {d}")

print("\nRESULT:", "ALL PASSED" if not fails else f"{fails} FAILED")
