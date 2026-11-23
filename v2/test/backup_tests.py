"""Simulated tests for V2_2_Backup_Data (run: python3 build.py && python3 test/backup_tests.py)."""
import contextlib, io, json, re, sys, traceback
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backup_harness import World, run, G

FAIL = []
def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + str(msg)[:300])
    if not cond:
        FAIL.append(msg)

def go(w, extra="", **kw):
    out, err, ns = io.StringIO(), None, None
    with contextlib.redirect_stdout(out):
        try:
            ns = run(w, extra, **kw)
        except Exception as exc:
            err = exc
            traceback.print_exc(file=out)
    return ns, err, out.getvalue()

def steps(ns, item, step):
    return [r["Status"] for r in ns["RESULTS"] if r["ItemId"] == item and r["Step"] == step]

KQ = G(111)
FREEZE = '\nFREEZE_STARTED_UTC = "2020-01-01T00:00:00Z"'

print("B1 dry run, parquet: nothing written, no DevOps, no Kusto Copy")
w = World()
ns, err, out = go(w, 'DRY_RUN = True\nKQL_BACKUP_MODE = "parquet"' + FREEZE)
check(err is None, out[-1500:] if err else "no error")
check(not w.adls and not any(h == "dev.azure.com" for _, h, _ in w.calls) and not w.kc_runs, "read only")
check(steps(ns, KQ, "KQL export") == ["Dry run"], steps(ns, KQ, "KQL export"))

print("B2 real run, parquet: Parquet export, paste block for V2_3, mode and freeze recorded")
w = World()
ns, err, out = go(w, 'KQL_BACKUP_MODE = "parquet"' + FREEZE, run_id="20260101T000000Z")
check(err is None, out[-1500:] if err else "no error")
check(steps(ns, KQ, "KQL export") == ["OK"] and not steps(ns, KQ, "Kusto Copy") and not w.kc_runs, "parquet only")
started = json.loads(w.adls["fb/20260101T000000Z/_run_started.json"])
check(started["kqlBackupMode"] == "parquet" and started["freezeStartedUtc"] == "2020-01-01T00:00:00Z", started)
check('SECONDARY_BACKUP_RUN_ID = "20260101T000000Z"' in out and 'FREEZE_STARTED_UTC = "2020-01-01T00:00:00Z"' in out,
      "paste block printed")
check(not any(h == "dev.azure.com" for _, h, _ in w.calls), "no DevOps calls (Git link is the Git copy)")

print("B3 adx: rows copied to ADX, schema script kept in ADLS, destination recorded")
w = World()
ns, err, out = go(w, 'KQL_BACKUP_MODE = "adx"' + FREEZE, run_id="20260101T000000Z")
check(err is None, out[-1500:] if err else "no error")
check(not steps(ns, KQ, "KQL export") and steps(ns, KQ, "KQL schema") == ["OK"] and steps(ns, KQ, "Kusto Copy") == ["OK"],
      [(r["Step"], r["Status"]) for r in ns["RESULTS"] if r["ItemId"] == KQ])
check(any(k.endswith("/database_schema.kql") for k in w.adls), "schema script saved")
dest = [json.loads(v) for k, v in w.adls.items() if k.endswith("/kusto_copy/_destination.json")]
check(len(dest) == 1 and dest[0]["database"] == "K1db" and dest[0]["status"] == "OK" and len(dest[0]["tables"]) == 3, dest)
check(not any("/data/" in k and k.endswith(".parquet") and KQ in k for k in w.adls), "no Parquet export")

print("B4 both: Parquet and ADX")
w = World()
ns, err, out = go(w, 'KQL_BACKUP_MODE = "both"' + FREEZE)
check(steps(ns, KQ, "KQL export") == ["OK"] and steps(ns, KQ, "Kusto Copy") == ["OK"], "both copies")

