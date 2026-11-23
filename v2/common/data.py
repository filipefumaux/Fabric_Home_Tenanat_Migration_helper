

# ---------------------------------------------------------------- data restore helpers (V2_6 only)
KQL_TIMEOUT_SECONDS = 4 * 60 * 60
KQL_TERMINAL = {"Completed", "Failed", "PartiallySucceeded", "Abandoned", "BadInput", "Canceled", "Cancelled",
                "Throttled"}


def adls_read_range(path, start, end):
    return http("GET", adls_url(path), "storage", headers={**ADLS_HEADERS, "Range": f"bytes={start}-{end}"},
                label="adls").content


def onelake_url(workspace_id, item_id, rel_path):
    return f"{ONELAKE_DFS}/{workspace_id}/{item_id}/{quote(rel_path, safe='/')}"


def onelake_list(workspace_id, item_id, folder):
    """Every file under <item>/<folder> as (relative path, size). Empty if the folder is missing."""
    params = {"resource": "filesystem", "recursive": "true", "directory": f"{item_id}/{folder}"}
    out = []
    while True:
        try:
            resp = http("GET", f"{ONELAKE_DFS}/{workspace_id}", "storage", headers=ADLS_HEADERS, params=params,
                        label="onelake")
        except HttpError as exc:
            if exc.status == 404:
                return out
            raise
        for e in resp.json().get("paths", []):
            if str(e.get("isDirectory", "false")).lower() == "true":
                continue
            rel = e["name"].split("/", 1)[1] if "/" in e["name"] else e["name"]
            out.append((rel, int(e.get("contentLength") or 0)))
        token = resp.headers.get("x-ms-continuation")
        if not token:
            return out
        params = {**params, "continuation": token}


def onelake_size(workspace_id, item_id, rel_path):
    """Size of a OneLake file, or None if it doesn't exist."""
    try:
        resp = http("HEAD", onelake_url(workspace_id, item_id, rel_path), "storage", headers=ADLS_HEADERS,
                    label="onelake")
        return int(resp.headers.get("Content-Length") or 0)
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise


def onelake_delete(workspace_id, item_id, rel_path):
    http("DELETE", onelake_url(workspace_id, item_id, rel_path), "storage", headers=ADLS_HEADERS, label="onelake")


def restore_file(src_path, size, workspace_id, item_id, rel_path, may_replace_partial):
    """Copy one backup file into OneLake. Never overwrites a complete file: returns "copied" or "already there".
    may_replace_partial: this notebook started loading the item, so a different-size file is a leftover of an
    interrupted run and may be replaced."""
    url = onelake_url(workspace_id, item_id, rel_path)
    try:
        http("PUT", url, "storage", headers={**ADLS_HEADERS, "If-None-Match": "*"}, params={"resource": "file"},
             label="onelake", retry=False)
    except HttpError as exc:
        if exc.status != 409:
            raise
        existing = onelake_size(workspace_id, item_id, rel_path)
        if existing == size:
            return "already there"
        if not may_replace_partial:
            raise HttpError(409, "Exists", f"a different file already exists ({existing} bytes, backup {size})", url)
        http("PUT", url, "storage", headers=ADLS_HEADERS, params={"resource": "file"}, label="onelake")
    pos = 0
    while pos < size:
        chunk = adls_read_range(src_path, pos, min(pos + CHUNK_BYTES, size) - 1)
        if not chunk:
            raise HttpError(None, "ShortRead", f"no bytes at offset {pos}", src_path)
        http("PATCH", url, "storage", headers=ADLS_HEADERS, data=chunk,
             params={"action": "append", "position": str(pos)}, label="onelake")
        pos += len(chunk)
    http("PATCH", url, "storage", headers=ADLS_HEADERS, data=b"", params={"action": "flush", "position": str(pos)},
         label="onelake")
    return "copied"


def abfss_adls(path):
    return f"abfss://{ADLS_CONTAINER}@{ADLS_ACCOUNT}.dfs.core.windows.net/{path.strip('/')}"


def abfss_onelake(workspace_id, item_id, rel_path=""):
    return f"abfss://{workspace_id}@onelake.dfs.fabric.microsoft.com/{item_id}" + (f"/{rel_path.strip('/')}" if rel_path.strip("/") else "")


