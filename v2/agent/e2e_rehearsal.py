"""End-to-end rehearsal of the whole migration chain in a disposable Fabric workspace, with a GREEN/RED report.

What it does (each step is resumable; the state is in agent/e2e/<rehearsal id>/state.json):
  0. local   runs the five local test suites on this exact build (code build IDs go into the report)
  1. ws      creates a NEW workspace "E2E-REHEARSAL-<id>" on --capacity and adds --member principals (the Git /
             Kusto Copy service principal, as Contributor); OR, with --workspace, reuses a lab workspace listed in
             lab_config.E2E["REHEARSAL_WORKSPACE_IDS"]: it must be clean (not linked to Git, no Fabric items) or
             --reset wipes it first (Git disconnect + hard delete of every non-Power BI item)
  2. V2_T    creates the test items and their data; optional independent copies and one measured large copy
  3. V2_0    permission check (real, with write tests) for the rehearsal workspace
  4. V2_1    dry run -> CONNECT ID -> real run (links the workspace to Git, first commit)
  5. V2_2    backup (real); freeze = the moment this step started
  6. V2_3    snapshot (real); the workspace must be READY
  7. V2_4    dry run -> DELETE ID -> real run (hard delete); checks the workspace is empty of migrated items
  8. V2_5    dry run -> PLAN ID -> real restore; FIX ID -> reference fix; a last dry run must show nothing left
  9. V2_6    dry run -> DATA ID -> real data load; must end "ALL DATA VERIFIED"
 10. compare items before (after V2_T) and after (V2_6), by type and name
 11. report  agent/e2e/<id>/E2E_REPORT.md (+ .json): GREEN only if every check passes

Safety:
  * Real runs (including the delete) happen ONLY in the rehearsal workspace: one this rehearsal created (name
    starts with "E2E-REHEARSAL-", description carries the rehearsal ID) or a reused one listed in
    lab_config.E2E["REHEARSAL_WORKSPACE_IDS"] (checked at start and again before every real step). Before every real
    step the workspace is read again and checked; V2_4 to V2_6 also check that the snapshot manifest holds only
    that workspace. Inside the uploaded notebook the guard refuses any other WORKSPACE_IDS / TARGET_WORKSPACE_ID
    and any WORKSPACE_MAP.
  * The confirmation IDs come from the dry run of the same step, a few minutes earlier, never from the command line.
  * The runner workspace (where the AGENT_* notebooks live) must be a different workspace.
  * It never deletes the rehearsal workspace by itself: `cleanup` does, only that workspace, only with --yes.
  * The ATM tenant move itself is not part of the rehearsal (it can't be simulated); the report says so.

Usage (from v2/, after `az login` as the person who runs the notebooks, Admin of the rehearsal workspace):
  python agent/e2e_rehearsal.py start --workspace <listed lab workspace> [--reset]      (runner from lab_config)
  python agent/e2e_rehearsal.py start --workspace <listed lab workspace> --copies 4 --large-mib 1024 \\
      --seed-prefix bkp_stress
  python agent/e2e_rehearsal.py start --capacity <capacity id> --runner-workspace <id> \\
      --member ServicePrincipal:<sp object id>:Contributor [--notebooks out_lab]
  python agent/e2e_rehearsal.py verify-build <rehearsal id> --notebooks out    (ship build = rehearsed code?)
  python agent/e2e_rehearsal.py resume <rehearsal id>
  python agent/e2e_rehearsal.py report <rehearsal id>
  python agent/e2e_rehearsal.py cleanup <rehearsal id> --yes
"""
import argparse, datetime as dt, glob, hashlib, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fabric_agent_run as far   # noqa: E402

V2 = os.path.dirname(HERE)
PREFIX = "E2E-REHEARSAL-"
NOTEBOOKS = {"V2_T": "V2_T_Create_Test_Items", "V2_0": "V2_0_Permission_Check",
             "V2_1": "V2_1_Connect_Workspaces_To_Git", "V2_2": "V2_2_Backup_Data", "V2_3": "V2_3_Git_Snapshot",
             "V2_4": "V2_4_Delete_Fabric_Items", "V2_5": "V2_5_Restore_From_Git", "V2_6": "V2_6_Restore_Data"}
STEPS = ["local", "ws", "V2_T", "baseline", "V2_0", "V2_1", "V2_2", "V2_3", "V2_4", "V2_5", "V2_6", "compare"]
GOOD = {"OK", "Dry run", "Skipped", "Unchanged", "Info", "Ready", "Not applicable", "LEFT ALONE", "PASS", "INFO"}
WARN = {"Warning", "Manual step", "Check", "WARN"}
KEPT_BY_V2_4 = {"SemanticModel", "Report", "Dashboard", "PaginatedReport", "Datamart"}   # Power BI items stay
STAGING_RE = re.compile(r"^Staging(Lakehouse|Warehouse)ForDataflows(_\d+)?$")
SUITES = ["test/tests.py", "test/backup_tests.py", "test/agent_tests.py", "test/testitems_tests.py",
          "test/e2e_tests.py"]
NOT_COVERED = [
    "The ATM tenant move itself (Microsoft runs it); the 24/48-hour wait after the delete is not simulated.",
    "Data volumes: the rehearsal uses small test data; time a large Lakehouse/Eventhouse separately.",
    "Item types and settings your organization has that the test items don't (V2_0 and the V2_3 inventory list them).",
    "Cross-region copy speed (fastcp) and ADX costs at production scale.",
]
SEED_TYPES = ("Lakehouse", "SQLEndpoint", "Warehouse", "Eventhouse", "KQLDatabase", "Notebook",
              "DataPipeline", "Dataflow", "DataAgent", "GraphModel", "Ontology")
