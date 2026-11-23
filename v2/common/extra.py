

def adls_list(path, recursive=True):
    """[(path relative to `path`, size, is_dir)] under a folder of the container; [] if it doesn't exist."""
    url = f"https://{ADLS_ACCOUNT}.dfs.core.windows.net/{ADLS_CONTAINER}"
    params = {"resource": "filesystem", "recursive": str(recursive).lower(), "directory": path.strip("/")}
    out, prefix = [], path.strip("/") + "/"
    while True:
        try:
            resp = http("GET", url, "storage", headers=ADLS_HEADERS, params=params, label="adls")
        except HttpError as exc:
            if exc.status == 404:
                return out
            raise
        for e in resp.json().get("paths", []):
            name = e["name"][len(prefix):] if e["name"].startswith(prefix) else e["name"]
            out.append((name, int(e.get("contentLength") or 0), str(e.get("isDirectory", "false")).lower() == "true"))
        token = resp.headers.get("x-ms-continuation")
        if not token:
            return out
        params = {**params, "continuation": token}


def adls_read(path):
    return http("GET", adls_url(path), "storage", headers=ADLS_HEADERS, label="adls").content


def adls_delete(path):
    http("DELETE", adls_url(path), "storage", headers=ADLS_HEADERS, label="adls")


def adls_exists(path):
    try:
        http("HEAD", adls_url(path), "storage", headers=ADLS_HEADERS, label="adls")
        return True
    except HttpError as exc:
        if exc.status == 404:
            return False
        raise


# ---------------------------------------------------------------- Power BI item types
# Power BI items survive a cross-region move; every other item type is a Fabric item that must be deleted first.
POWER_BI_TYPES = {"Report", "SemanticModel", "Dashboard", "PaginatedReport"}
# Items Fabric creates and deletes together with their parent; they are never saved in Git on their own.
CHILD_TYPES = {"SQLEndpoint"}
DATA_TYPES = {"Lakehouse": "Data", "Warehouse": "Data", "KQLDatabase": "KQL export"}
# Dataflow Gen2 staging items: Fabric creates them for Dataflows Gen2 (one Lakehouse and one Warehouse per workspace,
# named StagingLakehouseForDataflows_<timestamp> / StagingWarehouseForDataflows_<timestamp>) and creates them again
# when a Dataflow Gen2 runs. Not saved in Git, not backed up, not restored; V2_4 still deletes them (a Fabric item
# left behind blocks a tenant migration).
STAGING_RE = re.compile(r"^Staging(Lakehouse|Warehouse)ForDataflows(_\d+)?$", re.IGNORECASE)


def is_dataflow_staging(item):
    return bool(item.get("staging")) or (item.get("type") in ("Lakehouse", "Warehouse", "SQLEndpoint")
                                         and bool(STAGING_RE.match(str(item.get("displayName") or ""))))
KQL_ADX_STEP = "Kusto Copy"      # V2_2 with KQL_BACKUP_MODE "adx"/"both": the KQL rows are in Azure Data Explorer


def data_step(item_type, status_of):
    """(backup step, status) holding an item's data. KQL databases: the Parquet export if it's OK, otherwise the
    Kusto Copy to ADX if that's OK, otherwise whichever the backup recorded. status_of(step) -> status or ""."""
    step = DATA_TYPES.get(item_type)
    if item_type != "KQLDatabase":
        return step, status_of(step) if step else ""
    exp, adx = status_of(step), status_of(KQL_ADX_STEP)
    if exp == "OK" or not adx:
        return (step, exp) if exp or not adx else (KQL_ADX_STEP, adx)
    if adx == "OK" or not exp:
        return KQL_ADX_STEP, adx
    return step, exp


GUID_ANY_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def definition_hash(parts):
    """Fingerprint of an item definition (every part except .platform), to prove it didn't change."""
    h = hashlib.sha256()
    for p in sorted(parts, key=lambda x: x["path"]):
        if p["path"] == ".platform":
            continue
        h.update(p["path"].encode("utf-8") + b"\0" + base64.b64decode(p.get("payload") or "") + b"\0")
    return h.hexdigest()


def adls_only(item):
    """Item in a Git-linked workspace that Git doesn't hold: V2_5 recreates it from its ADLS definition.
    ("secondary backup ..." is the label older snapshots used.)"""
    rec = str(item.get("recovery") or "")
    return not item.get("inGit") and (rec.startswith("ADLS definition (V2_5") or rec.startswith("secondary backup"))


def key_of(item_type, name):
    return f"{item_type}|{str(name).strip().lower()}"


# ---------------------------------------------------------------- Azure DevOps (read the snapshot, manage branches)
ADO_API_VERSION = "7.1"
_ado_auth = {}


ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"     # Azure DevOps' fixed Entra application ID


def ado_mode():
    mode = str(globals().get("ADO_AUTH", "pat") or "pat").strip().lower().replace("-", "_").replace(" ", "_")
    if mode in ("sp", "spn", "serviceprincipal"):
        mode = "service_principal"
    if mode not in ("pat", "service_principal"):
        raise ValueError(f"ADO_AUTH = {globals().get('ADO_AUTH')!r}: use \"pat\" or \"service_principal\"")
    return mode


def ado_auth_missing():
    """Settings the chosen ADO_AUTH needs that are still empty or placeholders."""
    need = ["ADO_PAT_SECRET_NAME"] if ado_mode() == "pat" else ["ADO_SP_TENANT_ID", "ADO_SP_CLIENT_ID", "ADO_SP_SECRET_NAME"]
    return [n for n in need if not str(globals().get(n) or "").strip() or "<" in str(globals().get(n))]


