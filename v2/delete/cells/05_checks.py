missing = [n for n in ("SNAPSHOT_ID", "ADLS_ACCOUNT", "KEY_VAULT_URL") if "<" in str(globals()[n])] + ado_auth_missing()
if missing:
    raise ValueError("Fill in these settings first: " + ", ".join(missing))
MANIFEST = load_manifest(SNAPSHOT_ID)
PREFIX = migration_prefix(SNAPSHOT_ID)
age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(MANIFEST["createdUtc"])).total_seconds() / 3600
if age_h > MAX_SNAPSHOT_AGE_HOURS:
    raise RuntimeError(f"The snapshot is {age_h:.1f} hours old (limit {MAX_SNAPSHOT_AGE_HOURS}). Take a new snapshot.")
try:
    STATE = json.loads(adls_read(f"{PREFIX}/delete_state.json"))
except HttpError as exc:
    if exc.status != 404:
        raise
    STATE = {"disconnected": [], "deleted": []}

wanted = {norm_id(w) for w in WORKSPACE_IDS}
if None in wanted:
    raise ValueError("WORKSPACE_IDS must contain workspace IDs (GUIDs).")
HERE_WS = current_workspace_id()
if HERE_WS and any(w.get("ready") and w["id"].lower() == HERE_WS and (not wanted or w["id"] in wanted)
                   for w in MANIFEST["workspaces"]):
    raise RuntimeError("Stopped: this notebook runs inside a workspace it would clear. " + RUN_FROM_ELSEWHERE)
TARGETS, rows = [], []
sec = MANIFEST["secondaryBackup"]
backup_ok = adls_exists(f"{sec['root'].strip('/')}/{sec['runId']}/_run_completed.json")
RAW = MANIFEST.get("rawCopy") or {}
raw_ok = not RAW.get("enabled") or adls_exists(f"{PREFIX}/raw/_run_completed.json")
BACKUP_LOG = pd.DataFrame(columns=["ItemId", "Step", "Status"])
if backup_ok:
    import io
    BACKUP_LOG = pd.read_csv(io.BytesIO(adls_read(f"{sec['root'].strip('/')}/{sec['runId']}/backup_log.csv")),
                             dtype=str).fillna("")


def data_backup_problems(mws):
    """Every Lakehouse, Warehouse and KQL database must still have its data in the backup run, as the backup logged."""
    out, folder = [], mws.get("secondaryFolder") or ""
    dirs = {d.rsplit("__", 1)[-1].lower(): d for d, _, is_dir in (adls_list(folder, recursive=False) if folder else [])
            if is_dir}
    for i in mws["items"]:
        if i["type"] not in DATA_TYPES or is_dataflow_staging(i):
            continue

        def status_of(s, i=i):
            r = BACKUP_LOG[(BACKUP_LOG.ItemId.str.lower() == i["id"].lower()) & (BACKUP_LOG.Step == s)]
            return r.iloc[-1].Status if len(r) else ""
        step, status = data_step(i["type"], status_of)
        status = status or "not recorded"
        d = dirs.get(i["id"].lower())
        if status != "OK":
            out.append(f"{i['displayName']} ({i['type']}): data backup was {status}")
        elif not d:
            out.append(f"{i['displayName']} ({i['type']}): its backup folder is missing")
        elif step == KQL_ADX_STEP and not adls_exists(f"{folder}/{d}/kusto_copy/_destination.json"):
            out.append(f"{i['displayName']} ({i['type']}): its rows are in ADX but kusto_copy/_destination.json is "
                       "missing in the backup (V2_6 needs it to find them)")
        elif i["type"] in ("Lakehouse", "Warehouse") and not adls_exists(f"{folder}/{d}/data_manifest.csv"):
            out.append(f"{i['displayName']} ({i['type']}): data_manifest.csv is missing in the backup")
    return out


def changed_since_raw(mws, items, only=None):
    """Each item's live definition must equal the raw copy taken with the snapshot (workspaces without Git; on the
    Git route, the items Git doesn't hold)."""
    out, present = [], {i["id"] for i in items}
    for i in mws["items"]:
        if i["type"] in CHILD_TYPES or i["id"] not in present or is_dataflow_staging(i) or (only and not only(i)):
            continue
        if not i.get("rawHash"):
            out.append(f"{i['displayName']} ({i['type']}): no raw copy fingerprint in the manifest")
            continue
        d = fabric("POST", f"/workspaces/{mws['id']}/items/{i['id']}/getDefinition").get("definition") or {}
        if definition_hash(d.get("parts") or []) != i["rawHash"]:
            out.append(f"{i['displayName']} ({i['type']}): its definition changed since the snapshot: take a new "
                       "snapshot")
    return out


DELETE_ORDER = ["DataAgent", "Ontology", "GraphModel", "DataPipeline", "Dataflow", "Notebook"]
TAIL_ORDER = ["Warehouse", "KQLDatabase", "Eventhouse", "Lakehouse"]


def order_key(t):
    if t in DELETE_ORDER:
        return DELETE_ORDER.index(t)
    if t in TAIL_ORDER:
        return 100 + TAIL_ORDER.index(t)
    return 50                                   # acknowledged other types: between consumers and storage


