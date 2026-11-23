import csv, io

missing = [n for n in ("SNAPSHOT_ID", "ADLS_ACCOUNT") if "<" in str(globals()[n])]
if missing:
    raise ValueError("Fill in these settings first: " + ", ".join(missing))
if KQL_RESTORE_SOURCE not in ("auto", "parquet", "adx"):
    raise ValueError("KQL_RESTORE_SOURCE must be 'auto', 'parquet' or 'adx'.")
for _n in ("WAREHOUSE_LOAD", "SQL_OBJECTS"):
    if globals()[_n] not in ("script", "run", "off"):
        raise ValueError(f"{_n} must be 'script', 'run' or 'off'.")
if LAKEHOUSE_COPY_METHOD not in ("fastcp", "per_file"):
    raise ValueError("LAKEHOUSE_COPY_METHOD must be 'fastcp' or 'per_file'.")
MANIFEST = load_manifest(SNAPSHOT_ID)
PREFIX = migration_prefix(SNAPSHOT_ID)
SEC = MANIFEST["secondaryBackup"]
if (SEC["account"], SEC["container"]) != (ADLS_ACCOUNT, ADLS_CONTAINER):
    raise ValueError(f"The snapshot's backup run is in {SEC['account']}/{SEC['container']}: use the same ADLS_ACCOUNT "
                     "and ADLS_CONTAINER.")
RUN_FOLDER = f"{SEC['root'].strip('/')}/{SEC['runId']}"
if not adls_exists(f"{RUN_FOLDER}/_run_completed.json"):
    raise RuntimeError(f"Backup run {RUN_FOLDER} has no _run_completed.json: it never finished. Stop.")
BACKUP_LOG = pd.read_csv(io.BytesIO(adls_read(f"{RUN_FOLDER}/backup_log.csv")), dtype=str).fillna("")


def backup_row(item_id, step):
    rows = BACKUP_LOG[(BACKUP_LOG.ItemId.str.lower() == item_id.lower()) & (BACKUP_LOG.Step == step)]
    return rows.iloc[-1].to_dict() if len(rows) else {}


def read_json(path, default):
    try:
        return json.loads(adls_read(path))
    except HttpError as exc:
        if exc.status != 404:
            raise
        return default


STATE = read_json(f"{PREFIX}/data_state.json", {"items": {}})
RESTORE_STATE = read_json(f"{PREFIX}/restore_state.json", {"workspaces": {}})


def save_state():
    adls_write(f"{PREFIX}/data_state.json", json.dumps(STATE, indent=2, sort_keys=True))


def item_state(item):
    return STATE["items"].setdefault(item["id"], {})


known = {w["id"] for w in MANIFEST["workspaces"]}
bad = [k for k in WORKSPACE_MAP if norm_id(k) not in known or not norm_id(WORKSPACE_MAP[k])]
if bad:
    raise ValueError(f"WORKSPACE_MAP entries not in the manifest or not GUIDs: {bad}")
ws_map = {norm_id(k): norm_id(v) for k, v in WORKSPACE_MAP.items()}

STEP_OF = {"Lakehouse": "Data", "Warehouse": "Data", "KQLDatabase": "KQL export", "SQLEndpoint": "SQL scripts"}
SWITCH = {"Lakehouse": RESTORE_LAKEHOUSE_DATA, "KQLDatabase": RESTORE_KQL_DATA,
          "Warehouse": WAREHOUSE_LOAD != "off", "SQLEndpoint": SQL_OBJECTS != "off"}
ITEM_DIR_RE = re.compile(r"^(?P<name>.*)\.(?P<type>[A-Za-z]+)__(?P<id>[0-9a-f-]{36})$")
ITEMS, rows = [], []


def read_data_manifest(raw):
    """[(path, size, status)] from a V2_2 data_manifest.csv. Backups written before 2026-11-17 left the lastModified
    column unquoted ("Tue, 17 Nov 2026 07:35:24 GMT" has commas), so a row can have more than 4 fields: path is the
    first, size the second, status the last."""
    out = []
    rows = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
    next(rows, None)                                     # header
    for row in rows:
        if not row:
            continue
        if len(row) < 4 or not str(row[1]).strip().isdigit():
            raise ValueError(f"data_manifest.csv: can't read the row {row[:4]}")
        out.append((row[0], int(row[1]), row[-1].strip()))
    return out