SEED_SIZE_METRICS = {"Lakehouse": "active_parquet_bytes", "Warehouse": "active_parquet_bytes",
                     "Eventhouse": "compressed_extent_bytes"}


# ---------------------------------------------------------------- state
def folder(rid):
    return os.path.join(HERE, "e2e", rid)


def load(rid):
    p = os.path.join(folder(rid), "state.json")
    if not os.path.exists(p):
        raise SystemExit(f"No rehearsal {rid} (looked for {p})")
    return json.load(open(p, encoding="utf-8"))


def save(st):
    os.makedirs(folder(st["id"]), exist_ok=True)
    tmp = os.path.join(folder(st["id"]), "state.json.tmp")
    json.dump(st, open(tmp, "w", encoding="utf-8"), indent=2, default=str)
    os.replace(tmp, os.path.join(folder(st["id"]), "state.json"))


def now():
    return dt.datetime.now(dt.timezone.utc)


def record(st, step, ok, checks, **extra):
    """checks: [(name, "PASS"|"WARN"|"FAIL", detail)]"""
    st["steps"][step] = {"ok": ok, "finishedUtc": now().isoformat(), "checks": checks, **extra}
    save(st)
    for name, res, detail in checks:
        print(f"  [{res}] {step} / {name}: {detail}")
    return ok


# ---------------------------------------------------------------- Fabric helpers (as the az login user)
def workspace(ws_id):
    return far.call("GET", f"/workspaces/{ws_id}", ok=(200,)).json()


def items(ws_id):
    out, url = [], f"/workspaces/{ws_id}/items"
    while url:
        body = far.call("GET", url, ok=(200,)).json()
        out += body.get("value", [])
        url = body.get("continuationUri")
    return [{"id": i["id"], "type": i["type"], "displayName": i["displayName"]} for i in out]


def assert_rehearsal_workspace(st):
    """Refuse anything real unless the workspace is the one this rehearsal created."""
    ws_id = st.get("workspace")
    if not ws_id:
        raise SystemExit("REHEARSAL GUARD: no rehearsal workspace recorded")
    if ws_id.lower() == st["runner"].lower():
        raise SystemExit("REHEARSAL GUARD: the runner workspace can't be the rehearsal workspace")
    w = workspace(ws_id)
    if st.get("reused"):
        listed = {x.lower() for x in lab_e2e().get("REHEARSAL_WORKSPACE_IDS", [])}
        if ws_id.lower() not in listed or ws_id.lower() not in {x.lower() for x in st.get("allowlist", [])}:
            raise SystemExit(f"REHEARSAL GUARD: workspace {ws_id} isn't in lab_config.E2E['REHEARSAL_WORKSPACE_IDS']. "
                             "Stopping.")
        return w
    if not w.get("displayName", "").startswith(PREFIX) or st["id"] not in (w.get("description") or ""):
        raise SystemExit(f"REHEARSAL GUARD: workspace {ws_id} ({w.get('displayName')}) wasn't created by rehearsal "
                         f"{st['id']} (name must start with {PREFIX} and the description carry the ID). Stopping.")
    return w


def lab_e2e():
    sys.path.insert(0, V2)
    try:
        import importlib, lab_config
        importlib.reload(lab_config)
        return getattr(lab_config, "E2E", {}) or {}
    except ImportError:
        return {}


def git_state(ws_id):
    return far.call("GET", f"/workspaces/{ws_id}/git/connection", ok=(200,)).json().get("gitConnectionState")


RESET_ORDER = ["DataPipeline", "Notebook", "Dataflow", "DataAgent", "Ontology", "GraphModel", "KQLQueryset",
               "KQLDashboard", "KQLDatabase", "Eventhouse", "Warehouse", "Lakehouse"]


def to_wipe(ws_id):
    return [i for i in items(ws_id) if i["type"] not in KEPT_BY_V2_4 and i["type"] != "SQLEndpoint"]


def reset_workspace(st):
    """Reused lab workspace only: Git disconnect, then hard delete every non-Power BI item. Returns check rows."""
    assert_rehearsal_workspace(st)
    ws, checks = st["workspace"], []
    if git_state(ws) not in (None, "NotConnected"):
        far.call("POST", f"/workspaces/{ws}/git/disconnect", ok=(200,))
        checks.append(("Git disconnected", "PASS", "the rehearsal links it again to a fresh folder"))
    victims = sorted(to_wipe(ws), key=lambda i: RESET_ORDER.index(i["type"]) if i["type"] in RESET_ORDER else -1)
    for i in victims:
        far.call("DELETE", f"/workspaces/{ws}/items/{i['id']}?hardDelete=true", ok=(200, 204, 404))
    for _ in range(40):
        left = to_wipe(ws)
        if not left:
            break
        time.sleep(15)
    checks.append(("workspace wiped", "PASS" if not left else "FAIL",
                   f"{len(victims)} item(s) hard-deleted" + (f"; still there: {[(i['type'], i['displayName']) for i in left]}"
                                                            if left else "")))
    return checks


def assert_manifest(st, snapshot):
    path = f"{st['adls'][2]}/_migration/{snapshot}/manifest.json"
    text = far.read_adls(st["adls"], path)
    if text is None:
        raise SystemExit(f"REHEARSAL GUARD: snapshot manifest not readable: {path}")
    ids = [w["id"].lower() for w in json.loads(text).get("workspaces", [])]
    if ids != [st["workspace"].lower()]:
        raise SystemExit(f"REHEARSAL GUARD: snapshot {snapshot} holds workspaces {ids}, not only the rehearsal "
                         f"workspace {st['workspace']}. Stopping before anything is deleted or restored.")


