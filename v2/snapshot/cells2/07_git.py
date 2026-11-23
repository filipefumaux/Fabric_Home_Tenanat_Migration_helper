# Sync check: read-only for every workspace first. Nothing is committed or updated by this cell unless
# COMMIT_PENDING_CHANGES = True, DRY_RUN = False and (with REQUIRE_ALL_WORKSPACES_SYNCED) nothing else is out of sync.
SYNC_ROWS = []
SYNC_OK = ("Synced", "Uncommitted (will be committed)", "No Git (ADLS route)")


def sync_row(ws, status, detail="", fix="", **extra):
    g = ws.get("git") or {}
    ws["sync"] = status
    SYNC_ROWS.append({"Workspace": ws["name"], "Sync": status, "Branch": g.get("branchName") or "",
                      "Git folder": g.get("directoryName") or "", "Workspace commit": extra.get("wh") or "",
                      "DevOps branch head": extra.get("head") or "", "Uncommitted": extra.get("out", 0),
                      "Incoming": extra.get("inc", 0), "Detail": detail, "Fix": fix})
    if status not in SYNC_OK:
        block(ws, "Git sync", f"{status}: {detail}" + (f" | FIX: {fix}" if fix else ""))


def check_sync(ws):
    name, ws_id = ws["name"], ws["id"]
    if ws.get("noGit"):
        try:
            state = git_connection(ws_id).get("gitConnectionState")
        except HttpError:
            state = None
        return sync_row(ws, "No Git (ADLS route)", "in NO_GIT_WORKSPACE_IDS: Git isn't used; V2_5 recreates the items "
                        "from the raw copy in ADLS" + (f" (the workspace is {state}; that link is ignored)"
                                                       if state not in (None, "NotConnected") else ""))
    try:
        conn = git_connection(ws_id)
    except HttpError as exc:
        return sync_row(ws, "Can't read", f"Git connection: {git_fail_text(exc, ws_id)}",
                        "You need Contributor or higher on the workspace.")
    gp = conn.get("gitProviderDetails") or {}
    ws["git"] = {k: gp.get(k) for k in ("gitProviderType", "organizationName", "projectName", "repositoryName",
                                         "branchName", "directoryName")}
    state = conn.get("gitConnectionState")
    if state != "ConnectedAndInitialized":
        return sync_row(ws, "Not connected", f"Git connection state: {state}",
                        "Connect it with V2_1_Connect_Workspaces_To_Git (or Workspace settings > Git integration).")
    if gp.get("gitProviderType") != "AzureDevOps":
        return sync_row(ws, "Wrong provider", f"connected to {gp.get('gitProviderType')}, not Azure DevOps",
                        "Reconnect it to the Azure DevOps repository (V2_1).")
    if str(gp.get("organizationName", "")).lower() != ADO_ORGANIZATION.lower():
        return sync_row(ws, "Wrong organization", f"connected to {gp.get('organizationName')}, not {ADO_ORGANIZATION}",
                        f"Reconnect it to {ADO_ORGANIZATION} (V2_1), or fix ADO_ORGANIZATION.")
    try:
        st = git_status(ws_id)
    except HttpError as exc:
        return sync_row(ws, "Git status failed", git_fail_text(exc, ws_id, gp))
    wh, remote = st.get("workspaceHead"), st.get("remoteCommitHash")
    try:
        head = (ado_ref(gp, gp.get("branchName")) or {}).get("objectId")
    except HttpError as exc:
        return sync_row(ws, "Can't read DevOps", f"branch {gp.get('branchName')}: {exc.short()}", wh=wh,
                        fix="Check the DevOps sign-in of this notebook (ADO_AUTH) and its Code (Read) access.")
    if not head:
        return sync_row(ws, "Branch missing", f"branch {gp.get('branchName')} isn't in the repository", wh=wh,
                        fix="Reconnect the workspace to an existing branch (V2_1).")
    changes = st.get("changes") or []
    incoming = [c for c in changes if c.get("remoteChange") or c.get("conflictType") not in (None, "None")]
    outgoing = [c for c in changes if c.get("workspaceChange") and c not in incoming]
    deletions = [c for c in outgoing if c["workspaceChange"] == "Deleted"]
    for c in changes:
        md = c.get("itemMetadata") or {}
        log(name, {"type": md.get("itemType"), "displayName": md.get("displayName")}, "Git status", "Info",
            f"workspace: {c.get('workspaceChange') or '-'}, Git: {c.get('remoteChange') or '-'}, "
            f"conflict: {c.get('conflictType') or 'None'}")
    ws["status_raw"], ws["outgoing"] = st, outgoing
    counts = {"wh": wh, "head": head, "out": len(outgoing), "inc": len(incoming)}
    if not wh:
        return sync_row(ws, "Never synced", "the workspace has no synced commit yet", **counts,
                        fix="Source control > Commit (or Update) once, then run this notebook again.")
    if incoming:
        return sync_row(ws, "Incoming changes", f"{len(incoming)} change(s) or conflict(s) in Git not in the workspace",
                        "Source control > Updates > Update all (resolve conflicts first), then run again.", **counts)
    if deletions and not ALLOW_COMMITTING_DELETIONS:
        names = ", ".join(f"{(c.get('itemMetadata') or {}).get('displayName')}" for c in deletions)
        return sync_row(ws, "Uncommitted deletions", f"{len(deletions)} item(s) in Git but deleted in the workspace "
                        f"({names}); committing would remove them from Git",
                        "Restore the items, or confirm they were deleted on purpose and set "
                        "ALLOW_COMMITTING_DELETIONS = True.", **counts)
    if wh != head:
        # The branch is shared: other workspaces' commits move its head. This workspace is still synced when its
        # own Git folder is the same at its commit and at the branch head.
        try:
            same = ado_tree_ids(gp, wh) == ado_tree_ids(gp, head)
        except HttpError as exc:
            return sync_row(ws, "Can't read DevOps", f"folder at {wh} / {head}: {exc.short()}", **counts)
        if not same:
            return sync_row(ws, "Behind the branch", f"its Git folder changed after commit {wh[:8]} (head {head[:8]})",
                            "Source control > Updates > Update all, then run again.", **counts)
    if outgoing:
        if not COMMIT_PENDING_CHANGES:
            return sync_row(ws, "Uncommitted changes", f"{len(outgoing)} workspace change(s) not committed to Git",
                            "Source control > Changes > Commit (or set COMMIT_PENDING_CHANGES = True), then run "
                            "again.", **counts)
        ws["pendingCommit"] = len(outgoing)
        ws["pendingAdded"] = {((c.get("itemMetadata") or {}).get("itemIdentifier") or {}).get("objectId")
                              for c in outgoing if c["workspaceChange"] == "Added"}
        ws["head"] = wh
        return sync_row(ws, "Uncommitted (will be committed)", f"{len(outgoing)} change(s); COMMIT_PENDING_CHANGES",
                        **counts)
    ws["head"] = wh
    sync_row(ws, "Synced", "" if wh == head else "branch moved on through other folders only", **counts)