def _clean_secret(value):
    value = str(value or "").strip().strip('"').strip("'").strip()
    register_sensitive_value(value)
    return value


_sp_auth = {}


def sp_header():
    """Entra token (Bearer header) of the service principal in ADO_SP_* for Azure DevOps, renewed 5 minutes before it
    expires. Used by ADO_AUTH = "service_principal" and to check GIT_LINK_AUTH = "service_principal"."""
    if "header" in _sp_auth and _sp_auth.get("expires", 0) > time.time() + 300:
        return _sp_auth["header"]
    secret = _clean_secret(notebookutils.credentials.getSecret(KEY_VAULT_URL, ADO_SP_SECRET_NAME))
    kind, _ = secret_shape(secret)
    _sp_auth.update(secret_length=len(secret), secret_kind=kind)
    url = f"https://login.microsoftonline.com/{quote(str(ADO_SP_TENANT_ID).strip())}/oauth2/v2.0/token"
    try:
        tok = http("POST", url, data={"grant_type": "client_credentials", "client_id": str(ADO_SP_CLIENT_ID).strip(),
                                      "client_secret": secret, "scope": ADO_RESOURCE + "/.default"},
                   label="entra").json()
    except HttpError as exc:
        raise HttpError(exc.status, exc.code or "SignInFailed",
                        f"service principal sign-in failed: {exc.message} {sp_signin_fix(exc.message, kind)}".strip(),
                        url) from None
    _sp_auth["header"] = "Bearer " + tok["access_token"]
    register_sensitive_value(tok["access_token"])
    _sp_auth["expires"] = time.time() + int(tok.get("expires_in", 3600))
    return _sp_auth["header"]


def ado_header():
    """Authorization header for Azure DevOps: a personal access token, or an Entra token of a service principal
    (renewed 5 minutes before it expires, so long runs keep working). Secrets come from Key Vault and are never shown."""
    if ado_mode() == "pat":
        if "header" not in _ado_auth:
            pat = _clean_secret(notebookutils.credentials.getSecret(KEY_VAULT_URL, ADO_PAT_SECRET_NAME))
            _ado_auth.update(mode="pat", pat_length=len(pat), pat_shape=secret_shape(pat))
            _ado_auth["header"] = "Basic " + base64.b64encode((":" + pat).encode()).decode()
        return _ado_auth["header"]
    _ado_auth["mode"] = "service_principal"
    try:
        header = sp_header()
    finally:
        _ado_auth.update(pat_length=_sp_auth.get("secret_length"), sp_secret_kind=_sp_auth.get("secret_kind"))
    _ado_auth.update(header=header, expires=_sp_auth["expires"])
    return header


def sp_signin_fix(message, kind=""):
    m = str(message or "")
    if kind == "pat":
        return ("The Key Vault secret looks like a personal access token, not a client secret: set ADO_AUTH = \"pat\" "
                "or store the app's client secret value.")
    if "AADSTS7000215" in m or "AADSTS7000222" in m:
        return ("Fix: the client secret is wrong or expired. Store the secret's Value (not its Secret ID) from the app "
                "registration > Certificates & secrets, as a new version of the Key Vault secret.")
    if "AADSTS700016" in m:
        return "Fix: ADO_SP_CLIENT_ID isn't an app in tenant ADO_SP_TENANT_ID: check both IDs."
    if "AADSTS90002" in m or "AADSTS900023" in m:
        return "Fix: ADO_SP_TENANT_ID is wrong: use the Directory (tenant) ID from the app registration's overview."
    return "Check ADO_SP_TENANT_ID, ADO_SP_CLIENT_ID and the client secret value."


def secret_shape(value):
    """(kind, hint) describing what a secret looks like, without revealing it. kind 'pat' = plausible DevOps token."""
    v, n = value, len(value)
    if not v:
        return "empty", "The secret is empty: paste the token as the secret value."
    if re.fullmatch(r"[a-z2-7]{52}", v) or (n == 84 and re.fullmatch(r"[A-Za-z0-9]{84}", v)):
        return "pat", ""
    if re.match(r"^[A-Za-z0-9_\-.~]{3}\dQ~", v) or (n == 40 and "~" in v):
        return "client secret", ("It looks like an Entra ID app registration client secret (40 characters with 'Q~'), "
                                 "not an Azure DevOps personal access token. Create a token in Azure DevOps (User "
                                 "settings > Personal access tokens, scope Code: Read & write) and store that value.")
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", v):
        return "GUID", ("It is a GUID: probably a client ID, tenant ID or a client secret's 'Secret ID'. None of "
                        "these is an Azure DevOps token.")
    if v.startswith(("ghp_", "gho_", "ghs_", "github_pat_")):
        return "GitHub token", "It looks like a GitHub token, not an Azure DevOps personal access token."
    if re.fullmatch(r"[0-9a-fA-F]{40}", v):
        return "40 hex", ("It is 40 hexadecimal characters: a Git commit ID or an old GitHub token, not an Azure "
                          "DevOps personal access token.")
    if v.startswith(("{", "[")):
        return "JSON", "It holds a JSON value: store only the token itself."
    if v.lower().startswith(("http://", "https://")):
        return "URL", "It holds a URL: store the token, and put the organization in ADO_ORGANIZATION."
    if v.lower().startswith(("bearer ", "basic ")) or re.search(r"\s", v):
        return "several values", "It holds more than one value (spaces or a 'Bearer'/'Basic' prefix): store only the token."
    if v.startswith("eyJ"):
        return "JWT", "It is an Entra access token (JWT), which expires within hours: store a personal access token."
    return "unknown", (f"Azure DevOps tokens have 52 or 84 characters; this has {n}. Check that the secret holds "
                       "the token and nothing else.")


