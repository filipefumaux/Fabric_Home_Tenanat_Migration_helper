RAW_PREFIX = f"{migration_prefix(SNAPSHOT_ID)}/raw"
GIT_MIRROR_PREFIX = f"{migration_prefix(SNAPSHOT_ID)}/git"
raw_log = []


def raw_item_properties(item):
    if item["type"] == "Lakehouse" and item.get("sqlEndpointId"):
        return {"sqlEndpointProperties": {"id": item["sqlEndpointId"]}}
    if item["type"] == "KQLDatabase" and item.get("parentEventhouseItemId"):
        return {"parentEventhouseItemId": item["parentEventhouseItemId"]}
    return {}


def save_raw(ws, item):
    folder = f"{RAW_PREFIX}/{safe_name(ws['name'])}__{ws['id']}/{safe_name(item['displayName'])}.{item['type']}__{item['id']}"
    d = fabric("POST", f"/workspaces/{ws['id']}/items/{item['id']}/getDefinition").get("definition") or {}
    parts = d.get("parts") or []
    if not parts:
        raise HttpError(None, "EmptyDefinition", "the definition came back empty", item["id"])
    size = 0
    for p in parts:
        data = base64.b64decode(p.get("payload") or "")
        size += adls_write(f"{folder}/definition/{p['path']}", data)
    adls_write(f"{folder}/item.json", json.dumps({
        "id": item["id"], "displayName": item["displayName"], "type": item["type"], "description": "",
        "workspaceId": ws["id"], "workspaceName": ws["name"], "properties": raw_item_properties(item),
        "definitionFormat": d.get("format")}, indent=2))
    stored = [n for n, _, is_dir in adls_list(f"{folder}/definition") if not is_dir]
    if len(stored) != len(parts):
        raise HttpError(None, "Verify", f"{len(stored)} of {len(parts)} parts found in ADLS after writing", folder)
    item["rawHash"] = definition_hash(parts)     # V2_4 compares the live definition with it (workspaces without Git)
    return len(parts), size


def save_git_mirror(ws):
    try:
        mirror = f"{GIT_MIRROR_PREFIX}/{safe_name(ws['name'])}__{ws['id']}"
        paths = ado_tree(ws["git"], ws["head"])
        for p in paths:
            adls_write(f"{mirror}/files{p}", ado_file(ws["git"], ws["head"], p))
        adls_write(f"{mirror}/_commit.json", json.dumps({**ws["git"], "commit": ws["head"], "files": len(paths)},
                                                        indent=2))
        ws["gitMirror"] = mirror
        log(ws["name"], {}, "Git file copy", "OK", f"{len(paths)} files of {ws['head']} in {mirror}/files")
    except HttpError as exc:
        block(ws, "Git file copy", exc.short())


if SAVE_RAW_COPY_TO_ADLS and not DRY_RUN:
    for ws in WS.values():
        if not ws["ready"]:
            continue
        for item in ws["items"]:
            if item["type"] in CHILD_TYPES or item.get("staging"):
                continue
            try:
                n, size = save_raw(ws, item)
                item["rawCopy"] = "OK"
                raw_log.append({"Workspace": ws["name"], "WorkspaceId": ws["id"], "Item": item["displayName"],
                                "Type": item["type"], "ItemId": item["id"], "Step": "Definition", "Status": "OK",
                                "Files": n, "Bytes": size, "Rows": "", "Detail": "raw copy from the V2 snapshot"})
            except HttpError as exc:
                item["rawCopy"] = f"Failed: {exc.short()}"
                raw_log.append({"Workspace": ws["name"], "WorkspaceId": ws["id"], "Item": item["displayName"],
                                "Type": item["type"], "ItemId": item["id"], "Step": "Definition", "Status": "Failed",
                                "Files": 0, "Bytes": 0, "Rows": "", "Detail": exc.short()})
                if item["type"] in ITEM_TYPES:
                    item["status"] = "BLOCKED"
                    item["reasons"].append(f"raw copy failed: {exc.short()}")
                    log(ws["name"], item, "Raw copy", "Blocked", exc.short())
                else:
                    log(ws["name"], item, "Raw copy", "Warning", exc.short())
        # file copy of the snapshot commit (workspaces without Git have none)
        if not ws.get("noGit"):
            save_git_mirror(ws)
        if any(i["status"] != "READY" for i in ws["items"]) or ws["blockers"]:
            ws["ready"], ws["status"] = False, "BLOCKED"
            if not ws["blockers"]:
                ws["blockers"].append("Raw copy: failed for at least one in-scope item")
    if raw_log:
        df_raw = pd.DataFrame(raw_log)
        adls_write(f"{RAW_PREFIX}/_run_started.json", json.dumps({"runId": f"_migration/{SNAPSHOT_ID}/raw",
                                                                  "startedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                                                  "kind": "raw definitions from the V2 snapshot"}))
        adls_write(f"{RAW_PREFIX}/backup_log.csv", df_raw.to_csv(index=False))
        adls_write(f"{RAW_PREFIX}/_run_completed.json", json.dumps({
            "runId": f"_migration/{SNAPSHOT_ID}/raw", "finishedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "items": len(df_raw), "problems": int((df_raw.Status != "OK").sum())}))
        print(f"Raw copy: {int((df_raw.Status == 'OK').sum())} of {len(df_raw)} definitions saved to {RAW_PREFIX}/")
    by_key = {(w["name"], i["displayName"], i["type"]): i for w in WS.values() for i in w["items"]}
    GATE_DF["Raw copy"] = [by_key.get((r.Workspace, r.Item, r.Type), {}).get("rawCopy", "") for r in GATE_DF.itertuples()]
    GATE_DF["Status"] = [by_key.get((r.Workspace, r.Item, r.Type), {}).get("status", r.Status) for r in GATE_DF.itertuples()]
elif SAVE_RAW_COPY_TO_ADLS:
    print("Dry run: the raw copy is made when DRY_RUN = False.")
