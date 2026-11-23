def copy_per_file(it, files):
    """The original copy: every file through this notebook, MAX_PARALLEL_FILES at a time. Returns (copied, there,
    bytes, errors)."""
    copied, there, nbytes, errors = 0, 0, 0, []
    with ThreadPoolExecutor(MAX_PARALLEL_FILES) as pool:
        futs = {pool.submit(restore_file, f"{it['folder']}/data/{rel}", size, it["ws"], it["target"], rel, True):
                (rel, size) for rel, size in files}
        for fut in as_completed(futs):
            rel, size = futs[fut]
            try:
                if fut.result() == "already there":
                    there += 1
                else:
                    copied += 1
                nbytes += size
            except HttpError as exc:
                errors.append(f"{rel}: {exc.short()}")
    return copied, there, nbytes, errors


def bulk_units(files, all_files):
    """Folders fastcp can copy whole: each Delta table folder, plus Files/ when no Delta table lives under it.
    Returns ({unit: [(rel, size)]}, [(rel, size) left for the per-file copy])."""
    roots = {table_root(r) for r, _ in all_files if table_root(r)}      # tables known from their _delta_log
    files_has_tables = any(r.startswith("Files/") for r in roots)
    units, rest = {}, []
    for rel, size in files:
        root = next((r for r in roots if rel.startswith(r + "/")), None)
        if root:
            units.setdefault(root, []).append((rel, size))
        elif rel.startswith("Files/") and not files_has_tables:
            units.setdefault("Files", []).append((rel, size))
        else:
            rest.append((rel, size))
    return units, rest


def copy_bulk(it, units, delta_logs):
    """fastcp each unit folder from the backup into the Lakehouse (existing files are never replaced). Phase 1
    (delta_logs False) leaves every _delta_log out; phase 2 copies only the _delta_log folders. Returns the error
    texts; files it didn't place are picked up by the per-file copy afterwards."""
    errors = []
    for unit in sorted(units):
        src_rel = unit + ("/_delta_log" if delta_logs else "")
        parent = src_rel.rsplit("/", 1)[0] if "/" in src_rel else ""
        dst_rel = parent if FASTCP_LAYOUT == "into" else src_rel
        print(f"  fastcp {src_rel} ({len(units[unit])} file(s), {human(sum(s for _, s in units[unit]))})")
        t0 = time.time()
        try:
            if dst_rel:
                onelake_mkdir(it["ws"], it["target"], dst_rel)
            fastcp(abfss_adls(f"{it['folder']}/data/{src_rel}"), abfss_onelake(it["ws"], it["target"], dst_rel),
                   exclude_delta_log=not delta_logs and unit != "Files")
            print(f"    done in {time.time() - t0:,.0f} s")
        except Exception as exc:
            errors.append(f"{src_rel}: {exc.short() if isinstance(exc, HttpError) else str(exc)[:300]}")
    return errors


def load_lakehouse(it):
    st = item_state(it)
    st["started"] = True
    st["tables"] = sorted(set(st.get("tables", [])) | {table_root(r) for r, _ in it["todo"] if table_root(r)})
    save_state()
    first = [f for f in it["todo"] if "/_delta_log/" not in f[0]]
    last = sorted(f for f in it["todo"] if "/_delta_log/" in f[0])
    method = LAKEHOUSE_COPY_METHOD
    if method == "fastcp":
        layout, why = fastcp_layout()
        if not layout:
            method = "per_file"
            log(it["workspace"], it, "Lakehouse data", "Info", f"fastcp not usable here ({why}); copying file by file")
    copied, there, nbytes, errors, bulk = 0, 0, 0, [], 0
    for phase, batch in ((1, first), (2, last)):
        if errors and phase == 2:
            errors.append("Delta logs not copied because data files failed: the tables stay invisible, not corrupt. "
                          "Run again.")
            break
        todo = batch
        if method == "fastcp" and batch:
            units, rest = bulk_units(batch, it["data"])
            if phase == 2:
                units = {u: f for u, f in units.items() if u != "Files"}
            bulk_errors = copy_bulk(it, units, delta_logs=phase == 2)
            now = dict(onelake_list(it["ws"], it["target"], "Files") + onelake_list(it["ws"], it["target"], "Tables"))
            rest_set = set(rest)
            placed = [(r, s) for r, s in batch if now.get(r) == s and (r, s) not in rest_set]
            bulk += len(placed)
            nbytes += sum(s for _, s in placed)
            placed_set = set(placed)
            todo = [f for f in batch if f not in placed_set]
            if bulk_errors:
                log(it["workspace"], it, "Lakehouse data", "Info",
                    f"fastcp failed for {len(bulk_errors)} folder(s), those files are copied one by one: {bulk_errors[0]}")
        c, t, b, e = copy_per_file(it, todo)
        copied, there, nbytes, errors = copied + c, there + t, nbytes + b, errors + e
    how = (f"{bulk} by fastcp, {copied} file by file" if method == "fastcp" else f"{copied} copied")
    log(it["workspace"], it, "Lakehouse data", "OK" if not errors else ("Partial" if copied or bulk else "Failed"),
        f"{how}, {there} already there, {human(nbytes)}" +
        (f" | {len(errors)} error(s), first: {errors[0]}" if errors else ""))

for it in [i for i in TODO if i["type"] == "Lakehouse"] if PROCEED else []:
    print(f"Lakehouse data: {it['workspace']} / {it['displayName']} ({len(it['todo'])} files)")
    try:
        load_lakehouse(it)
    except HttpError as exc:
        log(it["workspace"], it, "Lakehouse data", "Failed", exc.short())
if not PROCEED:
    print("Skipped (dry run).")
