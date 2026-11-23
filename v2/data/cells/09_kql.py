def load_kql(it):
    uri, db, st = it["uri"], it["db"], item_state(it)
    if it["missingTables"]:
        schema = f"{it['folder']}/database_schema.kql"
        if not adls_exists(schema):
            raise HttpError(None, "NoSchema", f"tables {it['missingTables']} are missing and the backup has no schema "
                            "script", schema)
        kusto_mgmt(uri, db, ".execute database script with (ContinueOnErrors=false) <|\n" +
                   adls_read(schema).decode("utf-8"))
    tables = {safe_name(t["TableName"]): t["TableName"] for t in kusto_mgmt(uri, db, ".show tables")}
    done = st.setdefault("done", {})
    started = st.setdefault("started", [])
    saved = st.setdefault("policies", {})
    if not saved:
        for row in kusto_mgmt(uri, db, ".show table * policy update"):
            pol = row.get("Policy")
            if pol and pol not in ("[]", "null"):
                saved[row["EntityName"].split("].[")[-1].strip("[]")] = pol
        save_state()
    errors = []
    try:
        for t in saved:
            kusto_mgmt(uri, db, f".delete table [{kql_string(t)}] policy update")
        for folder, rels in sorted(it["folders"].items()):
            table = tables.get(folder)
            if not table:
                errors.append(f"{folder}: no matching table in the restored database")
                continue
            if table in done:
                continue
            current = kusto_count(uri, db, table)
            if current:
                errors.append(f"{table}: an earlier run was interrupted during ingestion and the table has {current:,} "
                              f"rows. Check it, run '.clear table [{table}] data' and run again." if table in started
                              else f"{table}: already has {current:,} rows; not touched")
                continue
            started.append(table)
            save_state()
            if it["source"] == "adx":
                src = (f"cluster({kql_string(it['adx']['cluster'])}).database({kql_string(it['adx']['database'])})"
                       f".[{kql_string(rels[0])}]")
                op = kusto_mgmt(uri, db, f".set-or-append async [{kql_string(table)}] <| {src}",
                                retry=False)[0]["OperationId"]
                kusto_wait(uri, db, op, f"copy {table} from ADX")
                rels = []
            for i in range(0, len(rels), 50):
                sources = ", ".join(f"h'https://{ADLS_ACCOUNT}.blob.core.windows.net/{ADLS_CONTAINER}/"
                                    f"{quote(it['folder'] + '/data/' + r, safe='/')};impersonate'" for r in rels[i:i + 50])
                op = kusto_mgmt(uri, db, f".ingest async into table [{kql_string(table)}] ({sources}) "
                                         "with (format='parquet')", retry=False)[0]["OperationId"]
                kusto_wait(uri, db, op, f"ingest {table}")
            done[table] = kusto_count(uri, db, table)
            save_state()
            print(f"    {table}: {done[table]:,} rows")
    finally:
        for t, pol in saved.items():
            kusto_mgmt(uri, db, f".alter table [{kql_string(t)}] policy update {kql_string(pol)}")
        if saved:
            st["policies"] = {}
            save_state()
    log(it["workspace"], it, "KQL data", "OK" if not errors else ("Partial" if done else "Failed"),
        f"{len(done)} table(s), {sum(done.values()):,} rows" + (f"; update policies of {len(saved)} table(s) paused "
        "and put back" if saved else "") + (f" | {len(errors)} problem(s), first: {errors[0]}" if errors else ""))


for it in [i for i in TODO if i["type"] == "KQLDatabase"] if PROCEED else []:
    print(f"KQL data: {it['workspace']} / {it['displayName']}")
    try:
        load_kql(it)
    except HttpError as exc:
        log(it["workspace"], it, "KQL data", "Failed", exc.short())
if not PROCEED:
    print("Skipped (dry run).")
