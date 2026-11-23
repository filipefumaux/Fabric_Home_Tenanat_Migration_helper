if DRY_RUN:
    print(f"DRY_RUN = True: nothing changed. To connect, set DRY_RUN = False and CONFIRM_CONNECT_ID = \"{CONNECT_ID}\".")
elif PLAN and CONFIRM_CONNECT_ID != CONNECT_ID:
    raise RuntimeError(f"CONFIRM_CONNECT_ID doesn't match this plan's CONNECT ID ({CONNECT_ID}). Review the plan above "
                       "and confirm again.")
elif PLAN:
    if CREATE_CONNECTION:
        try:
            GIT_CONNECTION_ID = create_git_connection(CREATE_CONNECTION, ADO_ORGANIZATION, ADO_PROJECT, ADO_REPOSITORY)["id"]
            log("(all)", {}, "Git connection", "OK", f"created {CREATE_CONNECTION} ({GIT_CONNECTION_ID})")
        except HttpError as exc:
            raise RuntimeError(f"Couldn't create the Fabric connection {CREATE_CONNECTION}: {exc.short()}. Check the "
                               "service principal settings and its client secret; if a connection with this name "
                               "exists but isn't shared with you, ask its owner to share it (Manage connections and "
                               "gateways > Manage users) or set GIT_CONNECTION_NAME to a new name. Nothing was "
                               "linked.") from None
        CREATE_CONNECTION = None
    if not BRANCH_HEAD:
        ado_create_branch(REPO, ADO_BRANCH, default_head)
        print(f"Created branch {ADO_BRANCH} from {default_branch}")
    for p in PLAN:
        try:
            if p["addAdmin"]:
                add_me_as_workspace_admin(p["id"])
                log(p["name"], {}, "Workspace role", "OK", f"{WHO['upn']} added as Admin (remove after the migration)")
            if p.get("credsOnly"):
                set_my_git_credentials(p["id"], GIT_CONNECTION_ID)
                log(p["name"], {}, "Git sign-in", "OK", f"your Git credentials now use connection {GIT_CONNECTION_ID}")
                continue
            if p.get("createFolder"):
                try:
                    head = ado_create_folder(REPO, ADO_BRANCH, p["folder"],
                                             f"# {p['name']}\n\nGit folder of Fabric workspace {p['name']} ({p['id']}), "
                                             "created by V2_1 for the tenant home-region migration. Fabric item "
                                             "folders are added next to this file by Git integration.\n")
                except HttpError as exc:
                    log(p["name"], {}, "Git folder", "Failed", f"{exc.short()} | FIX: the notebook's DevOps sign-in "
                        f"(ADO_AUTH) needs Contribute on {ADO_REPOSITORY}; or create {p['folder']} by hand (any file "
                        "inside it) and rerun.")
                    continue
                p["createFolder"] = False
                log(p["name"], {}, "Git folder", "OK", f"created {p['folder']} on {ADO_BRANCH} ({(head or '')[:8]})")
            body = {"gitProviderDetails": {"gitProviderType": "AzureDevOps", **REPO, "branchName": ADO_BRANCH,
                                           "directoryName": p["folder"]}}
            if GIT_CONNECTION_ID:
                body["myGitCredentials"] = {"source": "ConfiguredConnection", "connectionId": GIT_CONNECTION_ID}
            if not p["resume"]:
                fabric("POST", f"/workspaces/{p['id']}/git/connect", body)
            init = fabric("POST", f"/workspaces/{p['id']}/git/initializeConnection",
                          {"initializationStrategy": "PreferWorkspace"})
            action = init.get("requiredAction")
            if action == "CommitToGit":
                fabric("POST", f"/workspaces/{p['id']}/git/commitToGit",
                       {"mode": "All", "workspaceHead": init.get("workspaceHead"),
                        "comment": f"Initial commit of workspace {p['name']}"}, want_result=False)
            elif action == "UpdateFromGit":
                raise HttpError(None, "UnexpectedAction", "Fabric wants to update the workspace from Git; stopped "
                                "without updating. Check the folder in the repository.", p["id"])
            log(p["name"], {}, "Connect", "OK", f"{ADO_BRANCH}:{p['folder']} ({action})")
            if GIT_CONNECTION_ID and creds_need_fix(my_git_credentials(p["id"]), GIT_CONNECTION_ID) \
                    and not SWITCH_MY_GIT_CREDENTIALS:
                log(p["name"], {}, "Git sign-in", "Info", "linked. If the workspace screens show 'Can't fetch "
                    f"branches': Source control > account > choose connection {git_connection_label()}"
                    " (the notebook doesn't change your Git sign-in unless SWITCH_MY_GIT_CREDENTIALS = True).")
            elif GIT_CONNECTION_ID and creds_need_fix(my_git_credentials(p["id"]), GIT_CONNECTION_ID):
                try:
                    set_my_git_credentials(p["id"], GIT_CONNECTION_ID)
                    log(p["name"], {}, "Git sign-in", "OK", f"your Git credentials use connection {GIT_CONNECTION_ID}")
                except HttpError as exc:
                    log(p["name"], {}, "Git sign-in", "Warning", f"{exc.short()} | FIX: linked, but the workspace "
                        "screens may show 'Can't fetch branches': Source control > account > choose connection "
                        f"{GIT_CONNECTION_ID}, or rerun this notebook.")
        except HttpError as exc:
            log(p["name"], {}, "Connect", "Failed", f"{exc.short()} | FIX: {git_connect_fix(exc)}")
    failed = [p for p in PLAN if any(r["Workspace"] == p["name"] and r["Step"] == "Connect" and r["Status"] == "Failed"
                                     for r in RESULTS)]
    if failed:
        print("\nNot linked. After fixing the cause, rerun this notebook (it resumes). To link by hand instead: "
              "Workspace settings > Git integration > Azure DevOps, with:")
        print("(The Git folder must exist in the branch before an API link; the settings screen creates it for you.)")
        show(pd.DataFrame([{"Workspace": p["name"], "Organization": ADO_ORGANIZATION, "Project": ADO_PROJECT,
                            "Repository": ADO_REPOSITORY, "Branch": ADO_BRANCH, "Git folder": p["folder"]}
                           for p in failed]))
        print("then Source control > Commit all. Rerun this notebook to verify.")
if GIT_CONNECTION_ID:
    print(f"\nGit connection used: {GIT_CONNECTION_ID}. V2_5 (restore) uses the same one with the same GIT_LINK_AUTH "
          f"and GIT_CONNECTION_NAME (or set GIT_CONNECTION_ID = \"{GIT_CONNECTION_ID}\" there).")
