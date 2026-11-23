CHECKS = []


def record(area, check, status, detail="", fix=""):
    CHECKS.append({"Area": area, "Check": check, "Result": status, "Detail": str(detail), "How to fix": fix})
    print(f"[{status:4}] {area} / {check}" + (f": {detail}" if detail else ""))
    if fix and status in ("FAIL", "WARN"):
        print(f"       FIX: {fix}")


def explain(exc):
    """How to fix a failed call, from its HTTP status and message."""
    text = f"{getattr(exc, 'code', '')} {exc}".lower()
    status = getattr(exc, "status", None)
    if "private link" in text or "publicnetworkaccess" in text or "firewall" in text or "not authorized to perform" \
            in text and "network" in text:
        return ("Network: the resource blocks public access. Create a Fabric managed private endpoint from this "
                "workspace to it (owner approves), or allow the Fabric service in its firewall.")
    if "secretnotfound" in text or (status == 404 and "vault" in text):
        return "The secret name is wrong or the secret doesn't exist in this Key Vault."
    if status == 203 or "signinpage" in text:
        return ("Azure DevOps rejected the token: see the 'diagnose' rows below for which part (token, organization, "
                "project or repository) is wrong.")
    if status == 401 or "tf400813" in text or "unauthorized" in text:
        return "The token is invalid or expired (DevOps PAT: renew it and update the Key Vault secret)."
    if status == 403 or "forbidden" in text or "insufficient" in text:
        return "Permission missing: see the check's description for the role needed."
    if status == 404:
        return "Not found: check the name/ID in the settings."
    return "See the detail; rerun after fixing."


missing = [n for n in ("ADO_ORGANIZATION", "ADO_PROJECT", "ADO_REPOSITORY", "KEY_VAULT_URL",
                       "ADLS_ACCOUNT") if "<" in str(globals()[n])] + ado_auth_missing()
if missing:
    raise ValueError("Fill in these settings first: " + ", ".join(missing))

ADO_ORGANIZATION = ado_org_name(ADO_ORGANIZATION)   # a URL works too

WHO = me()
record("Runtime", "identity", "WARN" if WHO["isApp"] else "PASS", WHO["upn"] or "(unknown)",
       "Service principal / workspace identity: Git linking needs GIT_LINK_AUTH = \"service_principal\" (or "
       "\"connection_id\"). Run interactively as a user otherwise." if WHO["isApp"] else "")

# ---------------------------------------------------------------- runtime
for aud in ("pbi", "storage"):
    try:
        notebookutils.credentials.getToken(aud)
        record("Runtime", f"token for {aud}", "PASS")
    except Exception as exc:
        record("Runtime", f"token for {aud}", "FAIL", exc, "Run the notebook in Fabric as a user with access.")

# ---------------------------------------------------------------- Fabric admin
try:
    http("GET", FABRIC_API + "/admin/workspaces", "pbi", params={"type": "Workspace"}, label="fabric")
    record("Fabric", "Fabric administrator (admin APIs)", "PASS")
except HttpError as exc:
    record("Fabric", "Fabric administrator (admin APIs)", "FAIL" if exc.status in (401, 403) else "WARN", exc.short(),
           "Assign the Fabric administrator role (Entra or Microsoft 365 admin center).")

# ---------------------------------------------------------------- Fabric tenant settings for Git integration
def _groups(t):
    return ", ".join(g.get("name") or g.get("graphId", "?") for g in t.get("enabledSecurityGroups") or [])


try:
    GIT_SETTINGS = git_tenant_settings()
    sync = next((t for k, t in GIT_SETTINGS.items() if "synchronize workspace items" in k.lower()), None)
    geo = next((t for k, t in GIT_SETTINGS.items() if "other geographical locations" in k.lower()), None)
    for label, t, needed in (("Git integration ('Users can synchronize workspace items with their Git repositories')",
                              sync, "FAIL"), (f"cross-geography Git ('{CROSS_GEO_TITLE}')", geo, "WARN")):
        if t is None:
            record("Fabric", f"tenant setting: {label}", "INFO", "not found in the tenant settings list",
                   "Check it in Admin portal > Tenant settings > Git integration.")
        elif t.get("enabled") and not t.get("enabledSecurityGroups"):
            record("Fabric", f"tenant setting: {label}", "PASS", "enabled for the entire organization")
        elif t.get("enabled"):
            record("Fabric", f"tenant setting: {label}", "WARN", f"enabled only for: {_groups(t)}",
                   "Everyone who runs V2_1 to V2_5 must be in one of these groups.")
        else:
            deleg = [n for n, f in (("capacity admins", "delegateToCapacity"), ("workspace admins", "delegateToWorkspace"))
                     if t.get(f)]
            record("Fabric", f"tenant setting: {label}", needed,
                   "disabled" + (f" at tenant level (delegated to {' and '.join(deleg)}, who may have enabled it)"
                                 if deleg else ""),
                   CROSS_GEO_FIX + " Needed when a capacity and the DevOps organization are in different "
                   "geographies, which the move to the new home region can cause even if it works today."
                   if t is geo else "A Fabric administrator must enable it: Git integration can't work without it.")
