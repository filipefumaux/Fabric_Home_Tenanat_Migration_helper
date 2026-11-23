# Optional one-copy stress producer. No Spark session, external credentials, or package installation.
import hashlib, io, math
from urllib.parse import quote, unquote

_SEED_TARGET_BYTES = LARGE_DATA_MIB * 1024**2
_SEED_MAX_ROWS = 8192                # at most ~16 MiB of payload per in-memory frame
_SEED_MAX_BATCHES = 256             # shared source batches, including any consumer-specific top-ups
_SEED_PUBLISH_ATTEMPTS = 90          # 15 minutes, also bounded by the current family's overall deadline
_SEED_POLL_SECONDS = 10
_SEED_MAX_SECONDS = 2 * 60 * 60     # per storage family; no background work
_SEED_MAX_FILE_BYTES = 64 * 1024**2
_SEED_COLUMNS = [("seed_owner", "string"), ("seed_batch", "long"), ("seed_row", "long"), ("payload", "string")]
_SEED_DELTA_CACHE = {}
_SEED_POOL = None
_SEED_DEADLINE = None
SEED_METRICS = {}
if LARGE_DATA_MIB or MEASURE_DATA_SIZE:
    for _family in ("Lakehouse", "Warehouse", "Eventhouse"):
        if _family in CREATE_TYPES:
            SEED_METRICS[_family] = {
                "itemId": "", "table": f"{PREFIX}_stress" if LARGE_DATA_MIB else
                f"{PREFIX}_{dict(Lakehouse='sales', Warehouse='orders', Eventhouse='events')[_family]}",
                "rows": 0, "storedBytes": 0, "targetBytes": _SEED_TARGET_BYTES, "status": "PENDING",
                "sizeMetric": "compressed_extent_bytes" if _family == "Eventhouse" else "active_parquet_bytes"}


class SeedError(RuntimeError):
    pass


class _SeedNotReady(SeedError):
    pass


def _seed_check_deadline():
    if _SEED_DEADLINE is not None and time.monotonic() > _SEED_DEADLINE:
        raise SeedError("Stress producer time limit reached. Rerun the same parameters to resume owned batches; "
                        "inspect capacity/publishing/ingestion errors first. No data was removed.")


def _seed_relative(path):
    decoded = unquote(path)
    if (not decoded or decoded.startswith("/") or "\\" in decoded or ":" in decoded
            or any(p in ("", ".", "..") for p in decoded.split("/"))):
        raise SeedError(f"Unsafe OneLake/Delta path: {path!r}. Refusing an external or ambiguous data file.")
    return decoded


def _seed_url(item_id, path):
    return f"{ONELAKE_DFS}/{WS}/{item_id}/{quote(_seed_relative(path), safe='/')}"


def _seed_read(item_id, path):
    head = http("HEAD", _seed_url(item_id, path), "storage", ok404=True)
    if head is None:
        return None
    try:
        length = int(head.headers["Content-Length"])
    except (KeyError, ValueError) as exc:
        raise SeedError(f"Missing/invalid OneLake file length at {path}; refusing an unbounded download.") from exc
    if not 0 <= length <= _SEED_MAX_FILE_BYTES:
        raise SeedError(f"OneLake file at {path} exceeds the 64 MiB seed-reader memory bound; no data was downloaded.")
    return http("GET", _seed_url(item_id, path), "storage", ok404=True)


def _seed_list(item_id, directory):
    """List metadata only. In particular, never infer Warehouse bytes from a data-file listing."""
    out, continuation, seen = [], None, set()
    for _ in range(64):
        _seed_check_deadline()
        params = {"resource": "filesystem", "directory": f"{item_id}/{_seed_relative(directory)}",
                  "recursive": "false", "maxResults": "5000"}
        if continuation:
            params["continuation"] = continuation
        resp = http("GET", f"{ONELAKE_DFS}/{WS}", "storage", params=params, ok404=True)
        if resp is None:
            return out
        out.extend(resp.json().get("paths") or [])
        continuation = resp.headers.get("x-ms-continuation")
        if not continuation:
            return out
        if continuation in seen:
            raise SeedError("OneLake returned a repeated continuation token; refusing an incomplete size measurement.")
        seen.add(continuation)
    raise SeedError("OneLake metadata listing exceeded 64 pages; narrow the test scope instead of assuming a size.")


def _seed_stats(add):
    stats = add.get("stats")
    if isinstance(stats, str):
        stats = json.loads(stats)
    return stats if isinstance(stats, dict) else (add.get("stats_parsed") or {})


def _seed_checkpoint_rows(raw):
    import pyarrow.parquet as pq  # already part of the Fabric Python / deltalake runtime
    reader = pq.ParquetFile(io.BytesIO(raw))
    for batch in reader.iter_batches(batch_size=1024):
        yield from batch.to_pylist()


