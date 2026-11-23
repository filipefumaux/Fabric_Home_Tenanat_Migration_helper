RESULTS.clear()
selected_types = set(ITEM_TYPES)
if INCLUDE_KQL_DATABASES and "Eventhouse" in selected_types:
    selected_types.add("KQLDatabase")
only_ids = {norm_id(x) for x in ONLY_ITEM_IDS} - {None}

workspace_ids = []
for raw in WORKSPACE_IDS:
    ws_id = norm_id(raw)
    if not ws_id:
        raise ValueError(f"Not a workspace ID (GUID): {raw!r}")
    if ws_id not in workspace_ids:
        workspace_ids.append(ws_id)
if not workspace_ids:
    raise ValueError("WORKSPACE_IDS is empty. Add the workspace IDs to back up in the settings cell.")

WORKSPACES, ITEMS, STAGING_SKIPPED = {}, [], []
git_ws_names = Counter()
for ws_id in workspace_ids:
    try:
        ws = fabric("GET", f"/workspaces/{ws_id}")
        all_items = list(fabric_pages(f"/workspaces/{ws_id}/items"))
    except HttpError as exc:
        print(f"  ! workspace {ws_id}: {exc.short()}")
        log({"workspaceId": ws_id}, "Inventory", "Failed",
            "No access to the workspace (needs Contributor or higher)" if exc.status in (401, 403) else exc.short())
        continue
    name = ws.get("displayName") or ws_id
    git_ws_names[safe_name(name)] += 1
    WORKSPACES[ws_id] = {"id": ws_id, "name": name, "adls": f"{safe_name(name)}__{ws_id}"}

    types_here = {i.get("type") for i in all_items}
    extra = {}
    if types_here & {"Lakehouse", "SQLEndpoint"}:
        for lh in fabric_pages(f"/workspaces/{ws_id}/lakehouses"):
            props = lh.get("properties") or {}
            extra[norm_id(lh["id"])] = props
            sql = props.get("sqlEndpointProperties") or {}
            if norm_id(sql.get("id")):
                extra[norm_id(sql["id"])] = {"lakehouseId": norm_id(lh["id"]),
                                             "connectionString": sql.get("connectionString")}
    if types_here & {"Eventhouse", "KQLDatabase"}:
        for db in fabric_pages(f"/workspaces/{ws_id}/kqlDatabases"):
            extra[norm_id(db["id"])] = db.get("properties") or {}

    picked = [i for i in all_items if i.get("type") in selected_types - {"KQLDatabase"}]
    picked_ids = {norm_id(i["id"]) for i in picked}
    for i in all_items:  # KQL databases follow their parent Eventhouse
        if i.get("type") == "KQLDatabase" and "KQLDatabase" in selected_types:
            parent = norm_id(extra.get(norm_id(i["id"]), {}).get("parentEventhouseItemId"))
            if parent in picked_ids:
                picked.append(i)
    for i in picked:
        iid = norm_id(i["id"])
        parent = norm_id(extra.get(iid, {}).get("parentEventhouseItemId"))
        if only_ids and iid not in only_ids and parent not in only_ids:
            continue
        if is_dataflow_staging(i):
            STAGING_SKIPPED.append(f"{name} / {i.get('displayName')} ({i['type']})")
            continue
        ITEMS.append({"id": iid, "displayName": i.get("displayName") or iid, "type": i["type"],
                      "description": i.get("description", ""), "folderId": i.get("folderId"),
                      "workspaceId": ws_id, "workspaceName": name, "properties": extra.get(iid, {})})

# Folder names: ADLS keeps the IDs; the Git copy uses Fabric Git integration's "<name>.<Type>" layout.
for ws in WORKSPACES.values():
    base = safe_name(ws["name"])
    ws["git"] = base if git_ws_names[base] == 1 else f"{base}_{ws['id'][:8]}"
seen = Counter((i["workspaceId"], safe_name(i["displayName"]).lower(), i["type"]) for i in ITEMS)
for i in ITEMS:
    base = safe_name(i["displayName"])
    unique = seen[(i["workspaceId"], base.lower(), i["type"])] == 1
    i["git"] = f"{base if unique else base + '_' + i['id'][:8]}.{i['type']}"
    i["adls"] = f"{ADLS_ROOT.strip('/')}/{RUN_ID}/{WORKSPACES[i['workspaceId']]['adls']}/{base}.{i['type']}__{i['id']}"

if not ITEMS:
    raise RuntimeError("No items of the selected types were found in the listed workspaces.")
plan = pd.DataFrame(ITEMS)
print(f"{len(ITEMS)} items in {len(WORKSPACES)} of {len(workspace_ids)} workspaces")
if STAGING_SKIPPED:
    print(f"Left out, Dataflow Gen2 staging items (Fabric recreates them; V2_4 still deletes them): "
          + ", ".join(STAGING_SKIPPED))
show(plan.pivot_table(index="workspaceName", columns="type", values="id", aggfunc="count", fill_value=0))
