import io, os, shutil, subprocess, tarfile, tempfile
from collections import deque

KC_STEP = "Kusto Copy"
ADX_URI = ADX_CLUSTER_URI.rstrip("/")
ADX_HOST = ADX_URI.split("//", 1)[-1].split("/")[0]
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NOTABLE_RE = re.compile(r"error|exception|fail|warn|denied|forbidden|unauthori", re.I)


def kql_endpoint(item):
    props = item["properties"] or {}
    if not props.get("queryServiceUri"):
        props = fabric("GET", f"/workspaces/{item['workspaceId']}/kqlDatabases/{item['id']}").get("properties", {})
    return props.get("queryServiceUri"), props.get("databaseName") or item["displayName"]


def adx_database_for(item, database):
    for key, value in ADX_DATABASE_MAP.items():
        if norm_id(key) == item["id"] or key == database:
            return value
    return adx_safe_name(ADX_DATABASE_PATTERN.format(database=database, workspace=item["workspaceName"],
                                                     id=item["id"], id8=item["id"][:8]))


def folder_part(text):
    """Stable, URL-safe folder name: readable prefix + hash of the exact name (so case and symbols can't collide)."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text)[:60] + "_" + hashlib.sha1(text.encode()).hexdigest()[:8]


def staging_path(dest_db, item, table):
    return "/".join([KC_STAGING_FOLDER.strip("/"), ADX_HOST, folder_part(dest_db), item["id"], folder_part(table)])


def staging_uri(path):
    return f"https://{ADLS_ACCOUNT}.blob.core.windows.net/{ADLS_CONTAINER}/{path}"


def staging_has_progress(path):
    """True if Kusto Copy already left files (its progress) in this staging folder."""
    try:
        resp = http("GET", f"https://{ADLS_ACCOUNT}.dfs.core.windows.net/{ADLS_CONTAINER}", "storage",
                    headers=ADLS_HEADERS, label="adls", params={"resource": "filesystem", "recursive": "false",
                                                                "directory": path, "maxResults": "1"})
    except HttpError as exc:
        if exc.status == 404:
            return False
        raise
    return bool(resp.json().get("paths"))


def reset_staging(path):
    """Delete a staging folder (Kusto Copy's progress only; the rows are in the Eventhouse and copied again)."""
    params = {"recursive": "true"}
    while True:
        try:
            resp = http("DELETE", adls_url(path), "storage", headers=ADLS_HEADERS, params=params, label="adls")
        except HttpError as exc:
            if exc.status == 404:
                return
            raise
        token = resp.headers.get("x-ms-continuation")
        if not token:
            return
        params = {"recursive": "true", "continuation": token}


def yaml_str(value):
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid YAML double-quoted scalar


def job_yaml(job):
    lines = ["activities:",
             f"  - activityName: {yaml_str(job['table'])}",
             "    source:",
             f"      clusterUri: {yaml_str(job['source_uri'])}",
             f"      databaseName: {yaml_str(job['source_db'])}",
             f"      tableName: {yaml_str(job['table'])}",
             "    destination:",
             f"      clusterUri: {yaml_str(ADX_URI)}",
             f"      databaseName: {yaml_str(job['dest_db'])}",
             f"      tableName: {yaml_str(job['table'])}",
             "stagingStorageDirectories:",
             f"  - {yaml_str(job['staging'])}",
             f"copyMode: {yaml_str(KC_COPY_MODE)}"]
    if KC_EXPORT_COUNT:
        lines.append(f"exportCount: {int(KC_EXPORT_COUNT)}")
    return "\n".join(lines) + "\n"


def redact(line):
    line = ANSI_RE.sub("", line).rstrip()
    line = re.sub(r"(https?://[^\s?\"']+)\?[^\s\"']*", r"\1?<redacted>", line)   # SAS query strings
    secret = _kc_tokens.get("secret")
    return redact_sensitive_text(line.replace(secret, "<redacted>") if secret else line)


def install_kusto_copy():
    url = f"https://github.com/Azure/kusto-copy/releases/download/{quote(KC_VERSION)}/kusto-copy-linux.tar.gz"
    data = http("GET", url, timeout=900, label="github").content
    digest = hashlib.sha256(data).hexdigest()
    if digest != KC_SHA256.strip().lower():
        raise RuntimeError(f"Kusto Copy download {KC_VERSION} has SHA-256 {digest}, expected {KC_SHA256}. "
                           "Not running it. Check KC_VERSION and KC_SHA256 against the GitHub release page.")
    folder = tempfile.mkdtemp(prefix="kusto-copy-")
    target = os.path.join(folder, "kc")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        files = [m for m in tar.getmembers() if m.isfile()]
        exe = next((m for m in files if os.path.basename(m.name) in ("kc", "kusto-copy")), None)
        exe = exe or (files[0] if len(files) == 1 else None)
        if exe is None:
            raise RuntimeError("No Kusto Copy executable in the package: " + ", ".join(m.name for m in files))
        with tar.extractfile(exe) as src, open(target, "wb") as dst:   # only the executable, no path traversal
            shutil.copyfileobj(src, dst)
    os.chmod(target, 0o755)
    print(f"Kusto Copy {KC_VERSION} downloaded, SHA-256 verified: {target}")
    return target


def kc_environment():
    env = dict(os.environ)
    for name in ("AZURE_CLIENT_CERTIFICATE_PATH", "AZURE_FEDERATED_TOKEN_FILE", "AZURE_USERNAME", "AZURE_PASSWORD"):
        env.pop(name, None)
    env.update(AZURE_TENANT_ID=KC_TENANT_ID, AZURE_CLIENT_ID=KC_CLIENT_ID, AZURE_CLIENT_SECRET=kc_secret())
    env.setdefault("DOTNET_BUNDLE_EXTRACT_BASE_DIR", tempfile.gettempdir())
    # Kusto Copy asks for a named culture. With the ICU library (libicu) present, .NET uses it; without it, .NET
    # runs in invariant mode and must be allowed to map named cultures to the invariant one, or it stops with
    # CultureNotFoundException ("Only the invariant culture is supported").
    import glob
    has_icu = any(glob.glob(f"{d}/libicuuc.so*") for d in ("/usr/lib", "/usr/lib64", "/lib", "/lib64",
                                                           "/usr/lib/x86_64-linux-gnu", "/lib/x86_64-linux-gnu"))
    env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "0" if has_icu else "1"
    env["DOTNET_SYSTEM_GLOBALIZATION_PREDEFINED_CULTURES_ONLY"] = "false"
    return env


def run_kusto_copy(exe, job, env):
    """Run one table's job. Returns (exit code, timed out, log lines). The secret is passed only in the environment."""
    work = tempfile.mkdtemp(prefix="kc-job-")
    yaml_path = os.path.join(work, "job.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(job["yaml"])
    lines, last_print, timed_out = deque(maxlen=5000), 0.0, threading.Event()
    proc = subprocess.Popen([exe, "-y", yaml_path], cwd=work, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)

    def stop():
        timed_out.set()
        proc.kill()
    timer = threading.Timer(KC_TIMEOUT_HOURS * 3600, stop)
    timer.daemon = True
    timer.start()
    try:
        for raw in proc.stdout:
            line = redact(raw)
            if not line:
                continue
            lines.append(line)
            if NOTABLE_RE.search(line) or time.monotonic() - last_print > 600:   # errors, plus a heartbeat
                print(f"    [{job['table']}] {line}")
                last_print = time.monotonic()
        code = proc.wait()
    finally:
        timer.cancel()
        shutil.rmtree(work, ignore_errors=True)
    return code, timed_out.is_set(), list(lines)


def plan_database(item, seen):
    uri, database = kql_endpoint(item)
    if not uri:
        log(item, KC_STEP, "Failed", "No query URI returned for this KQL database")
        return []
    dest_db = adx_database_for(item, database)
    item["adxDatabase"] = dest_db
    missing = False
    if ADX_CREATE_DATABASES:
        try:
            existing = adx_existing_databases()
        except (HttpError, RuntimeError) as exc:
            log(item, KC_STEP, "Failed", f"Can't list the ADX databases in Azure: {exc}")
            return []
        if dest_db.lower() in existing:
            dest_db = item["adxDatabase"] = existing[dest_db.lower()]
        elif DRY_RUN:
            missing = True
            ADX_CREATED[dest_db] = "would create"
        else:
            print(f"  Creating ADX database {dest_db} on {ADX_HOST} ...")
            try:
                notes = create_adx_database(dest_db)
            except (HttpError, RuntimeError) as exc:
                log(item, KC_STEP, "Failed", f"Can't create ADX database '{dest_db}': {exc}. The Kusto Copy service "
                    "principal needs Contributor on the ADX cluster; or create the database yourself, make the "
                    "service principal Admin on it and set ADX_CREATE_DATABASES = False.")
                return []
            print(f"  Created ADX database {dest_db}" + (f" ({'; '.join(notes)})" if notes else ""))
    try:   # rows per destination table (an empty or missing table means a fresh copy, see stale_progress)
        dest_tables = {} if missing else \
            {r["TableName"]: int(r.get("TotalRowCount") or 0)
             for r in kusto_mgmt(ADX_URI, dest_db, ".show tables details", auth=kc_auth(ADX_URI))}
    except HttpError as exc:
        log(item, KC_STEP, "Access denied" if exc.status in (401, 403) else "Failed",
            f"ADX database '{dest_db}' on {ADX_HOST}: {exc.short()}. " + (
                "The service principal must be Admin on it (it is, on databases V2_2 created itself)."
                if ADX_CREATE_DATABASES else
                "Create the database first and make the service principal an Admin on it "
                "(or set ADX_CREATE_DATABASES = True)."))
        return []
    jobs, clashes = [], []
    for t in kusto_mgmt(uri, database, ".show tables details"):
        table = t["TableName"]
        key = (dest_db.lower(), table.lower())
        if key in seen and seen[key] != item["id"]:
            clashes.append(table)
            continue
        seen[key] = item["id"]
        path = staging_path(dest_db, item, table)
        job = {"item": item, "table": table, "source_uri": uri.rstrip("/"), "source_db": database,
               "dest_db": dest_db, "source_rows": int(t.get("TotalRowCount") or 0),
               "exists": table in dest_tables, "dest_rows": dest_tables.get(table, 0),
               "staging_path": path, "staging": staging_uri(path)}
        # Kusto Copy (BackfillAndNew) keeps in the staging folder what it already copied, and later runs copy only
        # newer rows. If the ADX table is empty or gone (database deleted and created again, table dropped or
        # cleared), that progress is stale: the run would copy nothing. Then the folder is reset and the whole
        # history copied again; no duplicates, the table is empty.
        job["stale_progress"] = job["dest_rows"] == 0 and staging_has_progress(path)
        job["yaml"] = job_yaml(job)
        jobs.append(job)
    if clashes:
        log(item, KC_STEP, "Failed", f"{len(clashes)} table(s) also come from another KQL database mapped to "
            f"ADX database '{dest_db}' and were skipped: {', '.join(clashes[:10])}. Map the databases to "
            "different ADX databases in ADX_DATABASE_MAP.")
    return jobs


def prepare_table(job):
    """Create the destination table (or add missing columns) from the source schema."""
    if not ADX_CREATE_TABLES:
        if not job["exists"]:
            raise HttpError(None, "Missing table", f"'{job['table']}' doesn't exist in ADX and ADX_CREATE_TABLES "
                            "is False", ADX_URI)
        return
    name = f"[{kql_string(job['table'])}]"
    row = kusto_mgmt(job["source_uri"], job["source_db"], f".show table {name} cslschema")[0]
    props = f" with (folder={kql_string(row['Folder'])})" if row.get("Folder") else ""
    kusto_mgmt(ADX_URI, job["dest_db"], f".create-merge table {name} ({row['Schema']}){props}",
               auth=kc_auth(ADX_URI))


def copy_table(exe, env, job):
    try:
        return _copy_table(exe, env, job)
    except Exception as exc:   # one table's failure must not stop the others
        return job, "Failed", redact(f"{type(exc).__name__}: {exc}"), None


def _copy_table(exe, env, job):
    item = job["item"]
    try:
        prepare_table(job)
    except HttpError as exc:
        return job, "Failed", f"create table: {exc.short()}", None
    note = ""
    if job["stale_progress"]:
        try:
            reset_staging(job["staging_path"])
        except HttpError as exc:
            return job, "Failed", (f"the ADX table is empty but Kusto Copy has progress from an earlier copy in "
                                   f"{job['staging_path']}, and resetting it failed: {exc.short()}. Delete that "
                                   "folder and run again."), None
        note = "; old Kusto Copy progress reset (the ADX table was empty), full history copied"
        print(f"    [{job['table']}] ADX table empty but staging has old progress: reset, copying the full history")
    started = time.monotonic()
    code, timed_out, lines = run_kusto_copy(exe, job, env)
    minutes = (time.monotonic() - started) / 60
    base = f"{item['adls']}/kusto_copy/{folder_part(job['table'])}"
    try:
        adls_write(base + ".yaml", job["yaml"])
        adls_write(base + ".log", "\n".join(lines) + "\n")
    except HttpError as exc:
        print(f"    [{job['table']}] log not saved: {exc.short()}")
    if timed_out:
        return job, "Partial", f"stopped after {KC_TIMEOUT_HOURS} h; the next run resumes it", None
    if code != 0:
        last = next((l for l in reversed(lines) if NOTABLE_RE.search(l)), lines[-1] if lines else "no output")
        return job, "Failed", f"Kusto Copy exit code {code}: {last}", None
    count = kusto_mgmt(ADX_URI, job["dest_db"], f"[{kql_string(job['table'])}] | count",
                       auth=kc_auth(ADX_URI), kind="query")
    rows = int(count[0].get("Count", 0)) if count else 0
    if rows < job["source_rows"]:
        return job, "Partial", (f"{rows:,} rows in ADX vs {job['source_rows']:,} in the source at the start "
                                "(check ADX retention, or rows still being ingested; Kusto Copy log: "
                                f"{base}.log){note}"), rows
    return job, "OK", f"{rows:,} rows in ADX ({minutes:.0f} min){note}", rows


def summarize(item, results):
    if not results:
        return
    dest = results[0][0]["dest_db"]
    ok = [r for r in results if r[1] == "OK"]
    bad = [r for r in results if r[1] != "OK"]
    rows = sum(r[3] or 0 for r in results)
    detail = f"{len(ok)}/{len(results)} tables copied to {ADX_HOST}/{dest}, {rows:,} rows in ADX"
    if bad:
        detail += f" | {len(bad)} not complete, first: {bad[0][0]['table']}: {bad[0][2]}"
    status = "OK" if not bad else ("Failed" if all(r[1] == "Failed" for r in results) else "Partial")
    log(item, KC_STEP, status, detail, files=len(results), rows=rows)
    inventory_row(item, dest, ADX_CREATED.get(dest, "existing"), len(results), rows, status)
    if not DRY_RUN:   # where the rows are, for V2_6 (restore from ADX)
        try:
            adls_write(f"{item['adls']}/kusto_copy/_destination.json", json.dumps({
                "cluster": ADX_CLUSTER_URI.rstrip("/"), "database": dest, "status": status,
                "tables": {r[0]["table"]: {"status": r[1], "rows": r[3]} for r in results}}, indent=2))
        except HttpError as exc:
            print(f"    destination record not saved: {exc.short()}")


ADX_INVENTORY = []


def inventory_row(item, dest, database_state, tables, rows, status):
    ADX_INVENTORY.append({"Workspace": item["workspaceName"], "WorkspaceId": item["workspaceId"],
                          "KQL database": item["displayName"], "KQL database item ID": item["id"],
                          "ADX cluster": ADX_URI, "ADX database": dest, "ADX database state": database_state,
                          "Tables": tables, "Rows": rows, "Status": status})


kql_items = [i for i in ITEMS if i["type"] == "KQLDatabase"]
if not COPY_KQL_TO_ADX:
    print(f"Kusto Copy to ADX skipped (KQL_BACKUP_MODE = \"{KQL_BACKUP_MODE}\").")
    kql_items = []
JOBS, seen = [], {}
for item in kql_items:
    print(f"Planning {item['workspaceName']} / {item['displayName']}")
    try:
        jobs = plan_database(item, seen)
    except HttpError as exc:
        log(item, KC_STEP, "Access denied" if exc.status in (401, 403) else "Failed", exc.short())
        continue
    if not jobs and not any(r["ItemId"] == item["id"] and r["Step"] == KC_STEP for r in RESULTS):
        log(item, KC_STEP, "Not applicable", "No tables in this KQL database")
    if DRY_RUN:
        if jobs:
            missing = sum(not j["exists"] for j in jobs)
            dest = jobs[0]["dest_db"]
            create = ADX_CREATED.get(dest) == "would create"
            log(item, KC_STEP, "Dry run", f"would copy {len(jobs)} tables, "
                f"{sum(j['source_rows'] for j in jobs):,} rows to {ADX_HOST}/{dest}"
                + (" (database to create)" if create else "") + f"; {missing} table(s) to create"
                + (f"; {stale} empty ADX table(s) with old Kusto Copy progress: reset and copied in full"
                   if (stale := sum(j["stale_progress"] for j in jobs)) else ""),
                files=len(jobs), rows=sum(j["source_rows"] for j in jobs))
            inventory_row(item, dest, "would create" if create else "existing", len(jobs),
                          sum(j["source_rows"] for j in jobs), "Dry run")
    else:
        JOBS += jobs

if JOBS:
    try:
        KC_EXE, KC_ENV = install_kusto_copy(), kc_environment()
    except (HttpError, RuntimeError, OSError, tarfile.TarError) as exc:
        for item in {j["item"]["id"]: j["item"] for j in JOBS}.values():
            log(item, KC_STEP, "Failed", f"Kusto Copy not installed: {exc}")
        JOBS = []
    by_item = defaultdict(list)
if JOBS:
    print(f"Copying {len(JOBS)} tables, {KC_PARALLEL_JOBS} at a time. Errors and a progress line every "
          "10 minutes per table are printed; the full logs go to ADLS (kusto_copy/ under each KQL database).")
    with ThreadPoolExecutor(max_workers=max(1, KC_PARALLEL_JOBS)) as pool:
        futures = [pool.submit(copy_table, KC_EXE, KC_ENV, j) for j in JOBS]
        for fut in as_completed(futures):
            job, status, detail, rows = fut.result()
            print(f"  {job['item']['displayName']} / {job['table']}: {status} - {detail}")
            by_item[job["item"]["id"]].append((job, status, detail, rows))
    for item in kql_items:
        summarize(item, by_item.get(item["id"], []))

if ADX_INVENTORY:
    print("\nWhere each KQL database goes in ADX (every run's list is saved as adx_inventory.csv in the run folder):")
    show(pd.DataFrame(ADX_INVENTORY).drop(columns=["WorkspaceId", "ADX cluster"]))
    if not DRY_RUN:
        try:
            adls_write(f"{RUN_PREFIX}/adx_inventory.csv", pd.DataFrame(ADX_INVENTORY).to_csv(index=False))
        except HttpError as exc:
            print(f"adx_inventory.csv not saved: {exc.short()}")
