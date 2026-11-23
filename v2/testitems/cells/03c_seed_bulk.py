# One writer per (TARGET_WORKSPACE_ID, PREFIX). Parent owns copy count, distinct prefixes and serialization.
# Reruns reconcile committed ranges first; they never repeat a bulk load just because its acknowledgement was lost.

def _seed_validate_groups(records):
    pool, found = _seed_pool(), {}
    for row in records:
        batch = int(row["seed_batch"])
        if batch in found or not 0 <= batch < len(pool["plans"]):
            raise SeedError("Untracked/duplicate destination batch. Refusing to ingest into existing user data.")
        plan = pool["plans"][batch]
        if (int(row["rows"]) != plan["rows"] or int(row["distinct_rows"]) != plan["rows"]
                or int(row["first_row"]) != plan["first_row"]
                or int(row["last_row"]) != plan["first_row"] + plan["rows"] - 1
                or row["owner_min"] != pool["owner"] or row["owner_max"] != pool["owner"]
                or int(row["bad_payload"]) != 0):
            raise SeedError(f"Destination batch {batch} ownership/schema/range mismatch or partial ingestion. "
                            "No repair/duplicate append is attempted. Inspect the batch; use a fresh PREFIX if needed.")
        found[batch] = plan
    if sorted(found) != list(range(len(found))):
        raise SeedError("Destination has a batch gap. Refusing to append past unverified synthetic ranges.")
    return found


def _seed_verify_hashes(records, rows):
    pool = _seed_pool()
    expected = {i: hashlib.sha256(seed_payload(pool["owner"], i).encode()).hexdigest() for i in {0, rows - 1}}
    observed = {int(r["seed_row"]): str(r["payload_hash"]).lower() for r in records}
    if len(records) != len(expected) or observed != expected:
        raise SeedError("Destination first/last payload SHA-256 mismatch. Row counts alone aren't content verification.")


def _seed_drive(family, item_id, table, read_groups, measure, append, verify=None):
    global _SEED_DEADLINE
    _SEED_DEADLINE = time.monotonic() + _SEED_MAX_SECONDS
    for _ in range(_SEED_MAX_BATCHES + 1):
        _seed_check_deadline()
        groups = read_groups()
        rows = sum(p["rows"] for p in groups.values())
        stored = 0
        if rows:
            metric = measure(rows)
            stored = metric["storedBytes"]
            if metric["status"] == "OK":
                if verify:
                    verify(rows)
                plans = _seed_pool()["plans"][:len(groups)]
                metric["sourceFingerprint"] = hashlib.sha256(
                    "".join(f"{p['seed_batch']}:{p['rows']}:{p['payloadSha256']};" for p in plans).encode()).hexdigest()
                step(family, NAMES[family], item_id, "OK",
                     f"{table}: {rows:,} rows; {stored:,} {metric['sizeMetric']}; target {_SEED_TARGET_BYTES:,} bytes")
                return
        # Row widths are merely a first-batch proposal. Only the subsequent persisted metric decides success.
        bytes_per_row = stored / rows if rows else 2048
        increment = min(_SEED_TARGET_BYTES - stored, _SEED_TARGET_BYTES / 32)
        wanted = max(1, min(_SEED_MAX_ROWS, int(increment * .98 / bytes_per_row)))
        source = _seed_source(len(groups), wanted, as_delta=family == "Lakehouse")
        append(source)
    raise SeedError(f"{family} did not reach its physical size target within {_SEED_MAX_BATCHES} batches. "
                    "Inspect compression/publication errors; rerun the same parameters to resume. No data was removed.")


def seed_large_lakehouse(lh_id):
    pool = _seed_pool()
    def groups():
        return _seed_lh_groups(pool, seed_delta_snapshot(lh_id, pool["tablePath"]))
    _seed_drive("Lakehouse", lh_id, pool["table"], groups,
                lambda rows: seed_measure_delta("Lakehouse", lh_id, pool["table"], pool["tablePath"],
                                                rows, _SEED_TARGET_BYTES),
                lambda source: None)  # _seed_source commits the owned Delta batch atomically