# ---------------------------------------------------------------- running a notebook
def run_nb(st, key, overrides=None, confirm=None, real=True):
    """Run one notebook as AGENT_<name> in the runner workspace, scoped to the rehearsal workspace.
    Returns (ok, facts). The transcript is saved under agent/runs/<run id>/ and listed in the state."""
    name = NOTEBOOKS[key]
    path = os.path.join(st["notebooks"], name + ".ipynb")
    ov = dict(overrides or {})
    for k in ov:
        if far.NEVER_SET.match(k):
            raise SystemExit(f"REHEARSAL: {k} can't be an override (confirmations come from the dry run)")
    for k in (confirm or {}):
        if not (k.startswith("CONFIRM_") or k == "FIX_STALE_REFERENCES"):
            raise SystemExit(f"REHEARSAL: {k} isn't a confirmation setting")
    if "DRY_RUN" not in ov and key not in ("V2_T", "V2_0"):
        ov["DRY_RUN"] = not real
    if real:
        assert_rehearsal_workspace(st)
    st["runCount"] = st.get("runCount", 0) + 1     # unique per run, also within the same second
    run_id = f"{now().strftime('%Y%m%dT%H%M%SZ')}-e2e{st['id'][-6:]}-{st['runCount']:02d}-{key}{'' if real else '-dry'}"
    nb_name, nb = far.wrap(path, ov, True, run_id, list(st["adls"]),
                           e2e={"workspace": st["workspace"], "confirm": confirm or {}})
    mode = "REAL" if real else "dry run"
    print(f"\n[e2e] {key} {mode}: {nb_name} overrides={sorted(ov)} confirm={sorted(confirm or {})}", flush=True)
    item_id, _ = far.upsert_notebook(st["runner"], "AGENT_" + nb_name, nb)
    started = time.time()
    job = far.run_job(st["runner"], item_id, st.get("timeout", 5400))
    text, status, local = far.fetch_transcript(tuple(st["adls"]), run_id, nb_name)
    st.setdefault("transcripts", []).append({"step": key, "mode": mode, "runId": run_id,
                                             "file": os.path.join(local, nb_name + ".txt"),
                                             "minutes": round((time.time() - started) / 60, 1)})
    save(st)
    ok = job.get("status") == "Completed" and bool(status) and status.get("status") == "completed"
    ran = ((status or {}).get("facts") or {}).get("BUILD_ID")
    want = (st.get("fingerprint") or {}).get(nb_name)
    if ok and ran and want and ran != want:
        print(f"[e2e] {key}: the notebook that ran is build {ran}, expected {want}")
        ok = False
    if not ok:
        tail = (text or "")[-3000:]
        print(f"[e2e] {key} {mode} did not complete: job {job.get('status')}, notebook "
              f"{(status or {}).get('status')}, error {(status or {}).get('error')}\n{tail}")
    return ok, (status or {}).get("facts") or {}


def table_check(facts, var, label):
    """PASS/WARN/FAIL from the status counts of a log table the notebook left (RESULTS, CHECK_DF, ...)."""
    t = facts.get(var)
    if not isinstance(t, dict):
        return (label, "FAIL", f"{var} not reported by the notebook")
    counts = t.get("statusCounts") or {}
    bad = {k: v for k, v in counts.items() if k not in GOOD and k not in WARN}
    warn = {k: v for k, v in counts.items() if k in WARN}
    first = "; ".join(f"{p.get('Item') or p.get('Check') or ''} {p.get('Step') or ''}: "
                      f"{p.get('Status') or p.get('Result')} - {p.get('Detail', '')}"
                      for p in (t.get("problems") or []))
    if bad:
        return (label, "FAIL", f"{counts} | {first}")
    if warn:
        return (label, "WARN", f"{counts} | {first}")
    return (label, "PASS", f"{t.get('rows', 0)} rows: {counts}")


def overall(checks):
    return all(c[1] != "FAIL" for c in checks)


# ---------------------------------------------------------------- steps
def step_local(st):
    if st.get("skip_local"):
        return record(st, "local", False, [("local test suites", "FAIL", "skipped with --skip-local-tests: "
                                            "no green light without them")])
    checks = []
    for suite in SUITES:
        t0 = time.time()
        p = subprocess.run([sys.executable, suite], cwd=V2, capture_output=True, text=True, timeout=1800)
        last = (p.stdout.strip().splitlines() or [""])[-1]
        ok = p.returncode == 0 and last.strip() == "RESULT: ALL PASSED"
        checks.append((suite, "PASS" if ok else "FAIL", f"{last.strip() or p.stderr.strip()[-300:]} "
                       f"({time.time() - t0:.0f} s)"))
    return record(st, "local", overall(checks), checks)


def step_ws(st):
    if st.get("reused"):
        w = assert_rehearsal_workspace(st)
        st["workspaceName"] = w["displayName"]
        save(st)
        busy = to_wipe(st["workspace"])
        linked = git_state(st["workspace"]) not in (None, "NotConnected")
        if (busy or linked) and not st.get("reset"):
            return record(st, "ws", False, [("reused workspace is clean", "FAIL",
                f"{w['displayName']}: linked to Git: {linked}; {len(busy)} Fabric item(s): "
                + ", ".join(f"{i['type']} {i['displayName']}" for i in busy[:40])
                + ". Start again with --reset to disconnect Git and hard-delete them (ask the owner first).")])
        checks = reset_workspace(st) if (busy or linked) else []
        checks.append(("rehearsal workspace", "PASS", f"reused {w['displayName']} ({st['workspace']})"))
        return record(st, "ws", overall(checks), checks)
    if st.get("workspace"):
        assert_rehearsal_workspace(st)
        return record(st, "ws", True, [("rehearsal workspace", "PASS", f"{st['workspaceName']} ({st['workspace']}) "
                                        "already created")])
    name = PREFIX + st["id"]
    body = {"displayName": name, "description": f"Disposable workspace of migration rehearsal {st['id']}. "
            "Created and deleted by agent/e2e_rehearsal.py.", "capacityId": st["capacity"]}
    w = far.call("POST", "/workspaces", json=body, ok=(200, 201)).json()
    st["workspace"], st["workspaceName"] = w["id"], name
    save(st)
    checks = [("rehearsal workspace", "PASS", f"created {name} ({w['id']}) on capacity {st['capacity']}")]
    for m in st.get("members", []):
        typ, pid, role = m.split(":")
        far.call("POST", f"/workspaces/{w['id']}/roleAssignments", ok=(200, 201),
                 json={"principal": {"id": pid, "type": typ}, "role": role})
        checks.append(("member", "PASS", f"{typ} {pid} as {role}"))
    return record(st, "ws", True, checks)


