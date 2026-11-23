missing = [n for n in ("SNAPSHOT_ID", "ADLS_ACCOUNT", "KEY_VAULT_URL") if "<" in str(globals()[n])] + ado_auth_missing()
if missing:
    raise ValueError("Fill in these settings first: " + ", ".join(missing))
MANIFEST = load_manifest(SNAPSHOT_ID)
PREFIX = migration_prefix(SNAPSHOT_ID)
try:
    STATE = json.loads(adls_read(f"{PREFIX}/restore_state.json"))
except HttpError as exc:
    if exc.status != 404:
        raise
    STATE = {"workspaces": {}}


def save_state():
    adls_write(f"{PREFIX}/restore_state.json", json.dumps(STATE, indent=2, sort_keys=True))


def branch_label(name, ws_id):
    return (re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:60] or "workspace") + "-" + ws_id[:8]


known = {w["id"] for w in MANIFEST["workspaces"]}
bad = [k for k in WORKSPACE_MAP if norm_id(k) not in known or not norm_id(WORKSPACE_MAP[k])]
if bad:
    raise ValueError(f"WORKSPACE_MAP entries not in the manifest or not GUIDs: {bad}")
ws_map = {norm_id(k): norm_id(v) for k, v in WORKSPACE_MAP.items()}

if RESTORE_SOURCE not in ("git", "adls"):
    raise ValueError('RESTORE_SOURCE must be "git" or "adls".')
RAW = MANIFEST.get("rawCopy") or {}
RAW_ROOT = f"{ADLS_ROOT.strip('/')}/{RAW.get('definitionsRunId', '')}"
CREATE_ORDER = ["Lakehouse", "Warehouse", "Eventhouse", "KQLDatabase", "Notebook", "Dataflow", "DataPipeline",
                "GraphModel", "Ontology", "DataAgent"]


def create_rank(t):
    return CREATE_ORDER.index(t) if t in CREATE_ORDER else 50


STAGING_ACTION = "not restored: Dataflow Gen2 staging, Fabric recreates it when a Dataflow Gen2 runs"


def route_of(mws):
    return "adls" if RESTORE_SOURCE == "adls" or mws.get("gitRoute") == "none" else "git"


def raw_folder(mws, item):
    return f"{RAW_ROOT}/{safe_name(mws['name'])}__{mws['id']}/{safe_name(item['displayName'])}.{item['type']}__{item['id']}"


def plan_adls(tgt, mws, items):
    """Workspace recreated from the raw definitions in ADLS (no Git)."""
    problems, st = tgt["problems"], tgt["state"]
    created = st.get("created", {})
    present = {key_of(i["type"], i["displayName"]): i for i in items}
    if not RAW.get("enabled") or not adls_exists(f"{RAW_ROOT}/_run_completed.json"):
        problems.append("the raw copy of the snapshot is missing in ADLS: nothing to recreate the items from")
    tgt["create"] = []
    for item in sorted(mws["items"], key=lambda x: (create_rank(x["type"]), x["displayName"].lower())):
        k = key_of(item["type"], item["displayName"])
        if is_dataflow_staging(item):
            action = STAGING_ACTION
        elif item["type"] in CHILD_TYPES:
            action = "recreated with its parent"
        elif item["id"] in created:
            action = "already restored"
            tgt["expected"].append(k)
        elif item.get("rawCopy") != "OK":
            problems.append(f"{item['displayName']} ({item['type']}) has no raw definition in ADLS")
            action = "not recoverable from ADLS"
        elif k in present and not (item["type"] == "KQLDatabase" and item.get("parentEventhouseItemId") in created):
            problems.append(f"{item['displayName']} ({item['type']}) already exists in the target workspace")
            action = "blocked: exists"
        else:
            action = "create from ADLS definition"
            tgt["expected"].append(k)
            tgt["create"].append(item)
        rows.append({"Workspace": mws["name"], "Target": tgt["id"], "Found by": tgt["how"], "Item": item["displayName"],
                     "Type": item["type"], "Action": action})
    for p in problems:
        log(mws["name"], {}, "Plan", "Blocked", p)


