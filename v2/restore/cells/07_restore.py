if DRY_RUN:
    print(f"DRY_RUN = True: nothing changed. To restore, set DRY_RUN = False and CONFIRM_PLAN_ID = \"{PLAN_ID}\".")
elif TODO and CONFIRM_PLAN_ID != PLAN_ID:
    raise RuntimeError(f"CONFIRM_PLAN_ID doesn't match this plan's PLAN ID ({PLAN_ID}). Something changed since the "
                       "dry run, or the ID wasn't pasted. Review the plan above and confirm again.")

if any(t.get("route") == "git" for t in TODO) and not DRY_RUN and CREATE_CONNECTION:
    try:
        GIT_CONNECTION_ID = create_git_connection(CREATE_CONNECTION, *_repos[0])["id"]
        log("(all)", {}, "Git connection", "OK", f"created {CREATE_CONNECTION} ({GIT_CONNECTION_ID})")
    except HttpError as exc:
        raise RuntimeError(f"Couldn't create the Fabric connection {CREATE_CONNECTION}: {exc.short()}. Check the "
                           "service principal settings and its client secret. Nothing was restored.") from None
    CREATE_CONNECTION = None

def remap_ids(text, id_map):
    return GUID_ANY_RE.sub(lambda m: (id_map[m.group(0).lower()].upper() if m.group(0).isupper() else
                                      id_map[m.group(0).lower()]) if m.group(0).lower() in id_map else m.group(0), text)


def adls_definition(mws, item, id_map):
    """Definition parts of an item from the raw copy (fallback: the V2_2 backup), with old IDs replaced."""
    folder = raw_folder(mws, item)
    files = [n for n, _, d in adls_list(f"{folder}/definition") if not d] if adls_exists(f"{folder}/item.json") else []
    if not files and mws.get("secondaryFolder"):
        folder = f"{mws['secondaryFolder']}/{safe_name(item['displayName'])}.{item['type']}__{item['id']}"
        files = [n for n, _, d in adls_list(f"{folder}/definition") if not d]
    if not files:
        raise HttpError(None, "NoDefinition", "no definition files in ADLS (raw copy or V2_2 backup)", folder)
    try:
        fmt = json.loads(adls_read(f"{folder}/item.json")).get("definitionFormat")
    except (HttpError, ValueError):
        fmt = None
    parts = []
    for rel in sorted(files):
        rel = rel.split("/definition/", 1)[-1] if "/definition/" in rel else rel
        if rel == ".platform":
            continue
        data = adls_read(f"{folder}/definition/{rel}")
        try:
            data = remap_ids(data.decode("utf-8"), id_map).encode("utf-8")
        except UnicodeDecodeError:
            pass
        parts.append({"path": rel, "payload": base64.b64encode(data).decode(), "payloadType": "InlineBase64"})
    return {"parts": parts, **({"format": fmt} if fmt else {})}


def find_item(ws_id, typ, name):
    return next((i for i in workspace_items(ws_id) if i["type"] == typ and i["displayName"] == name), None)


def create_items(t, mws, id_map):
    """Create t["create"] from their ADLS definitions, storage items first; old IDs inside definitions are replaced
    with id_map (new IDs of the workspace and of items already restored)."""
    st = t["state"]
    created = st.setdefault("created", {})
    id_map.update({k.lower(): v for k, v in created.items()})
    for item in t["create"]:
        if item["id"] in created:
            continue
        definition = adls_definition(mws, item, id_map)
        existing = find_item(t["id"], item["type"], item["displayName"])
        if existing and item["type"] == "KQLDatabase":
            # Creating an Eventhouse also creates its first KQL database (same name): give it the saved definition.
            fabric("POST", f"/workspaces/{t['id']}/items/{existing['id']}/updateDefinition", {"definition": definition})
            new_id, how = existing["id"], "definition applied to the database created with its Eventhouse"
        elif existing:
            raise HttpError(None, "ItemExists", f"{item['displayName']} ({item['type']}) already exists", t["id"])
        else:
            body = {"displayName": item["displayName"], "type": item["type"], "definition": definition}
            how = "created from its ADLS definition"
            try:
                res = fabric("POST", f"/workspaces/{t['id']}/items", body)
            except HttpError as exc:
                if item["type"] not in ("Lakehouse", "Warehouse") or exc.status not in (400, 409):
                    raise
                # Some storage items can't be created with a definition: create them empty (schemas kept).
                body.pop("definition")
                if item["type"] == "Lakehouse" and item.get("defaultSchema"):
                    body["creationPayload"] = {"enableSchemas": True}
                res = fabric("POST", f"/workspaces/{t['id']}/items", body)
                how = f"created WITHOUT its definition ({exc.short()})" + (
                    "; Warehouse tables must exist before V2_6 loads them" if item["type"] == "Warehouse" else "")
            new_id = (res or {}).get("id") or (find_item(t["id"], item["type"], item["displayName"]) or {}).get("id")
            if not new_id:
                raise HttpError(None, "NotFound", f"{item['displayName']} ({item['type']}) not found after creating it",
                                t["id"])
        created[item["id"]] = new_id
        id_map[item["id"].lower()] = new_id
        if item["type"] == "Lakehouse" and item.get("sqlEndpointId"):
            try:
                props = fabric("GET", f"/workspaces/{t['id']}/lakehouses/{new_id}").get("properties") or {}
                ep = (props.get("sqlEndpointProperties") or {}).get("id")
                if ep:
                    id_map[item["sqlEndpointId"].lower()] = ep
            except HttpError as exc:
                log(t["name"], item, "SQL endpoint", "Warning", exc.short())
        save_state()
        log(t["name"], {**item, "id": new_id}, "Create (ADLS)", "Warning" if "WITHOUT" in how else "OK", how)