except HttpError as exc:
    record("Fabric", "tenant settings for Git", "WARN", exc.short(),
           "Couldn't read tenant settings (Fabric administrator needed). Check Git integration settings by hand.")

try:
    CAP_REGION = {c["id"]: c.get("region", "") for c in fabric_pages("/capacities")}
except HttpError:
    CAP_REGION = {}

# ---------------------------------------------------------------- how Fabric will sign in to DevOps for the Git link
LINK_MODE = None
try:
    LINK_MODE = git_link_mode()
    LINK = plan_git_link(WHO, ADO_ORGANIZATION, ADO_PROJECT, ADO_REPOSITORY, allow_create=False)
    record("Fabric", f"Git link sign-in (GIT_LINK_AUTH = \"{LINK_MODE}\")", "INFO" if LINK["create"] else "PASS",
           LINK["text"] + (" V2_1 creates it when you confirm its plan." if LINK["create"] else ""))
except (ValueError, RuntimeError, HttpError) as exc:
    record("Fabric", "Git link sign-in (GIT_LINK_AUTH)", "FAIL", str(exc),
           "Fix GIT_LINK_AUTH and the settings it needs (see the settings cell), then rerun.")

# ---------------------------------------------------------------- workspaces
ws_ids = [norm_id(w) for w in WORKSPACE_IDS]
if not ws_ids or None in ws_ids:
    raise ValueError("WORKSPACE_IDS must list at least one workspace ID (GUID).")
for ws_id in dict.fromkeys(ws_ids):
    area = f"Workspace {ws_id[:8]}"
    try:
        info = fabric("GET", f"/workspaces/{ws_id}")
        area = f"Workspace {info.get('displayName', ws_id)}"
        items = workspace_items(ws_id)
        record(area, "read workspace and items", "PASS", f"{len(items)} items")
    except HttpError as exc:
        record(area, "read workspace and items", "FAIL", exc.short(), "Add the user to the workspace (Admin to "
               "connect Git and delete items; Contributor at least).")
        continue
    role = my_workspace_role(ws_id)
    record(area, "your workspace role (Admin needed to link, disconnect and restore Git)",
           "INFO" if role is None else ("PASS" if role == "Admin" else "FAIL"),
           "couldn't read it (admin API)" if role is None else (role or "not a member"),
           "" if role == "Admin" else "Fabric administrator isn't enough: be added as workspace Admin, or set "
           "ADD_ME_AS_WORKSPACE_ADMIN = True in V2_1.")
    cap = info.get("capacityId") or ""
    record(area, "capacity assigned", "PASS" if cap else "FAIL",
           f"{cap} (region {CAP_REGION.get(cap) or 'unknown'}: compare with the DevOps organization's region)" if cap else "",
           "" if info.get("capacityId") else "Git integration needs the workspace on a Fabric capacity.")
    try:
        conn = git_connection(ws_id)
        state, gp = conn.get("gitConnectionState"), conn.get("gitProviderDetails") or {}
    except HttpError as exc:
        state, gp = None, {}
        record(area, "Git connection", "FAIL", exc.short(), explain(exc))
    if state == "ConnectedAndInitialized":
        same = str(gp.get("organizationName", "")).lower() == ADO_ORGANIZATION.lower()
        record(area, "Git connection", "PASS" if same else "WARN",
               f"{gp.get('organizationName')}/{gp.get('projectName')}/{gp.get('repositoryName')} "
               f"{gp.get('branchName')} {gp.get('directoryName')}",
               "" if same else "Connected to another organization than ADO_ORGANIZATION.")
        cred = my_git_credentials(ws_id)
        src = str(cred.get("source")) + (f" ({cred.get('connectionId')})" if cred.get("connectionId") else "")
        want_conn = git_link_mode() != "my_account"
        try:
            st = git_status(ws_id)
            note = ""
            if want_conn and cred.get("source") != "ConfiguredConnection":
                note = (f". Git calls work. If the workspace screens show 'Can't fetch branches', choose the Fabric "
                        f"connection {git_connection_label()} in Source control > account")
            record(area, "Git status (Contributor + your Git sign-in work)", "PASS",
                   f"your Git sign-in: {src}; {len(st.get('changes') or [])} uncommitted/incoming change(s){note}")
        except HttpError as exc:
            record(area, "Git status (Contributor + your Git sign-in work)", "FAIL",
                   f"your Git sign-in: {src}; {exc.short()}", git_fail_text(exc, ws_id, gp).split(" | FIX: ", 1)[-1])
    elif state:
        record(area, "Git connection", "INFO", f"{state}: connect it with V2_1_Connect_Workspaces_To_Git")
    for t in ITEM_TYPES:
        sample = next((i for i in items if i["type"] == t), None)
        if not sample:
            continue
        try:
            d = fabric("POST", f"/workspaces/{ws_id}/items/{sample['id']}/getDefinition").get("definition") or {}
            record(area, f"read definition ({t})", "PASS", f"{sample['displayName']}: {len(d.get('parts') or [])} parts")
        except HttpError as exc:
            record(area, f"read definition ({t})", "FAIL", exc.short(),
                   "Needed for the raw copy. Contributor or higher; some preview types may not support it yet.")