print("B5 settings guards: freeze in the future, empty freeze warns, bad mode, KC falls back to ADO_SP_*")
w = World()
ns, err, out = go(w, 'FREEZE_STARTED_UTC = "2999-01-01T00:00:00Z"')
check(isinstance(err, RuntimeError) and "in the future" in str(err) and not w.adls, str(err))
w = World()
ns, err, out = go(w, 'DRY_RUN = True\nFREEZE_STARTED_UTC = ""')
check(err is None and "FREEZE_STARTED_UTC is empty" in out, "warning")
ns, err, out = go(World(), 'KQL_BACKUP_MODE = "kusto"')
check(isinstance(err, ValueError) and "KQL_BACKUP_MODE" in str(err), str(err))
w = World()
ns, err, out = go(w, 'DRY_RUN = True\nKQL_BACKUP_MODE = "adx"\nKC_TENANT_ID = ""\nKC_CLIENT_ID = ""\nKC_CLIENT_SECRET_NAME = ""'
                  '\nADO_SP_TENANT_ID = "tenant-1"\nADO_SP_CLIENT_ID = "app-1"\nADO_SP_SECRET_NAME = "kc-secret"' + FREEZE)
check(err is None and ns["KC_CLIENT_ID"] == "app-1" and ns["KC_CLIENT_SECRET_NAME"] == "kc-secret", out[-800:] if err else "fallback")
ns, err, out = go(World(), 'KQL_BACKUP_MODE = "adx"\nKC_TENANT_ID = ""\nKC_CLIENT_ID = ""' + FREEZE)
check(isinstance(err, ValueError) and "KC_TENANT_ID" in str(err), str(err))

print("B6 Lakehouse layout recorded: schema-enabled (Tables/<schema>/<table>) and without schemas")
w = World()
ns, err, out = go(w, FREEZE, run_id="20260101T000000Z")
lay = [json.loads(v) for k, v in w.adls.items() if k.endswith("/lakehouse.json")]
check(err is None and len(lay) == 1 and not lay[0]["schemaEnabled"] and lay[0]["tables"] == ["Tables/t1"], lay)
w = World(); w.lh_schema = "dbo"
lh = w.onelake[(G(1), G(107))] if (G(1), G(107)) in w.onelake else next(v for k, v in w.onelake.items() if k[1] == G(107))
for k in [k for k in lh if k.startswith("Tables/t1/")]:
    lh[k.replace("Tables/t1/", "Tables/dbo/t1/")] = lh.pop(k)
lh["Tables/sales/orders/part-0.parquet"] = b"O" * 10
lh["Tables/sales/orders/_delta_log/00000000000000000000.json"] = b"{}"
ns, err, out = go(w, FREEZE, run_id="20260101T000000Z")
lay = [json.loads(v) for k, v in w.adls.items() if k.endswith("/lakehouse.json")]
data = [r["Detail"] for r in ns["RESULTS"] if r["ItemId"] == G(107) and r["Step"] == "Data"]
check(err is None and lay and lay[0]["schemaEnabled"] and lay[0]["schemas"] == ["dbo", "sales"]
      and lay[0]["tables"] == ["Tables/dbo/t1", "Tables/sales/orders"], (lay, out[-1500:] if err else ""))
check(data and "schema-enabled (default schema dbo): 2 table(s) in 2 schema(s) dbo, sales" in data[0], data)
check(any(k.endswith("/data/Tables/sales/orders/part-0.parquet") for k in w.adls), "non-dbo schema copied")

print("B7 ADX databases named per KQL database and created by V2_2; you added as Viewer; inventory saved")
import base64 as _b64
def jwt(claims):
    return "h." + _b64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".s"
AUTO = '\nKQL_BACKUP_MODE = "adx"\nADX_CREATE_DATABASES = True\nADX_DATABASE_PATTERN = "{workspace}__{database}__{id8}"' + FREEZE
w = World(); w.runner_token = jwt({"upn": "me@contoso.com", "tid": "t"})
ns, err, out = go(w, 'DRY_RUN = True' + AUTO)
inv = ns and ns.get("ADX_INVENTORY")
check(err is None and not w.arm_puts and inv and inv[0]["ADX database state"] == "would create", out[-1500:] if err else inv)
name = inv[0]["ADX database"] if inv else ""
check(name.endswith("__K1db__" + KQ[:8]) and re.fullmatch(r"[A-Za-z0-9_.-]+", name), name)
check("(database to create)" in " ".join(r["Detail"] for r in ns["RESULTS"] if r["Step"] == "Kusto Copy"), "dry run says create")
ns, err, out = go(w, '' + AUTO, run_id="20260101T000000Z")
check(err is None and [d for d, _ in w.arm_puts] == [name] and "softDeletePeriod" not in w.arm_puts[0][1]["properties"]
      and w.arm_puts[0][1]["properties"]["hotCachePeriod"] == "P1D", out[-1500:] if err else w.arm_puts)