def seed_prefixes(st):
    base, copies = st.get("seed_prefix", "bkp_test"), st.get("copies", 1)
    return [base] if copies == 1 else [f"{base}_{i:02d}" for i in range(1, copies + 1)]


def seed_metric_checks(prefix, ids, metrics, target, large_target):
    if not isinstance(ids, dict) or not isinstance(metrics, dict):
        return [(f"{prefix} stored-size evidence", "FAIL", "IDS or SEED_METRICS was not reported")]
    checks = []
    for kind, size_metric in SEED_SIZE_METRICS.items():
        m = metrics.get(kind)
        related = [ids.get(kind)]
        if kind == "Eventhouse":
            related.append(ids.get("KQLDatabase"))
        allowed_ids = {i for i in related if isinstance(i, str) and i}
        valid = (isinstance(m, dict) and m.get("status") == "OK" and m.get("sizeMetric") == size_metric
                 and isinstance(m.get("itemId"), str) and m["itemId"] in allowed_ids
                 and isinstance(m.get("table"), str) and bool(m["table"])
                 and type(m.get("rows")) is int and m["rows"] > 0
                 and type(m.get("storedBytes")) is int and m["storedBytes"] > 0
                 and type(m.get("targetBytes")) is int and m["targetBytes"] == target)
        if not valid:
            checks.append((f"{prefix} {kind} stored bytes", "FAIL",
                           f"missing/invalid measured rows, bytes, item identity or physical-size metric: {m}"))
            continue
        stored = m["storedBytes"]
        if target:
            within = target * 95 <= stored * 100 <= target * 110
            detail = f"{stored} stored bytes; target {target}; required 95%-110%; metric {size_metric}"
        else:
            within = not large_target or stored * 10 < large_target
            detail = (f"{stored} stored bytes in small copy; must be below 10% of {large_target}; metric {size_metric}"
                      if large_target else f"{stored} stored bytes measured; metric {size_metric}")
        checks.append((f"{prefix} {kind} stored bytes", "PASS" if within else "FAIL", detail))
    return checks


def step_v2t(st):
    checks, saved = [], st.setdefault("seedCopies", {})
    large_target = st.get("large_mib", 0) * 1024 ** 2
    stress = st.get("copies", 1) > 1 or bool(large_target)
    for i, prefix in enumerate(seed_prefixes(st)):
        earlier = saved.get(prefix)
        if earlier and earlier.get("ok"):
            checks += earlier["checks"]
            continue
        overrides = {"TARGET_WORKSPACE_ID": st["workspace"]}
        if stress or prefix != "bkp_test":
            overrides["PREFIX"] = prefix
        target = large_target if i == 0 else 0
        if stress:
            overrides.update(LARGE_DATA_MIB=st.get("large_mib", 0) if i == 0 else 0, MEASURE_DATA_SIZE=True)
            if st.get("isolated_stress_runtime"):
                overrides["ISOLATED_STRESS_RUNTIME"] = True
        ok, f = run_nb(st, "V2_T", overrides)
        failed = f.get("failed") if isinstance(f.get("failed"), dict) else {"rows": None}
        label = f"{prefix} " if stress else ""
        current = [(label + "V2_T completed", "PASS" if ok else "FAIL",
                    "test items created" if ok else "see transcript"),
                   (label + "V2_T steps", "PASS" if failed.get("rows") == 0 else "FAIL",
                    "all steps succeeded" if failed.get("rows") == 0 else
                    f"{failed.get('rows')} step(s) need attention: "
                    + "; ".join(str(r) for r in (failed.get("records") or [])))]
        if stress:
            current += seed_metric_checks(prefix, f.get("IDS"), f.get("SEED_METRICS"), target, large_target)
        saved[prefix] = {"ok": overall(current), "checks": current,
                         "ids": f.get("IDS") or {}, "metrics": f.get("SEED_METRICS") or {}}
        save(st)
        checks += current
        if not overall(current):
            return record(st, "V2_T", False, checks)
    return record(st, "V2_T", overall(checks), checks)


def step_baseline(st):
    before = [i for i in items(st["workspace"])]
    st["before"] = before
    types = sorted({i["type"] for i in before})
    checks = [("items before", "PASS" if before else "FAIL", f"{len(before)} items: {', '.join(types)}")]
    if st.get("copies", 1) > 1 or st.get("large_mib", 0):
        by_id, explicit = {i["id"]: i for i in before}, []
        for prefix in seed_prefixes(st):
            ids = (st.get("seedCopies", {}).get(prefix) or {}).get("ids") or {}
            missing = []
            for kind in SEED_TYPES:
                iid = ids.get(kind)
                if not isinstance(iid, str) or by_id.get(iid, {}).get("type") != kind:
                    missing.append(kind)
                else:
                    explicit.append(iid)
            checks.append((f"{prefix} explicit item types", "FAIL" if missing else "PASS",
                           f"missing or wrong type: {missing}" if missing else "all 11 explicit item types present"))
        expected = st.get("copies", 1) * len(SEED_TYPES)
        distinct = len(set(explicit))
        checks.append(("independent copies", "PASS" if len(explicit) == distinct == expected else "FAIL",
                       f"{distinct} distinct explicit item IDs; expected {expected}; system-managed extras do not count"))
    return record(st, "baseline", overall(checks), checks)