# ---------------------------------------------------------------- Key Vault / sign-in to Azure DevOps
ADO_MODE = ado_mode()
record("Azure DevOps", "sign-in method", "PASS",
       "personal access token" if ADO_MODE == "pat" else f"service principal {ADO_SP_CLIENT_ID} (tenant {ADO_SP_TENANT_ID})")
try:
    ado_header()
    record("Key Vault", "read the DevOps credential secret", "PASS")
    if ADO_MODE == "service_principal":
        record("Azure DevOps", "service principal sign-in", "PASS", "got an Entra token for Azure DevOps")
except HttpError as exc:
    record("Key Vault", "read the DevOps credential secret", "PASS")
    record("Azure DevOps", "service principal sign-in", "FAIL", exc.message,
           sp_signin_fix(exc.message, _ado_auth.get("sp_secret_kind", "")))
except Exception as exc:
    record("Key Vault", "read the DevOps credential secret", "FAIL", str(exc),
           explain(exc) + " The user needs 'Key Vault Secrets User' (RBAC) or Get on secrets (access policy).")

# ---------------------------------------------------------------- Azure DevOps
def diagnose_devops():
    """Narrow a failed repository read down to the token, the organization, the project or the repository."""
    sp = _ado_auth.get("mode") == "service_principal"
    if not sp:
        n = _ado_auth.get("pat_length", 0)
        kind, hint = _ado_auth.get("pat_shape", ("unknown", ""))
        looks = "looks like an Azure DevOps token" if kind == "pat" else f"looks like: {kind}"
        record("Azure DevOps", "diagnose: token format",
               "PASS" if kind == "pat" else ("WARN" if kind == "unknown" else "FAIL"),
               f"the secret holds {n} characters and {looks} (Azure DevOps tokens have 52 or 84)",
               hint + (" Then update the Key Vault secret (new version) and rerun this notebook; nothing else "
                       "changes. Or, if this is the client secret of an app registration you want to use, set "
                       "ADO_AUTH = \"service_principal\" with ADO_SP_TENANT_ID, ADO_SP_CLIENT_ID and "
                       "ADO_SP_SECRET_NAME." if hint else ""))
        if kind not in ("pat", "unknown"):
            return                               # the token is the problem; the other checks would only repeat it
    org = f"https://dev.azure.com/{quote(ADO_ORGANIZATION)}"
    try:
        me = ado_call(f"{org}/_apis/connectionData", params={"api-version": "7.1-preview"})
        user = (me.get("authenticatedUser") or {}).get("providerDisplayName") or "?"
        record("Azure DevOps", "diagnose: organization accepts the token", "PASS", f"{ADO_ORGANIZATION}: token of {user}")
    except HttpError as exc:
        if sp:
            record("Azure DevOps", "diagnose: organization accepts the token", "FAIL", exc.short(),
                   f"Organization '{ADO_ORGANIZATION}' doesn't accept the service principal. " + SP_NOT_IN_ORG +
                   " Also check that the organization's policies allow service principals and that ADO_ORGANIZATION "
                   "is the name in https://dev.azure.com/<name>.")
            return
        record("Azure DevOps", "diagnose: organization accepts the token", "FAIL", exc.short(),
               f"Organization '{ADO_ORGANIZATION}' doesn't accept this token. In order of likelihood: (1) the token "
               "expired or was revoked: create a new one and update the Key Vault secret; (2) it was created in "
               "another organization: create it in this one, or choose 'All accessible organizations'; (3) "
               "ADO_ORGANIZATION is wrong: use the name in https://dev.azure.com/<name>; (4) the organization's "
               "policies block it (personal access tokens restricted, or 'IP conditional access policy "
               "validation' on, which blocks calls from Fabric's addresses): ask the Azure DevOps admin; (5) the "
               "organization is connected to another Entra tenant than the token's user.")
        return
    try:
        ado_call(f"{org}/_apis/projects/{quote(ADO_PROJECT)}")
        record("Azure DevOps", "diagnose: project", "PASS", ADO_PROJECT)
    except HttpError as exc:
        record("Azure DevOps", "diagnose: project", "FAIL", exc.short(),
               f"Project '{ADO_PROJECT}' not found or not visible to the token's user: check the name and that the "
               "user (or service principal) is a project member; a token needs the Project and Team (Read) or Code scope.")
        return
    try:
        repos = ado_call(f"{org}/{quote(ADO_PROJECT)}/_apis/git/repositories").get("value", [])
        names = sorted(r.get("name", "") for r in repos)
        ok = ADO_REPOSITORY.lower() in (x.lower() for x in names)
        record("Azure DevOps", "diagnose: repository", "PASS" if ok else "FAIL",
               f"repositories visible: {', '.join(names[:10]) or 'none'}",
               "" if ok else f"'{ADO_REPOSITORY}' isn't one of them: fix ADO_REPOSITORY, or give the user access.")
    except HttpError as exc:
        record("Azure DevOps", "diagnose: repository", "FAIL", exc.short(),
               "Can't list repositories: a token needs the Code (Read & write) scope; a service principal needs Read on the "
               "repositories (project Contributors).")