check(w.adx_viewers == [(name, "aaduser=me@contoso.com")], w.adx_viewers)
check(steps(ns, KQ, "Kusto Copy") == ["OK"] and len(w.adx[name]) == 3, steps(ns, KQ, "Kusto Copy"))
dest = [json.loads(v) for k, v in w.adls.items() if k.endswith("/kusto_copy/_destination.json")]
check(dest and dest[0]["database"] == name, dest)
csv = w.adls.get("fb/20260101T000000Z/adx_inventory.csv", b"")
csv = csv.decode() if isinstance(csv, bytes) else csv
check(name in csv and ",created," in csv, csv[:400])
ns, err, out = go(w, '' + AUTO, run_id="20260102T000000Z")
check(err is None and len(w.arm_puts) == 1 and "existing" in str(ns["ADX_INVENTORY"]), "second run reuses the database")
w = World(); w.arm_actions = ["Microsoft.Kusto/clusters/read", "Microsoft.Kusto/clusters/databases/read"]
ns, err, out = go(w, '' + AUTO)
check(isinstance(err, RuntimeError) and "Contributor" in str(err) and not w.arm_puts, str(err))
w = World(); w.arm_actions = ["Microsoft.Kusto/*"]
ns, err, out = go(w, 'DRY_RUN = True' + AUTO)
check(err is None and "can create missing databases" in out, out[-800:] if err else "wildcard action accepted")
w = World()
ns, err, out = go(w, 'DRY_RUN = True\nADX_SUBSCRIPTION_ID = "sub-1"\nADX_RESOURCE_GROUP = "rg-1"' + AUTO)
check(err is None and not any(p == "/subscriptions" for _, h, p in w.calls if h == "management.azure.com"), "direct lookup")
check(ns["adx_safe_name"]("Sales WS / Ops__KQL db!__1b2c3d4e") == "Sales_WS_Ops__KQL_db__1b2c3d4e",
      ns["adx_safe_name"]("Sales WS / Ops__KQL db!__1b2c3d4e"))

print("B8 Warehouse: data files missing from the folder listing are copied from the Delta log; empty tables noted")
WH = G(109)
def wh_world():
    w = World()
    w.onelake[next(k for k in w.onelake if k[1] == WH)] = {
        "Tables/dbo/S/_delta_log/00000000000000000000.json": b'{"metaData":{}}\n',
        "Tables/dbo/S/_delta_log/00000000000000000001.json":
            b'{"add":{"path":"8F%20A.parquet","size":7}}\n{"add":{"path":"gone.parquet","size":3}}\n{"commitInfo":{}}',
        "Tables/dbo/S/_delta_log/00000000000000000002.json": b'{"remove":{"path":"gone.parquet"}}',
        "Tables/dbo/S/8F A.parquet": b"H" * 7,
        "Tables/dbo/E/_delta_log/00000000000000000000.json": b'{"metaData":{}}'}
    w.hidden = {"Tables/dbo/S/8F A.parquet"}
    return w
w = wh_world()
ns, err, out = go(w, 'DRY_RUN = True' + FREEZE)
d = [r for r in ns["RESULTS"] if r["ItemId"] == WH and r["Step"] == "Data"] if ns else []
check(err is None and d and d[0]["Status"] == "Dry run" and "would copy 5 files" in d[0]["Detail"]
      and "1 data file(s) not in the folder listing" in d[0]["Detail"] and "dbo/E" in d[0]["Detail"]
      and "DATA_LAKE_LOG_PUBLISHING" in d[0]["Detail"], out[-1500:] if err else d)
w = wh_world()
ns, err, out = go(w, FREEZE, run_id="20260101T000000Z")
d = [r for r in ns["RESULTS"] if r["ItemId"] == WH and r["Step"] == "Data"] if ns else []
got = [k for k in w.adls if f"/data/Tables/dbo/S/" in k and k.endswith(".parquet")]
check(err is None and d and d[0]["Status"] == "OK" and len(got) == 1 and got[0].endswith("/8F A.parquet")
      and w.adls[got[0]] == b"H" * 7, (d, got))
w = wh_world()
w.onelake[next(k for k in w.onelake if k[1] == WH)]["Tables/dbo/S/_delta_log/00000000000000000003.json"] = \
    b'{"add":{"path":"abfss://x@onelake.dfs.fabric.microsoft.com/y/z.parquet","size":1}}'