def _seed_require_lakehouse():
    if SEED_METRICS.get("Lakehouse", {}).get("status") != "OK":
        raise SeedError("Large Lakehouse seeding/measurement failed. Bulk consumers are blocked; fix it and rerun "
                        "the same PREFIX. No new source or destination batches were appended.")
    return _seed_pool()


def _seed_sql_run(conn, statement):
    """Same SDK EXEC/SELECT contract as sql_run, but stress writes must not hide a commit failure."""
    escaped = statement.replace("'", "''")
    conn.query(f"EXEC(N'{escaped}'); SELECT 1 AS done;")
    if hasattr(conn, "commit"):
        conn.commit()


def seed_large_warehouse(wh_id, conn):
    pool = _seed_require_lakehouse()
    short, table = f"{PREFIX}_stress", f"dbo.{PREFIX}_stress"
    columns = conn.query("SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
                         f"WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = '{short}' ORDER BY ORDINAL_POSITION")
    exists = columns is not None and len(columns) > 0
    _seed_destination_marker("Warehouse", wh_id, table, exists)
    if not exists:
        _seed_sql_run(conn, f"CREATE TABLE {table} (seed_owner VARCHAR(64) NOT NULL, seed_batch BIGINT NOT NULL, "
                            "seed_row BIGINT NOT NULL, payload VARCHAR(2048) NOT NULL)")
        columns = conn.query("SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
                             f"WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = '{short}' ORDER BY ORDINAL_POSITION")
    expected = [("seed_owner", "varchar", 64), ("seed_batch", "bigint", None),
                ("seed_row", "bigint", None), ("payload", "varchar", 2048)]
    if columns is None or len(columns) != len(expected):
        raise SeedError("Warehouse stress schema missing or changed; no bulk load is safe.")
    for actual, (column, dtype, length) in zip(columns.to_dict("records"), expected):
        if (actual["COLUMN_NAME"] != column or str(actual["DATA_TYPE"]).lower() != dtype
                or (length is not None and int(actual["CHARACTER_MAXIMUM_LENGTH"]) != length)):
            raise SeedError("Warehouse stress schema collision; don't alter/adopt the existing table.")
    def groups():
        frame = conn.query(
            f"SELECT seed_batch, COUNT_BIG(*) AS [rows], COUNT_BIG(DISTINCT seed_row) AS distinct_rows, "
            "MIN(seed_row) AS first_row, MAX(seed_row) AS last_row, MIN(seed_owner) AS owner_min, "
            "MAX(seed_owner) AS owner_max, "
            f"SUM(CAST(CASE WHEN payload IS NULL OR LEN(payload) <> 2048 THEN 1 ELSE 0 END AS BIGINT)) AS bad_payload "
            f"FROM {table} GROUP BY seed_batch")
        return _seed_validate_groups([] if frame is None else frame.to_dict("records"))
    def append(source):
        # COPY accepts exact OneLake files using the executing notebook user's identity. No credentials/roles are created.
        locations = ", ".join("'" + _seed_url(pool["lh"], p) + "'" for p, _ in source["paths"])
        _seed_sql_run(conn, f"COPY INTO {table} FROM {locations} WITH (FILE_TYPE = 'PARQUET')")
    def verify(rows):
        frame = conn.query(f"SELECT seed_row, CONVERT(VARCHAR(64), HASHBYTES('SHA2_256', payload), 2) AS payload_hash "
                           f"FROM {table} WHERE seed_row IN (0, {rows - 1})")
        _seed_verify_hashes([] if frame is None else frame.to_dict("records"), rows)
    _seed_drive("Warehouse", wh_id, table, groups,
                lambda rows: seed_measure_delta("Warehouse", wh_id, table, f"Tables/dbo/{short}", rows, _SEED_TARGET_BYTES),
                append, verify)


