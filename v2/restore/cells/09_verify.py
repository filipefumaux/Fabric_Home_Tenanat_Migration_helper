ID_MAP, RESTORED = {}, []
for t in TARGETS:
    if not t["done"] or t["problems"]:
        continue
    RESTORED.append(t)
    mws = next(w for w in MANIFEST["workspaces"] if w["id"] == t["old"])
    items = workspace_items(t["id"])
    now = {key_of(i["type"], i["displayName"]): i for i in items}
    t["items"] = items
    missing = [k for k in t["expected"] if k not in now]
    for k in t["expected"]:
        typ, nm = k.split("|", 1)
        log(t["name"], {"type": typ, "displayName": now.get(k, {}).get("displayName", nm), "id": now.get(k, {}).get("id", "")},
            "Verify", "OK" if k in now else "Failed", "" if k in now else "not in the workspace after the update")
    if t.get("route") != "adls":   # recreated from ADLS: no Git link to check
        try:
            conn = git_connection(t["id"])
            head = (conn.get("gitSyncDetails") or {}).get("head")
            stt = git_status(t["id"])
            pending = [c for c in stt.get("changes") or []
                       if c.get("remoteChange") or c.get("conflictType") not in (None, "None")]
            ok = head == t["state"].get("branchHead") and not pending
            log(t["name"], {}, "Verify Git sync", "OK" if ok else "Failed",
                f"workspace at {head}, restore branch at {t['state'].get('branchHead')}, {len(pending)} change(s) "
                "not applied")
            # "Modified" only: items kept in the workspace during the migration (left out of the restore branch) show as
            # "Added" and are expected.
            rewritten = [c for c in stt.get("changes") or [] if c.get("workspaceChange") == "Modified" and c not in pending]
            for c in rewritten:
                md = c.get("itemMetadata") or {}
                log(t["name"], {"type": md.get("itemType", ""), "displayName": md.get("displayName", ""), "id": ""},
                    "Verify Git sync", "Info",
                    "shows as uncommitted: Fabric rewrote its definition after the import (new IDs). Expected; commit "
                    "it to the restore branch after the reference fix and V2_6, not before")
        except HttpError as exc:
            log(t["name"], {}, "Verify Git sync", "Failed", git_fail_text(exc, t["id"]))
    if t["old"] != t["id"]:
        ID_MAP[t["old"]] = t["id"]
    for i in mws["items"]:
        n = now.get(key_of(i["type"], i["displayName"]))
        if n and n["id"] != i["id"]:
            ID_MAP[i["id"].lower()] = n["id"]
            if i["type"] == "Lakehouse" and i.get("sqlEndpointId"):
                try:
                    props = fabric("GET", f"/workspaces/{t['id']}/lakehouses/{n['id']}").get("properties") or {}
                    new_ep = (props.get("sqlEndpointProperties") or {}).get("id")
                    if new_ep:
                        ID_MAP[i["sqlEndpointId"].lower()] = new_ep
                except HttpError as exc:
                    log(t["name"], n, "SQL endpoint", "Warning", exc.short())
    print(f"{t['name']}: {len(t['expected']) - len(missing)} of {len(t['expected'])} items back"
          + (f"; MISSING: {', '.join(missing)}" if missing else ""))
if not RESTORED:
    print("No restored workspace to verify yet.")
