if not DRY_RUN:
    df = save_log(PREFIX, "restore_log")
    show(df[~df.Status.isin(["OK", "Info"])])
if RESTORED:
    sec = MANIFEST["secondaryBackup"]
    moved = [f"{t['name']}: {t['old']} -> {t['id']}" for t in RESTORED if t["old"] != t["id"]]
    print("Step 6 - data: open V2_6_Restore_Data with these settings:\n")
    print(f'SNAPSHOT_ID = "{SNAPSHOT_ID}"\nADLS_ACCOUNT = "{sec["account"]}"\nADLS_CONTAINER = "{sec["container"]}"\n'
          f'ADLS_ROOT = "{ADLS_ROOT}"')
    print("DRY_RUN = True                            # then CONFIRM_DATA_ID as usual")
    print("(V2_6 finds each workspace where this notebook restored it, from restore_state.json"
          + (f"; IDs changed: {'; '.join(moved)})" if moved else ")"))
    print("\nThen, by hand: switch each workspace from the restore branch to the team's working branch (Source control "
          "> Branch) once verified; recreate connections, gateways and item permissions; rebind anything listed as "
          "'Manual step' above.")
    print("If a workspace's Source control or Git integration screen shows 'Can't fetch branches': Source control > "
          f"account > choose the Fabric connection {git_connection_label() if git_link_mode() != 'my_account' else '(your own Azure DevOps sign-in)'}"
          ". Git sign-in is per person and per workspace; each person who uses Source control picks it once.")

if RAW.get("enabled") and RESTORE_SOURCE == "git":
    print(f"""
If Azure DevOps is unavailable: set RESTORE_SOURCE = "adls" and run this notebook again (dry run first). Every
workspace is then recreated from the raw definitions saved with the snapshot ({RAW['definitionsRunId']}), and
V2_6 loads the data as usual. The repository files of each snapshot commit are in
{ADLS_ROOT.strip('/')}/{RAW['gitMirror']}/<workspace>/files/: push them to any Git repository to use Git again.""")
if any(t.get("route") == "adls" for t in RESTORED):
    print("\nWorkspaces recreated from ADLS have no Git link. Link them when you're ready with V2_1 "
          "(it commits the workspace into an empty Git folder).")