def ado_org_name(value):
    """'contoso', 'https://dev.azure.com/contoso/...' or 'https://contoso.visualstudio.com' -> 'contoso'."""
    text = str(value or "").strip().rstrip("/")
    m = re.match(r"^(?:https?://)?dev\.azure\.com/([^/]+)", text, re.I) or \
        re.match(r"^(?:https?://)?([^./]+)\.visualstudio\.com", text, re.I)
    return m.group(1) if m else text


SIGN_IN_PAGE = ("Azure DevOps answered with its sign-in page instead of data (HTTP 203): it didn't accept the "
                "personal access token for this organization")


SP_NOT_IN_ORG = ("Add the service principal to the organization (Organization settings > Users > Add users, "
                 "search the app name, Basic access) and to the project's Contributors group; the organization must "
                 "be connected to the app's Entra tenant.")


def ado_call(url, method="GET", params=None, body=None, raw=False, auth=None):
    """auth = another Authorization header (for example sp_header()); default: the ADO_AUTH sign-in."""
    sp = auth is not None or _ado_auth.get("mode") == "service_principal"
    try:
        resp = http(method, url, auth=auth or ado_header(), params={"api-version": ADO_API_VERSION, **(params or {})},
                    json_body=body, label="devops", headers={"Accept": "application/octet-stream"} if raw else None)
    except HttpError as exc:
        if exc.status == 401 and (sp or _ado_auth.get("mode") == "service_principal"):
            raise HttpError(401, exc.code or "Unauthorized", f"{exc.message} {SP_NOT_IN_ORG}".strip(), url) from None
        raise
    ctype = resp.headers.get("Content-Type", "json")
    if resp.status_code == 203 or "html" in ctype:
        if sp or _ado_auth.get("mode") == "service_principal":
            raise HttpError(203, "SignInPage", "Azure DevOps answered with its sign-in page instead of data (HTTP "
                            "203): it didn't accept the service principal's token. " + SP_NOT_IN_ORG, url)
        kind, hint = _ado_auth.get("pat_shape", ("pat", ""))
        raise HttpError(203, "SignInPage", SIGN_IN_PAGE + (f". {hint}" if kind != "pat" else ""), url)
    if raw:
        return resp.content
    if "json" not in ctype:
        raise HttpError(resp.status_code, "NotJson", f"Azure DevOps returned {ctype}, not JSON", url)
    return resp.json() if resp.content else {}


def ado(gp, method, path, params=None, body=None, raw=False):
    """Call the Azure DevOps Git API of the repository in a Fabric Git connection (gp = gitProviderDetails)."""
    url = (f"https://dev.azure.com/{quote(ado_org_name(gp['organizationName']))}/{quote(gp['projectName'])}"
           f"/_apis/git/repositories/{quote(gp['repositoryName'])}{path}")
    return ado_call(url, method, params, body, raw)


def ado_ref(gp, branch):
    """{'objectId', 'isLocked'} of a branch, or None if it doesn't exist."""
    refs = ado(gp, "GET", "/refs", params={"filter": f"heads/{branch}"}).get("value", [])
    return next((r for r in refs if r.get("name") == f"refs/heads/{branch}"), None)


def ado_create_branch(gp, branch, commit):
    res = ado(gp, "POST", "/refs", body=[{"name": f"refs/heads/{branch}", "oldObjectId": "0" * 40,
                                          "newObjectId": commit}]).get("value", [{}])
    if not res or not res[0].get("success"):
        raise HttpError(None, (res[0] if res else {}).get("updateStatus", "Failed"),
                        f"couldn't create branch {branch}", branch)


def ado_lock(gp, branch, locked=True):
    ado(gp, "PATCH", "/refs", params={"filter": f"heads/{branch}"}, body={"isLocked": locked})


def ado_delete_branch(gp, branch, commit):
    res = ado(gp, "POST", "/refs", body=[{"name": f"refs/heads/{branch}", "oldObjectId": commit,
                                          "newObjectId": "0" * 40}]).get("value", [{}])
    if not res or not res[0].get("success"):
        raise HttpError(None, (res[0] if res else {}).get("updateStatus", "Failed"), f"couldn't delete {branch}", branch)


def ado_default_head(gp):
    """(default branch name, its head commit) of the repository."""
    repo = ado(gp, "GET", "")
    name = (repo.get("defaultBranch") or "refs/heads/main")[len("refs/heads/"):]
    ref = ado_ref(gp, name)
    return name, (ref or {}).get("objectId")


def ado_tree(gp, commit, missing_ok=False):
    """Every file path under the connection's Git folder at a commit ([] if the folder is missing and missing_ok)."""
    scope = "/" + (gp.get("directoryName") or "").strip("/")
    try:
        body = ado(gp, "GET", "/items", params={"scopePath": scope, "recursionLevel": "Full",
                                                "versionDescriptor.version": commit,
                                                "versionDescriptor.versionType": "commit"})
    except HttpError as exc:
        if missing_ok and exc.status == 404:
            return []
        raise
    return [e["path"] for e in body.get("value", []) if e.get("gitObjectType") == "blob"]


