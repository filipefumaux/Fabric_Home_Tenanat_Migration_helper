TERMINAL_KQL_STATES = {"Completed", "Failed", "PartiallySucceeded", "Abandoned", "BadInput", "Canceled",
                       "Cancelled", "Throttled"}


def export_kql_table(uri, database, table, dest_folder):
    dest = (f"https://{ADLS_ACCOUNT}.blob.core.windows.net/{ADLS_CONTAINER}/"
            f"{quote(dest_folder, safe='/')}/{quote(safe_name(table), safe='')}")
    command = (f'.export async compressed to parquet (h@"{dest};impersonate") '
               f'with (namePrefix={kql_string(safe_name(table))}, parquetDatetimePrecision="microsecond") '
               f'<| [{kql_string(table)}]')
    op = kusto_mgmt(uri, database, command, retry=False)[0]["OperationId"]
    deadline = time.monotonic() + KQL_EXPORT_TIMEOUT_SECONDS
    while True:
        time.sleep(5)
        state = kusto_mgmt(uri, database, f".show operations {op}")[0]
        if state.get("State") in TERMINAL_KQL_STATES:
            break
        if time.monotonic() > deadline:
            raise HttpError(None, "Timeout", f"export operation {op} still running", uri)
    if state.get("State") != "Completed":
        raise HttpError(None, state.get("State"), state.get("Status") or f"operation {op}", uri)
    details = kusto_mgmt(uri, database, f".show operation {op} details")
    return len(details), sum(int(d.get("NumRecords") or 0) for d in details), \
        sum(int(d.get("SizeInBytes") or 0) for d in details)


def backup_kql_database(item):
    props = item["properties"] or {}
    if not props.get("queryServiceUri"):
        props = fabric("GET", f"/workspaces/{item['workspaceId']}/kqlDatabases/{item['id']}").get("properties", {})
    uri, database = props.get("queryServiceUri"), props.get("databaseName") or item["displayName"]
    if not uri:
        log(item, "KQL export", "Failed", "No query URI returned for this KQL database")
        return
    tables = kusto_mgmt(uri, database, ".show tables details")
    total_rows = sum(int(t.get("TotalRowCount") or 0) for t in tables)
    total_size = sum(int(t.get("TotalOriginalSize") or 0) for t in tables)
    if DRY_RUN:
        log(item, "KQL export", "Dry run", f"would export {len(tables)} tables, {total_rows:,} rows "
            f"(~{human(total_size)} uncompressed)", files=len(tables), size=total_size, rows=total_rows)
        return
    schema = kusto_mgmt(uri, database, ".show database schema as csl script")
    adls_write(f"{item['adls']}/database_schema.kql",
               "\n".join(str(r.get("DatabaseSchemaScript", "")) for r in schema) + "\n")
    files = rows = size = 0
    errors, mismatches = [], []
    for t in tables:
        name = t["TableName"]
        try:
            f, r, s = export_kql_table(uri, database, name, f"{item['adls']}/data")
            files, rows, size = files + f, rows + r, size + s
            if r != int(t.get("TotalRowCount") or 0):
                mismatches.append(f"{name}: {r:,} exported vs {int(t.get('TotalRowCount') or 0):,} at start")
        except HttpError as exc:
            errors.append(f"{name}: {exc.short()}")
        print(f"    {name}: done ({len(errors)} errors so far)")
    detail = f"{len(tables) - len(errors)}/{len(tables)} tables, {rows:,} rows, {files} Parquet files"
    if mismatches:
        detail += " | row counts changed during export (new ingestion?): " + "; ".join(mismatches[:5])
    if errors:
        detail += f" | {len(errors)} failed, first: {errors[0]}"
    log(item, "KQL export", "OK" if not errors else ("Partial" if len(errors) < len(tables) else "Failed"),
        detail, files=files, size=size, rows=rows)


def save_kql_schema_only(item):
    """KQL_BACKUP_MODE = "adx": no Parquet export, but keep the schema script in ADLS (V2_6 creates missing tables
    from it before copying the rows back from ADX)."""
    props = item["properties"] or {}
    if not props.get("queryServiceUri"):
        props = fabric("GET", f"/workspaces/{item['workspaceId']}/kqlDatabases/{item['id']}").get("properties", {})
    uri, database = props.get("queryServiceUri"), props.get("databaseName") or item["displayName"]
    if not uri:
        log(item, "KQL schema", "Failed", "No query URI returned for this KQL database")
        return
    if DRY_RUN:
        log(item, "KQL schema", "Dry run", "would save database_schema.kql")
        return
    schema = kusto_mgmt(uri, database, ".show database schema as csl script")
    adls_write(f"{item['adls']}/database_schema.kql",
               "\n".join(str(r.get("DatabaseSchemaScript", "")) for r in schema) + "\n")
    log(item, "KQL schema", "OK", "database_schema.kql saved (rows go to ADX: Kusto Copy step)")


kql_items = [i for i in ITEMS if i["type"] == "KQLDatabase"]
if not EXPORT_KQL_TABLES:
    print(f"Parquet export skipped (KQL_BACKUP_MODE = \"{KQL_BACKUP_MODE}\"); saving the schema scripts only.")
    for item in kql_items:
        try:
            save_kql_schema_only(item)
        except HttpError as exc:
            log(item, "KQL schema", "Access denied" if exc.status in (401, 403) else "Failed", exc.short())
for n, item in enumerate(kql_items if EXPORT_KQL_TABLES else [], 1):
    print(f"[{n}/{len(kql_items)}] KQL database {item['workspaceName']} / {item['displayName']}")
    try:
        backup_kql_database(item)
    except HttpError as exc:
        log(item, "KQL export", "Access denied" if exc.status in (401, 403) else "Failed", exc.short())
