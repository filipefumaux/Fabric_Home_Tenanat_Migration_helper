SQL_MODULES = """
SELECT s.name AS schema_name, o.name AS object_name, o.type_desc, m.definition
FROM sys.sql_modules AS m
JOIN sys.objects AS o ON o.object_id = m.object_id
JOIN sys.schemas AS s ON s.schema_id = o.schema_id
WHERE o.is_ms_shipped = 0
"""
SQL_PERMISSIONS = """
SELECT pr.name AS principal_name, pr.type_desc AS principal_type, pe.class_desc, pe.permission_name,
       pe.state_desc, OBJECT_SCHEMA_NAME(pe.major_id) AS schema_name, OBJECT_NAME(pe.major_id) AS object_name
FROM sys.database_permissions AS pe
JOIN sys.database_principals AS pr ON pr.principal_id = pe.grantee_principal_id
"""


def sql_connection(item):
    data_utils = getattr(notebookutils, "data", None)
    if data_utils is None or not hasattr(data_utils, "connect_to_artifact"):
        raise RuntimeError("notebookutils.data isn't available here; run this as a Python (not PySpark) notebook")
    attempts = [(item["id"], item["workspaceId"], item["type"])]
    if item["type"] == "SQLEndpoint" and item["properties"].get("lakehouseId"):
        attempts.append((item["properties"]["lakehouseId"], item["workspaceId"], "Lakehouse"))
    last = None
    for args in attempts:
        try:
            return data_utils.connect_to_artifact(*args)
        except Exception as exc:  # the utility raises several exception types
            last = exc
    raise RuntimeError(f"Couldn't connect: {str(last)}")


def query_frame(conn, text, columns):
    """The Fabric connector returns None, not an empty table, when a query has no rows."""
    df = conn.query(text)
    return pd.DataFrame(columns=columns) if df is None else df


def script_sql(item):
    if DRY_RUN:
        log(item, "SQL scripts", "Dry run", "would script views, functions, procedures and permissions")
        return
    try:
        conn = sql_connection(item)
        modules = query_frame(conn, SQL_MODULES, ["schema_name", "object_name", "type_desc", "definition"])
        permissions = query_frame(conn, SQL_PERMISSIONS, ["principal_name", "principal_type", "class_desc",
                                                          "permission_name", "state_desc", "schema_name",
                                                          "object_name"])
    except Exception as exc:
        log(item, "SQL scripts", "Failed", str(exc))
        return
    names = Counter()
    for row in modules.itertuples(index=False):
        base = f"{safe_name(row.schema_name)}/{safe_name(row.type_desc)}/{safe_name(row.object_name)}"
        names[base] += 1
        suffix = f"_{names[base]}" if names[base] > 1 else ""
        adls_write(f"{item['adls']}/sql/{base}{suffix}.sql", str(row.definition or "") + "\n")
    adls_write(f"{item['adls']}/sql/permissions.csv", permissions.to_csv(index=False))
    log(item, "SQL scripts", "OK", f"{len(modules)} objects, {len(permissions)} permission rows",
        files=len(modules) + 1)


for item in [i for i in ITEMS if i["type"] in SCRIPT_SQL_ITEM_TYPES]:
    script_sql(item)
show(pd.DataFrame([r for r in RESULTS if r["Step"] in ("SQL scripts", "KQL export")]))