def ado_file(gp, commit, path):
    return ado(gp, "GET", "/items", params={"path": path, "versionDescriptor.version": commit,
                                            "versionDescriptor.versionType": "commit", "$format": "octetStream"},
               raw=True)


def git_items_at(gp, commit):
    """Fabric items saved in Git at a commit: {key_of(type, name): {type, displayName, logicalId, folder, files}}."""
    paths = ado_tree(gp, commit)
    out = {}
    for p in paths:
        if not p.endswith("/.platform"):
            continue
        folder = p[: -len("/.platform")]
        try:
            meta = json.loads(ado_file(gp, commit, p).decode("utf-8-sig"))
        except ValueError as exc:
            raise HttpError(None, "BadPlatformFile", f"{p} is not valid JSON: {exc}", p) from None
        md, cfg = meta.get("metadata") or {}, meta.get("config") or {}
        files = [q for q in paths if q.startswith(folder + "/")]
        out[key_of(md.get("type"), md.get("displayName"))] = {
            "type": md.get("type"), "displayName": md.get("displayName"), "logicalId": cfg.get("logicalId"),
            "folder": folder, "files": len(files)}
    return out


# ---------------------------------------------------------------- Fabric Git integration
FOLDER_MARKER = "README.md"     # placeholder V2_1 writes so the workspace's Git folder exists before linking


def folder_marker_path(folder):
    return "/" + folder.strip("/") + "/" + FOLDER_MARKER


def ado_create_folder(gp, branch, folder, text):
    """Create a Git folder by pushing a small marker file to the branch: the Fabric Git connect API (unlike the
    workspace settings screen) doesn't create a missing folder and fails with GitProviderResourceNotFound.
    Returns the new branch head."""
    ref = ado_ref(gp, branch)
    if not ref:
        raise HttpError(None, "BranchNotFound", f"branch {branch} doesn't exist", branch)
    res = ado(gp, "POST", "/pushes", body={
        "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": ref["objectId"]}],
        "commits": [{"comment": f"Create Git folder {folder} for Fabric Git integration",
                     "changes": [{"changeType": "add", "item": {"path": folder_marker_path(folder)},
                                  "newContent": {"content": text, "contentType": "rawtext"}}]}]})
    return ((res.get("commits") or [{}])[0]).get("commitId")


def ado_tree_ids(gp, commit):
    """{path: blob objectId} under the connection's Git folder at a commit: compares folder content cheaply."""
    scope = "/" + (gp.get("directoryName") or "").strip("/")
    body = ado(gp, "GET", "/items", params={"scopePath": scope, "recursionLevel": "Full",
                                            "versionDescriptor.version": commit,
                                            "versionDescriptor.versionType": "commit"})
    return {e["path"]: e.get("objectId") for e in body.get("value", []) if e.get("gitObjectType") == "blob"}


def ado_create_tag(gp, name, commit, message):
    """Annotated tag (refs/tags/<name>) on a commit. Returns the tag's commit, or raises."""
    existing = ado(gp, "GET", "/refs", params={"filter": f"tags/{name}"}).get("value", [])
    if any(r.get("name") == f"refs/tags/{name}" for r in existing):
        raise HttpError(None, "TagExists", f"tag {name} already exists", name)
    return ado(gp, "POST", "/annotatedtags", body={"name": name, "taggedObject": {"objectId": commit},
                                                    "message": message})


# ---------------------------------------------------------------- who runs this, and their workspace role
def token_claims(audience="pbi"):
    """Claims of the runner's Entra token (no secret is printed): oid, upn, idtyp ('app' = service principal)."""
    try:
        part = notebookutils.credentials.getToken(audience).split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def me():
    c = token_claims()
    return {"oid": str(c.get("oid", "")).lower(), "upn": c.get("upn") or c.get("unique_name") or c.get("appid", ""),
            "tid": str(c.get("tid", "")).lower(),
            "isApp": c.get("idtyp") == "app" or ("appid" in c and "upn" not in c and "unique_name" not in c)}


def _tenant_from_headers(headers):
    tid = headers.get("X-VSS-ResourceTenant") or headers.get("x-vss-resourcetenant")
    if tid is None:
        m = re.search(r"login\.(?:microsoftonline\.com|windows\.net)/([0-9a-fA-F-]{36})",
                      headers.get("WWW-Authenticate") or headers.get("www-authenticate") or "")
        tid = m.group(1) if m else None
    if tid is None:
        return None
    tid = tid.split(",")[0].strip().lower()
    return "" if tid in ("", "00000000-0000-0000-0000-000000000000") else tid


def sp_settings_missing():
    return [n for n in ("ADO_SP_TENANT_ID", "ADO_SP_CLIENT_ID", "ADO_SP_SECRET_NAME")
            if not str(globals().get(n) or "").strip() or "<" in str(globals().get(n))]


def sp_repo_check(org, project, repo):
    """(True, '') if the service principal in ADO_SP_* can read the repository, else (False, error)."""
    url = f"https://dev.azure.com/{quote(org)}/{quote(project)}/_apis/git/repositories/{quote(repo)}"
    try:
        ado_call(url, auth=sp_header())
        return True, ""
    except HttpError as exc:
        return False, exc.short()