def restore_adls(t):
    """Recreate a whole workspace from ADLS (no Git)."""
    st, mws = t["state"], next(w for w in MANIFEST["workspaces"] if w["id"] == t["old"])
    st.setdefault("created", {})
    st.update(target=t["id"], targetFoundBy=t.get("how", ""), route="adls")
    save_state()
    create_items(t, mws, {t["old"].lower(): t["id"]} if t["old"] != t["id"] else {})
    st["updated"] = True
    save_state()
    t["done"] = True


def restore_outside_git(t):
    """Git route: after the Git update, create the items Git doesn't hold from ADLS. Their definitions may point to
    items that Git just recreated: map those by type and name."""
    if not t["create"]:
        return
    mws = next(w for w in MANIFEST["workspaces"] if w["id"] == t["old"])
    now = {key_of(i["type"], i["displayName"]): i["id"] for i in workspace_items(t["id"])}
    id_map = {t["old"].lower(): t["id"]} if t["old"] != t["id"] else {}
    for i in mws["items"]:
        n = now.get(key_of(i["type"], i["displayName"]))
        if n and n != i["id"]:
            id_map[i["id"].lower()] = n
    create_items(t, mws, id_map)


for t in ([] if DRY_RUN else TODO):
    if t.get("route") == "adls":
        try:
            restore_adls(t)
        except HttpError as exc:
            log(t["name"], {}, "Restore", "Failed", exc.short())
            print(f"{t['name']}: stopped ({exc.short()}). Fix it and run again; items already created are skipped.")
        continue
    name, git, st = t["name"], t["git"], t["state"]
    try:
        if st.get("updated"):
            restore_outside_git(t)
            t["done"] = True
            continue
        # 1. Restore branch from the snapshot commit, minus the items already in the workspace.
        branch = st.get("branch") or f"{RESTORE_BRANCH_PREFIX}/{SNAPSHOT_ID}/{branch_label(name, t['old'])}"
        ref = ado_ref(git, branch)
        if not ref:
            ado_create_branch(git, branch, git["commit"])
            head = git["commit"]
        elif ref["objectId"] in (git["commit"], st.get("branchHead")):
            head = ref["objectId"]
        else:
            raise HttpError(None, "BranchMoved", f"{branch} exists at {ref['objectId']}, not at the snapshot", branch)
        st.update(branch=branch, branchHead=head, target=t["id"], targetFoundBy=t.get("how", ""))
        save_state()
        if t["exclude"] and head == git["commit"]:
            paths = [p for p in t["tree"] if any(p.startswith(f + "/") for f in t["exclude"])]
            push = ado(git, "POST", "/pushes", body={
                "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": head}],
                "commits": [{"comment": f"Restore {SNAPSHOT_ID}: leave out {len(t['exclude'])} item(s) that already "
                                        "exist in the workspace",
                             "changes": [{"changeType": "delete", "item": {"path": p}} for p in paths]}]})
            head = push["commits"][0]["commitId"]
            st["branchHead"] = head
            save_state()
            log(name, {}, "Restore branch", "OK", f"left out {len(t['exclude'])} existing item(s): {head}")
        if LOCK_RESTORE_BRANCHES:
            ado_lock(git, branch)
        log(name, {}, "Restore branch", "OK", f"{branch} at {head}")

        # 2. Connect the workspace to the restore branch, same folder as before.
        conn_state = t["connState"]
        if conn_state in (None, "NotConnected"):
            body = {"gitProviderDetails": {"gitProviderType": "AzureDevOps", "organizationName": git["organizationName"],
                                           "projectName": git["projectName"], "repositoryName": git["repositoryName"],
                                           "branchName": branch, "directoryName": git["directoryName"]}}
            if GIT_CONNECTION_ID:
                body["myGitCredentials"] = {"source": "ConfiguredConnection", "connectionId": GIT_CONNECTION_ID}
            fabric("POST", f"/workspaces/{t['id']}/git/connect", body)
            st["connected"] = True
            save_state()
            conn_state = "Connected"
        if conn_state != "ConnectedAndInitialized":
            init = fabric("POST", f"/workspaces/{t['id']}/git/initializeConnection",
                          {"initializationStrategy": "PreferRemote"})
            required, remote, ws_head = init.get("requiredAction"), init.get("remoteCommitHash"), init.get("workspaceHead")
        else:   # rerun after an interruption: already connected and initialized on the restore branch
            s = git_status(t["id"])
            remote, ws_head = s.get("remoteCommitHash"), s.get("workspaceHead")
            required = "UpdateFromGit" if any(c.get("remoteChange") for c in s.get("changes") or []) else "None"
        log(name, {}, "Git connect", "OK", f"required action: {required}")

        # 3. Update from Git: only adds items. No override consent and no conflict policy, so Fabric refuses to
        #    start if anything in the workspace would be overwritten.
        if required == "UpdateFromGit":
            fabric("POST", f"/workspaces/{t['id']}/git/updateFromGit",
                   {"remoteCommitHash": remote, "workspaceHead": ws_head, "options": {"allowOverrideItems": False}},
                   want_result=False)
            log(name, {}, "Update from Git", "OK", f"workspace updated to {remote}")
        elif required == "CommitToGit":
            raise HttpError(None, "UnexpectedAction", "Fabric wants to commit the workspace to Git instead of "
                            "updating it. Stopped; nothing was committed.", t["id"])
        st["updated"] = True
        save_state()
        restore_outside_git(t)
        t["done"] = True
    except HttpError as exc:
        why = git_fail_text(exc, t["id"], git)
        log(name, {}, "Restore", "Failed", why)
        print(f"{name}: stopped ({why}). Fix it and run again; finished steps are skipped.")
