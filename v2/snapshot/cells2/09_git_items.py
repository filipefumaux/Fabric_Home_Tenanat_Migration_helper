for ws in WS.values():
    if not ws.get("head"):
        continue
    try:
        saved = git_items_at(ws["git"], ws["head"])
    except HttpError as exc:
        block(ws, "Git content", f"can't read the repository at {ws['head']} ({exc.short()}). Check the Azure DevOps "
                                 "token in Key Vault and its Code (Read) scope.")
        continue
    ws["gitItems"] = saved
    for item in ws["items"] + ws["pbi"]:
        g = saved.get(key_of(item["type"], item["displayName"]))
        item["inGit"] = bool(g)
        item.update(logicalId=(g or {}).get("logicalId", ""), gitFolder=(g or {}).get("folder", ""),
                    gitFiles=(g or {}).get("files", 0))
        if not g and item["id"] in ws.get("pendingAdded", ()):
            item["inGit"] = "after commit"
    # Schema-enabled Lakehouse: Fabric recreates it from Git with schemas only if its definition says so
    # (lakehouse.metadata.json with defaultSchema). Without it the restored Lakehouse can't take the tables back.
    for item in ws["items"]:
        if item["type"] != "Lakehouse" or not item.get("defaultSchema") or item.get("inGit") is not True:
            continue
        try:
            meta = json.loads(ado_file(ws["git"], ws["head"], item["gitFolder"] + "/lakehouse.metadata.json")
                              .decode("utf-8-sig") or "{}")
        except (HttpError, ValueError):
            meta = {}
        if meta.get("defaultSchema"):
            log(ws["name"], item, "Lakehouse schemas", "OK", f"schema-enabled (default {meta['defaultSchema']}), "
                "kept in its Git definition")
        else:
            item["schemaProblem"] = ("schema-enabled Lakehouse, but its Git definition has no lakehouse.metadata.json "
                                     "with defaultSchema: restored from Git it would have no schemas and its tables "
                                     "couldn't be loaded back. Commit the Lakehouse again from Source control, then "
                                     "run again")
            log(ws["name"], item, "Lakehouse schemas", "Blocked", item["schemaProblem"])
    in_git = sum(1 for i in ws["items"] if i.get("inGit"))
    print(f"{ws['name']}: {len(saved)} item folders in Git; {in_git} of {len(ws['items'])} Fabric items found")
