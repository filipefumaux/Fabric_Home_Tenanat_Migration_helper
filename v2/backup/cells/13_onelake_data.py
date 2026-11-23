def shortcut_prefixes(item):
    if item["type"] != "Lakehouse":
        return []
    shortcuts = fabric_pages(f"/workspaces/{item['workspaceId']}/items/{item['id']}/shortcuts")
    return [f"{(s.get('path') or '').strip('/')}/{s.get('name', '')}".strip("/") for s in shortcuts]


def lakehouse_layout(item, files):
    """Schema-enabled Lakehouse: tables are Tables/<schema>/<table>; otherwise Tables/<table>. Recorded in
    lakehouse.json so V2_6 can check that the restored Lakehouse has the same layout before copying."""
    ds = (item.get("properties") or {}).get("defaultSchema") or ""
    tables = sorted({rel.split("/_delta_log/")[0] for rel, _, _ in files
                     if rel.startswith("Tables/") and "/_delta_log/" in rel})
    schemas = sorted({t.split("/")[1] for t in tables if t.count("/") >= 2}) if ds else []
    return {"schemaEnabled": bool(ds), "defaultSchema": ds, "schemas": schemas, "tables": tables}


def delta_check(item, files):
    """Read each table's Delta log in OneLake (JSON commits only, read-only) and make sure every data file it
    references is in the copy list. Warehouse data files don't always show up in a folder listing, so missing
    ones are added by the path in the log. Returns (extra files to copy, notes, problems)."""
    from urllib.parse import unquote
    listed = {rel for rel, _, _ in files}
    tables = sorted({rel.split("/_delta_log/")[0] for rel in listed if "/_delta_log/" in rel})
    extra, notes, problems, empty = [], [], [], []
    for table in tables:
        logs = sorted(rel for rel in listed if rel.startswith(table + "/_delta_log/")
                      and re.fullmatch(r"\d{20}\.json", rel.rsplit("/", 1)[-1]))
        if not logs or not logs[0].endswith("00000000000000000000.json"):
            notes.append(f"{table}: Delta log starts at a checkpoint, data files not cross-checked")
            continue
        active = {}
        for rel in logs:
            size = next(f[1] for f in files if f[0] == rel)
            text = onelake_read(item["workspaceId"], item["id"], rel, 0, size - 1).decode("utf-8") if size else ""
            for line in text.splitlines():
                try:
                    action = json.loads(line) if line.strip() else {}
                except ValueError:
                    continue
                if "add" in action:
                    active[action["add"]["path"]] = action["add"]
                elif "remove" in action:
                    active.pop(action["remove"]["path"], None)
        if not active:
            empty.append(table.split("/", 1)[-1])
            continue
        for path, add in active.items():
            if "://" in path:
                problems.append(f"{table}: data file outside the table folder ({path.split('?')[0]}), not copied")
                continue
            rel = f"{table}/{unquote(path)}"
            if rel not in listed:
                extra.append((rel, int(add.get("size") or 0), ""))
                listed.add(rel)
    if empty:
        notes.append(f"{len(empty)} table(s) with no data files in the published Delta log ({', '.join(empty[:10])}): "
                     "empty, or the Warehouse hasn't published its Delta log yet (check "
                     "SELECT name, DATA_LAKE_LOG_PUBLISHING_DESC FROM sys.databases; it must be AUTO)")
    if extra:
        notes.append(f"{len(extra)} data file(s) not in the folder listing were added from the Delta log")
    return extra, notes, problems


def layout_text(lay):
    if lay["schemaEnabled"]:
        return (f"schema-enabled (default schema {lay['defaultSchema']}): {len(lay['tables'])} table(s) in "
                f"{len(lay['schemas'])} schema(s) {', '.join(lay['schemas'])}".rstrip())
    return f"no schemas: {len(lay['tables'])} table(s)"