def ado_org_tenant_info(org, project=None, repo=None):
    """(tenant ID, how it was found) of the Entra tenant the DevOps organization is connected to. '' = not connected
    to Entra, None = couldn't tell. 1) Azure DevOps names it in the X-VSS-ResourceTenant (or WWW-Authenticate) header
    of an anonymous call. 2) Otherwise, if the service principal in ADO_SP_* can read the repository, the organization
    is connected to the service principal's tenant: Azure DevOps only accepts service principals of its own tenant."""
    for path in ("_apis/projects?api-version=" + ADO_API_VERSION, "", "_apis/connectionData"):
        try:
            resp = _session().get(f"https://dev.azure.com/{quote(org)}/{path}", timeout=30, allow_redirects=False)
        except requests.RequestException:
            continue
        tid = _tenant_from_headers(resp.headers)
        if tid is not None:
            return tid, "named by Azure DevOps"
    if project and repo and not sp_settings_missing():
        try:
            ok, _ = sp_repo_check(org, project, repo)
        except HttpError:
            ok = False
        if ok:
            return (str(ADO_SP_TENANT_ID).strip().lower(),
                    f"the organization accepts service principal {str(ADO_SP_CLIENT_ID).strip()} of tenant "
                    f"{str(ADO_SP_TENANT_ID).strip()}, and Azure DevOps only accepts service principals of the Entra "
                    "tenant it's connected to")
    return None, ("Azure DevOps didn't name its tenant, and the service principal couldn't confirm it" if project
                  else "Azure DevOps didn't name its tenant")


def ado_org_tenant(org, project=None, repo=None):
    return ado_org_tenant_info(org, project, repo)[0]


def my_workspace_role(ws_id):
    """Your role in a workspace via the admin API (Fabric administrator): 'Admin', 'Member', ..., '' = none,
    None = couldn't tell."""
    who = me()
    try:
        users = fabric("GET", f"/admin/workspaces/{ws_id}/users").get("accessDetails") or []
    except HttpError:
        return None
    for u in users:
        pr = u.get("principal") or {}
        upn = ((pr.get("userDetails") or {}).get("userPrincipalName") or "").lower()
        if str(pr.get("id", "")).lower() == who["oid"] or (upn and upn == str(who["upn"]).lower()):
            return (u.get("workspaceAccessDetails") or {}).get("workspaceRole") or ""
    return ""


def add_me_as_workspace_admin(ws_id):
    """Fabric/Power BI administrator API: add the runner as Admin of a workspace."""
    who = me()
    http("POST", f"https://api.powerbi.com/v1.0/myorg/admin/groups/{ws_id}/users", "pbi",
         json_body={"identifier": who["oid"], "principalType": "App" if who["isApp"] else "User",
                    "groupUserAccessRight": "Admin"}, label="fabric")


CROSS_GEO_TITLE = "Users can export items to Git repositories in other geographical locations"
CROSS_GEO_FIX = ("The workspace's capacity is in another geography than the Azure DevOps organization. A Fabric "
                 "administrator must enable Admin portal > Tenant settings > Git integration > '" + CROSS_GEO_TITLE +
                 "' for the entire organization, or for a security group that includes everyone who runs V2_1 to "
                 "V2_5 (the setting applies to the person doing the Git operation, not to the DevOps account). Only "
                 "item definitions cross geographies, never data. Wait a few minutes after Apply, then rerun: the "
                 "notebook resumes. Linking by hand fails the same way until the setting is on.")


def git_tenant_settings():
    """Git integration tenant settings from the admin API, by title (admin role needed)."""
    res = fabric("GET", "/admin/tenantsettings")
    return {t.get("title", t.get("settingName", "")): t for t in res.get("tenantSettings", [])
            if "git" in (t.get("title", "") + t.get("tenantSettingGroup", "")).lower()}


def git_connection_url(org, project, repo):
    return f"https://dev.azure.com/{quote(org)}/{quote(project)}/_git/{quote(repo)}/"


def find_git_connection(name):
    """Fabric connection with this display name that you can use, or None."""
    return next((c for c in fabric_pages("/connections") if c.get("displayName") == name), None)


def create_git_connection(name, org, project, repo):
    """Fabric cloud connection 'Azure DevOps - Source control' with the service principal in ADO_SP_*. The
    client secret is read from Key Vault and only sent to Fabric."""
    secret = _clean_secret(notebookutils.credentials.getSecret(KEY_VAULT_URL, ADO_SP_SECRET_NAME))
    body = {"displayName": name, "connectivityType": "ShareableCloud",
            "connectionDetails": {"type": "AzureDevOpsSourceControl", "creationMethod": "AzureDevOpsSourceControl.Contents",
                                  "parameters": [{"dataType": "Text", "name": "url",
                                                  "value": git_connection_url(org, project, repo)}]},
            "credentialDetails": {"credentials": {"credentialType": "ServicePrincipal",
                                                  "tenantId": str(ADO_SP_TENANT_ID).strip(),
                                                  "servicePrincipalClientId": str(ADO_SP_CLIENT_ID).strip(),
                                                  "servicePrincipalSecret": secret}}}
    return fabric("POST", "/connections", body)


GIT_LINK_MODES = {"service_principal": "service principal (Fabric connection)",
                  "my_account": "your own Azure DevOps sign-in",
                  "connection_id": "an existing Fabric connection (GIT_CONNECTION_ID)"}


