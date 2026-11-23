SNAPSHOT_BRANCH = f"{SNAPSHOT_BRANCH_PREFIX}/{SNAPSHOT_ID}"
SNAPSHOT_TAG = f"{SNAPSHOT_BRANCH_PREFIX}-{SNAPSHOT_ID}"

if DRY_RUN:
    print("DRY_RUN = True: no commit, no branch, no manifest. Fix the BLOCKED rows, then run with DRY_RUN = False.")
    print(f"Snapshot branch for all workspaces: {SNAPSHOT_BRANCH}" + (f", tag {SNAPSHOT_TAG}" if CREATE_SNAPSHOT_TAG else ""))
    if SYNC_STOP:
        print(f"A real run would stop here too: not every workspace is synced ({', '.join(NOT_SYNCED)}).")
elif SYNC_STOP:
    print(f"NO SNAPSHOT TAKEN: not every workspace is synced with Git ({', '.join(NOT_SYNCED)}). Nothing was "
          "committed, no branch, no raw copy, no manifest. Follow the Fix column of the sync table, then run again.")
else:
    groups = defaultdict(list)                       # one shared branch per repository + working branch
    for ws in WS.values():
        if ws["ready"] and not ws.get("noGit"):
            g = ws["git"]
            groups[(g["organizationName"].lower(), g["projectName"].lower(), g["repositoryName"].lower(),
                    g["branchName"])].append(ws)
    for (_, _, repo, branch_name), members in groups.items():
        gp = members[0]["git"]
        try:
            head = (ado_ref(gp, branch_name) or {}).get("objectId")
            if not head:
                raise HttpError(None, "NoBranch", f"branch {branch_name} not found", branch_name)
            # Every workspace's folder must be identical at the shared head and at its own commit: nothing changed
            # in its folder after it was committed.
            for ws in members:
                if ws["head"] != head and ado_tree_ids(ws["git"], ws["head"]) != ado_tree_ids(ws["git"], head):
                    ws["ready"], ws["status"] = False, "BLOCKED"
                    block(ws, "Snapshot branch", f"its Git folder changed after its commit ({branch_name} is now at "
                          f"{head}): take a new snapshot")
            members = [ws for ws in members if ws["ready"]]
            if not members:
                continue
            if ado_ref(gp, SNAPSHOT_BRANCH):
                raise HttpError(None, "BranchExists", f"branch {SNAPSHOT_BRANCH} already exists in {repo}", repo)
            ado_create_branch(gp, SNAPSHOT_BRANCH, head)
            if LOCK_SNAPSHOT_BRANCHES:
                ado_lock(gp, SNAPSHOT_BRANCH)
            ref = ado_ref(gp, SNAPSHOT_BRANCH) or {}
            if ref.get("objectId") != head or (LOCK_SNAPSHOT_BRANCHES and not ref.get("isLocked")):
                raise HttpError(None, "Verify", f"branch {SNAPSHOT_BRANCH} isn't at {head} and locked", repo)
        except HttpError as exc:
            for ws in members:
                ws["ready"], ws["status"] = False, "BLOCKED"
                block(ws, "Snapshot branch", f"{exc.short()}. The token needs Create branch and Contribute.")
            continue
        tag = ""
        if CREATE_SNAPSHOT_TAG:
            try:
                ado_create_tag(gp, SNAPSHOT_TAG, head, f"Fabric migration snapshot {SNAPSHOT_ID}: "
                               + ", ".join(w["name"] for w in members))
                tag = SNAPSHOT_TAG
            except HttpError as exc:
                for ws in members:
                    log(ws["name"], {}, "Snapshot tag", "Warning", f"{exc.short()} (the locked branch still holds "
                        "the snapshot; tags need the Create tag permission)")
        for ws in members:
            ws["ownCommit"], ws["head"] = ws["head"], head
            ws["snapshotBranch"], ws["snapshotTag"] = SNAPSHOT_BRANCH, tag
            log(ws["name"], {}, "Snapshot branch", "OK", f"{SNAPSHOT_BRANCH} at {head}"
                + (" (locked)" if LOCK_SNAPSHOT_BRANCHES else "") + (f", tag {tag}" if tag else ""))

    keep = ("id", "type", "displayName", "logicalId", "gitFolder", "gitFiles", "inGit", "sqlEndpointId",
            "parentEventhouseItemId", "defaultSchema", "staging", "secondary", "rawCopy", "rawHash", "recovery", "status", "reasons")
    MANIFEST = {
        "snapshotId": SNAPSHOT_ID, "createdUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "itemTypes": ITEM_TYPES, "acknowledgedItemTypes": ACKNOWLEDGED_ITEM_TYPES,
        "secondaryBackup": {"account": ADLS_ACCOUNT, "container": ADLS_CONTAINER, "root": ADLS_ROOT,
                            "runId": SECONDARY_BACKUP_RUN_ID, "startedUtc": run_started,
                            "freezeStartedUtc": FREEZE_STARTED_UTC},
        "rawCopy": {"enabled": SAVE_RAW_COPY_TO_ADLS, "definitionsRunId": f"_migration/{SNAPSHOT_ID}/raw",
                    "gitMirror": f"_migration/{SNAPSHOT_ID}/git"} if SAVE_RAW_COPY_TO_ADLS else {"enabled": False},
        "workspaces": [{
            "id": w["id"], "name": w["name"], "capacityId": w["capacityId"], "ready": w["ready"],
            "gitRoute": "none" if w.get("noGit") else "git",
            "blockers": w["blockers"], "secondaryFolder": w.get("secondaryFolder", ""),
            "gitMirror": w.get("gitMirror", ""),
            "git": {**w["git"], "commit": w.get("head"), "snapshotBranch": w.get("snapshotBranch"),
                    "snapshotTag": w.get("snapshotTag", ""), "workspaceCommit": w.get("ownCommit", w.get("head")),
                    "locked": bool(w.get("snapshotBranch")) and LOCK_SNAPSHOT_BRANCHES},
            "items": [{k: i.get(k) for k in keep} for i in w["items"]],
            "powerBiItems": [{k: i.get(k) for k in ("id", "type", "displayName", "inGit", "gitFolder")} for i in w["pbi"]],
        } for w in WS.values()]}
    prefix = migration_prefix(SNAPSHOT_ID)
    raw = json.dumps(MANIFEST, indent=2, sort_keys=True, default=str).encode()
    adls_write(f"{prefix}/manifest.json", raw)
    adls_write(f"{prefix}/manifest.sha256", hashlib.sha256(raw).hexdigest())
    adls_write(f"{prefix}/items.csv", GATE_DF.to_csv(index=False))
    save_log(prefix, "snapshot_log")
    ready = [w["name"] for w in WS.values() if w["ready"]]
    print(f"\nSNAPSHOT ID: {SNAPSHOT_ID}\nSnapshot branch (all workspaces): {SNAPSHOT_BRANCH}"
          + (f", tag {SNAPSHOT_TAG}" if CREATE_SNAPSHOT_TAG else ""))
    print(f"Manifest: abfss://{ADLS_CONTAINER}@{ADLS_ACCOUNT}.dfs.core.windows.net/{prefix}/manifest.json")
    print(f"READY ({len(ready)}): {', '.join(ready) or '-'}")
    blocked = [w["name"] for w in WS.values() if not w["ready"]]
    if blocked:
        print(f"BLOCKED ({len(blocked)}): {', '.join(blocked)}. Don't delete anything in these workspaces.")
    print("\nFrom now on, no changes in these workspaces until the restore is verified. Next: V2_4_Delete_Fabric_Items "
          "with this SNAPSHOT ID.")
