ADO_ORGANIZATION = ado_org_name(ADO_ORGANIZATION)   # a URL works too
missing = [n for n in ("ADO_ORGANIZATION", "ADO_PROJECT", "ADO_REPOSITORY", "KEY_VAULT_URL")
           if "<" in str(globals()[n])] + ado_auth_missing()
if missing:
    raise ValueError("Fill in these settings first: " + ", ".join(missing))
ws_ids = [norm_id(w) for w in WORKSPACE_IDS]
if not ws_ids or None in ws_ids:
    raise ValueError("WORKSPACE_IDS must list at least one workspace ID (GUID).")
WHO = me()
if current_workspace_id() in ws_ids:
    print("NOTE: this notebook runs inside a workspace it links. Its notebooks will be saved in Git and deleted by "
          "V2_4 with the other items. " + RUN_FROM_ELSEWHERE)
print(f"Running as {WHO['upn'] or '(unknown)'}" + (" (service principal)" if WHO["isApp"] else ""))
LINK = plan_git_link(WHO, ADO_ORGANIZATION, ADO_PROJECT, ADO_REPOSITORY)
GIT_CONNECTION_ID, CREATE_CONNECTION = LINK["connectionId"], LINK["create"]
print(f"Git link method (GIT_LINK_AUTH = \"{LINK['mode']}\"): Fabric will sign in to Azure DevOps with {LINK['text']}")
print(f"(ADO_AUTH = \"{ado_mode()}\" is only how this notebook itself reads and writes in DevOps.)")
REPO = {"organizationName": ADO_ORGANIZATION, "projectName": ADO_PROJECT, "repositoryName": ADO_REPOSITORY}
try:
    default_branch, default_head = ado_default_head(REPO)
    ref = ado_ref(REPO, ADO_BRANCH)
except HttpError as exc:
    raise RuntimeError(f"Can't read repository {ADO_ORGANIZATION}/{ADO_PROJECT}/{ADO_REPOSITORY} ({exc.short()}). "
                       "Check the names, the Key Vault secret and the token's Code scope.") from None
BRANCH_HEAD = (ref or {}).get("objectId")
if not BRANCH_HEAD and not CREATE_BRANCH_IF_MISSING:
    raise RuntimeError(f"Branch {ADO_BRANCH} doesn't exist and CREATE_BRANCH_IF_MISSING is False.")
print(f"Checked in DevOps: repository {ADO_REPOSITORY} found; branch {ADO_BRANCH} " +
      (f"exists (latest commit {BRANCH_HEAD[:8]})" if BRANCH_HEAD else
       f"doesn't exist yet: it will be created from {default_branch} when you confirm"))
if LINK["mode"] == "service_principal":
    sp_ok, sp_err = sp_repo_check(ADO_ORGANIZATION, ADO_PROJECT, ADO_REPOSITORY)
    if not sp_ok:
        raise RuntimeError(f"Stopped: service principal {str(ADO_SP_CLIENT_ID).strip()} can't read repository "
                           f"{ADO_REPOSITORY} ({sp_err}). Fabric would fail to link with it. " + SP_NOT_IN_ORG)
    print(f"Checked in DevOps: service principal {str(ADO_SP_CLIENT_ID).strip()} can read repository {ADO_REPOSITORY}")
ORG_TENANT, ORG_TENANT_HOW = ado_org_tenant_info(ADO_ORGANIZATION, ADO_PROJECT, ADO_REPOSITORY)
if ORG_TENANT is None:
    same_tenant = (f"couldn't tell which Entra tenant it uses ({ORG_TENANT_HOW}). Check it in Azure DevOps > "
                   "Organization settings > Microsoft Entra; it must be the Fabric tenant " + (WHO.get("tid") or "?"))
elif ORG_TENANT == "":
    same_tenant = "NO: the DevOps organization isn't connected to any Entra tenant"
elif ORG_TENANT == WHO.get("tid"):
    same_tenant = f"yes, same Entra tenant as Fabric ({ORG_TENANT}; {ORG_TENANT_HOW})"
else:
    same_tenant = (f"NO: the DevOps organization uses Entra tenant {ORG_TENANT}, Fabric uses {WHO.get('tid') or '?'} "
                   f"({ORG_TENANT_HOW})")
print(f"DevOps organization {ADO_ORGANIZATION} in the Fabric tenant: {same_tenant}")
if same_tenant.startswith("NO") and not ALLOW_CROSS_TENANT:
    raise RuntimeError(f"Stopped: {same_tenant}. " + SAME_TENANT_FIX)


def folder_for(ws_id, name):
    path = DIRECTORY_MAP.get(ws_id) or DIRECTORY_PATTERN.format(
        workspace=re.sub(r"[^A-Za-z0-9 ._-]+", "_", name).strip() or ws_id, id=ws_id)
    return "/" + path.strip("/")