def git_link_mode():
    """How Fabric signs in to Azure DevOps for the workspaces' Git link (GIT_LINK_AUTH)."""
    raw = globals().get("GIT_LINK_AUTH")
    if raw is None:                       # older settings cells
        if globals().get("CREATE_GIT_CONNECTION"):
            return "service_principal"
        return "connection_id" if str(globals().get("GIT_CONNECTION_ID") or "").strip() else "my_account"
    m = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    m = {"sp": "service_principal", "spn": "service_principal", "serviceprincipal": "service_principal",
         "user": "my_account", "me": "my_account", "automatic": "my_account", "sso": "my_account",
         "connection": "connection_id", "configuredconnection": "connection_id"}.get(m, m)
    if m not in GIT_LINK_MODES:
        raise ValueError(f"GIT_LINK_AUTH = {raw!r}: use \"service_principal\", \"my_account\" or \"connection_id\"")
    return m


def _read_git_connection(cid):
    try:
        c = fabric("GET", f"/connections/{cid}")
    except HttpError as exc:
        raise RuntimeError(f"Can't read Fabric connection {cid} ({exc.short()}): check GIT_CONNECTION_ID, and that you "
                           "have User permission on it (Settings > Manage connections and gateways).") from None
    t = (c.get("connectionDetails") or {}).get("type")
    if t != "AzureDevOpsSourceControl":
        raise RuntimeError(f"Fabric connection {cid} ({c.get('displayName')}) is of type {t}, not Azure DevOps - "
                           "Source control: choose another connection.")
    return c


def plan_git_link(who, org, project, repo, allow_create=True):
    """How Fabric will sign in to Azure DevOps when it links a workspace, from GIT_LINK_AUTH. Returns {mode,
    connectionId, create (name of the connection to create on confirm, or None), text}. Raises before anything
    changes when the choice can't work."""
    mode = git_link_mode()
    cid = str(globals().get("GIT_CONNECTION_ID") or "").strip()
    if mode == "my_account":
        if cid:
            raise ValueError("GIT_CONNECTION_ID is filled but GIT_LINK_AUTH = \"my_account\": empty GIT_CONNECTION_ID, "
                             "or set GIT_LINK_AUTH = \"connection_id\" to use that connection.")
        if who["isApp"]:
            raise RuntimeError("This notebook runs as a service principal / workspace identity, which has no Azure "
                               "DevOps sign-in of its own: set GIT_LINK_AUTH = \"service_principal\".")
        return {"mode": mode, "connectionId": "", "create": None,
                "text": f"your own Azure DevOps sign-in ({who['upn']}). That only works if the DevOps organization "
                        f"is connected to the same Entra tenant as Fabric and {who['upn']} is a member of it, with "
                        "Basic access and project Contributors."}
    if mode == "connection_id":
        if not cid:
            raise ValueError("GIT_LINK_AUTH = \"connection_id\" needs GIT_CONNECTION_ID.")
        c = _read_git_connection(cid)
        return {"mode": mode, "connectionId": cid, "create": None,
                "text": f"the identity stored in Fabric connection {c.get('displayName')} ({cid}). Give that identity "
                        "Basic access and project Contributors in Azure DevOps."}
    need = sp_settings_missing()
    if need:
        raise ValueError("GIT_LINK_AUTH = \"service_principal\" needs: " + ", ".join(need) +
                         " (fill them even if ADO_AUTH = \"pat\").")
    sp_tid = str(ADO_SP_TENANT_ID).strip().lower()
    if who.get("tid") and sp_tid != who["tid"] and not globals().get("ALLOW_CROSS_TENANT"):
        raise RuntimeError(f"Stopped: the service principal's tenant ({sp_tid}) isn't the Fabric tenant ({who['tid']}). "
                           "Use an app registration from the same tenant (ALLOW_CROSS_TENANT is not part of this "
                           "migration).")
    sp_text = (f"service principal {str(ADO_SP_CLIENT_ID).strip()} (not tied to a person). Fabric stores its client "
               "secret in the connection: when you renew the secret, update the connection too.")
    if cid:
        c = _read_git_connection(cid)
        return {"mode": mode, "connectionId": cid, "create": None,
                "text": f"Fabric connection {c.get('displayName')} ({cid}), which signs in as the " + sp_text}
    name = str(globals().get("GIT_CONNECTION_NAME") or "").strip() or f"fabric-migration-{org}-{project}-{repo}"
    found = find_git_connection(name)
    if found:
        t = (found.get("connectionDetails") or {}).get("type")
        cred = (found.get("credentialDetails") or {}).get("credentialType")
        if t != "AzureDevOpsSourceControl" or (cred and cred != "ServicePrincipal"):
            raise RuntimeError(f"A Fabric connection named {name} already exists but it's {t} / {cred}, not Azure DevOps "
                               "- Source control with a service principal: set another GIT_CONNECTION_NAME.")
        return {"mode": mode, "connectionId": found["id"], "create": None,
                "text": f"existing Fabric connection {name} ({found['id']}), which signs in as the " + sp_text}
    if not allow_create:
        return {"mode": mode, "connectionId": "", "create": name,
                "text": f"Fabric connection {name} (doesn't exist yet), which will sign in as the " + sp_text}
    return {"mode": mode, "connectionId": "", "create": name,
            "text": f"Fabric connection {name}, created when you confirm the plan, which signs in as the " + sp_text}


SAME_TENANT_FIX = (
    "This migration keeps everything in one Entra tenant: the Azure DevOps organization must be connected to the "
    "same Entra tenant as Fabric. Either use a DevOps organization that is already in this tenant, or connect this "
    "one: Azure DevOps > Organization settings > Microsoft Entra > Connect directory (Project Collection "
    "Administrator; existing users must exist in the tenant or they lose access, so plan it with the DevOps admin). "
    "Then add the people who run the notebooks (Basic access, project Contributors), open a workspace > Source "
    "control once and sign in. Linking across tenants (ALLOW_CROSS_TENANT = True) is not part of this migration.")