def onelake_mkdir(workspace_id, item_id, rel_path):
    """Create a OneLake folder (and its parents); no error if it exists."""
    http("PUT", onelake_url(workspace_id, item_id, rel_path), "storage", headers=ADLS_HEADERS,
         params={"resource": "directory"}, label="onelake")


FASTCP_FLAGS = "--overwrite=false"            # never replace a file that is already there
FASTCP_LAYOUT = None                          # "into" / "contents" once probed; "" = fastcp not usable here


def fastcp(src, dst, exclude_delta_log=False):
    flags = FASTCP_FLAGS + (" --exclude-path=_delta_log" if exclude_delta_log else "")
    ok = notebookutils.fs.fastcp(src, dst, True, {"flags": flags})
    if ok is False:
        raise RuntimeError(f"fastcp returned False for {src}")


def fastcp_layout():
    """How notebookutils.fs.fastcp places a folder, learned once on a tiny ADLS-to-ADLS copy (nothing is written to
    any Fabric item): "into" = copying folder T to an existing folder P gives P/T (azcopy), "contents" = it gives P
    itself. Also checks that the _delta_log exclusion works. Returns (layout or "", reason)."""
    global FASTCP_LAYOUT
    if FASTCP_LAYOUT is not None:
        return FASTCP_LAYOUT, ""
    base = f"{PREFIX}/_fastcp_probe_{NOW_ID}"
    reason = ""
    try:
        adls_write(f"{base}/src/t/keep.bin", b"k")
        adls_write(f"{base}/src/t/_delta_log/skip.json", b"s")
        adls_write(f"{base}/dst/_marker", b"m")
        fastcp(abfss_adls(f"{base}/src/t"), abfss_adls(f"{base}/dst"), exclude_delta_log=True)
        got = {p for p, _, d in adls_list(f"{base}/dst") if not d} - {"_marker"}
        if got == {"t/keep.bin"}:
            FASTCP_LAYOUT = "into"
        elif got == {"keep.bin"}:
            FASTCP_LAYOUT = "contents"
        else:
            reason = (f"unexpected layout {sorted(got)}" if not any("_delta_log" in p for p in got)
                      else "the _delta_log exclusion isn't applied")
    except Exception as exc:
        reason = (exc.short() if isinstance(exc, HttpError) else str(exc))[:300]
    finally:
        try:
            http("DELETE", adls_url(base), "storage", headers=ADLS_HEADERS, params={"recursive": "true"}, label="adls")
        except Exception:
            pass
    if FASTCP_LAYOUT is None:
        FASTCP_LAYOUT = ""
    return FASTCP_LAYOUT, reason


def table_root(rel):
    """'Tables/dbo/sales/_delta_log/0001.json' -> 'Tables/dbo/sales'; None outside a Delta table."""
    return rel.split("/_delta_log/")[0] if "/_delta_log/" in rel else None


def kusto_mgmt(query_uri, database, command, retry=True, kind="mgmt"):
    resp = http("POST", query_uri.rstrip("/") + f"/v1/rest/{kind}", "kusto",
                json_body={"db": database, "csl": command}, label="kusto", retry=retry)
    tables = resp.json().get("Tables") or []
    if not tables:
        return []
    cols = [c["ColumnName"] for c in tables[0].get("Columns", [])]
    return [dict(zip(cols, row)) for row in tables[0].get("Rows", [])]


def kql_string(text):
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def kusto_wait(uri, db, op, label):
    deadline = time.monotonic() + KQL_TIMEOUT_SECONDS
    while True:
        time.sleep(5)
        state = kusto_mgmt(uri, db, f".show operations {op}")[0]
        if state.get("State") in KQL_TERMINAL:
            if state.get("State") != "Completed":
                raise HttpError(None, state.get("State"), state.get("Status") or label, uri)
            return
        if time.monotonic() > deadline:
            raise HttpError(None, "Timeout", f"{label}: operation {op} still running", uri)


def kusto_count(uri, db, table):
    rows = kusto_mgmt(uri, db, f"[{kql_string(table)}] | count", kind="query")
    return int(rows[0].get("Count", 0)) if rows else 0