def seed_large_eventhouse(eh_id, kql_id, uri, database):
    pool = _seed_require_lakehouse()
    table = f"{PREFIX}_stress"
    found = kusto(uri, database, f".show tables | where TableName == '{table}'")
    if len(found) > 1 or (found and found[0].get("DocString") != pool["description"]):
        raise SeedError("Eventhouse stress ownership collision; don't merge/alter an existing table.")
    _seed_destination_marker("Eventhouse", kql_id, table, bool(found))
    if not found:
        definition = ", ".join(f"{c}:{t}" for c, t in _SEED_COLUMNS)
        kusto(uri, database, f".create table {table} ({definition}) with (docstring='{pool['description']}')")
    schema = kusto(uri, database, f".show table {table} schema as json")
    actual = json.loads(schema[0]["Schema"]).get("OrderedColumns", []) if len(schema) == 1 else []
    if [(c.get("Name"), c.get("CslType")) for c in actual] != _SEED_COLUMNS:
        raise SeedError("Eventhouse stress schema collision; no ingestion is safe.")
    def groups():
        records = kusto(uri, database,
                        f"{table} | summarize rows=count(), distinct_rows=count_distinct(seed_row), "
                        "first_row=min(seed_row), last_row=max(seed_row), owner_min=take_any(seed_owner), "
                        f"owner_max=take_any(seed_owner), bad_payload=countif(seed_owner != '{pool['owner']}' "
                        "or isempty(payload) or strlen(payload) != 2048) "
                        "by seed_batch", "query")
        result = _seed_validate_groups(records)
        if result:
            allowed = {"ingest-by:" + _seed_tag(pool, n) for n in result}
            tagsets = kusto(uri, database, f"{table} | extend ExtentTags=extent_tags() "
                            "| summarize by Tags=tostring(ExtentTags)", "query")
            if not tagsets:
                raise SeedError("No extent-tag evidence returned for populated KQL data; no retry is safe.")
            for extent in tagsets:
                tags = json.loads(extent["Tags"]) if extent.get("Tags") else []
                if (not isinstance(tags, list) or not tags or not all(isinstance(t, str) for t in tags)
                        or not set(tags).issubset(allowed)):
                    raise SeedError("Untracked KQL extent tags in the stress table. Refusing retry/ownership ambiguity.")
        return result
    def append(source):
        locators = ", ".join(f"h'abfss://{WS}@{ONELAKE_DFS.split('://', 1)[1]}/{pool['lh']}/"
                              f"{quote(_seed_relative(p), safe='/')};impersonate'" for p, _ in source["paths"])
        tag = _seed_tag(pool, source["seed_batch"])
        mapping = json.dumps([{"Column": c, "DataType": t, "Properties": {"Field": c}} for c, t in _SEED_COLUMNS],
                             separators=(",", ":"))
        results = kusto(uri, database, f".ingest into table {table} ({locators}) with (format='parquet', "
                        f"ingestionMapping='{mapping}', ingestIfNotExists='{json.dumps([tag])}', "
                        f"tags='{json.dumps(['ingest-by:' + tag])}')")
        if not results or any(r.get("HasErrors") not in (False, "false", "False") for r in results):
            raise SeedError("KQL bulk ingestion failed or omitted its result: " + json.dumps(results) +
                            ". Inspect ingestion errors; rerun only the same owned batches, never remove tags.")
    def verify(rows):
        records = kusto(uri, database, f"{table} | where seed_row in (0, {rows - 1}) "
                        "| project seed_row, payload_hash=hash_sha256(payload)", "query")
        _seed_verify_hashes(records, rows)
    _seed_drive("Eventhouse", eh_id, table, groups,
                lambda rows: seed_measure_kql(eh_id, uri, database, table, rows, _SEED_TARGET_BYTES), append, verify)


def _seed_tag(pool, batch):
    return f"v2t-{pool['owner']}-{batch:06d}"