FOLDER_FIRST = (
    "Most common cause: the Git folder doesn't exist in the branch (the connect API doesn't create it, unlike the "
    "workspace settings screen). V2_1 creates it before linking; if you link by hand or with another tool, create "
    "the folder first (a file inside it, for example README.md). ")
GIT_NOT_FOUND_FIX = (
    "Fabric couldn't see the organization, project, repository or branch with your own Azure DevOps sign-in. This "
    "notebook already found all of them with the PAT/service principal, so the names are right (those settings are "
    "only for the notebook's own DevOps calls). With your own sign-in Fabric only sees DevOps organizations connected "
    "to this Fabric tenant's Entra ID in which you are a member. Check, in this order: (1) you are a member of the "
    "organization with Basic access and in the project's Contributors; (2) the organization is connected to this "
    "Entra tenant (V2_0 row 'organization in the same Entra tenant as Fabric'). " + SAME_TENANT_FIX + " Optional, "
    "still within the same tenant: GIT_LINK_AUTH = \"service_principal\" links with the service principal "
    "instead of your own account.")
GIT_NOT_FOUND_WITH_CONNECTION_FIX = (
    "Fabric couldn't see the repository, branch or folder with the Git connection's identity. " + FOLDER_FIRST +
    "Otherwise: Fabric couldn't see the repository with the Git connection's identity. Give that identity (for example the "
    "service principal) access: Azure DevOps > Organization settings > Users > Add (Basic), and the project's "
    "Contributors group. Check that the connection URL points to this organization/project/repository, and that "
    "the connection's tenant ID is the tenant the DevOps organization is connected to.")


def git_connect_fix(exc):
    """How to fix a failed Git connect / initialize call."""
    text = f"{getattr(exc, 'code', '')} {exc}".lower()
    if "principaltypenotsupported" in text or "service principal" in text:
        return ("The notebook runs as a service principal / workspace identity: Fabric only accepts that with a "
                "stored connection: set GIT_LINK_AUTH = \"service_principal\", or run the notebook interactively as "
                "a user.")
    if "gitproviderresourcenotfound" in text:
        return FOLDER_FIRST + "Otherwise: " + GIT_NOT_FOUND_FIX if git_link_mode() == "my_account" else \
            GIT_NOT_FOUND_WITH_CONNECTION_FIX
    if "insufficientprivileges" in text or getattr(exc, "status", None) == 403:
        return ("You must be Admin of the workspace (Fabric administrator isn't enough). Set "
                "ADD_ME_AS_WORKSPACE_ADMIN = True, or ask a workspace admin to add you.")
    if "credential" in text or "gitprovider" in text or "tf400813" in text or "unauthorized" in text:
        return ("Fabric has no working Azure DevOps credentials for you. Either open any workspace > Source control "
                "once and sign in to Azure DevOps (same Entra tenant as the DevOps organization), or set "
                "GIT_LINK_AUTH = \"service_principal\" so Fabric signs in with the service principal instead.")
    if "crossgeo" in text or "cross geo" in text or "cross-geo" in text:
        return CROSS_GEO_FIX
    if "capacity" in text:
        return "Assign the workspace to a Fabric capacity first."
    if "alreadyconnected" in text:
        return "Already connected: rerun the notebook, it will check the existing link."
    if "directory" in text or "folder" in text or "branch" in text:
        return "Check the branch and folder: the folder must be empty or belong to this workspace."
    return "See the error; if it persists, link this workspace by hand with the values in the table."


def current_workspace_id():
    """Workspace this notebook runs in ('' if unknown)."""
    try:
        ctx = notebookutils.runtime.context
        return str(ctx.get("currentWorkspaceId") or "").lower()
    except Exception:
        return ""


RUN_FROM_ELSEWHERE = ("Run the migration notebooks from a separate workspace that isn't migrated (for example "
                      "'fabric-migration-control'): this workspace is in scope, so its notebooks, including the one "
                      "running, would be deleted with the other items.")


def my_git_credentials(ws_id):
    """Your own Git credentials for a linked workspace ({'source': 'Automatic' | 'ConfiguredConnection' | 'None',
    'connectionId'}). Per user and per workspace: the workspace screens (Source control, Git integration) use them."""
    try:
        return fabric("GET", f"/workspaces/{ws_id}/git/myGitCredentials")
    except HttpError as exc:
        return {"source": "?", "error": exc.short()}


def creds_need_fix(cred, connection_id):
    """True if your Git credentials for the workspace should be switched to the Fabric connection."""
    if git_link_mode() == "my_account":
        return False
    return cred.get("source") != "ConfiguredConnection" or bool(connection_id and cred.get("connectionId") != connection_id)


def set_my_git_credentials(ws_id, connection_id):
    fabric("PATCH", f"/workspaces/{ws_id}/git/myGitCredentials",
           {"source": "ConfiguredConnection", "connectionId": connection_id})


CREDS_WHY = ("your own Git sign-in for this workspace is '{src}', so the workspace screens (Source control, Git "
             "integration) try your personal Azure DevOps account and show 'Can't fetch branches' / 'connection "
             "removed'. The link itself is fine.")