ns, err, out = go(w, FREEZE, run_id="20260101T000000Z")
d = [r for r in ns["RESULTS"] if r["ItemId"] == WH and r["Step"] == "Data"] if ns else []
check(err is None and d and d[0]["Status"] == "Partial" and "outside the table folder" in d[0]["Detail"], d)

print("B9 SQL analytics endpoint with no views/functions/procedures (connector returns None): no crash")
w = World()
w.sql_empty = True
ns, err, out = go(w, FREEZE, run_id="20260101T000000Z")
d = [r for r in ns["RESULTS"] if r["Step"] == "SQL scripts"] if ns else []
perm = [k for k in w.adls if k.endswith("/sql/permissions.csv")]
check(err is None and d and all(r["Status"] == "OK" and "0 objects, 0 permission rows" in r["Detail"] for r in d)
      and perm and not [k for k in w.adls if "/sql/" in k and k.endswith(".sql")], out[-1500:] if err else d)

print("B10 Dataflow Gen2 staging items are left out of the backup (Fabric recreates them)")
class StagingWorld(World):
    with_staging = True
w = StagingWorld()
ns, err, out = go(w, FREEZE, run_id="20260101T000000Z")
staged = [r for r in ns["RESULTS"] if str(r["Item"]).startswith("Staging")] if ns else ["error"]
check(err is None and not staged and "Left out, Dataflow Gen2 staging items" in out and
      not [k for k in w.adls if "Staging" in k], out[-1500:] if err else staged)

print("B11 data_manifest.csv is valid CSV: lastModified with commas is quoted (4 columns per row)")
import csv as _csv
w = World()
ns, err, out = go(w, 'KQL_BACKUP_MODE = "parquet"' + FREEZE, run_id="20260101T000000Z")
mans = [v.decode() for k, v in w.adls.items() if k.endswith("/data_manifest.csv")]
rows = [r for m in mans for r in list(_csv.reader(io.StringIO(m)))[1:] if r]
check(err is None and mans and rows and all(len(r) == 4 and r[1].isdigit() and r[2].startswith("Tue, 17 Nov")
                                           and r[3] == "copied" for r in rows), rows[:3])

print("B12 ADX database deleted and created again: stale Kusto Copy progress is reset, all rows copied again")
w = World()
ns, err, out = go(w, AUTO, run_id="20260101T000000Z")
name = ns["ADX_INVENTORY"][0]["ADX database"] if ns else ""
check(err is None and w.adx.get(name) == {"T1": 100, "T 2": 0, "T3": 5}, w.adx.get(name))
ns, err, out = go(w, AUTO, run_id="20260102T000000Z")    # unchanged source: nothing new, nothing duplicated
check(err is None and w.adx[name] == {"T1": 100, "T 2": 0, "T3": 5}
      and not any("T1" in d for d in getattr(w, "adls_deleted", [])), w.adx[name])
del w.adx[name]; w.adls_deleted = []                      # someone drops the ADX database
ns, err, out = go(w, 'DRY_RUN = True' + AUTO, run_id="20260103T000000Z")
d = " ".join(r["Detail"] for r in ns["RESULTS"] if r["Step"] == "Kusto Copy") if ns else ""
check(err is None and "3 empty ADX table(s) with old Kusto Copy progress" in d and not w.adls_deleted, d)
ns, err, out = go(w, AUTO, run_id="20260104T000000Z")
d = [r for r in ns["RESULTS"] if r["Step"] == "Kusto Copy"] if ns else []
check(err is None and w.adx.get(name) == {"T1": 100, "T 2": 0, "T3": 5} and len(w.adls_deleted) == 3
      and d and d[0]["Status"] == "OK", out[-1500:] if err else (w.adx.get(name), d))
check("progress reset" in out, "reset is reported")
w.adx[name]["T3"] = 0                                     # one table cleared, the others keep their rows
w.adls_deleted = []
ns, err, out = go(w, AUTO, run_id="20260105T000000Z")
check(err is None and w.adx[name] == {"T1": 100, "T 2": 0, "T3": 5}
      and not any("T1" in x for x in w.adls_deleted), (w.adx[name], w.adls_deleted))

print("\nRESULT:", "ALL PASSED" if not FAIL else f"{len(FAIL)} FAILED")