def ws_settings(st):
    return {"WORKSPACE_IDS": [st["workspace"]]}


def step_v20(st):
    ok, f = run_nb(st, "V2_0", ws_settings(st))
    checks = [("V2_0 completed", "PASS" if ok else "FAIL", ""), table_check(f, "CHECK_DF", "permission checks")]
    return record(st, "V2_0", overall(checks), checks)


def dry_then_real(st, key, id_var, confirm_var, overrides):
    ok, f = run_nb(st, key, overrides, real=False)
    if not ok or not f.get(id_var):
        return False, f, [(f"{key} dry run", "FAIL", f"no {id_var}" if ok else "dry run failed, see transcript")]
    checks = [(f"{key} dry run", "PASS", f"{id_var} {f[id_var]}")]
    ok, f2 = run_nb(st, key, overrides, confirm={confirm_var: f[id_var]}, real=True)
    checks.append((f"{key} real run", "PASS" if ok else "FAIL", "completed" if ok else "see transcript"))
    return ok, f2, checks


def step_v21(st):
    folder_ = f"/e2e-rehearsals/{st['id']}/{st['workspaceName']}"     # a fresh, empty Git folder per rehearsal
    ok, f, checks = dry_then_real(st, "V2_1", "CONNECT_ID", "CONFIRM_CONNECT_ID",
                                  {**ws_settings(st), "DIRECTORY_MAP": {st["workspace"]: folder_}})
    if ok:
        checks.append(table_check(f, "RESULTS", "connect log"))
    return record(st, "V2_1", overall(checks), checks)


def step_v22(st):
    st["freeze"] = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    save(st)
    ok, f = run_nb(st, "V2_2", {**ws_settings(st), "FREEZE_STARTED_UTC": st["freeze"]})
    st["backupRun"] = f.get("RUN_ID")
    checks = [("V2_2 completed", "PASS" if ok and st["backupRun"] else "FAIL", f"run {st['backupRun']}"),
              table_check(f, "RESULTS", "backup log")]
    return record(st, "V2_2", overall(checks), checks)


def step_v23(st):
    ok, f = run_nb(st, "V2_3", {**ws_settings(st), "SECONDARY_BACKUP_RUN_ID": st.get("backupRun") or "",
                                "FREEZE_STARTED_UTC": st.get("freeze") or ""})
    st["snapshot"] = f.get("SNAPSHOT_ID")
    ready, blocked = f.get("ready") or [], f.get("blocked") or []
    checks = [("V2_3 completed", "PASS" if ok and st["snapshot"] else "FAIL", f"snapshot {st['snapshot']}"),
              ("workspace READY", "PASS" if ready == [st["workspaceName"]] and not blocked else "FAIL",
               f"READY {ready}, BLOCKED {blocked}"),
              table_check(f, "RESULTS", "snapshot log")]
    return record(st, "V2_3", overall(checks), checks)


def step_v24(st):
    assert_manifest(st, st["snapshot"])
    ok, f, checks = dry_then_real(st, "V2_4", "DELETE_ID", "CONFIRM_DELETE_ID",
                                  {"SNAPSHOT_ID": st["snapshot"], **ws_settings(st)})
    if ok:
        checks.append(table_check(f, "RESULTS", "delete log"))
        left = [i for i in items(st["workspace"]) if i["type"] not in KEPT_BY_V2_4 and i["type"] != "SQLEndpoint"
                and not STAGING_RE.match(i["displayName"])]
        checks.append(("workspace empty of migrated items", "PASS" if not left else "FAIL",
                       "nothing left" if not left else f"still there: {[(i['type'], i['displayName']) for i in left]}"))
    return record(st, "V2_4", overall(checks), checks)


def step_v25(st):
    assert_manifest(st, st["snapshot"])
    snap = {"SNAPSHOT_ID": st["snapshot"]}
    ok, f, checks = dry_then_real(st, "V2_5", "PLAN_ID", "CONFIRM_PLAN_ID", snap)
    if not ok:
        return record(st, "V2_5", False, checks)
    checks.append(table_check(f, "RESULTS", "restore log"))
    ok, f = run_nb(st, "V2_5", snap, real=False)            # what's left: references to fix
    refs = (f.get("REF_DF") or {}).get("records") or []
    auto = [r for r in refs if r.get("Fix") != "manual"]
    if ok and auto and f.get("FIX_ID"):
        checks.append(("references found", "PASS", f"{len(auto)} automatic, FIX ID {f['FIX_ID']}"))
        ok2, _ = run_nb(st, "V2_5", snap, confirm={"FIX_STALE_REFERENCES": True, "CONFIRM_FIX_ID": f["FIX_ID"],
                                                    "CONFIRM_PLAN_ID": f.get("PLAN_ID", "")}, real=True)
        checks.append(("reference fix", "PASS" if ok2 else "FAIL", "applied" if ok2 else "see transcript"))
        ok, f = run_nb(st, "V2_5", snap, real=False)
    refs = (f.get("REF_DF") or {}).get("records") or []
    manual = [r for r in refs if r.get("Fix") == "manual"]
    left = [r for r in refs if r.get("Fix") != "manual"]
    checks.append(("final V2_5 dry run", "PASS" if ok and not f.get("TODO_count") and not left else "FAIL",
                   f"items still to restore: {f.get('TODO_count')}, automatic references left: {len(left)}"))
    if manual:
        checks.append(("manual references", "WARN", f"{len(manual)}: " + "; ".join(
            f"{r.get('Item')} {r.get('Part')}" for r in manual[:5])))
    return record(st, "V2_5", overall(checks), checks)