def check_manifest_files(it, files):
    """The backup's data_manifest.csv lists every file it copied: each must still be in ADLS with that size."""
    if not any(rel == "data_manifest.csv" for rel, _, _ in files):
        return ["data_manifest.csv is missing in the backup"]
    man = read_data_manifest(adls_read(f"{it['folder']}/data_manifest.csv"))
    have = dict(it["data"])
    bad = [path for path, size, status in man if status == "copied" and have.get(path) != size]
    failed = sum(1 for _, _, status in man if status != "copied")
    out = []
    if bad:
        out.append(f"{len(bad)} file(s) listed in data_manifest.csv are missing or changed in ADLS, first: {bad[0]}")
    if failed:
        out.append(f"{failed} file(s) failed during the backup")
    return out


def lakehouse_layout_problem(it):
    """The backup's table layout (lakehouse.json, or the table folders) must match the restored Lakehouse:
    schema-enabled = Tables/<schema>/<table>, otherwise Tables/<table>. Returns (problem or "", text)."""
    try:
        lay = json.loads(adls_read(f"{it['folder']}/lakehouse.json"))
    except HttpError as exc:
        if exc.status != 404:
            raise
        tables = sorted({table_root(r) for r, _ in it["data"] if table_root(r)})
        deep = [t for t in tables if t.count("/") >= 2]
        lay = {"schemaEnabled": bool(tables) and len(deep) == len(tables), "defaultSchema": "", "tables": tables,
               "schemas": sorted({t.split("/")[1] for t in deep}), "inferred": True}
    props = fabric("GET", f"/workspaces/{it['ws']}/lakehouses/{it['target']}").get("properties") or {}
    target_ds = props.get("defaultSchema") or ""
    text = (f"schemas: {', '.join(lay['schemas']) or '-'}" if lay["schemaEnabled"] else "no schemas")
    if not lay["tables"]:
        return "", text
    if lay["schemaEnabled"] and not target_ds:
        return ("the backup is from a schema-enabled Lakehouse (tables in Tables/<schema>/<table>) but the restored "
                "Lakehouse has no schemas: its tables would not be readable. Recreate it with schemas enabled (its Git "
                "definition lakehouse.metadata.json must have defaultSchema), then run again"), text
    if not lay["schemaEnabled"] and target_ds:
        return ("the backup is from a Lakehouse without schemas (Tables/<table>) but the restored Lakehouse is "
                f"schema-enabled (default schema {target_ds}): its tables would not be readable. Recreate it without "
                "schemas, then run again"), text
    return "", text


def plan_lakehouse(it):
    problem, layout = lakehouse_layout_problem(it)
    if problem:
        it["problems"].append(problem)
        return "BLOCKED", problem, [problem]
    st = item_state(it)
    target = dict(onelake_list(it["ws"], it["target"], "Files") + onelake_list(it["ws"], it["target"], "Tables"))
    ours = set(st.get("tables", []))
    in_target = {table_root(r) for r in target if table_root(r)}
    skip = sorted(t for t in {table_root(r) for r, _ in it["data"] if table_root(r)} if t in in_target and t not in ours)
    todo, there, conflicts = [], 0, []
    for rel, size in it["data"]:
        if table_root(rel) in skip:
            continue
        if rel in target:
            if target[rel] == size:
                there += 1
                continue
            if not (st.get("started") and (table_root(rel) in ours or rel.startswith("Files/"))):
                conflicts.append(rel)
                continue
        todo.append((rel, size))
    it.update(todo=todo, skipTables=skip, conflicts=conflicts)
    detail = f"{len(todo)} file(s), {human(sum(s for _, s in todo))} to copy; {there} already there; {layout}"
    if skip:
        detail += f" | tables that already hold data in the target, left alone: {', '.join(skip)}"
    if conflicts:
        detail += f" | {len(conflicts)} file(s) exist with different content, left alone: {conflicts[0]}"
    return ("load" if todo else "nothing to load"), detail, [len(todo), skip, conflicts]


