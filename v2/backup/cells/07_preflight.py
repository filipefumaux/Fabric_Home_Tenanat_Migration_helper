RUN_PREFIX = f"{ADLS_ROOT.strip('/')}/{RUN_ID}"
placeholders = [n for n in ("ADLS_ACCOUNT", "ADLS_CONTAINER", "ADO_ORGANIZATION", "ADO_PROJECT", "ADO_REPOSITORY",
                            "KEY_VAULT_URL", "ADO_PAT_SECRET_NAME")
                if "<" in str(globals()[n]) and (n.startswith("ADLS") or PUSH_DEFINITIONS_TO_DEVOPS)]
KC_NEEDED = COPY_KQL_TO_ADX and any(i["type"] == "KQLDatabase" for i in ITEMS)
if KC_NEEDED:
    placeholders += [n for n in ("ADX_CLUSTER_URI", "KC_TENANT_ID", "KC_CLIENT_ID", "KC_CLIENT_SECRET_NAME",
                                 "KEY_VAULT_URL") if "<" in str(globals()[n]) and n not in placeholders]
if placeholders:
    raise ValueError("Fill in these settings first: " + ", ".join(placeholders) + (" (Kusto Copy: or ADO_SP_TENANT_ID / ADO_SP_CLIENT_ID / ADO_SP_SECRET_NAME)" if any(p.startswith("KC_") for p in placeholders) else ""))

STARTED_UTC = dt.datetime.now(dt.timezone.utc)
if FREEZE_STARTED_UTC:
    try:
        _freeze = dt.datetime.fromisoformat(FREEZE_STARTED_UTC.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f'FREEZE_STARTED_UTC "{FREEZE_STARTED_UTC}" isn\'t a date and time like '
                         '"2026-11-16T10:00:00Z"') from None
    if _freeze.tzinfo is None:
        raise ValueError("FREEZE_STARTED_UTC needs the time zone: end it with Z (UTC)")
    if _freeze > STARTED_UTC:
        raise RuntimeError(f"FREEZE_STARTED_UTC ({FREEZE_STARTED_UTC}) is in the future: V2_3 needs a backup that "
                           "starts after the freeze. Run this after the freeze has started.")
    print(f"Change freeze started {FREEZE_STARTED_UTC}; this run starts {STARTED_UTC.isoformat()}")
else:
    print("WARNING: FREEZE_STARTED_UTC is empty. V2_3 will block this run until you set the freeze time there; "
          "fine for a test, not for the migration.")
print(f"Eventhouse data: KQL_BACKUP_MODE = \"{KQL_BACKUP_MODE}\"" + (
    "" if not COPY_KQL_TO_ADX else f" (ADX {ADX_CLUSTER_URI}, Kusto Copy as {KC_CLIENT_ID})"))
if DRY_RUN:
    print("DRY_RUN = True: nothing will be written to ADLS.")
else:
    try:
        adls_write(f"{RUN_PREFIX}/_run_started.json", json.dumps({
            "runId": RUN_ID, "startedUtc": STARTED_UTC.isoformat(), "freezeStartedUtc": FREEZE_STARTED_UTC,
            "kqlBackupMode": KQL_BACKUP_MODE, "adxCluster": ADX_CLUSTER_URI if COPY_KQL_TO_ADX else "",
            "workspaces": list(WORKSPACES.values()), "itemTypes": sorted(selected_types),
            "items": len(ITEMS)}, indent=2))
        print(f"ADLS OK: writing to abfss://{ADLS_CONTAINER}@{ADLS_ACCOUNT}.dfs.core.windows.net/{RUN_PREFIX}/")
    except HttpError as exc:
        raise RuntimeError(f"Can't write to ADLS ({exc.short()}). The person running the notebook needs "
                           "'Storage Blob Data Contributor' on the container, and the storage firewall must "
                           "allow this Fabric notebook.") from None

ADO_STATE = {}
if PUSH_DEFINITIONS_TO_DEVOPS and not DRY_RUN:
    try:
        repo = ado("GET", "")
        refs = ado("GET", "/refs", params={"filter": f"heads/{ADO_BRANCH}"}).get("value", [])
        ref = next((r for r in refs if r.get("name") == f"refs/heads/{ADO_BRANCH}"), None)
    except HttpError as exc:
        raise RuntimeError(f"Can't reach the Azure DevOps repository ({exc.short()}). Check the organization, "
                           "project, repository, Key Vault secret and the token's 'Code (Read & write)' scope."
                           ) from None
    if ref:
        ADO_STATE = {"head": ref["objectId"], "exists": True}
    else:
        default = repo.get("defaultBranch")
        base = None
        if default:
            refs = ado("GET", "/refs", params={"filter": default.replace("refs/", "", 1)}).get("value", [])
            base = next((r["objectId"] for r in refs if r.get("name") == default), None)
        ADO_STATE = {"head": base or "0" * 40, "exists": False}
    print(f"Azure DevOps OK: {ADO_REPOSITORY}, branch {ADO_BRANCH} "
          f"({'exists' if ADO_STATE['exists'] else 'will be created'})")

if KC_NEEDED:
    try:
        kc_auth(ADX_CLUSTER_URI)
    except Exception as exc:
        raise RuntimeError(f"The Kusto Copy service principal can't sign in ({exc}). Check KC_TENANT_ID, "
                           "KC_CLIENT_ID (empty = ADO_SP_TENANT_ID / ADO_SP_CLIENT_ID), the Key Vault secret, and "
                           "that you can read secrets in that vault.") from None
    print(f"Kusto Copy service principal OK: signed in for {ADX_CLUSTER_URI}")
    if ADX_CREATE_DATABASES:
        try:
            _c = adx_cluster_arm()
            _can = adx_can_create()
        except (HttpError, RuntimeError) as exc:
            raise RuntimeError(f"ADX_CREATE_DATABASES = True, but: {exc}") from None
        if not _can:
            raise RuntimeError(
                f"ADX_CREATE_DATABASES = True, but the Kusto Copy service principal ({KC_CLIENT_ID}) can't create "
                f"databases on {_c['name']}. Give it Contributor on the cluster (Azure portal > the ADX cluster > "
                "Access control (IAM) > Add role assignment), or create the databases yourself and set "
                "ADX_CREATE_DATABASES = False.")
        print(f"ADX cluster OK: {_c['name']} ({_c['location']}); the service principal can create missing databases "
              f"(names: {ADX_DATABASE_PATTERN})")