rows, PLAN, used = [], [], {}
for ws_id in dict.fromkeys(ws_ids):
    try:
        info = fabric("GET", f"/workspaces/{ws_id}")
        conn = git_connection(ws_id)
        items = workspace_items(ws_id)
    except HttpError as exc:
        rows.append({"Workspace": ws_id, "State": "?", "Action": "blocked", "Detail": f"can't read it: {exc.short()}"})
        continue
    name, state = info.get("displayName", ws_id), conn.get("gitConnectionState")
    gp = conn.get("gitProviderDetails") or {}
    row = {"Workspace": name, "WorkspaceId": ws_id, "State": state, "Fabric items": sum(
        i["type"] not in POWER_BI_TYPES for i in items), "Repository": gp.get("repositoryName", ""),
        "Branch": gp.get("branchName", ""), "Folder": gp.get("directoryName", "")}
    if state == "ConnectedAndInitialized":
        same = (str(gp.get("organizationName", "")).lower(), str(gp.get("projectName", "")).lower(),
                str(gp.get("repositoryName", "")).lower()) == (ADO_ORGANIZATION.lower(), ADO_PROJECT.lower(),
                                                                ADO_REPOSITORY.lower())
        cred = my_git_credentials(ws_id)
        row["Your Git sign-in"] = cred.get("source")
        if same and creds_need_fix(cred, GIT_CONNECTION_ID) and SWITCH_MY_GIT_CREDENTIALS:
            row.update(Action="already connected; switch your Git sign-in to the connection",
                       Detail=CREDS_WHY.format(src=cred.get("source")))
            PLAN.append({"id": ws_id, "name": name, "folder": gp.get("directoryName", ""), "resume": True,
                         "addAdmin": False, "credsOnly": True})
        else:
            detail = "" if same else "connected to a different repository: check this is intended"
            if same and creds_need_fix(cred, GIT_CONNECTION_ID):
                detail = ("synced; nothing to do here. If the workspace screens show 'Can't fetch branches': "
                          + CREDS_WHY.format(src=cred.get("source")) + " Fix: Source control > account > choose "
                          f"connection {git_connection_label()}.")
            row.update(Action="already connected (left as is)", Detail=detail)
        used[(gp.get("branchName"), str(gp.get("directoryName", "")).strip("/").lower())] = name
    elif state == "Connected":
        mine = (str(gp.get("repositoryName", "")).lower(), gp.get("branchName"), "/" + str(gp.get(
            "directoryName", "")).strip("/").lower()) == (ADO_REPOSITORY.lower(), ADO_BRANCH,
                                                         folder_for(ws_id, name).lower())
        if mine:
            row.update(Action="finish link (first commit)", Detail="linked earlier but never synced")
            PLAN.append({"id": ws_id, "name": name, "folder": folder_for(ws_id, name), "resume": True,
                         "addAdmin": False})
        else:
            row.update(Action="blocked", Detail="connected elsewhere but never synced: finish it in Source "
                       "control, or disconnect")
    else:
        folder = folder_for(ws_id, name)
        row["Repository"], row["Branch"], row["Folder"] = ADO_REPOSITORY, ADO_BRANCH, folder
        problems = []
        if not info.get("capacityId"):
            problems.append("no capacity assigned")
        base = BRANCH_HEAD or default_head
        existing = ado_tree({**REPO, "directoryName": folder}, base, missing_ok=True) if base else []
        if [x for x in existing if x.lower() != folder_marker_path(folder).lower()]:
            problems.append(f"Git folder {folder} isn't empty")
        if (ADO_BRANCH, folder.strip("/").lower()) in used:
            problems.append(f"folder {folder} is already used by {used[(ADO_BRANCH, folder.strip('/').lower())]}")
        used[(ADO_BRANCH, folder.strip("/").lower())] = name
        role, add_admin = my_workspace_role(ws_id), False
        if role is not None and role != "Admin":
            if ADD_ME_AS_WORKSPACE_ADMIN:
                add_admin = True
            else:
                problems.append(f"you're {role or 'not a member'} here; linking needs Admin (set "
                                "ADD_ME_AS_WORKSPACE_ADMIN = True, or ask a workspace admin)")
        row["Your role"] = "unknown" if role is None else (role or "none")
        if problems:
            row.update(Action="blocked", Detail="; ".join(problems))
        else:
            row.update(Action=("add you as Admin, " if add_admin else "") + ("" if existing else "create Git folder, ")
                       + "connect + first commit", Detail="")
            PLAN.append({"id": ws_id, "name": name, "folder": folder, "resume": False, "addAdmin": add_admin,
                         "createFolder": not existing})
    rows.append(row)

CHECK_DF = pd.DataFrame(rows)
show(CHECK_DF.drop(columns=["WorkspaceId"], errors="ignore"))
CONNECT_ID = plan_hash({"repo": REPO, "branch": ADO_BRANCH, "head": BRANCH_HEAD, "plan": PLAN,
                        "linkMode": LINK["mode"], "connection": GIT_CONNECTION_ID, "createConnection": CREATE_CONNECTION})
print(f"\n{len(PLAN)} workspace(s) to connect. CONNECT ID: {CONNECT_ID}")
