rows = []
for ws_id in dict.fromkeys(ws_ids):
    try:
        info = fabric("GET", f"/workspaces/{ws_id}")
        conn = git_connection(ws_id)
        if conn.get("gitConnectionState") != "ConnectedAndInitialized":
            rows.append({"Workspace": info.get("displayName", ws_id), "State": conn.get("gitConnectionState")})
            continue
        gp = conn["gitProviderDetails"]
        st = git_status(ws_id)
        saved = git_items_at(gp, st["remoteCommitHash"])
        not_in_git = [f"{i['displayName']} ({i['type']})" for i in workspace_items(ws_id)
                      if i["type"] not in CHILD_TYPES and key_of(i["type"], i["displayName"]) not in saved]
        rows.append({"Workspace": info.get("displayName", ws_id), "State": "ConnectedAndInitialized",
                     "Branch": gp.get("branchName"), "Folder": gp.get("directoryName"),
                     "Items in Git": len(saved), "Uncommitted changes": len(st.get("changes") or []),
                     "Not in Git": ", ".join(not_in_git)})
    except HttpError as exc:
        rows.append({"Workspace": ws_id, "State": f"error: {git_fail_text(exc, ws_id)}"})
VERIFY_DF = pd.DataFrame(rows)
show(VERIFY_DF)
if RESULTS:
    show(pd.DataFrame(RESULTS))
print("Items listed under 'Not in Git' rely on the ADLS copies (V2_2 backup and the snapshot's raw copy).")