def plan_kql(it):
    props = fabric("GET", f"/workspaces/{it['ws']}/kqlDatabases/{it['target']}").get("properties") or {}
    it["uri"], it["db"] = props.get("queryServiceUri"), props.get("databaseName") or it["displayName"]
    if not it["uri"]:
        raise HttpError(None, "NoQueryUri", "no query URI for the restored KQL database", it["target"])
    folders = defaultdict(list)
    if it["source"] == "adx":     # one entry per table: the source table name in ADX
        for t in it["adx"].get("tables") or {}:
            folders[safe_name(t)].append(t)
    else:
        for rel, size in it["data"]:
            if "/" in rel and rel.lower().endswith(".parquet"):
                folders[rel.split("/")[0]].append(rel)
    tables = {safe_name(t["TableName"]): t["TableName"] for t in kusto_mgmt(it["uri"], it["db"], ".show tables")}
    st = item_state(it)
    busy, todo = [], {}
    for f, rels in sorted(folders.items()):
        t = tables.get(f)
        if t and t in st.get("done", {}):
            continue
        if t and t not in st.get("done", {}) and t not in st.get("started", []) and kusto_count(it["uri"], it["db"], t):
            busy.append(t)
            continue
        todo[f] = sorted(rels)
    it.update(folders=todo, busy=busy, missingTables=[f for f in todo if f not in tables])
    expected = it["backupRows"]
    detail = (f"{len(todo)} table(s) from ADX {it['adx'].get('cluster')} / {it['adx'].get('database')}"
              if it["source"] == "adx" else
              f"{len(todo)} table(s), {sum(len(v) for v in todo.values())} Parquet file(s)") + \
        (f", {int(float(expected)):,} rows backed up" if expected else "")
    if it["missingTables"]:
        detail += f" | {len(it['missingTables'])} table(s) not in the database: the backed-up schema script runs first"
    if busy:
        detail += f" | tables that already hold rows, left alone: {', '.join(busy)}"
    return ("ingest" if todo else "nothing to load"), detail, [sorted(todo), busy, it["missingTables"]]


def plan_warehouse(it):
    tables = sorted({table_root(r) for r, _ in it["data"] if table_root(r)})
    it["tables"] = {}
    manual = []
    for t in tables:
        res, problems = delta_active_files(it["folder"], t, it["data"])
        if res is None or problems:
            manual.append(f"{t}: {'; '.join(problems)}")
        else:
            it["tables"][t] = res
    it["manual"] = manual
    rows_ = sum(r["rows"] or 0 for r in it["tables"].values())
    detail = f"{len(it['tables'])} table(s), {rows_:,} rows by the Delta log; mode '{WAREHOUSE_LOAD}'"
    mapped = sum(bool(r.get("columns")) for r in it["tables"].values())
    if mapped:
        detail += f" | {mapped} column-mapped table(s): typed OPENROWSET, physical to logical columns"
    if manual:
        detail += f" | {len(manual)} table(s) need loading by hand: {manual[0]}"
    return (f"COPY INTO ({WAREHOUSE_LOAD})" if it["tables"] else "manual load" if manual else "nothing to load"), detail, \
        [it["folder"], sorted(it["tables"].items()), manual]


def plan_sql(it):
    return (f"SQL objects ({SQL_OBJECTS})" if it["sql"] else "nothing to load"), \
        f"{len(it['sql'])} view(s)/function(s)/procedure(s); mode '{SQL_OBJECTS}'", [sorted(it["sql"])]


PLANNERS = {"Lakehouse": plan_lakehouse, "KQLDatabase": plan_kql, "Warehouse": plan_warehouse, "SQLEndpoint": plan_sql}

