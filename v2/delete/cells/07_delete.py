to_delete = PLAN_DF[PLAN_DF.Action == "delete"]
if DRY_RUN:
    print(f"DRY_RUN = True: nothing deleted. {len(to_delete)} item(s) would be deleted. To go ahead set DRY_RUN = False "
          f"and CONFIRM_DELETE_ID = \"{DELETE_ID}\".")
elif CONFIRM_DELETE_ID != DELETE_ID:
    raise RuntimeError(f"CONFIRM_DELETE_ID doesn't match this plan's DELETE ID ({DELETE_ID}). Something changed since "
                       "the dry run, or the ID wasn't pasted. Review the plan above and confirm again.")
else:
    def save_state():
        adls_write(f"{PREFIX}/delete_state.json", json.dumps(STATE, indent=2))

    for t in TARGETS:
        if t["problems"]:
            continue
        if t["connected"]:
            try:
                fabric("POST", f"/workspaces/{t['id']}/git/disconnect")
            except HttpError as exc:
                log(t["name"], {}, "Git disconnect", "Failed", f"{git_fail_text(exc, t['id'])}. Nothing deleted in this workspace.")
                continue
            STATE["disconnected"].append(t["id"])
            save_state()
            log(t["name"], {}, "Git disconnect", "OK", "the deletions can't reach the Git branch")
        for r in to_delete[to_delete.WorkspaceId == t["id"]].itertuples():
            item = {"id": r.ItemId, "type": r.Type, "displayName": r.Item}
            try:
                fabric("DELETE", f"/workspaces/{t['id']}/items/{r.ItemId}"
                       + ("?hardDelete=true" if HARD_DELETE else ""))
                log(t["name"], item, "Delete", "OK", "hard delete" if HARD_DELETE else
                    "SOFT delete: purge it from the workspace recycle bin before the migration")
            except HttpError as exc:
                if exc.status == 404:
                    log(t["name"], item, "Delete", "OK", "already gone")
                else:
                    log(t["name"], item, "Delete", "Failed", exc.short())
                    continue
            STATE["deleted"].append(r.ItemId)
            save_state()
        left = [i for i in workspace_items(t["id"]) if i["type"] not in POWER_BI_TYPES]
        log(t["name"], {}, "Verify", "OK" if not left else "Failed",
            "no Fabric items left" if not left else "still there: " + ", ".join(
                f"{i['displayName']} ({i['type']})" for i in left))
    df = save_log(PREFIX, "delete_log")
    show(df[df.Status != "OK"])
    print(f"Done. Log: {PREFIX}/delete_log_{NOW_ID}.csv. "
          + ("Items were hard-deleted. " if HARD_DELETE else
             "HARD_DELETE = False: the items are only soft-deleted and WILL BLOCK the migration until purged. ")
          + "Before the capacities are deleted, rerun the dry run to catch auto-created items (for example "
          "DataExploration or Dataflow staging items), then migrate and run V2_5_Restore_From_Git.")