TARGETS, rows = [], []
for mws in MANIFEST["workspaces"]:
    route = route_of(mws) if mws["ready"] else None
    if not mws["ready"] or (route == "git" and not mws["git"].get("snapshotBranch")):
        print(f"{mws['name']}: skipped (not READY in the manifest)")
        continue
    old = mws["id"]
    git, st = mws["git"], STATE["workspaces"].setdefault(mws["id"], {})
    new, how = resolve_target_workspace(old, mws["name"], ws_map.get(old), st.get("target"))
    if not new:
        TARGETS.append({"old": old, "id": old, "name": mws["name"], "git": git, "state": st, "exclude": [],
                        "expected": [], "done": False, "problems": [how], "how": how})
        log(mws["name"], {}, "Plan", "Blocked", how)
        rows.append({"Workspace": mws["name"], "Target": "", "Found by": how, "Item": "", "Type": "",
                     "Action": "BLOCKED"})
        continue
    tgt = {"old": old, "id": new, "name": mws["name"], "git": git, "problems": [], "state": st, "exclude": [],
           "expected": [], "done": bool(st.get("updated")), "gitDone": bool(st.get("updated")), "how": how,
           "route": route, "create": []}
    TARGETS.append(tgt)
    problems = tgt["problems"]
    try:
        info = fabric("GET", f"/workspaces/{new}")
        if not info.get("capacityId"):
            problems.append("the workspace has no capacity: assign the new-region capacity first")
        items = workspace_items(new)
        conn = git_connection(new)
    except HttpError as exc:
        problems.append(f"can't read the target workspace {new} ({exc.short()})")
        continue
    tgt["capacityId"] = info.get("capacityId")
    if route == "adls":
        plan_adls(tgt, mws, items)
        continue
    try:
        ref = ado_ref(git, git["snapshotBranch"])
        if not ref or ref.get("objectId") != git["commit"]:
            problems.append(f"snapshot branch {git['snapshotBranch']} is missing or no longer at {git['commit']}")
        saved = git_items_at(git, git["commit"])
        tgt["tree"] = ado_tree(git, git["commit"])
    except HttpError as exc:
        problems.append(f"can't read the snapshot in Azure DevOps ({exc.short()})")
        continue
    present = {key_of(i["type"], i["displayName"]): i for i in items}
    for item in mws["items"]:
        k = key_of(item["type"], item["displayName"])
        if is_dataflow_staging(item):
            action = STAGING_ACTION
        elif item["type"] in CHILD_TYPES:
            action = "recreated with its parent"
        elif item.get("inGit") is True:
            if k not in saved:
                problems.append(f"{item['displayName']} ({item['type']}) is missing from the snapshot commit")
                action = "missing in Git"
            elif k in present and not tgt["gitDone"]:
                problems.append(f"{item['displayName']} ({item['type']}) already exists in the target workspace")
                action = "blocked: exists"
            else:
                action = "already restored" if tgt["gitDone"] else "restore from Git"
                tgt["expected"].append(k)
        elif adls_only(item):
            created = st.get("created", {})
            if item["id"] in created:
                action = "already restored"
            elif item.get("rawCopy") != "OK" and (item.get("secondary") or {}).get("Definition") not in ("OK", "Unchanged"):
                problems.append(f"{item['displayName']} ({item['type']}) isn't in Git and has no definition in ADLS")
                action = "not recoverable from ADLS"
            elif k in present:
                problems.append(f"{item['displayName']} ({item['type']}) already exists in the target workspace")
                action = "blocked: exists"
            else:
                action = "create from ADLS definition (not in Git), after the Git update"
                tgt["create"].append(item)
            tgt["expected"].append(k)
        else:
            action = "not recoverable (acknowledged)"
        rows.append({"Workspace": mws["name"], "Target": new, "Found by": how, "Item": item["displayName"],
                     "Type": item["type"], "Action": action})
    for k, g in saved.items():
        if k in present:
            tgt["exclude"].append(g["folder"])
            if not tgt["gitDone"]:
                rows.append({"Workspace": mws["name"], "Target": new, "Found by": how, "Item": g["displayName"], "Type": g["type"],
                             "Action": "left out of the restore branch: already in the workspace"})
        elif k not in tgt["expected"] and not any(key_of(i["type"], i["displayName"]) == k for i in mws["items"]):
            tgt["expected"].append(k)
            pbi = any(key_of(i["type"], i["displayName"]) == k for i in mws.get("powerBiItems", []))
            rows.append({"Workspace": mws["name"], "Target": new, "Found by": how, "Item": g["displayName"], "Type": g["type"],
                         "Action": "already restored" if tgt["gitDone"] else
                         ("restore from Git (Power BI item missing from the workspace)" if pbi else
                          "restore from Git (in the commit, not in the manifest's item list)")})
    tgt["create"].sort(key=lambda x: (create_rank(x["type"]), x["displayName"].lower()))
    tgt["done"] = tgt["gitDone"] and not tgt["create"]
    state = conn.get("gitConnectionState")
    cur_branch = (conn.get("gitProviderDetails") or {}).get("branchName")
    if state not in (None, "NotConnected") and not (st.get("branch") and cur_branch == st["branch"]):
        problems.append(f"the workspace is connected to Git (branch {cur_branch}); disconnect it first")
    tgt["connState"] = state
    for p in problems:
        log(mws["name"], {}, "Plan", "Blocked", p)

