DEFINITIONS = {}  # item id -> {part path: bytes}


def normalize_platform(item, parts):
    """Make sure .platform names the item and has a unique logicalId (Fabric Git needs one per item)."""
    try:
        platform = json.loads(parts[".platform"].decode("utf-8-sig")) if ".platform" in parts else {}
    except ValueError:
        platform = {}
    platform.setdefault("$schema", "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
                                   "platformProperties/2.0.0/schema.json")
    meta = platform.setdefault("metadata", {})
    meta.setdefault("type", item["type"])
    meta.setdefault("displayName", item["displayName"])
    if item.get("description") and not meta.get("description"):
        meta["description"] = item["description"]
    config = platform.setdefault("config", {})
    config.setdefault("version", "2.0")
    if norm_id(config.get("logicalId")) in (None, ZERO_GUID):
        config["logicalId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "fabric-item/" + item["id"]))
    item["logicalId"] = config["logicalId"]
    parts[".platform"] = (json.dumps(platform, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def backup_definition(item):
    meta = {k: item.get(k) for k in ("id", "displayName", "type", "description", "folderId",
                                     "workspaceId", "workspaceName", "properties")}
    if not DRY_RUN:
        adls_write(f"{item['adls']}/item.json", json.dumps(meta, indent=2, ensure_ascii=False))
    if item["type"] in NO_DEFINITION_TYPES:
        log(item, "Definition", "Not applicable", "SQL analytics endpoints have no item definition; "
            "their views, functions and permissions are scripted in the SQL step")
        return
    try:
        result = fabric("POST", f"/workspaces/{item['workspaceId']}/items/{item['id']}/getDefinition")
    except HttpError as exc:
        if exc.status in (401, 403):
            status, detail = "Access denied", "Needs Contributor or higher on the workspace. " + exc.short()
        elif exc.code in ("OperationNotSupportedForItem", "UnsupportedItemType", "ItemDefinitionNotSupported"):
            status, detail = "Not supported", exc.short()
        else:
            status, detail = "Failed", exc.short()
        log(item, "Definition", status, detail)
        return
    parts, other = {}, []
    for part in (result.get("definition") or {}).get("parts", []):
        if part.get("payloadType", "InlineBase64") != "InlineBase64":
            other.append(part.get("path"))
            continue
        parts[part["path"].lstrip("/")] = base64.b64decode(part.get("payload") or "")
    if not parts:
        log(item, "Definition", "Failed", "The definition came back empty")
        return
    normalize_platform(item, parts)
    DEFINITIONS[item["id"]] = parts
    size = sum(len(v) for v in parts.values())
    if not DRY_RUN and BACKUP_DEFINITIONS_TO_ADLS:
        for path, data in parts.items():
            adls_write(f"{item['adls']}/definition/{path}", data)
    note = f"{len(parts)} parts" + (f"; skipped non-inline parts: {other}" if other else "")
    log(item, "Definition", "OK", note, files=len(parts), size=size)


with ThreadPoolExecutor(4) as pool:
    futures = {pool.submit(backup_definition, i): i for i in ITEMS}
    for n, fut in enumerate(as_completed(futures), 1):
        try:
            fut.result()
        except HttpError as exc:
            log(futures[fut], "Definition", "Failed", exc.short())
        if n % 25 == 0:
            print(f"  definitions: {n}/{len(ITEMS)}")

defs = pd.DataFrame([r for r in RESULTS if r["Step"] == "Definition"])
show(defs.pivot_table(index="Type", columns="Status", values="ItemId", aggfunc="count", fill_value=0))