for mws in MANIFEST["workspaces"]:
    if wanted and mws["id"] not in wanted:
        continue
    name, ws_id, problems = mws["name"], mws["id"], []
    if not mws["ready"]:
        if wanted:
            problems.append("BLOCKED in the manifest: " + (" | ".join(mws["blockers"]) or "see items.csv"))
        else:
            print(f"{name}: skipped (BLOCKED in the manifest)")
            continue
    if not backup_ok:
        problems.append(f"secondary backup run {sec['runId']} is missing in ADLS")
    if backup_ok:
        problems += data_backup_problems(mws)
    if not raw_ok:
        problems.append("the raw copy (raw/_run_completed.json) is missing in ADLS")
    no_git = mws.get("gitRoute") == "none"
    git, state, items = mws["git"], None, []
    if no_git:
        if not RAW.get("enabled"):
            problems.append("workspace without Git, but the snapshot has no raw copy to restore it from")
        try:
            state = git_connection(ws_id).get("gitConnectionState")
            items = workspace_items(ws_id)
            problems += changed_since_raw(mws, items)
        except HttpError as exc:
            problems.append(f"can't read the workspace or its item definitions ({git_fail_text(exc, ws_id)})")
    else:
        if RAW.get("enabled") and not (mws.get("gitMirror") and adls_exists(f"{mws['gitMirror']}/_commit.json")):
            problems.append("the file copy of the snapshot commit is missing in ADLS")
        try:
            ref = ado_ref(git, git["snapshotBranch"]) if git.get("snapshotBranch") else None
            if not ref or ref.get("objectId") != git["commit"]:
                problems.append(f"snapshot branch {git.get('snapshotBranch')} is missing or no longer at {git['commit']}")
        except HttpError as exc:
            problems.append(f"can't check the snapshot branch ({exc.short()})")
        try:
            conn = git_connection(ws_id)
            state = conn.get("gitConnectionState")
            if state == "NotConnected":
                if ws_id not in STATE["disconnected"]:
                    problems.append("Git is disconnected, but not by this notebook: can't prove nothing changed since "
                                    "the snapshot")
            else:
                gp = conn.get("gitProviderDetails") or {}
                if (gp.get("repositoryName"), gp.get("branchName"), gp.get("directoryName")) != \
                        (git["repositoryName"], git["branchName"], git["directoryName"]):
                    problems.append("the Git connection changed since the snapshot")
                st = git_status(ws_id)
                if st.get("changes"):
                    problems.append(f"{len(st['changes'])} Git change(s) since the snapshot: take a new snapshot")
                # The shared branch may have moved (other workspaces' folders); this workspace must still be at its
                # snapshot commit, and its folder must be unchanged in the snapshot commit.
                if st.get("workspaceHead") not in (git["commit"], git.get("workspaceCommit")):
                    problems.append(f"the workspace moved since the snapshot (now at {st.get('workspaceHead')})")
            items = workspace_items(ws_id)
            if any(adls_only(i) for i in mws["items"]):
                if not RAW.get("enabled"):
                    problems.append("items outside Git are restored from the raw copy, but the snapshot has none")
                else:
                    problems += changed_since_raw(mws, items, only=adls_only)
        except HttpError as exc:
            problems.append(f"can't read the workspace or its Git status ({git_fail_text(exc, ws_id)})")
    known = {i["id"] for i in mws["items"]}
    new = [i for i in items if i["type"] not in POWER_BI_TYPES and i["id"] not in known]
    late_staging = [i for i in new if is_dataflow_staging(i)]     # a Dataflow Gen2 ran after the snapshot
    new = [i for i in new if not is_dataflow_staging(i)]
    if new:
        problems.append("created after the snapshot: " + ", ".join(f"{i['displayName']} ({i['type']})" for i in new))
    for p in problems:
        log(name, {}, "Check", "Blocked", p)
    present = {i["id"] for i in items}
    for i in sorted(mws["items"], key=lambda x: (order_key(x["type"]), x["displayName"].lower())):
        if i["type"] in CHILD_TYPES:
            action = "goes with its parent"
        elif i["id"] in present:
            action = "delete"
        else:
            action = "already gone"
        rows.append({"Workspace": name, "WorkspaceId": ws_id, "Item": i["displayName"], "Type": i["type"],
                     "ItemId": i["id"], "Action": action if not problems else "blocked", "Recovery": i.get("recovery")})
    for i in late_staging:
        rows.append({"Workspace": name, "WorkspaceId": ws_id, "Item": i["displayName"], "Type": i["type"],
                     "ItemId": i["id"], "Action": ("goes with its parent" if i["type"] in CHILD_TYPES else "delete")
                     if not problems else "blocked",
                     "Recovery": "Dataflow Gen2 staging created after the snapshot: Fabric recreates it"})
    TARGETS.append({"id": ws_id, "name": name, "problems": problems, "connected": state not in (None, "NotConnected")})

PLAN_DF = pd.DataFrame(rows, columns=["Workspace", "WorkspaceId", "Item", "Type", "ItemId", "Action", "Recovery"])
show(PLAN_DF.drop(columns=["WorkspaceId"]), rows=1000)
blocked = [t["name"] for t in TARGETS if t["problems"]]
if blocked:
    print("BLOCKED, nothing will be deleted in: " + ", ".join(blocked))
DELETE_ID = plan_hash({"snapshot": SNAPSHOT_ID, "hard": bool(HARD_DELETE), "rows": PLAN_DF[["WorkspaceId", "ItemId", "Action"]].values.tolist(),
                       "disconnect": [t["id"] for t in TARGETS if t["connected"] and not t["problems"]]})
print(f"\nDELETE ID: {DELETE_ID}  ({'HARD delete: permanent, as the migration requires' if HARD_DELETE else 'SOFT delete: the items would still block the migration'})")