def delta_mapped_columns(metadata):
    """Validated logical/physical names and SQL types for a flat, column-mapped Warehouse table."""
    try:
        schema = json.loads(metadata.get("schemaString") or "")
    except (TypeError, ValueError) as exc:
        return [], [f"column mapping has no readable schemaString: {exc}"]
    if not isinstance(schema, dict) or schema.get("type") != "struct" or not schema.get("fields"):
        return [], ["column mapping schemaString must describe a non-empty struct"]
    if metadata.get("partitionColumns"):
        return [], ["partitioned column-mapped tables need loading by hand: partition values aren't in Parquet"]
    sql_types = {"boolean": "bit", "byte": "smallint", "short": "smallint", "integer": "int", "long": "bigint",
                 "float": "real", "double": "float", "string": "varchar(max)", "binary": "varbinary(max)",
                 "date": "date", "timestamp": "datetime2(6)", "timestamp_ntz": "datetime2(6)"}
    columns, logical, physical = [], set(), set()
    for field in schema["fields"]:
        if not isinstance(field, dict):
            return [], ["column mapping schema contains an invalid field"]
        name, typ = field.get("name"), field.get("type")
        props = field.get("metadata") or {}
        source = props.get("delta.columnMapping.physicalName") if isinstance(props, dict) else None
        if not all(isinstance(n, str) and n and len(n) <= 128 and "\0" not in n for n in (name, source)):
            return [], [f"column mapping field {name!r} has no valid logical/physical name"]
        if "." in source:
            return [], [f"column mapping physical name {source!r} can't be bound as a flat Parquet column"]
        if name in logical or source in physical:
            return [], [f"column mapping has duplicate logical/physical names for {name!r}"]
        sql_type = sql_types.get(typ) if isinstance(typ, str) else None
        decimal = re.fullmatch(r"decimal\((\d+),\s*(\d+)\)", typ) if isinstance(typ, str) else None
        if decimal and 1 <= int(decimal[1]) <= 38 and 0 <= int(decimal[2]) <= int(decimal[1]):
            sql_type = f"decimal({int(decimal[1])},{int(decimal[2])})"
        if not sql_type:
            return [], [f"column mapping field {name!r} has unsupported Delta type {typ!r}"]
        logical.add(name)
        physical.add(source)
        columns.append({"name": name, "physical": source, "type": sql_type})
    return columns, []


