import io

RUN_PREFIX = f"{ADLS_ROOT.strip('/')}/{SECONDARY_BACKUP_RUN_ID}"
GOOD = ("OK", "Unchanged", "Not applicable")
run_problems = []
try:
    started = json.loads(adls_read(f"{RUN_PREFIX}/_run_started.json"))
    if not adls_exists(f"{RUN_PREFIX}/_run_completed.json"):
        run_problems.append(f"backup run {SECONDARY_BACKUP_RUN_ID} didn't finish (no _run_completed.json)")
    backup_log = pd.read_csv(io.BytesIO(adls_read(f"{RUN_PREFIX}/backup_log.csv")), dtype=str).fillna("")
except HttpError as exc:
    raise RuntimeError(f"Can't read backup run {RUN_PREFIX}/ ({exc.short()}). Check SECONDARY_BACKUP_RUN_ID and "
                       "your Storage Blob Data Reader/Contributor role.") from None
run_started = str(started.get("startedUtc", ""))
if not FREEZE_STARTED_UTC:
    run_problems.append("FREEZE_STARTED_UTC is empty: set when the change freeze started, so the check can prove the "
                        "backup is newer than the last change")
else:
    freeze = dt.datetime.fromisoformat(FREEZE_STARTED_UTC.replace("Z", "+00:00"))
    if not run_started or dt.datetime.fromisoformat(run_started.replace("Z", "+00:00")) < freeze:
        run_problems.append(f"backup run started {run_started or '(unknown)'}, before the freeze {FREEZE_STARTED_UTC}")
for p in run_problems:
    for ws in WS.values():
        block(ws, "Secondary backup", p)

STEP_STATUS = {(r.ItemId.lower(), r.Step): r.Status for r in backup_log.itertuples()}
for ws in WS.values():
    top = [n for n, _, is_dir in adls_list(RUN_PREFIX, recursive=False) if is_dir and n.endswith("__" + ws["id"])]
    files = adls_list(f"{RUN_PREFIX}/{top[0]}") if top else []
    def_files = Counter(p.split("/", 1)[0].rsplit("__", 1)[-1].lower() for p, _, d in files
                        if not d and "/definition/" in p)
    ws["secondaryFolder"] = f"{RUN_PREFIX}/{top[0]}" if top else ""
    for item in ws["items"]:
        if item["type"] not in ITEM_TYPES or item.get("staging"):
            continue
        steps = ["SQL scripts"] if item["type"] in CHILD_TYPES else ["Definition"]
        sec = {s: STEP_STATUS.get((item["id"].lower(), s), "") for s in steps}
        if item["type"] in DATA_TYPES:
            dstep, dstatus = data_step(item["type"], lambda s: STEP_STATUS.get((item["id"].lower(), s), ""))
            sec[dstep] = dstatus
        if "Definition" in sec and sec["Definition"] in GOOD and not def_files.get(item["id"].lower()):
            sec["Definition"] = "no files in ADLS"
        item["secondary"] = sec
    print(f"{ws['name']}: secondary backup folder {ws['secondaryFolder'] or 'NOT FOUND'}")
