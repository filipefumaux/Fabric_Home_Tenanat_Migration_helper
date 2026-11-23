def verify(it):
    st = item_state(it)
    if it["type"] == "Lakehouse":
        now = dict(onelake_list(it["ws"], it["target"], "Files") + onelake_list(it["ws"], it["target"], "Tables"))
        want = [(r, s) for r, s in it["data"] if table_root(r) not in it["skipTables"] and r not in it["conflicts"]]
        bad = [r for r, s in want if now.get(r) != s]
        left = len(it["skipTables"]) + len(it["conflicts"])
        return ("OK" if not bad and not left else "Failed" if bad else "Partial",
                f"{len(want) - len(bad)}/{len(want)} backed-up files present with the same size"
                + (f"; missing or different: {bad[0]}" if bad else "")
                + (f"; {left} table(s)/file(s) left alone (already in the target)" if left else ""))
    if it["type"] == "KQLDatabase":
        tables = {safe_name(t["TableName"]): t["TableName"] for t in kusto_mgmt(it["uri"], it["db"], ".show tables")}
        got = {f: kusto_count(it["uri"], it["db"], tables[f]) if f in tables else None for f in it["folders"]}
        total = sum(v or 0 for v in got.values()) + sum(v for t, v in st.get("done", {}).items()
                                                       if safe_name(t) not in got)
        expected = int(float(it["backupRows"])) if it["backupRows"] else None
        ok = expected is not None and total == expected and not it["busy"]
        return ("OK" if ok else "Failed" if not total else "Partial",
                f"{total:,} rows now" + (f", {expected:,} backed up" if expected is not None else ", backup count unknown")
                + (f"; tables left alone: {', '.join(it['busy'])}" if it["busy"] else ""))
    if it["type"] == "Warehouse":
        bad, empty, null_checks = [], 0, 0
        for root, res in sorted(it["tables"].items()):
            nulls = res.get("nullCounts") or {}
            if nulls:
                expressions = ["COUNT_BIG(*) AS n"] + [f"COUNT_BIG({sql_ident(c)}) AS c{j}"
                                                       for j, c in enumerate(nulls)]
                df = sql_query(it["ws"], it["target"], "Warehouse",
                               "SELECT " + ", ".join(expressions) + f" FROM {wh_table_name(root)}")
                n = int(df.iloc[0, 0])
            else:
                n = wh_count(it, root)
            empty += n == 0 and bool(res["rows"])
            differences = []
            if res["rows"] is not None and n != res["rows"]:
                differences.append(f"{n:,} rows, Delta log says {res['rows']:,}")
            for j, (column, expected) in enumerate(nulls.items()):
                actual = n - int(df.iloc[0, j + 1])
                null_checks += 1
                if actual != expected:
                    differences.append(f"{column}: {actual:,} NULLs, Delta log says {expected:,}")
            if differences:
                bad.append(f"{root}: " + "; ".join(differences))
        if WAREHOUSE_LOAD != "run" and bad and empty == len(bad):
            return "Manual step", (f"{empty} table(s) still empty: run the load script, then run this notebook again "
                                   "to check the row counts")
        return ("OK" if not bad and not it["manual"] else "Partial" if not bad else "Failed",
                f"{len(it['tables']) - len(bad)}/{len(it['tables'])} table(s) match the backed-up row count"
                + (f"; {null_checks} backed-up column NULL count(s) checked" if null_checks else "")
                + (f"; {bad[0]}" if bad else "") + (f"; {len(it['manual'])} table(s) by hand" if it["manual"] else ""))
    return None


if PROCEED:
    print("Checking every loaded item against the backup...")
    for it in TODO:
        try:
            res = verify(it)
        except Exception as exc:
            res = ("Failed", exc.short() if isinstance(exc, HttpError) else str(exc))
        if res:
            log(it["workspace"], it, "Verify", *res)
    df = save_log(PREFIX, "data_log")
    result = {"snapshot": SNAPSHOT_ID, "run": SEC["runId"], "dataId": DATA_ID, "finishedUtc": NOW_ID,
              "items": {i["id"]: {"name": i["displayName"], "type": i["type"], "action": i["action"]} for i in ITEMS},
              "verify": df[df.Step == "Verify"][["ItemId", "Status", "Detail"]].to_dict("records")}
    adls_write(f"{PREFIX}/data_result_{NOW_ID}.json", json.dumps(result, indent=2))
    attention = df[~df.Status.isin(["OK", "Info"])]
    show(df.pivot_table(index="Step", columns="Status", values="Item", aggfunc="count", fill_value=0))
    blocked = [i for i in ITEMS if i["action"] == "BLOCKED"]
    if attention.empty and not blocked:
        print("\nALL DATA VERIFIED: every loaded item matches its backup.")
    else:
        print(f"\n{len(attention)} row(s) need attention, {len(blocked)} item(s) BLOCKED (Manual step rows are "
              "expected work):")
        show(attention)
        print("Fix the cause and run again (new dry run + DATA ID): finished tables and files are never loaded twice.")
    print(f"Log: {ADLS_CONTAINER}/{PREFIX}/data_log_{NOW_ID}.csv")
