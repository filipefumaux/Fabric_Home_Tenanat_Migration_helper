ADO_ORGANIZATION = ado_org_name(ADO_ORGANIZATION)   # a URL works too
SNAPSHOT_ID = NOW_ID
missing = [n for n in ("ADO_ORGANIZATION", "KEY_VAULT_URL", "ADLS_ACCOUNT",
                       "SECONDARY_BACKUP_RUN_ID") if "<" in str(globals()[n])] + ado_auth_missing()
if missing:
    raise ValueError("Fill in these settings first: " + ", ".join(missing))
ws_ids = [norm_id(w) for w in WORKSPACE_IDS]
if not ws_ids or None in ws_ids:
    raise ValueError("WORKSPACE_IDS must list at least one workspace ID (GUID).")
NO_GIT = {norm_id(w) for w in NO_GIT_WORKSPACE_IDS}
if None in NO_GIT or NO_GIT - set(ws_ids):
    raise ValueError("NO_GIT_WORKSPACE_IDS must be workspace IDs (GUIDs) that are also in WORKSPACE_IDS.")
if NO_GIT and not SAVE_RAW_COPY_TO_ADLS:
    raise ValueError("Workspaces without Git (NO_GIT_WORKSPACE_IDS) are restored from the raw copy: set "
                     "SAVE_RAW_COPY_TO_ADLS = True.")
if ALLOW_SECONDARY_ONLY_ITEMS and not SAVE_RAW_COPY_TO_ADLS:
    raise ValueError("ALLOW_SECONDARY_ONLY_ITEMS = True needs SAVE_RAW_COPY_TO_ADLS = True: V2_4 compares those items "
                     "with their raw copy to prove they didn't change.")

WS = {}
for ws_id in dict.fromkeys(ws_ids):
    try:
        info = fabric("GET", f"/workspaces/{ws_id}")
        items = workspace_items(ws_id)
    except HttpError as exc:
        raise RuntimeError(f"Can't read workspace {ws_id} ({exc.short()}). You need Contributor or higher on it.") \
            from None
    name = info.get("displayName", ws_id)
    for i in items:
        i["workspaceName"] = name
    ws = {"id": ws_id, "name": name, "capacityId": info.get("capacityId"), "blockers": [], "git": {},
          "noGit": ws_id in NO_GIT,
          "items": [i for i in items if i["type"] not in POWER_BI_TYPES],
          "pbi": [i for i in items if i["type"] in POWER_BI_TYPES]}
    for i in ws["items"]:
        i["staging"] = is_dataflow_staging(i)
        if i["type"] == "Lakehouse" and not i["staging"]:
            try:
                props = fabric("GET", f"/workspaces/{ws_id}/lakehouses/{i['id']}").get("properties") or {}
                i["sqlEndpointId"] = (props.get("sqlEndpointProperties") or {}).get("id") or ""
                i["defaultSchema"] = props.get("defaultSchema") or ""      # set only on schema-enabled Lakehouses
            except HttpError as exc:
                log(name, i, "Inventory", "Warning", f"can't read the SQL endpoint ID: {exc.short()}")
        if i["type"] == "KQLDatabase":
            try:
                props = fabric("GET", f"/workspaces/{ws_id}/kqlDatabases/{i['id']}").get("properties") or {}
                i["parentEventhouseItemId"] = props.get("parentEventhouseItemId") or ""
            except HttpError as exc:
                log(name, i, "Inventory", "Warning", f"can't read the parent Eventhouse: {exc.short()}")
    WS[ws_id] = ws
    other = sorted({i["type"] for i in ws["items"] if i["type"] not in ITEM_TYPES})
    print(f"{name}: {len(ws['items'])} Fabric items, {len(ws['pbi'])} Power BI items"
          + (f"; other Fabric types: {', '.join(other)}" if other else ""))


def block(ws, step, reason):
    ws["blockers"].append(f"{step}: {reason}")
    log(ws["name"], {}, step, "Blocked", reason)