def step_v26(st):
    assert_manifest(st, st["snapshot"])
    ok, f, checks = dry_then_real(st, "V2_6", "DATA_ID", "CONFIRM_DATA_ID", {"SNAPSHOT_ID": st["snapshot"]})
    if ok:
        att = f.get("attention") or {}
        blk = f.get("blocked") or []
        blk = blk.get("rows", 0) if isinstance(blk, dict) else len(blk)
        verified = att.get("rows") == 0 and not blk
        checks.append(("ALL DATA VERIFIED", "PASS" if verified else "FAIL",
                       "every loaded item matches its backup" if verified else
                       f"{att.get('rows')} row(s) need attention, {blk} blocked: "
                       + "; ".join(f"{p.get('Item')} {p.get('Step')}: {p.get('Status')} - {str(p.get('Detail'))[:160]}"
                                   for p in (att.get("problems") or att.get("records") or [])[:5])))
        checks.append(table_check(f, "RESULTS", "data log"))
    return record(st, "V2_6", overall(checks), checks)


def step_compare(st):
    key = lambda i: (i["type"], i["displayName"])
    ignore = lambda i: STAGING_RE.match(i["displayName"]) or i["displayName"].startswith("AGENT_")
    before = {key(i) for i in st.get("before", []) if not ignore(i)}
    after_items = items(st["workspace"])
    st["after"] = after_items
    after = {key(i) for i in after_items if not ignore(i)}
    missing, extra = sorted(before - after), sorted(after - before)
    checks = [("every item back", "PASS" if not missing else "FAIL",
               f"{len(before)} items before, {len(after)} after" + (f"; missing {missing}" if missing else "")),
              ("no unexpected items", "PASS" if not extra else "WARN", f"extra {extra}" if extra else "none")]
    return record(st, "compare", overall(checks), checks)


RUNNERS = {"local": step_local, "ws": step_ws, "V2_T": step_v2t, "baseline": step_baseline, "V2_0": step_v20,
           "V2_1": step_v21, "V2_2": step_v22, "V2_3": step_v23, "V2_4": step_v24, "V2_5": step_v25,
           "V2_6": step_v26, "compare": step_compare}


# ---------------------------------------------------------------- report
def build_fingerprint(nb_dir):
    """Build ID of each notebook (hash of all cells but the settings cell, stamped by build.py), so the lab build
    and the release build of the same code match. Notebooks without a stamp: SHA-256 of the file."""
    out = {}
    for p in sorted(glob.glob(os.path.join(nb_dir, "V2_*.ipynb"))):
        text = open(p, encoding="utf-8").read()
        m = re.search(r'BUILD_ID = \\"([0-9a-f]{12})\\"', text)
        out[os.path.basename(p)[:-6]] = m.group(1) if m else "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    return out


def verdict(st):
    rows = [(s, c) for s in STEPS for c in (st["steps"].get(s) or {}).get("checks", [])]
    missing = [s for s in STEPS if s not in st["steps"]]
    fails = [r for r in rows if r[1][1] == "FAIL"]
    warns = [r for r in rows if r[1][1] == "WARN"]
    changed = st.get("fingerprint") != build_fingerprint(st["notebooks"])
    green = not missing and not fails and not changed
    return green, missing, fails, warns, changed