def git_connection_label():
    """Name of the Fabric connection used to link (GIT_CONNECTION_NAME or the default name)."""
    if git_link_mode() == "connection_id":
        return f"with ID {globals().get('GIT_CONNECTION_ID')}"
    return (str(globals().get("GIT_CONNECTION_NAME") or "").strip() or f"fabric-migration-{globals().get('ADO_ORGANIZATION')}"
            f"-{globals().get('ADO_PROJECT')}-{globals().get('ADO_REPOSITORY')}")


def git_creds_hint(ws_id, gp=None):
    """Plain-language fix for a Git call (status, commit, update, disconnect) that fails because of YOUR OWN Git
    sign-in for the workspace. The notebooks don't change that sign-in by themselves: you pick it in the workspace."""
    cred = my_git_credentials(ws_id)
    gp = gp or {}
    org, project, repo = (gp.get("organizationName") or globals().get("ADO_ORGANIZATION", ""),
                          gp.get("projectName") or globals().get("ADO_PROJECT", "<project>"),
                          gp.get("repositoryName") or globals().get("ADO_REPOSITORY", "<repository>"))
    name = str(globals().get("GIT_CONNECTION_NAME") or "").strip() or f"fabric-migration-{org}-{project}-{repo}"
    src = cred.get("source")
    now = f"Your Git sign-in for this workspace is '{src}'" + (
        f" (connection {cred.get('connectionId')})" if cred.get("connectionId") else "") + "."
    return (f"{now} Git calls run with YOUR OWN Git sign-in for each workspace. Open the workspace > Source control "
            f"> account/credentials and choose the Fabric connection '{name}' (the one used to link it; if it isn't "
            "listed, ask its owner to share it with you: Manage connections and gateways > Manage users), then rerun. "
            "If the workspace was linked with your own account (GIT_LINK_AUTH = \"my_account\"), sign in to Azure "
            "DevOps there instead.")


def git_fail_text(exc, ws_id, gp=None):
    """Full error text for a failed Fabric Git call plus the fix: role (403) or your own Git sign-in (other errors)."""
    text = exc.short()
    if "/git/" not in str(getattr(exc, "url", "") or ""):
        return text
    if exc.status == 403:
        return (text + " | FIX: Git calls need Contributor (status, commit, update) or Admin (connect, disconnect) on "
                "the workspace for the account running the notebook.")
    try:
        return text + " | FIX: " + git_creds_hint(ws_id, gp)
    except Exception:          # the hint is best effort; never hide the original error
        return text


def git_connection(ws_id):
    return fabric("GET", f"/workspaces/{ws_id}/git/connection")


def git_status(ws_id):
    return fabric("GET", f"/workspaces/{ws_id}/git/status")


def resolve_target_workspace(old_id, name, override=None, recorded=None):
    """(workspace ID after the move, how it was found) or (None, why not). A home-region move keeps the workspace
    IDs, so the same ID is tried first; if it's gone, the workspace with the same name (names are unique in a
    tenant). override = WORKSPACE_MAP entry; recorded = the target V2_5 already used (restore_state.json)."""
    if override:
        return override, "WORKSPACE_MAP"
    if recorded:
        return recorded, "same as V2_5" + ("" if recorded == old_id else " (ID changed)")
    try:
        fabric("GET", f"/workspaces/{old_id}")
        return old_id, "same ID"
    except HttpError as exc:
        if exc.status not in (403, 404):
            raise
    try:
        found = [w for w in fabric_pages("/workspaces") if (w.get("displayName") or "") == name]
    except HttpError as exc:
        return None, f"workspace {old_id} not found and the workspace list can't be read ({exc.short()})"
    if len(found) == 1:
        return norm_id(found[0]["id"]), f"found by name; ID changed from {old_id}"
    if not found:
        return None, (f"workspace {old_id} ({name}) not found, and no workspace you can access has that name. "
                      "Recreate it or add yourself to it; if it was renamed, set WORKSPACE_MAP")
    return None, f"workspace {old_id} not found and {len(found)} workspaces are named {name}: set WORKSPACE_MAP"


def workspace_items(ws_id):
    return [{"id": i["id"], "type": i["type"], "displayName": i["displayName"], "workspaceId": ws_id}
            for i in fabric_pages(f"/workspaces/{ws_id}/items")]


# ---------------------------------------------------------------- migration records (ADLS)
def migration_prefix(snapshot_id):
    return f"{ADLS_ROOT.strip('/')}/_migration/{snapshot_id}"


def plan_hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def load_manifest(snapshot_id):
    """The manifest written by the snapshot notebook, checked against its stored hash."""
    if not snapshot_id or "<" in snapshot_id:
        raise ValueError("Set SNAPSHOT_ID to the snapshot ID printed by the snapshot notebook.")
    prefix = migration_prefix(snapshot_id)
    try:
        raw = adls_read(f"{prefix}/manifest.json")
        stored = adls_read(f"{prefix}/manifest.sha256").decode().strip()
    except HttpError as exc:
        raise RuntimeError(f"Can't read the snapshot manifest under {prefix}/ ({exc.short()}).") from None
    if hashlib.sha256(raw).hexdigest() != stored:
        raise RuntimeError("The manifest doesn't match its stored hash: it was changed after the snapshot. Stop and "
                           "investigate; take a new snapshot.")
    return json.loads(raw)


def save_log(prefix, name):
    df = pd.DataFrame(RESULTS, columns=["Workspace", "Item", "Type", "ItemId", "Step", "Status", "Detail"])
    adls_write(f"{prefix}/{name}_{NOW_ID}.csv", df.to_csv(index=False))
    return df