for ws in WS.values():
    check_sync(ws)

NOT_SYNCED = [w["name"] for w in WS.values() if w.get("sync") not in SYNC_OK]
SYNC_STOP = REQUIRE_ALL_WORKSPACES_SYNCED and bool(NOT_SYNCED)
if SYNC_STOP:
    for ws in WS.values():
        if ws["name"] not in NOT_SYNCED:
            block(ws, "Git sync", f"not every workspace is synced ({', '.join(NOT_SYNCED)}); "
                  "REQUIRE_ALL_WORKSPACES_SYNCED takes the snapshot only when all are")

# Commit only with COMMIT_PENDING_CHANGES, a real run, and nothing else out of sync.
for ws in WS.values():
    if not ws.get("pendingCommit") or ws["blockers"]:
        continue
    if DRY_RUN:
        log(ws["name"], {}, "Git commit", "Dry run", f"would commit {ws['pendingCommit']} workspace change(s)")
        continue
    gp = ws["git"]
    try:
        fabric("POST", f"/workspaces/{ws['id']}/git/commitToGit",
               {"mode": "All", "workspaceHead": ws["status_raw"].get("workspaceHead"),
                "comment": f"Pre-migration snapshot {SNAPSHOT_ID}"}, want_result=False)
        st = git_status(ws["id"])
    except HttpError as exc:
        block(ws, "Git commit", f"commit failed ({git_fail_text(exc, ws['id'], gp)})")
        ws.pop("head", None)
        continue
    if st.get("changes"):
        block(ws, "Git commit", f"{len(st['changes'])} change(s) still pending after the commit (often an item type "
                                "that Git doesn't support). See the Git status rows.")
        ws.pop("head", None)
        continue
    ws["head"], ws["pendingCommit"] = st.get("workspaceHead") or st.get("remoteCommitHash"), 0
    log(ws["name"], {}, "Git commit", "OK", f"committed {len(ws['outgoing'])} change(s): {ws['head']}")

SYNC_DF = pd.DataFrame(SYNC_ROWS)
show(SYNC_DF)
synced = len(WS) - len(NOT_SYNCED)
print(f"Git sync: {synced} of {len(WS)} workspace(s) synced.")
if NOT_SYNCED:
    print(f"NOT SYNCED: {', '.join(NOT_SYNCED)}. Follow the Fix column, then run this notebook again."
          + (" Nothing will be created in this run (REQUIRE_ALL_WORKSPACES_SYNCED)." if SYNC_STOP else
             " Those workspaces are BLOCKED; the others continue."))
for ws in WS.values():
    if ws.get("head"):
        print(f"{ws['name']}: {ws['git']['repositoryName']} / {ws['git']['branchName']} / "
              f"{ws['git']['directoryName'] or '(root)'} at {ws['head']}"
              + (f" (+{ws['pendingCommit']} change(s) to commit)" if ws.get("pendingCommit") else ""))