def seed_delta_snapshot(item_id, table_path):
    """Replay active Delta add/remove actions, including classic checkpoints. add.size is physical Parquet bytes.

    Warehouse data files can be invisible in REST listings. Only _delta_log is listed/read here.
    No row length, Parquet footer uncompressed size, or historical/tombstoned file is counted.
    """
    table_path = _seed_relative(table_path)
    entries = _seed_list(item_id, table_path + "/_delta_log")
    names = {p["name"].rsplit("/", 1)[-1] for p in entries if str(p.get("isDirectory", "")).lower() != "true"}
    logs = sorted(int(n[:20]) for n in names if re.fullmatch(r"\d{20}\.json", n))
    if not names:
        return None
    if len(logs) > 4096:
        raise SeedError("Delta log exceeds the bounded test-item reader (4096 commits). Use a fresh test PREFIX.")
    cached = _SEED_DELTA_CACHE.get((item_id, table_path))
    active, metadata, version = {}, None, -1

    def apply(action):
        nonlocal metadata
        if action.get("metaData") is not None:
            metadata = action["metaData"]
        if action.get("remove") is not None:
            active.pop(action["remove"]["path"], None)
        if action.get("add") is not None:
            add = action["add"]
            _seed_relative(add["path"])
            if not add["path"].lower().endswith(".parquet") or add.get("deletionVector"):
                raise SeedError("Unsupported active Delta file/deletion vector; cannot certify test-table stored size.")
            if type(add.get("size")) is not int or add["size"] < 0:
                raise SeedError("Delta add.size is missing/invalid; a logical row size is not a storage metric.")
            active[add["path"]] = add
            if len(active) > 16384:
                raise SeedError("Active Delta file count exceeds the seed reader's metadata memory bound (16384).")

    checkpoints = [int(n[:20]) for n in names if
                   re.fullmatch(r"\d{20}\.checkpoint(?:\.\d{10}\.\d{10})?\.parquet", n)]
    tail = [n for n in logs if cached and n > cached["version"]]
    cache_usable = (cached and logs and logs[-1] >= cached["version"]
                    and max(checkpoints, default=-1) <= logs[-1]
                    and (not tail or (tail[0] == cached["version"] + 1
                                      and all(b == a + 1 for a, b in zip(tail, tail[1:])))))
    full_log = logs and logs[0] == 0 and all(b == a + 1 for a, b in zip(logs, logs[1:]))
    if cache_usable:
        active, metadata, version = dict(cached["active"]), cached["metadata"], cached["version"]
    elif not full_log:
        last = _seed_read(item_id, table_path + "/_delta_log/_last_checkpoint")
        if last is None:
            raise _SeedNotReady("Delta log gap: version 0 and a complete checkpoint are not published yet.")
        cp = last.json()
        if "v2Checkpoint" in cp:
            raise SeedError("V2 Delta checkpoints are not supported by this bounded seed reader. "
                            "Use a fresh test table or add a verified reader; don't waive the size check.")
        version = int(cp["version"])
        parts = int(cp.get("parts") or 1)
        if not 1 <= parts <= 64:
            raise SeedError("Delta checkpoint exceeds the bounded seed reader (64 parts).")
        for i in range(1, parts + 1):
            name = (f"{version:020d}.checkpoint.parquet" if parts == 1 else
                    f"{version:020d}.checkpoint.{i:010d}.{parts:010d}.parquet")
            resp = _seed_read(item_id, table_path + "/_delta_log/" + name)
            if resp is None:
                raise _SeedNotReady(f"Delta checkpoint part not published: {name}")
            if len(resp.content) > _SEED_MAX_FILE_BYTES:
                raise SeedError("Delta checkpoint part exceeds the seed reader's 64 MiB memory bound.")
            for action in _seed_checkpoint_rows(resp.content):
                apply(action)
    for n in logs:
        if n <= version:
            continue
        if n != version + 1:
            raise _SeedNotReady(f"Delta log gap: expected {version + 1}, found {n}. Wait for log publication.")
        resp = _seed_read(item_id, f"{table_path}/_delta_log/{n:020d}.json")
        if resp is None:
            raise _SeedNotReady(f"Delta version {n} is not published yet.")
        if len(resp.content) > _SEED_MAX_FILE_BYTES:
            raise SeedError("Delta commit exceeds the seed reader's 64 MiB memory bound.")
        if not resp.content.strip():
            raise _SeedNotReady(f"Empty/incomplete published Delta commit {n}. Wait for commit publication.")
        try:
            for line in resp.content.decode("utf-8").splitlines():
                if line.strip():
                    apply(json.loads(line))
        except (ValueError, UnicodeError) as exc:
            raise _SeedNotReady(f"Incomplete/invalid published Delta commit {n}: {exc}") from exc
        version = n
    rows = 0
    for add in active.values():
        count = _seed_stats(add).get("numRecords")
        if type(count) is not int or count < 0:
            raise _SeedNotReady("Delta numRecords statistics not published; cannot align byte size with actual rows.")
        rows += count
    snap = {"active": active, "metadata": metadata, "version": version, "rows": rows,
            "storedBytes": sum(a["size"] for a in active.values())}
    _SEED_DELTA_CACHE[(item_id, table_path)] = snap
    return snap


