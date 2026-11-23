rows = []
for ws in WS.values():
    for item in ws["items"]:
        t, reasons, recovery = item["type"], [], ""
        in_git = item.get("inGit")
        sec = item.get("secondary", {})
        def_ok = sec.get("Definition") in GOOD
        if item.get("staging"):
            recovery = "Dataflow Gen2 staging: Fabric recreates it; not saved or restored (V2_4 deletes it)"
        elif t in ITEM_TYPES:
            if t in CHILD_TYPES:
                recovery = "recreated with its Lakehouse/Warehouse; SQL objects from scripts"
            elif in_git:
                recovery = "Git" + (" (after the commit)" if in_git == "after commit" else "")
            elif ws.get("noGit"):
                recovery = "ADLS definition (workspace without Git)"
                if not def_ok:
                    reasons.append("no OK definition in the V2_2 backup")
            elif ALLOW_SECONDARY_ONLY_ITEMS and def_ok:
                recovery = "ADLS definition (V2_5, not in Git)"
            else:
                reasons.append("not saved in Git")
            reasons += [f"secondary {s}: {v or 'missing'}" for s, v in sec.items() if v not in GOOD]
            if item.get("schemaProblem"):
                reasons.append(item["schemaProblem"])
            if t in DATA_TYPES and sec.get(KQL_ADX_STEP) in GOOD:
                recovery += "; data from ADX (Kusto Copy)"
            elif t in DATA_TYPES and sec.get(DATA_TYPES[t]) in GOOD:
                recovery += "; data from ADLS"
        elif t in ACKNOWLEDGED_ITEM_TYPES:
            recovery = "Git (definition only, no data backup)" if in_git else "acknowledged: NOTHING saved"
        else:
            reasons.append(f"type {t} isn't in ITEM_TYPES or ACKNOWLEDGED_ITEM_TYPES. It will be deleted; "
                           + ("Git has its definition, but no data is backed up" if in_git else "nothing is saved"))
        item.update(recovery=recovery, reasons=reasons, status="READY" if not reasons else "BLOCKED")
        for r in reasons:
            log(ws["name"], item, "Gate", "Blocked", r)
        rows.append({"Workspace": ws["name"], "Item": item["displayName"], "Type": t, "Status": item["status"],
                     "Recovery": recovery, "In Git": in_git, "Logical ID": item.get("logicalId", ""),
                     **{f"Secondary {s}": v for s, v in sec.items()}, "Why blocked": "; ".join(reasons)})
    for item in ws["pbi"]:
        rows.append({"Workspace": ws["name"], "Item": item["displayName"], "Type": item["type"], "Status": "LEFT ALONE",
                     "Recovery": "Power BI item: moves with the workspace", "In Git": item.get("inGit")})
    ws["ready"] = not ws["blockers"] and all(i["status"] == "READY" for i in ws["items"])
    ws["status"] = ("READY after commit" if ws["ready"] and ws.get("pendingCommit") else
                    "READY" if ws["ready"] else "BLOCKED")

GATE_DF = pd.DataFrame(rows)
WS_DF = pd.DataFrame([{"Workspace": w["name"], "Workspace ID": w["id"], "Status": w["status"],
                       "Fabric items": len(w["items"]), "Blocked items": sum(i["status"] != "READY" for i in w["items"]),
                       "Repository": w["git"].get("repositoryName"), "Branch": w["git"].get("branchName"),
                       "Commit": w.get("head"), "Workspace blockers": " | ".join(w["blockers"])} for w in WS.values()])
show(WS_DF)
show(GATE_DF.sort_values(["Workspace", "Status", "Type"]), rows=1000)