if LINK_MODE == "service_principal" and not sp_settings_missing():
    try:
        sp_ok, sp_err = sp_repo_check(ADO_ORGANIZATION, ADO_PROJECT, ADO_REPOSITORY)
    except HttpError as exc:
        sp_ok, sp_err = False, exc.short()
    record("Azure DevOps", f"service principal {str(ADO_SP_CLIENT_ID).strip()} can read the repository (Fabric links "
           "with it)", "PASS" if sp_ok else "FAIL", ADO_REPOSITORY if sp_ok else sp_err, "" if sp_ok else SP_NOT_IN_ORG)

ORG_TENANT, ORG_TENANT_HOW = ado_org_tenant_info(ADO_ORGANIZATION, ADO_PROJECT, ADO_REPOSITORY)
same = ORG_TENANT == WHO.get("tid")
record("Azure DevOps", "organization in the same Entra tenant as Fabric",
       "INFO" if ORG_TENANT is None else ("PASS" if same else "FAIL"),
       f"couldn't tell ({ORG_TENANT_HOW}): check Organization settings > Microsoft Entra; it must be the Fabric tenant "
       f"{WHO.get('tid') or '?'}" if ORG_TENANT is None else (f"yes ({ORG_TENANT}; {ORG_TENANT_HOW})" if same else
       f"no: organization tenant {ORG_TENANT or 'none (not connected to Entra)'}, Fabric tenant {WHO.get('tid') or '?'}"),
       "" if ORG_TENANT is None or same else SAME_TENANT_FIX)
if (ADO_MODE == "service_principal" or LINK_MODE == "service_principal") and WHO.get("tid") and not sp_settings_missing():
    sp_same = str(ADO_SP_TENANT_ID).strip().lower() == WHO["tid"]
    record("Azure DevOps", "service principal in the same Entra tenant as Fabric", "PASS" if sp_same else "FAIL",
           str(ADO_SP_TENANT_ID), "" if sp_same else "Use an app registration from the Fabric tenant: this "
           "migration keeps everything in one tenant.")