def _seed_wait(check, what):
    last = "no published result"
    for attempt in range(_SEED_PUBLISH_ATTEMPTS):
        _seed_check_deadline()
        try:
            value = check()
            if value is not None:
                return value
        except _SeedNotReady as exc:
            last = str(exc)
        if attempt + 1 < _SEED_PUBLISH_ATTEMPTS:
            time.sleep(_SEED_POLL_SECONDS)
    raise SeedError(f"{what}: no valid positive persisted size after {_SEED_PUBLISH_ATTEMPTS} polls. {last} "
                    "Inspect Delta publishing/ingestion and existing permissions, then rerun the same parameters. "
                    "No roles, policies or data were changed to bypass this check.")


def seed_size_status(stored_bytes, target_bytes, rows):
    if type(rows) is not int or rows <= 0 or type(stored_bytes) is not int or stored_bytes <= 0:
        raise SeedError(f"Invalid persisted size measurement: rows={rows}, storedBytes={stored_bytes}. "
                        "A populated table cannot be reported as successfully measured at zero bytes.")
    if target_bytes and stored_bytes > target_bytes * 1.10:
        raise SeedError(f"Persisted size {stored_bytes} exceeds 110% of target {target_bytes}. "
                        "No cleanup/shrink is attempted; use a fresh PREFIX after investigating the physical metric.")
    return "OK" if not target_bytes or stored_bytes >= target_bytes * .95 else "GROWING"


def _seed_record(family, item_id, table, rows, stored_bytes, target_bytes):
    metric = {"itemId": item_id, "table": table, "rows": int(rows), "storedBytes": int(stored_bytes),
              "targetBytes": int(target_bytes), "sizeMetric": "compressed_extent_bytes" if family == "Eventhouse"
              else "active_parquet_bytes", "status": seed_size_status(int(stored_bytes), target_bytes, int(rows))}
    SEED_METRICS[family] = metric
    return metric


def seed_fail(family, exc):
    if family in SEED_METRICS:
        metric = SEED_METRICS[family]
        metric.update(status="Failed", error=str(exc))
        metric["itemId"] = metric["itemId"] or IDS.get(family, "")


def seed_measure_delta(family, item_id, table, table_path, expected_rows, target_bytes=0):
    def published():
        snap = seed_delta_snapshot(item_id, table_path)
        if snap is None or snap["rows"] != expected_rows or snap["storedBytes"] <= 0:
            raise _SeedNotReady(f"Delta publishing: expected {expected_rows} rows and positive active Parquet bytes; "
                                f"observed {None if snap is None else (snap['rows'], snap['storedBytes'])}.")
        return snap
    snap = _seed_wait(published, f"{family} {table}")
    return _seed_record(family, item_id, table, expected_rows, snap["storedBytes"], target_bytes)


def seed_measure_kql(item_id, uri, database, table, expected_rows, target_bytes=0):
    """CompressedSize of *all* current extents, not hot cache, OriginalSize or an estimate.

    Metric definition: https://learn.microsoft.com/kusto/management/show-extents
    Direct extents avoid .show table details' cached summary (documented as up to 15 minutes old).
    This excludes index bytes and replicas; it is the engine's compressed-extent byte metric, not billing size.
    """
    def published():
        extents = kusto(uri, database, f".show table {table} extents")
        rows, size = 0, 0
        for extent in extents:
            compressed, count = extent.get("CompressedSize"), extent.get("RowCount")
            if (isinstance(compressed, bool) or not isinstance(compressed, (int, float))
                    or not math.isfinite(compressed) or compressed < 0 or type(count) is not int or count < 0):
                raise _SeedNotReady("KQL CompressedSize/RowCount missing or invalid; OriginalSize isn't accepted.")
            if extent.get("DeletedRowCount", 0):
                raise SeedError("KQL extent has deleted rows; this is not an untouched owned seed table.")
            rows += count
            size += compressed
        if rows != expected_rows or rows <= 0 or size <= 0:
            raise _SeedNotReady(f"KQL publishing: expected {expected_rows} rows/positive compressed bytes; "
                                f"observed rows={rows}, compressedBytes={size}.")
        return int(size)
    size = _seed_wait(published, f"Eventhouse {table}")
    return _seed_record("Eventhouse", item_id, table, expected_rows, size, target_bytes)