def report(st):
    green, missing, fails, warns, changed = verdict(st)
    word = "GREEN" if green else "RED"
    L = [f"# Migration rehearsal {st['id']}: {word}", "",
         f"* Started {st['startedUtc']}; report {now().isoformat()}",
         f"* Rehearsal workspace: {st.get('workspaceName')} (`{st.get('workspace')}`), capacity `{st['capacity']}`",
         f"* Backup run `{st.get('backupRun')}`, snapshot `{st.get('snapshot')}`, freeze `{st.get('freeze')}`",
         f"* Notebooks: `{st['notebooks']}`" + (" - **changed since the rehearsal started: rerun it**" if changed else ""),
         f"* Rehearsal workspace {'reused (lab list)' if st.get('reused') else 'created for this rehearsal'}"
         + (", wiped first (--reset)" if st.get("reset") else ""),
         ""]
    if green:
        L += ["**GREEN:** the whole chain ran for real on this build: test items created, permissions checked, Git "
              "linked, data backed up, snapshot taken, items hard-deleted, restored from Git, data loaded and "
              "verified, and every item is back.", ""]
    else:
        L += ["**RED:** not ready to hand over. " + (f"Steps not run yet: {', '.join(missing)}. " if missing else "")
              + (f"{len(fails)} failed check(s), listed below." if fails else ""), ""]
    L += ["| Step | Check | Result | Detail |", "|---|---|---|---|"]
    for s in STEPS:
        for name, res, detail in (st["steps"].get(s) or {}).get("checks", []):
            text = str(detail).replace("|", "/").replace("\r\n", "\n").replace("\n", "<br>")
            L.append(f"| {s} | {name} | {res} | {text} |")
        if s not in st["steps"]:
            L.append(f"| {s} | - | NOT RUN | |")
    if warns:
        L += ["", "Warnings (don't block GREEN; review them):"] + [f"* {s} / {c[0]}: {c[2]}" for s, c in warns]
    limits = list(NOT_COVERED)
    stress = st.get("copies", 1) > 1 or st.get("large_mib", 0)
    if stress:
        L += ["", "## Seed profile",
              f"* Independent copies per explicit type: {st.get('copies', 1)}",
              f"* Large-data target per storage family in copy 1: {st.get('large_mib', 0)} MiB; other copies stay small.",
              "* Byte measurements refer to persisted data, not row-count or logical-size estimates.",
              "", "| Copy | Family | Rows | Stored bytes | Metric | Status |",
              "|---|---|---|---|---|---|"]
        for prefix, copy in sorted(st.get("seedCopies", {}).items()):
            for kind in SEED_SIZE_METRICS:
                m = copy.get("metrics", {}).get(kind) or {}
                L.append(f"| {prefix} | {kind} | {m.get('rows', 'not reported')} | "
                         f"{m.get('storedBytes', 'not reported')} | {m.get('sizeMetric', 'not reported')} | "
                         f"{m.get('status', 'not reported')} |")
        limits[1] = ("Data volumes: this run covers only its recorded seed profile, not unlimited volumes, "
                     "concurrent workloads or production throughput.")
    L += ["", "## Not covered by this rehearsal"] + [f"* {x}" for x in limits]
    L += ["", "## Build (the IDs each notebook prints when it runs; `verify-build` checks the release build)",
          "| Notebook | Build ID |", "|---|---|"] + \
         [f"| {k} | `{v}` |" for k, v in (st.get("fingerprint") or {}).items()]
    L += ["", "## Transcripts", "| Step | Mode | Minutes | File |", "|---|---|---|---|"] + \
         [f"| {t['step']} | {t['mode']} | {t['minutes']} | `{transcript_path(t['file'])}` |"
          for t in st.get("transcripts", [])]
    L += ["", f"Clean up when done: `python agent/e2e_rehearsal.py cleanup {st['id']} --yes` (deletes the rehearsal "
          "workspace only; the ADLS backup, the Git branches and any ADX database of this rehearsal stay)."]
    md = far.redact_sensitive_text("\n".join(L) + "\n")
    open(os.path.join(folder(st["id"]), "E2E_REPORT.md"), "w", encoding="utf-8").write(md)
    json.dump(far.redact_sensitive_data({"id": st["id"], "verdict": word, "missing": missing, "steps": st["steps"],
               "fingerprint": st.get("fingerprint"), "notCovered": limits,
               "seedProfile": {"copies": st.get("copies", 1), "largeMiB": st.get("large_mib", 0),
                               "prefix": st.get("seed_prefix", "bkp_test"), "results": st.get("seedCopies", {})}}),
              open(os.path.join(folder(st["id"]), "E2E_REPORT.json"), "w", encoding="utf-8"), indent=2, default=str)
    return green, md


def transcript_path(path):
    absolute = os.path.abspath(path)
    if os.path.splitdrive(absolute)[0].casefold() != os.path.splitdrive(V2)[0].casefold():
        return absolute
    return os.path.relpath(absolute, V2)