REPO = {"organizationName": ADO_ORGANIZATION, "projectName": ADO_PROJECT, "repositoryName": ADO_REPOSITORY}
if "header" in _ado_auth:
    try:
        default_branch, head = ado_default_head(REPO)
        record("Azure DevOps", "read repository", "PASS", f"default branch {default_branch} at {head}")
    except HttpError as exc:
        head = None
        record("Azure DevOps", "read repository", "FAIL", exc.short(),
               explain(exc) + " The token needs Code (Read & write) and access to the project.")
        diagnose_devops()
    if head and WRITE_TESTS:
        test_branch = f"permission-check/{NOW_ID}"
        steps = [("create branch", lambda: ado_create_branch(REPO, test_branch, head), "Create branch permission."),
                 ("push a commit", lambda: ado(REPO, "POST", "/pushes", body={
                     "refUpdates": [{"name": f"refs/heads/{test_branch}", "oldObjectId": head}],
                     "commits": [{"comment": "permission check", "changes": [{"changeType": "add", "item": {
                         "path": "/permission-check.txt"}, "newContent": {"content": NOW_ID,
                                                                          "contentType": "rawtext"}}]}]}),
                  "Contribute permission."),
                 ("lock branch", lambda: ado_lock(REPO, test_branch), "Contribute permission (locks the snapshot)."),
                 ("unlock branch", lambda: ado_lock(REPO, test_branch, False), "Contribute permission.")]
        for label, fn, need in steps:
            try:
                fn()
                record("Azure DevOps", label, "PASS")
            except HttpError as exc:
                record("Azure DevOps", label, "FAIL", exc.short(), f"{need} {explain(exc)}")
                break
        try:
            ref = ado_ref(REPO, test_branch)
            if ref:
                ado_delete_branch(REPO, test_branch, ref["objectId"])
                record("Azure DevOps", "delete test branch", "PASS")
        except HttpError as exc:
            record("Azure DevOps", "delete test branch", "WARN", exc.short(),
                   f"Delete branch {test_branch} by hand (needs Force push or branch owner).")

# ---------------------------------------------------------------- ADLS
try:
    adls_list(ADLS_ROOT, recursive=False)
    record("ADLS", "list the backup folder", "PASS")
except HttpError as exc:
    record("ADLS", "list the backup folder", "FAIL", exc.short(),
           explain(exc) + " The user needs Storage Blob Data Contributor on the container.")
if WRITE_TESTS:
    p = f"{ADLS_ROOT.strip('/')}/_migration/_permission_check/{NOW_ID}.txt"
    try:
        adls_write(p, NOW_ID)
        ok = adls_read(p).decode() == NOW_ID
        adls_delete(p)
        record("ADLS", "write, read and delete a file", "PASS" if ok else "FAIL")
    except HttpError as exc:
        record("ADLS", "write, read and delete a file", "FAIL", exc.short(),
               explain(exc) + " Storage Blob Data Contributor on the container is needed.")
if SECONDARY_BACKUP_RUN_ID:
    run = f"{ADLS_ROOT.strip('/')}/{SECONDARY_BACKUP_RUN_ID}"
    try:
        done = adls_exists(f"{run}/_run_completed.json")
        record("ADLS", f"backup run {SECONDARY_BACKUP_RUN_ID}", "PASS" if done else "FAIL",
               "" if done else "not completed or not found", "" if done else "Run V2_2_Backup_Data to the end.")
    except HttpError as exc:
        record("ADLS", f"backup run {SECONDARY_BACKUP_RUN_ID}", "FAIL", exc.short(), explain(exc))

# ---------------------------------------------------------------- data restore (V2_6)
# The restored items are new, but your role on the workspace is the same: test on today's items.
probe_rel = f"{ADLS_ROOT.strip('/')}/_migration/_permission_check/{NOW_ID}_probe.csv"
probe_url = f"https://{ADLS_ACCOUNT}.blob.core.windows.net/{ADLS_CONTAINER}/{quote(probe_rel, safe='/')}"
probe_ok = False
try:
    adls_write(probe_rel, "x\nprobe\n")
    probe_ok = True