def backup_onelake_data(item):
    folders = []
    if item["type"] == "Lakehouse":
        folders = [f for f, on in (("Files", COPY_LAKEHOUSE_FILES), ("Tables", COPY_LAKEHOUSE_TABLES)) if on]
    elif item["type"] == "Warehouse" and COPY_WAREHOUSE_TABLES:
        folders = ["Tables"]
    if not folders:
        return
    ws_id, item_id = item["workspaceId"], item["id"]
    try:
        skip = shortcut_prefixes(item)
        files, skipped = [], 0
        for folder in folders:
            for rel, size, modified in onelake_list(ws_id, item_id, folder):
                if any(rel == p or rel.startswith(p + "/") for p in skip):
                    skipped += 1
                else:
                    files.append((rel, size, modified))
    except HttpError as exc:
        log(item, "Data", "Access denied" if exc.status in (401, 403) else "Failed", exc.short())
        return
    total = sum(f[1] for f in files)
    note = f"{len(skip)} shortcut(s) not copied ({skipped} files)" if skip else ""
    try:
        extra, dnotes, dproblems = delta_check(item, files)
    except (HttpError, StopIteration, UnicodeDecodeError) as exc:
        extra, dnotes, dproblems = [], [f"Delta log not cross-checked: {exc}"], []
    files += extra
    total = sum(f[1] for f in files)
    lay = lakehouse_layout(item, files) if item["type"] == "Lakehouse" else None
    if lay:
        note = (layout_text(lay) + (f". {note}" if note else "")).strip()
    note = ". ".join(x for x in [note] + dnotes + dproblems if x)
    if DRY_RUN:
        log(item, "Data", "Partial" if dproblems else "Dry run",
            f"would copy {len(files)} files, {human(total)}. {note}".strip(),
            files=len(files), size=total)
        return

    # Data files first, Delta logs last, so every log entry in the copy points at files already copied.
    first = [f for f in files if "/_delta_log/" not in "/" + f[0]]
    last = sorted(f for f in files if "/_delta_log/" in "/" + f[0])
    copied, copied_bytes, errors, manifest = 0, 0, [], ["path,size,lastModified,status"]
    started = time.monotonic()
    for batch in (first, last):
        with ThreadPoolExecutor(MAX_PARALLEL_FILES) as pool:
            futures = {pool.submit(copy_onelake_file, ws_id, item_id, rel, size, f"{item['adls']}/data/{rel}"):
                       (rel, size, modified) for rel, size, modified in batch}
            for fut in as_completed(futures):
                rel, size, modified = futures[fut]
                try:
                    copied_bytes += fut.result()
                    copied += 1
                    status = "copied"
                except HttpError as exc:
                    errors.append(f"{rel}: {exc.short()}")
                    status = "failed"
                manifest.append(",".join('"' + str(v).replace('"', '""') + '"' for v in (rel, size, modified, status)))
                if copied % 500 == 0 and copied:
                    print(f"    {item['displayName']}: {copied}/{len(files)} files, {human(copied_bytes)}, "
                          f"{time.monotonic() - started:,.0f} s")
    adls_write(f"{item['adls']}/data_manifest.csv", "\n".join(manifest) + "\n")
    if lay:
        adls_write(f"{item['adls']}/lakehouse.json", json.dumps(lay, indent=2))
    detail = f"{copied}/{len(files)} files, {human(copied_bytes)}. {note}".strip()
    if errors:
        detail += f" | {len(errors)} failed, first: {errors[0]}"
    errors += dproblems
    log(item, "Data", "OK" if not errors else ("Partial" if copied else "Failed"), detail,
        files=copied, size=copied_bytes)


data_items = [i for i in ITEMS if i["type"] in ONELAKE_DATA_TYPES]
for n, item in enumerate(data_items, 1):
    print(f"[{n}/{len(data_items)}] {item['type']} {item['workspaceName']} / {item['displayName']}")
    backup_onelake_data(item)
show(pd.DataFrame([r for r in RESULTS if r["Step"] == "Data"]).drop(columns=["WorkspaceId", "ItemId"], errors="ignore"))
