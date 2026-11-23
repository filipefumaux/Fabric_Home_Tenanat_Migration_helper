def seed_payload(owner, row_id):
    """2 KiB deterministic, independent high-entropy ASCII per row; retries regenerate identical data."""
    entropy = hashlib.shake_256(f"V2_T_STRESS_V1|{owner}|{row_id}".encode()).digest(1536)
    return base64.b64encode(entropy).decode("ascii")


def _seed_frame(plan):
    count, start = plan["rows"], plan["first_row"]
    if not 1 <= count <= _SEED_MAX_ROWS:
        raise SeedError("Source batch exceeds the bounded in-memory row limit.")
    values = [seed_payload(plan["seed_owner"], i) for i in range(start, start + count)]
    frame = pd.DataFrame({"seed_owner": [plan["seed_owner"]] * count, "seed_batch": [plan["seed_batch"]] * count,
                          "seed_row": range(start, start + count), "payload": values})
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("ascii"))
    if plan.get("payloadSha256") and plan["payloadSha256"] != digest.hexdigest():
        raise SeedError("Synthetic source fingerprint changed. Keep the same producer/runtime or use a fresh PREFIX.")
    return frame, digest.hexdigest()


def _seed_encode_parquet(frame):
    import pyarrow as pa
    import pyarrow.parquet as pq
    out = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), out, compression="snappy",
                   use_dictionary=False, row_group_size=min(len(frame), 1024))
    data = out.getvalue()
    if len(data) > _SEED_MAX_FILE_BYTES:
        raise SeedError("Generated source Parquet exceeds the 64 MiB per-file memory bound.")
    return data


def _seed_properties(resp):
    props = {}
    for entry in resp.headers.get("x-ms-properties", "").split(","):
        if "=" in entry:
            k, v = entry.strip().split("=", 1)
            props[k] = base64.b64decode(v).decode("utf-8")
    return props


def _seed_put_once(item_id, path, data, owner, intent=False):
    """Exclusive file create + verified prefix append. Never truncate even an interrupted owned upload.

    Small JSON intents are also in file properties so an interrupted intent itself can be repaired.
    Parquet bytes are deterministically regenerated from the preceding immutable intent.
    """
    _seed_check_deadline()
    if not isinstance(data, bytes) or len(data) > _SEED_MAX_FILE_BYTES:
        raise SeedError("Owned upload must be bytes within the 64 MiB memory bound.")
    sha = hashlib.sha256(data).hexdigest()
    props = {"v2tOwner": owner, "v2tSha256": sha}
    if intent:
        if len(data) > 4096:
            raise SeedError("Source JSON intent exceeds its 4 KiB metadata bound.")
        props["v2tIntent"] = data.decode("utf-8")
    hdr = {"x-ms-version": "2023-11-03", "If-None-Match": "*",
           "x-ms-properties": ",".join(k + "=" + b64(v) for k, v in props.items())}
    try:
        http("PUT", _seed_url(item_id, path), "storage", params={"resource": "file"}, headers=hdr)
    except HttpError as exc:
        if exc.status not in (409, 412):
            raise
    existing = _seed_read(item_id, path)
    if existing is None:
        raise SeedError(f"Owned file disappeared during upload: {path}. Rerun after investigating concurrent activity.")
    actual_props = _seed_properties(existing)
    if any(actual_props.get(k) != v for k, v in props.items()) or not data.startswith(existing.content):
        raise SeedError(f"Ownership/content collision at {path}. Use a fresh PREFIX; no existing data was overwritten.")
    if existing.content == data:
        return
    position = len(existing.content)
    etag = existing.headers.get("ETag")
    if not etag:
        raise SeedError(f"Missing OneLake ETag at {path}. Refusing to append without a conditional flush.")
    http("PATCH", _seed_url(item_id, path), "storage", params={"action": "append", "position": str(position)},
         data=data[position:], headers={"x-ms-version": "2023-11-03"})
    # ADLS rejects conditional headers on append; the ETag condition belongs on the committing flush.
    http("PATCH", _seed_url(item_id, path), "storage", params={"action": "flush", "position": str(len(data))},
         data=b"", headers={"x-ms-version": "2023-11-03", "If-Match": etag})
    verified = _seed_read(item_id, path)
    if verified is None or hashlib.sha256(verified.content).hexdigest() != sha:
        raise SeedError(f"Owned source checksum verification failed: {path}. Rerun the same parameters; don't overwrite it.")