except HttpError as exc:
    record("Data restore", "probe file in ADLS", "FAIL", exc.short(), explain(exc))
for ws_id in dict.fromkeys(ws_ids):
    try:
        items = workspace_items(ws_id)
    except HttpError:
        continue
    area = f"Data restore {ws_id[:8]}"
    lh = next((i for i in items if i["type"] == "Lakehouse"), None)
    if lh and WRITE_TESTS:
        rel = f"Files/_permission_check_{NOW_ID}.txt"
        try:
            restore_file(probe_rel, len("x\nprobe\n"), ws_id, lh["id"], rel, False)
            onelake_delete(ws_id, lh["id"], rel)
            record(area, "write files into a Lakehouse (OneLake)", "PASS", lh["displayName"])
        except HttpError as exc:
            record(area, "write files into a Lakehouse (OneLake)", "FAIL", exc.short(),
                   "Contributor or higher on the workspace is needed to load Lakehouse data. " + explain(exc))
    kq = next((i for i in items if i["type"] == "KQLDatabase"), None)
    if kq:
        try:
            props = fabric("GET", f"/workspaces/{ws_id}/kqlDatabases/{kq['id']}").get("properties") or {}
            uri, db = props.get("queryServiceUri"), props.get("databaseName") or kq["displayName"]
            kusto_mgmt(uri, db, ".show tables")
            record(area, "read a KQL database", "PASS", kq["displayName"])
            if probe_ok:
                rows = kusto_mgmt(uri, db, f"externaldata(x:string) [h'{probe_url};impersonate'] "
                                           "with (format='csv', ignoreFirstRecord=true) | count", kind="query")
                n = int(rows[0].get("Count", 0)) if rows else 0
                record(area, "Eventhouse reads the backup with your identity", "PASS" if n == 1 else "FAIL",
                       f"{n} row(s) read", "" if n == 1 else "Unexpected result; see the detail.")
        except HttpError as exc:
            record(area, "KQL database / Eventhouse reads the backup", "FAIL", exc.short(),
                   "Ingestion reads the backup files with your identity (;impersonate): you need Storage Blob Data "
                   "Reader on the container, and the storage firewall must let the Eventhouse in (or use a managed "
                   "private endpoint). Contributor on the workspace gives database admin. " + explain(exc))
    wh = next((i for i in items if i["type"] == "Warehouse"), None)
    if wh and probe_ok:
        try:
            df = sql_query(ws_id, wh["id"], "Warehouse",
                           f"SELECT COUNT(*) AS n FROM OPENROWSET(BULK '{probe_url}', FORMAT = 'CSV', "
                           "FIRSTROW = 2) WITH (x VARCHAR(10)) AS r")
            n = int(df.iloc[0, 0])
            record(area, "Warehouse reads the backup with your identity", "PASS" if n == 1 else "WARN", f"{n} row(s)")
        except Exception as exc:
            record(area, "Warehouse reads the backup with your identity", "WARN", str(exc),
                   "COPY INTO reads the backup with your identity: Storage Blob Data Reader on the container and a "
                   "storage firewall that lets Fabric in. If only this probe fails, run the load script once on a "
                   "test table to confirm.")
if probe_ok:
    try:
        adls_delete(probe_rel)
    except HttpError:
        pass
for ws_id in dict.fromkeys(ws_ids):
    area = f"Data restore {ws_id[:8]}"
    try:
        samples = workspace_items(ws_id)
    except HttpError as exc:
        record(area, "SQL execution through Fabric connector", "WARN", exc.short(), explain(exc))
        continue
    for typ in ("Warehouse", "SQLEndpoint"):
        sample = next((i for i in samples if i["type"] == typ and not is_dataflow_staging(i)), None)
        if not sample:
            continue
        try:
            sql_execute(ws_id, sample["id"], typ, ["SELECT 1"])
            record(area, f"SQL execution through Fabric connector ({typ})", "PASS",
                   f"{sample['displayName']}: read-only SELECT 1 batch executed")
        except Exception as exc:
            record(area, f"SQL execution through Fabric connector ({typ})", "WARN", str(exc),
                   "The run modes need Fabric's notebookutils.data SQL connector and access to this item. "
                   "Fix the connection/runtime or use the script modes in V2_6 and run SQL in the SQL editor.")
