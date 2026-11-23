SCRIPTS = f"{PREFIX}/data/scripts"


def wh_table_name(root):
    parts = root.split("/")[1:]                      # Tables/<schema>/<table> or Tables/<table>
    return sql_ident(parts[0] if len(parts) > 1 else "dbo") + "." + sql_ident(parts[-1])


def wh_batches(it, root, res):
    base = f"https://{ADLS_ACCOUNT}.blob.core.windows.net/"
    urls = [base + quote(f"{ADLS_CONTAINER}/{it['folder']}/data/{root}/{p}", safe="/") for p in res["files"]]
    columns = res.get("columns")
    if columns:
        target = ", ".join(sql_ident(c["name"]) for c in columns)
        source = ", ".join("[src]." + sql_ident(c["physical"]) for c in columns)
        schema = ", ".join(sql_ident(c["physical"]) + " " + c["type"] for c in columns)
        # Fabric binds Parquet fields by their physical names, not renamed WITH column paths.
        selects = [f"SELECT {source}\nFROM OPENROWSET(BULK '{u}', FORMAT = 'PARQUET')\n"
                   f"WITH ({schema}) AS [src]" for u in urls]
        return [f"INSERT INTO {wh_table_name(root)} ({target})\n" + "\nUNION ALL\n".join(selects[i:i + 100]) + ";"
                for i in range(0, len(selects), 100)]
    return [f"COPY INTO {wh_table_name(root)}\nFROM " + ",\n    ".join("'" + u + "'" for u in urls[i:i + 100]) +
            "\nWITH (FILE_TYPE = 'PARQUET');" for i in range(0, len(urls), 100)]


def wh_count(it, root):
    df = sql_query(it["ws"], it["target"], "Warehouse", f"SELECT COUNT_BIG(*) AS n FROM {wh_table_name(root)}")
    return int(df.iloc[0, 0])


def load_warehouse(it):
    out = [f"-- Load the backed-up tables into the restored Warehouse '{it['displayName']}' (backup {SEC['runId']}).",
           "-- Run as a user with Storage Blob Data Reader on the backup container. Load only EMPTY tables.", ""]
    for root, res in sorted(it["tables"].items()):
        out += wh_batches(it, root, res) if res["files"] else [f"-- {wh_table_name(root)}: empty, nothing to load"]
        out.append(f"SELECT COUNT_BIG(*) AS rows_now, {res['rows'] if res['rows'] is not None else 'NULL'} AS "
                   f"rows_expected FROM {wh_table_name(root)};\n")
    for m in it["manual"]:
        out.append(f"-- NOT SCRIPTED, load by hand: {m}")
    path = f"{SCRIPTS}/{safe_name(it['workspace'])}/{safe_name(it['displayName'])}.Warehouse_load.sql"
    adls_write(path, "\n".join(out) + "\n")
    if WAREHOUSE_LOAD != "run":
        log(it["workspace"], it, "Warehouse data", "Manual step", f"run {path} in the Warehouse ({len(it['tables'])} "
            "tables)" + (f"; {len(it['manual'])} table(s) by hand" if it["manual"] else ""))
        return
    st = item_state(it)
    done, started, errors = st.setdefault("done", {}), st.setdefault("started", []), []
    for root, res in sorted(it["tables"].items()):
        if root in done or not res["files"]:
            continue
        try:
            current = wh_count(it, root)
            if current:
                errors.append(f"{root}: has {current:,} rows " + ("after an interrupted load: check it, TRUNCATE it and "
                              "run again" if root in started else "already; not touched"))
                continue
            started.append(root)
            save_state()
            sql_execute(it["ws"], it["target"], "Warehouse", wh_batches(it, root, res))
            done[root] = wh_count(it, root)
            save_state()
            print(f"    {root}: {done[root]:,} rows")
        except Exception as exc:                   # ODBC and connector errors have several types
            errors.append(f"{root}: {str(exc)}")
    errors += [f"by hand: {m}" for m in it["manual"]]
    log(it["workspace"], it, "Warehouse data", "OK" if not errors else ("Partial" if done else "Failed"),
        f"{len(done)}/{len(it['tables'])} table(s) loaded; script {path}" +
        (f" | {len(errors)} problem(s), first: {errors[0]}" if errors else ""))


def load_sql_objects(it):
    parts = [f"-- Views, functions and procedures of '{it['displayName']}' (backup {SEC['runId']}).",
             "-- Run in the restored SQL analytics endpoint after its tables have appeared there.", ""]
    bodies = {}
    for rel in sorted(it["sql"]):
        bodies[rel] = adls_read(f"{it['folder']}/{rel}").decode("utf-8").rstrip()
        parts += [f"-- {rel}", bodies[rel], "GO", ""]
    path = f"{SCRIPTS}/{safe_name(it['workspace'])}/{safe_name(it['displayName'])}.SQLEndpoint_objects.sql"
    adls_write(path, "\n".join(parts))
    if SQL_OBJECTS != "run":
        log(it["workspace"], it, "SQL objects", "Manual step", f"run {path}")
        return
    existing = set(sql_query(it["ws"], it["target"], "SQLEndpoint",
                             "SELECT s.name + '/' + o.name AS k FROM sys.objects o JOIN sys.schemas s "
                             "ON s.schema_id = o.schema_id WHERE o.is_ms_shipped = 0", columns=["k"]).iloc[:, 0])
    todo = {r: b for r, b in bodies.items() if f"{r.split('/')[1]}/{r.rsplit('/', 1)[1][:-4]}" not in existing}
    errors = {}
    for _ in range(3):                             # objects can depend on each other: retry the failures
        for rel in list(todo):
            try:
                sql_execute(it["ws"], it["target"], "SQLEndpoint", [todo[rel]])
                todo.pop(rel)
                errors.pop(rel, None)
            except Exception as exc:
                errors[rel] = str(exc)
        if not todo:
            break
    log(it["workspace"], it, "SQL objects", "OK" if not todo else "Partial",
        f"{len(bodies) - len(todo)}/{len(bodies)} object(s) present; script {path}" +
        (f" | {len(todo)} failed, first: {next(iter(errors.items()))}" if todo else ""))


for it in [i for i in TODO if i["type"] in ("Warehouse", "SQLEndpoint")] if PROCEED else []:
    print(f"{it['type']}: {it['workspace']} / {it['displayName']}")
    try:
        (load_warehouse if it["type"] == "Warehouse" else load_sql_objects)(it)
    except Exception as exc:
        log(it["workspace"], it, "Warehouse data" if it["type"] == "Warehouse" else "SQL objects", "Failed",
            exc.short() if isinstance(exc, HttpError) else str(exc))
if not PROCEED:
    print("Skipped (dry run).")