WHO = me()
_repos = sorted({(t["git"].get("organizationName"), t["git"].get("projectName"), t["git"].get("repositoryName"))
                 for t in TARGETS if t.get("route", "git") == "git"})
if git_link_mode() == "service_principal" and not str(GIT_CONNECTION_ID or "").strip() and len(_repos) > 1:
    raise ValueError("The workspaces use more than one repository and a Fabric connection covers one repository: "
                     "restore one repository at a time with GIT_LINK_AUTH = \"connection_id\" and its GIT_CONNECTION_ID.")
if _repos:
    LINK = plan_git_link(WHO, *_repos[0])
    print(f"Git link method (GIT_LINK_AUTH = \"{LINK['mode']}\"): Fabric will sign in to Azure DevOps with {LINK['text']}")
else:
    LINK = {"mode": "none", "connectionId": "", "create": None, "text": ""}
GIT_CONNECTION_ID, CREATE_CONNECTION = LINK["connectionId"], LINK["create"]
_adls = [t["name"] for t in TARGETS if t.get("route") == "adls"]
if _adls:
    print(f"Recreated from the raw definitions in ADLS (no Git): {', '.join(_adls)}"
          + (" (RESTORE_SOURCE = \"adls\")" if RESTORE_SOURCE == "adls" else " (kept without Git in V2_3)"))
if LINK["mode"] == "service_principal" and _repos:
    sp_ok, sp_err = sp_repo_check(*_repos[0])
    if not sp_ok:
        raise RuntimeError(f"Stopped: service principal {str(ADO_SP_CLIENT_ID).strip()} can't read repository "
                           f"{_repos[0][2]} ({sp_err}). " + SP_NOT_IN_ORG)

PLAN_DF = pd.DataFrame(rows, columns=["Workspace", "Target", "Found by", "Item", "Type", "Action"])
show(PLAN_DF, rows=1000)
blocked = [t["name"] for t in TARGETS if t["problems"]]
if blocked:
    print("BLOCKED, nothing will be restored in: " + ", ".join(blocked))
TODO = [t for t in TARGETS if not t["problems"] and not t["done"]]
PLAN_ID = plan_hash({"snapshot": SNAPSHOT_ID, "map": ws_map, "rows": PLAN_DF.values.tolist(),
                     "todo": [t["id"] for t in TODO], "source": RESTORE_SOURCE, "linkMode": LINK["mode"], "connection": GIT_CONNECTION_ID,
                     "createConnection": CREATE_CONNECTION})
print(f"\nPLAN ID: {PLAN_ID}" + ("" if TODO else "   (nothing left to restore)"))