def drive(st):
    for s in STEPS:
        if (st["steps"].get(s) or {}).get("ok"):
            print(f"[e2e] {s}: done earlier")
            continue
        print(f"\n[e2e] ===== {s} =====", flush=True)
        if not RUNNERS[s](st):
            print(f"[e2e] STOPPED at {s}. Fix the cause, then: python agent/e2e_rehearsal.py resume {st['id']}")
            break
    green, md = report(st)
    print("\n" + md)
    print(f"[e2e] report: {os.path.join(folder(st['id']), 'E2E_REPORT.md')}")
    return 0 if green else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--capacity", help="capacity ID for a NEW rehearsal workspace")
    s.add_argument("--workspace", help="reuse this lab workspace (must be in lab_config.E2E REHEARSAL_WORKSPACE_IDS)")
    s.add_argument("--reset", action="store_true", help="with --workspace: disconnect Git and hard-delete its items first")
    s.add_argument("--runner-workspace", default=os.environ.get("FABRIC_RUNNER_WORKSPACE_ID"))
    s.add_argument("--member", action="append", default=None, metavar="TYPE:ID:ROLE",
                   help="e.g. ServicePrincipal:<object id>:Contributor (the Git / Kusto Copy app)")
    s.add_argument("--notebooks", default="out_lab", help="folder with the built notebooks (default out_lab)")
    s.add_argument("--adls", help="account/container/root for transcripts (default: lab_config.py)")
    s.add_argument("--timeout", type=int, default=5400)
    s.add_argument("--copies", type=int, default=1, help="independent sets of all supported seed types (default 1)")
    s.add_argument("--large-mib", type=int, default=0,
                   help="persisted-data MiB target for copy 1 of Lakehouse/Warehouse/Eventhouse; 0 = small seed")
    s.add_argument("--seed-prefix", default="bkp_test", help="seed name prefix; numbered when --copies exceeds 1")
    s.add_argument("--isolated-stress-runtime", action="store_true",
                   help="opt in to a hash-pinned Delta wheel in GB seed jobs only; no shared environment changes")
    s.add_argument("--skip-local-tests", action="store_true", help="faster, but the report can't be GREEN")
    for c in ("resume", "report"):
        sub.add_parser(c).add_argument("id")
    v = sub.add_parser("verify-build", help="is this build (e.g. out/) the code the rehearsal ran?")
    v.add_argument("id")
    v.add_argument("--notebooks", default="out")
    c = sub.add_parser("cleanup")
    c.add_argument("id")
    c.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "start":
        if a.copies < 1:
            raise SystemExit("--copies must be at least 1")
        if a.large_mib < 0:
            raise SystemExit("--large-mib must not be negative")
        if a.isolated_stress_runtime and not a.large_mib:
            raise SystemExit("--isolated-stress-runtime requires --large-mib above zero")
        if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", a.seed_prefix)
                or a.seed_prefix.upper().startswith("AGENT_")):
            raise SystemExit("--seed-prefix must be 1-40 ASCII letters/digits/underscores, start with a letter, "
                             "and not use the reserved AGENT_ prefix")
        lab = lab_e2e()
        a.runner_workspace = a.runner_workspace or lab.get("RUNNER_WORKSPACE_ID")
        a.member = a.member if a.member is not None else list(lab.get("MEMBERS", []))
        if not a.runner_workspace or not re.match(r"^[0-9a-fA-F-]{36}$", a.runner_workspace):
            raise SystemExit("--runner-workspace: the runner workspace ID (or FABRIC_RUNNER_WORKSPACE_ID, or "
                             "lab_config.E2E RUNNER_WORKSPACE_ID)")
        allow = list(lab.get("REHEARSAL_WORKSPACE_IDS", []))
        if a.workspace:
            if a.workspace.lower() not in {x.lower() for x in allow}:
                raise SystemExit(f"--workspace {a.workspace} isn't in lab_config.E2E['REHEARSAL_WORKSPACE_IDS'] "
                                 f"({allow}). Only workspaces the owner listed there can be reused.")
            if a.workspace.lower() == a.runner_workspace.lower():
                raise SystemExit("--workspace can't be the runner workspace")
        elif not a.capacity:
            raise SystemExit("--capacity (new workspace) or --workspace (reuse a listed lab workspace)")
        if a.reset and not a.workspace:
            raise SystemExit("--reset only with --workspace")
        for m in a.member:
            if not re.match(r"^(User|Group|ServicePrincipal|ServicePrincipalProfile):[0-9a-fA-F-]{36}:"
                            r"(Admin|Member|Contributor|Viewer)$", m):
                raise SystemExit(f"--member {m}: use TYPE:OBJECT-ID:ROLE")
        nb_dir = os.path.join(V2, a.notebooks) if not os.path.isabs(a.notebooks) else a.notebooks
        missing = [n for n in NOTEBOOKS.values() if not os.path.exists(os.path.join(nb_dir, n + ".ipynb"))]
        if missing:
            raise SystemExit(f"Not built in {nb_dir}: {missing}. Run python build.py {a.notebooks} --lab first.")
        adls = tuple(a.adls.split("/", 2)) if a.adls else far.lab_adls()
        if not adls or len(adls) != 3:
            raise SystemExit("Transcript location unknown: pass --adls account/container/root")
        rid = now().strftime("%Y%m%d-%H%M%S")
        st = {"id": rid, "startedUtc": now().isoformat(), "capacity": a.capacity, "runner": a.runner_workspace,
              "members": a.member, "notebooks": nb_dir, "adls": list(adls), "timeout": a.timeout,
              "skip_local": a.skip_local_tests, "fingerprint": build_fingerprint(nb_dir), "steps": {},
              "copies": a.copies, "large_mib": a.large_mib, "seed_prefix": a.seed_prefix,
              "isolated_stress_runtime": a.isolated_stress_runtime}
        if a.workspace:
            st.update(workspace=a.workspace.lower(), reused=True, reset=a.reset, allowlist=allow)
        save(st)
        print(f"[e2e] rehearsal {rid} (state: {folder(rid)})")
        return drive(st)
    st = load(a.id)
    if a.cmd == "resume":
        if st.get("fingerprint") != build_fingerprint(st["notebooks"]):
            print("[e2e] the notebooks changed since this rehearsal started: the report will be RED. "
                  "Start a new rehearsal for a green light on the new build.")
        return drive(st)
    if a.cmd == "verify-build":
        nb_dir = os.path.join(V2, a.notebooks) if not os.path.isabs(a.notebooks) else a.notebooks
        have, want = build_fingerprint(nb_dir), st.get("fingerprint") or {}
        diff = sorted(k for k in set(have) | set(want) if have.get(k) != want.get(k))
        green = verdict(st)[0]
        markers = [m.lower() for m in lab_e2e().get("LAB_MARKERS", [])]
        leaks = []
        for p in sorted(glob.glob(os.path.join(nb_dir, "*"))):
            if os.path.isfile(p) and p.endswith((".ipynb", ".md", ".txt", ".py")):
                low = open(p, encoding="utf-8", errors="replace").read().lower()
                leaks += [f"{os.path.basename(p)}: {m}" for m in markers if m in low]
        print(f"rehearsal {st['id']}: {'GREEN' if green else 'NOT GREEN'}; {nb_dir}: "
              + ("same code as rehearsed" if not diff else f"DIFFERENT: {diff}")
              + ("; no lab values" if not leaks else f"; LAB VALUES FOUND: {leaks[:20]}"))
        return 0 if green and not diff and not leaks else 1
    if a.cmd == "report":
        green, md = report(st)
        print(md)
        return 0 if green else 1
    if a.cmd == "cleanup":
        if not a.yes:
            raise SystemExit("cleanup deletes the rehearsal workspace: add --yes")
        if st.get("reused"):
            raise SystemExit("This rehearsal reused a lab workspace: it isn't deleted. The next rehearsal wipes it "
                             "with start --workspace <id> --reset.")
        w = assert_rehearsal_workspace(st)
        far.call("DELETE", f"/workspaces/{st['workspace']}", ok=(200, 204))
        st["cleanedUtc"] = now().isoformat()
        save(st)
        print(f"[e2e] deleted rehearsal workspace {w['displayName']} ({st['workspace']})")
        return 0


if __name__ == "__main__":
    sys.exit(main())