for mws in MANIFEST["workspaces"]:
    if not mws["ready"]:
        print(f"{mws['name']}: skipped (not READY in the manifest)")
        continue
    old = mws["id"]
    rst = RESTORE_STATE["workspaces"].get(old, {})
    new, how = resolve_target_workspace(old, mws["name"], ws_map.get(old), rst.get("target"))
    ws_problems, now = ([] if new else [how]), {}
    new = new or old
    if how != "same ID" and new:
        print(f"{mws['name']}: target workspace {new} ({how})")
    if REQUIRE_GIT_RESTORE_DONE and not RESTORE_STATE["workspaces"].get(old, {}).get("updated"):
        ws_problems.append("step 5 (V2_5_Restore_From_Git) hasn't finished for this workspace (set REQUIRE_GIT_RESTORE_DONE = False only if "
                           "the items were recreated some other way)")
    try:
        now = {key_of(i["type"], i["displayName"]): i for i in workspace_items(new)}
    except HttpError as exc:
        ws_problems.append(f"can't read the target workspace {new} ({exc.short()})")
    folder = mws.get("secondaryFolder") or ""
    backed = {}
    for d, _, is_dir in (adls_list(folder, recursive=False) if folder else []):
        m = ITEM_DIR_RE.match(d)
        if is_dir and m:
            backed[m.group("id").lower()] = f"{folder}/{d}"
    for mi in mws["items"]:
        typ = mi["type"]
        if typ not in PLANNERS or is_dataflow_staging(mi):     # Dataflow Gen2 staging: nothing backed up, not restored
            continue
        it = {"workspace": mws["name"], "wsOld": old, "ws": new, "id": mi["id"].lower(), "type": typ,
              "displayName": mi["displayName"], "folder": backed.get(mi["id"].lower()), "problems": list(ws_problems),
              "target": (now.get(key_of(typ, mi["displayName"])) or {}).get("id")}
        step = STEP_OF[typ]
        if typ == "KQLDatabase":
            step = {"parquet": "KQL export", "adx": KQL_ADX_STEP}.get(KQL_RESTORE_SOURCE) or data_step(
                typ, lambda s, i=mi["id"]: backup_row(i, s).get("Status", ""))[0]
        it["step"], it["source"] = step, "adx" if step == KQL_ADX_STEP else "adls"
        b = backup_row(mi["id"], step)
        it["backupStatus"], it["backupRows"] = b.get("Status", ""), b.get("Rows", "")
        files = adls_list(it["folder"]) if it["folder"] else []
        it["data"] = [(rel[5:], size) for rel, size, d in files if not d and rel.startswith("data/")]
        it["sql"] = [rel for rel, _, d in files if not d and rel.startswith("sql/") and rel.endswith(".sql")]
        action, detail, sig = "off", f"switched off ({'RESTORE_LAKEHOUSE_DATA' if typ == 'Lakehouse' else 'RESTORE_KQL_DATA' if typ == 'KQLDatabase' else 'WAREHOUSE_LOAD' if typ == 'Warehouse' else 'SQL_OBJECTS'})", []
        if SWITCH[typ]:
            if not it["folder"]:
                it["problems"].append(f"no folder for this item in backup run {SEC['runId']}")
            if it["backupStatus"] != "OK":
                it["problems"].append(f"backup step '{step}' was {it['backupStatus'] or 'not recorded'}")
            if it["source"] == "adx" and it["folder"]:
                it["adx"] = read_json(f"{it['folder']}/kusto_copy/_destination.json", None)
                if not it["adx"]:
                    it["problems"].append("the backup has no kusto_copy/_destination.json: can't tell where the "
                                          "rows are in ADX")
            if typ in ("Lakehouse", "Warehouse") and it["folder"]:
                it["problems"] += check_manifest_files(it, files)
            if not it["target"]:
                it["problems"].append("not found in the target workspace: restore the item first (step 5, V2_5)")
            if not it["problems"]:
                try:
                    action, detail, sig = PLANNERS[typ](it)
                except HttpError as exc:
                    it["problems"].append(f"can't read the restored item ({exc.short()})")
        it["action"] = "BLOCKED" if it["problems"] else action
        it["detail"] = " | ".join(it["problems"]) if it["problems"] else detail
        it["sig"] = sig
        ITEMS.append(it)
        rows.append({"Workspace": it["workspace"], "Item": it["displayName"], "Type": typ, "Backup": it["backupStatus"],
                     "Target ID": it["target"] or "", "Action": it["action"], "Detail": it["detail"]})

PLAN_DF = pd.DataFrame(rows, columns=["Workspace", "Item", "Type", "Backup", "Target ID", "Action", "Detail"])
show(PLAN_DF)
TODO = [i for i in ITEMS if i["action"] not in ("BLOCKED", "off", "nothing to load")]
DATA_ID = plan_hash({"snapshot": SNAPSHOT_ID, "run": SEC["runId"], "modes": [WAREHOUSE_LOAD, SQL_OBJECTS],
                     "items": [(i["id"], i["ws"], i["target"], i["action"], i["sig"]) for i in ITEMS]})
blocked = [i for i in ITEMS if i["action"] == "BLOCKED"]
print(f"\n{len(TODO)} item(s) to load, {len(blocked)} BLOCKED. DATA ID: {DATA_ID}")
if blocked:
    print("BLOCKED items are not loaded. Fix them and run the dry run again, or restore them by hand.")
if DRY_RUN:
    PROCEED = False
    print(f"DRY_RUN = True: nothing loaded. To load, set DRY_RUN = False and CONFIRM_DATA_ID = \"{DATA_ID}\".")
elif CONFIRM_DATA_ID != DATA_ID:
    raise RuntimeError(f"CONFIRM_DATA_ID doesn't match this plan's DATA ID ({DATA_ID}). Something changed since the "
                       "dry run, or the ID wasn't pasted. Review the plan above and confirm again.")
else:
    PROCEED = True