def delta_active_files(folder, table, files):
    """Replay the Delta snapshot, retaining mapped schema and known null counts for verified Warehouse loads."""
    logs = sorted(rel for rel, _ in files if rel.startswith(table + "/_delta_log/")
                  and re.search(r"/\d{20}\.json$", rel))
    active, problems, metadata = {}, [], None
    start = 0
    versions = [int(rel[-25:-5]) for rel in logs]
    if not versions or versions[0] != 0:
        cp = next((rel for rel, _ in files if rel == table + "/_delta_log/_last_checkpoint"), None)
        if not cp:
            return None, ["the Delta log has no version 0 and no checkpoint"]
        try:
            import pyarrow.parquet as pq, io as _io
            ver = int(json.loads(adls_read(f"{folder}/data/{cp}"))["version"])
            start = ver + 1
            checkpoints = [rel for rel, _ in files if rel.startswith(table + "/_delta_log/") and
                           re.search(rf"/{ver:020d}\.checkpoint(\.\d+\.\d+)?\.parquet$", rel)]
            if not checkpoints:
                return None, [f"Delta checkpoint {ver} is missing from the backup"]
            for rel in checkpoints:
                for row in pq.read_table(_io.BytesIO(adls_read(f"{folder}/data/{rel}"))).to_pylist():
                    if row.get("add"):
                        active[row["add"]["path"]] = row["add"]
                    if row.get("metaData"):
                        metadata = row["metaData"]
        except Exception as exc:
            return None, [f"couldn't read the Delta checkpoint: {exc}"]
    tail = [v for v in versions if v >= start]
    if tail and (tail[0] != start or any(b != a + 1 for a, b in zip(tail, tail[1:]))):
        return None, ["the backed-up Delta log has missing versions"]
    for rel in logs:
        if int(rel[-25:-5]) < start:
            continue
        for line in adls_read(f"{folder}/data/{rel}").decode("utf-8").splitlines():
            if not line.strip():
                continue
            action = json.loads(line)
            if "add" in action:
                active[action["add"]["path"]] = action["add"]
            elif "remove" in action:
                active.pop(action["remove"]["path"], None)
            elif "metaData" in action:
                metadata = action["metaData"]
    if metadata is None:
        return None, ["the Delta snapshot has no schema metadata"]
    try:
        mode = dict(metadata.get("configuration") or {}).get("delta.columnMapping.mode", "none")
    except (TypeError, ValueError):
        return None, ["the Delta snapshot has invalid schema configuration"]
    columns = []
    if mode in ("name", "id"):
        columns, mapping_problems = delta_mapped_columns(metadata)
        problems += mapping_problems
    elif mode != "none":
        problems.append(f"unsupported Delta column mapping mode {mode!r}")
    if any(a.get("deletionVector") for a in active.values()):
        problems.append("deletion vectors: some files have deleted rows that COPY INTO would bring back")
    have = {rel for rel, _ in files}
    missing = [p for p in active if f"{table}/{p}" not in have]
    if missing:
        problems.append("active Delta data files are missing from the backup: " + ", ".join(missing))
    rows, statistics = 0, []
    for a in active.values():
        try:
            stats = json.loads(a.get("stats") or "{}")
            n = stats["numRecords"]
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                raise ValueError("invalid row count")
            rows += n
            statistics.append(stats)
        except (TypeError, ValueError, KeyError):
            rows = None
            break
    result = {"files": sorted(active), "rows": rows}
    if columns:
        result["columns"], result["nullCounts"] = columns, {}
        if rows is None:
            problems.append("column-mapped table has no complete row count in its Delta statistics")
        else:
            nulls = [s.get("nullCount") for s in statistics]
            if any(n is not None and not isinstance(n, dict) for n in nulls):
                problems.append("column-mapped table has an invalid Delta nullCount object")
            else:
                for c in columns:
                    values = [n.get(c["physical"]) if n is not None else None for n in nulls]
                    if any(v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0
                           or v > s["numRecords"]) for v, s in zip(values, statistics)):
                        problems.append(f"invalid Delta NULL count for column {c['name']!r}")
                    elif all(v is not None for v in values):
                        result["nullCounts"][c["name"]] = sum(values)
    return result, sorted(set(problems))


def sql_ident(name):
    return "[" + str(name).replace("]", "]]") + "]"


def sql_connection(ws_id, item_id, item_type):
    """The Fabric connector opens SQL analytics endpoints through their parent Lakehouse."""
    if item_type == "SQLEndpoint":
        for lakehouse in fabric_pages(f"/workspaces/{ws_id}/lakehouses"):
            endpoint = (lakehouse.get("properties") or {}).get("sqlEndpointProperties") or {}
            if endpoint.get("id") and str(endpoint["id"]).lower() == str(item_id).lower():
                item_id, item_type = lakehouse["id"], "Lakehouse"
                break
        else:
            raise RuntimeError(f"No parent Lakehouse found for SQL endpoint {item_id} in workspace {ws_id}. "
                               "Check that the restored Lakehouse and its SQL endpoint are ready.")
    return notebookutils.data.connect_to_artifact(item_id, ws_id, item_type)


def sql_query(ws_id, item_id, item_type, text, columns=("n",)):
    """Read-only query against a Warehouse / SQL analytics endpoint (notebookutils data connector).
    The connector returns None, not an empty table, when there are no rows."""
    conn = sql_connection(ws_id, item_id, item_type)
    df = conn.query(text)
    return pd.DataFrame(columns=list(columns)) if df is None else df


def sql_execute(ws_id, item_id, item_type, batches):
    """Run batches through the same Fabric SQL connector used to create the V2_T test data."""
    conn = sql_connection(ws_id, item_id, item_type)
    for batch in batches:
        escaped = batch.replace("'", "''")
        conn.query(f"EXEC(N'{escaped}'); SELECT 1 AS done;")