def _seed_json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _seed_get_plan(item_id, path, owner):
    resp = _seed_read(item_id, path)
    if resp is None:
        raise SeedError(f"Missing tracked source intent: {path}. Refusing to infer ownership from a filename.")
    props = _seed_properties(resp)
    if props.get("v2tOwner") != owner or not props.get("v2tIntent"):
        raise SeedError(f"Unowned source intent at {path}. Use a fresh PREFIX.")
    expected = props["v2tIntent"].encode("utf-8")
    if hashlib.sha256(expected).hexdigest() != props.get("v2tSha256"):
        raise SeedError(f"Corrupt source intent properties at {path}. No data was changed.")
    if resp.content != expected:
        _seed_put_once(item_id, path, expected, owner, intent=True)
    return json.loads(expected)


def _seed_lh_path(lh_id, table):
    props = fabric("GET", f"/workspaces/{WS}/lakehouses/{lh_id}").get("properties") or {}
    schema = props.get("defaultSchema") or ""
    if not isinstance(schema, str) or (schema and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", schema)):
        raise SeedError("Unknown Lakehouse schema layout. Inspect defaultSchema before creating owned test data.")
    return f"Tables/{schema + '/' if schema else ''}{table}"


def seed_small_lakehouse(lh_id, frame):
    """The measured/stress path never overwrites even the original small fixture. Legacy mode is unchanged."""
    table = f"{PREFIX}_sales"
    path = _seed_lh_path(lh_id, table)
    snap = seed_delta_snapshot(lh_id, path)
    description = f"V2_T_SMALL_V1:{WS}:{PREFIX}"
    if snap is not None:
        meta = snap["metadata"] or {}
        columns = [f["name"] for f in json.loads(meta.get("schemaString") or "{}").get("fields", [])]
        if meta.get("description") != description or columns != list(frame.columns) or snap["rows"] != len(frame):
            raise SeedError(f"Ownership/schema collision at {table}. Leave existing fixtures alone and use a fresh PREFIX.")
        return path
    if _seed_list(lh_id, path):
        raise SeedError(f"Non-Delta data already exists at {path}. Refusing to overwrite it.")
    from deltalake import write_deltalake
    write_deltalake(f"abfss://{WS}@{ONELAKE_DFS.split('://', 1)[1]}/{lh_id}/{path}", frame, mode="error",
                    description=description,
                    storage_options={"bearer_token": notebookutils.credentials.getToken("storage"),
                                     "use_fabric_endpoint": "true"})
    return path


def _seed_validate_plan(plan, pool, batch, start):
    if (plan.get("seed_owner") != pool["owner"] or plan.get("seed_batch") != batch or plan.get("first_row") != start
            or type(plan.get("rows")) is not int or not 1 <= plan["rows"] <= _SEED_MAX_ROWS
            or plan.get("kind") not in ("delta", "parquet")
            or not re.fullmatch(r"[0-9a-f]{64}", plan.get("payloadSha256", ""))):
        raise SeedError("Source ownership/schema/range collision in a tracked batch. Use a fresh PREFIX.")
    if plan["kind"] == "parquet":
        if (plan.get("path") != f"{pool['root']}/source/{batch:06d}.parquet"
                or not re.fullmatch(r"[0-9a-f]{64}", plan.get("fileSha256", ""))
                or type(plan.get("storedBytes")) is not int or not 0 < plan["storedBytes"] <= _SEED_MAX_FILE_BYTES):
            raise SeedError("Unsafe tracked Parquet source path/size/checksum.")


def _seed_pool():
    global _SEED_POOL
    if _SEED_POOL is not None:
        return _SEED_POOL
    lh_id = IDS.get("Lakehouse")
    if not lh_id:
        raise SeedError("Large seeding requires this group's successfully created Lakehouse.")
    table, root = f"{PREFIX}_stress", f"Files/_v2t_stress/{PREFIX}"
    path = _seed_lh_path(lh_id, table)
    scope = {"producer": "V2_T_STRESS_V1", "workspaceId": WS, "lakehouseId": lh_id, "prefix": PREFIX,
             "targetBytes": _SEED_TARGET_BYTES, "payloadBytes": 2048, "tablePath": path}
    owner = hashlib.sha256(_seed_json_bytes(scope)).hexdigest()
    pool = {"lh": lh_id, "table": table, "tablePath": path, "root": root, "owner": owner, "plans": [],
            "description": "V2_T_STRESS_V1:" + owner}
    snap = seed_delta_snapshot(lh_id, path)
    if snap is not None and (snap["metadata"] or {}).get("description") != pool["description"]:
        raise SeedError(f"Ownership collision at {path}. Never adopt an existing user/other-profile table.")
    if snap is None and _seed_list(lh_id, path):
        raise SeedError(f"Existing non-Delta/unpublished data at {path}. Refusing to overwrite or infer ownership.")
    _seed_put_once(lh_id, root + "/owner.json", _seed_json_bytes(scope), owner, intent=True)
    entries = _seed_list(lh_id, root + "/batches")
    files = sorted(p["name"].rsplit("/", 1)[-1] for p in entries)
    if len(files) > _SEED_MAX_BATCHES or any(not re.fullmatch(r"\d{6}\.json", f) for f in files):
        raise SeedError("Unexpected/too many source intents. Refusing untracked synthetic ranges.")
    start, raw_seen = 0, False
    for n, filename in enumerate(files):
        if filename != f"{n:06d}.json":
            raise SeedError("Gap in tracked source batches. Do not append past a missing intent.")
        plan = _seed_get_plan(lh_id, f"{root}/batches/{filename}", owner)
        _seed_validate_plan(plan, pool, n, start)
        if raw_seen and plan["kind"] == "delta":
            raise SeedError("Lakehouse append appears after frozen source top-ups. Refusing an ambiguous source history.")
        raw_seen |= plan["kind"] == "parquet"
        pool["plans"].append(plan)
        start += plan["rows"]
    _seed_lh_groups(pool, snap)
    _SEED_POOL = pool
    return pool


def _seed_lh_groups(pool, snap):
    if snap is None:
        return {}
    meta = snap["metadata"] or {}
    schema = json.loads(meta.get("schemaString") or "{}")
    columns = [(f.get("name"), f.get("type")) for f in schema.get("fields", [])]
    if meta.get("description") != pool["description"] or columns != _SEED_COLUMNS or meta.get("partitionColumns"):
        raise SeedError("Owned Lakehouse stress table ownership/schema changed. No append is safe.")
    groups = {}
    for filename, add in snap["active"].items():
        stats = _seed_stats(add)
        lo, hi, nulls = stats.get("minValues", {}), stats.get("maxValues", {}), stats.get("nullCount", {})
        batch, first, last = lo.get("seed_batch"), lo.get("seed_row"), hi.get("seed_row")
        if (type(batch) is not int or hi.get("seed_batch") != batch or type(first) is not int or type(last) is not int
                or lo.get("seed_owner") != pool["owner"] or hi.get("seed_owner") != pool["owner"]
                or any(nulls.get(c) != 0 for c, _ in _SEED_COLUMNS)
                or last - first + 1 != stats["numRecords"]):
            raise SeedError("Lakehouse source statistics don't describe a complete owned batch/range. "
                            "Inspect compaction/schema/concurrent writers before resuming.")
        groups.setdefault(batch, []).append((first, last, stats["numRecords"], filename, add["size"]))
    result = {}
    for batch, ranges in groups.items():
        ranges.sort()
        if not 0 <= batch < len(pool["plans"]):
            raise SeedError("Untracked batch in the owned Lakehouse. Refusing to append.")
        plan = pool["plans"][batch]
        pos = plan["first_row"]
        for first, last, count, _, _ in ranges:
            if first != pos:
                raise SeedError("Overlapping/missing source rows; refusing retry duplicates.")
            pos = last + 1
        if plan["kind"] != "delta" or pos != plan["first_row"] + plan["rows"]:
            raise SeedError("Published Lakehouse range doesn't match its immutable intent.")
        result[batch] = {"seed_batch": batch, "rows": plan["rows"], "first_row": plan["first_row"],
                         "paths": [(pool["tablePath"] + "/" + r[3], r[4]) for r in ranges]}
    if sorted(result) != list(range(len(result))):
        raise SeedError("Gap in committed Lakehouse batches; no append is safe.")
    return result


def _seed_delta_runtime():
    try:
        from deltalake import write_deltalake, CommitProperties, Transaction, WriterProperties
    except ImportError as exc:
        raise SeedError("Large seeding needs the existing Fabric Python deltalake runtime with CommitProperties, "
                        "Transaction and WriterProperties (delta-rs Python 0.22+ API). Use an approved compatible "
                        "Fabric Python environment; no packages are installed by this producer. " + str(exc)) from exc
    return write_deltalake, CommitProperties, Transaction, WriterProperties


def _seed_source(batch, wanted_rows, as_delta=False):
    """Reuse the same immutable source for WH and KQL. Only top-ups go to Files, not the size-gated LH table."""
    pool = _seed_pool()
    _seed_check_deadline()
    if not 0 <= batch < _SEED_MAX_BATCHES:
        raise SeedError(f"Source batch limit {_SEED_MAX_BATCHES} reached before the physical size target. "
                        "Inspect compression/metrics; no logical-size success fallback is allowed.")
    plans = pool["plans"]
    data, frame = None, None
    if batch == len(plans):
        if as_delta and any(p["kind"] != "delta" for p in plans):
            raise SeedError("Lakehouse size changed after consumer top-ups. Use a fresh PREFIX instead of enlarging it blindly.")
        plan = {"seed_owner": pool["owner"], "seed_batch": batch, "first_row": sum(p["rows"] for p in plans),
                "rows": int(wanted_rows), "kind": "delta" if as_delta else "parquet"}
        frame, plan["payloadSha256"] = _seed_frame(plan)
        if not as_delta:
            data = _seed_encode_parquet(frame)
            plan.update(path=f"{pool['root']}/source/{batch:06d}.parquet",
                        fileSha256=hashlib.sha256(data).hexdigest(), storedBytes=len(data))
        _seed_put_once(pool["lh"], f"{pool['root']}/batches/{batch:06d}.json", _seed_json_bytes(plan),
                       pool["owner"], intent=True)
        plans.append(plan)
    elif batch < len(plans):
        plan = plans[batch]
    else:
        raise SeedError("Source batch gap; refusing to generate an untracked range.")
    if plan["kind"] == "delta":
        snap = seed_delta_snapshot(pool["lh"], pool["tablePath"])
        groups = _seed_lh_groups(pool, snap)
        if batch not in groups:
            writer, CommitProperties, Transaction, WriterProperties = _seed_delta_runtime()
            if frame is None:
                frame, _ = _seed_frame(plan)
            writer(f"abfss://{WS}@{ONELAKE_DFS.split('://', 1)[1]}/{pool['lh']}/{pool['tablePath']}", frame,
                   mode="error" if snap is None else "append", description=pool["description"],
                   storage_options={"bearer_token": notebookutils.credentials.getToken("storage"),
                                    "use_fabric_endpoint": "true"},
                   writer_properties=WriterProperties(compression="SNAPPY", statistics_truncate_length=128),
                   # Delta 0.22 counts total commit attempts here; zero never attempts an append.
                   commit_properties=CommitProperties(app_transactions=[Transaction("v2t-" + pool["owner"], batch)],
                                                      max_commit_retries=1))
            def committed():
                current = seed_delta_snapshot(pool["lh"], pool["tablePath"])
                found = _seed_lh_groups(pool, current)
                if batch not in found:
                    raise _SeedNotReady(f"Owned Delta batch {batch} not published yet.")
                return found
            groups = _seed_wait(committed, "Owned Lakehouse source commit")
        return {**plan, **groups[batch]}
    if as_delta:
        raise SeedError("Cannot append a raw consumer source into the frozen size-gated Lakehouse table.")
    if data is None:
        frame, _ = _seed_frame(plan)
        data = _seed_encode_parquet(frame)
    if len(data) != plan["storedBytes"] or hashlib.sha256(data).hexdigest() != plan["fileSha256"]:
        raise SeedError("Parquet source encoding changed. Keep the original approved runtime; don't replace existing source files.")
    _seed_put_once(pool["lh"], plan["path"], data, pool["owner"])
    return {**plan, "paths": [(plan["path"], plan["storedBytes"])]}


def _seed_destination_marker(family, item_id, table, exists):
    pool = _seed_pool()
    path = f"{pool['root']}/{family.lower()}-{item_id}.json"
    value = {"producer": pool["description"], "itemId": item_id, "table": table,
             "columns": _SEED_COLUMNS, "sourceLakehouseId": pool["lh"]}
    if _seed_read(pool["lh"], path) is None and exists:
        raise SeedError(f"Ownership collision at {family} {table}: a table exists without its owned intent. "
                        "Use a fresh PREFIX; even an empty matching schema isn't permission to adopt user data.")
    _seed_put_once(pool["lh"], path, _seed_json_bytes(value), pool["owner"], intent=True)
