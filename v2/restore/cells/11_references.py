ref_rows, DEFS = [], {}
for t in RESTORED:
    restored_keys = set(t["expected"])
    for i in t["items"]:
        k = key_of(i["type"], i["displayName"])
        fixable = k in restored_keys and i["type"] not in POWER_BI_TYPES
        if not fixable and i["type"] not in SCAN_POWER_BI_TYPES:
            continue
        try:
            d = fabric("POST", f"/workspaces/{t['id']}/items/{i['id']}/getDefinition").get("definition") or {}
        except HttpError as exc:
            log(t["name"], i, "Reference scan", "Warning", f"can't read the definition: {exc.short()}")
            continue
        DEFS[i["id"]] = (t, i, d)
        for part in d.get("parts", []):
            try:
                text = base64.b64decode(part.get("payload") or "").decode("utf-8")
            except (UnicodeDecodeError, ValueError):
                continue
            found = Counter(g.lower() for g in GUID_ANY_RE.findall(text) if g.lower() in ID_MAP)
            for old_id, n in found.items():
                ref_rows.append({"Workspace": t["name"], "Item": i["displayName"], "Type": i["type"],
                                 "ItemId": i["id"], "Part": part["path"], "Old ID": old_id, "New ID": ID_MAP[old_id],
                                 "Count": n, "Fix": "automatic (with confirmation)" if fixable else "manual"})
REF_DF = pd.DataFrame(ref_rows, columns=["Workspace", "Item", "Type", "ItemId", "Part", "Old ID", "New ID", "Count", "Fix"])
FIX_ID = plan_hash(REF_DF.values.tolist())
if REF_DF.empty:
    print("No IDs of deleted items found in the restored definitions." if RESTORED else "")
else:
    show(REF_DF, rows=1000)
    print(f"\nFIX ID: {FIX_ID}")
    for r in REF_DF[REF_DF.Fix == "manual"].drop_duplicates("ItemId").itertuples():
        log(r.Workspace, {"type": r.Type, "displayName": r.Item, "id": r.ItemId}, "References", "Manual step",
            "points to a deleted item; rebind it (for a Direct Lake model: its new SQL endpoint)")

if FIX_STALE_REFERENCES and not REF_DF.empty and not DRY_RUN:
    if CONFIRM_FIX_ID != FIX_ID:
        raise RuntimeError(f"CONFIRM_FIX_ID doesn't match FIX ID {FIX_ID}. Review the list above and confirm again.")
    for item_id in REF_DF[REF_DF.Fix != "manual"].ItemId.unique():
        t, i, d = DEFS[item_id]
        parts = []
        for part in d.get("parts", []):
            if part["path"] == ".platform":
                continue
            data = base64.b64decode(part.get("payload") or "")
            try:
                text = data.decode("utf-8")
                data = GUID_ANY_RE.sub(lambda m: (ID_MAP[m.group(0).lower()].upper() if m.group(0).isupper() else
                                                  ID_MAP[m.group(0).lower()]) if m.group(0).lower() in ID_MAP
                                       else m.group(0), text).encode("utf-8")
            except UnicodeDecodeError:
                pass
            parts.append({"path": part["path"], "payload": base64.b64encode(data).decode(), "payloadType": "InlineBase64"})
        body = {"definition": {"parts": parts, **({"format": d["format"]} if d.get("format") else {})}}
        try:
            fabric("POST", f"/workspaces/{t['id']}/items/{item_id}/updateDefinition", body)
            log(t["name"], i, "Fix references", "OK", f"{int(REF_DF[REF_DF.ItemId == item_id].Count.sum())} ID(s) replaced")
        except HttpError as exc:
            log(t["name"], i, "Fix references", "Failed", exc.short())
