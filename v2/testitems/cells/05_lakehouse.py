def create_lakehouse():
    t, name = "Lakehouse", NAMES["Lakehouse"]
    lh_id, status = ensure_item(t, name)
    IDS[t] = lh_id
    step(t, name, lh_id, status)

    # Files: a CSV and a nested text file
    rows = ["id,product,amount"] + [f"{i},product-{i % 7},{i * 1.5:.2f}" for i in range(1, 101)]
    onelake_write(lh_id, "Files/raw/sample.csv", "\n".join(rows) + "\n")
    onelake_write(lh_id, "Files/raw/nested/readme.txt", "Test file for the Fabric migration notebooks.\n")
    step(t, name, lh_id, "OK", "Files/raw/sample.csv and Files/raw/nested/readme.txt written")

    # Delta table (deltalake is preinstalled in Fabric Python notebooks)
    try:
        from deltalake import write_deltalake
        df = pd.DataFrame({"id": range(1, 1001), "region": [["North", "South", "East", "West"][i % 4] for i in range(1000)],
                           "amount": [round(i * 2.5, 2) for i in range(1000)],
                           "sale_date": pd.date_range("2026-01-01", periods=1000, freq="h")})
        if LARGE_DATA_MIB or MEASURE_DATA_SIZE:
            small_path = seed_small_lakehouse(lh_id, df)
        else:
            write_deltalake(f"abfss://{WS}@onelake.dfs.fabric.microsoft.com/{lh_id}/Tables/{PREFIX}_sales", df,
                            mode="overwrite",
                            storage_options={"bearer_token": notebookutils.credentials.getToken("storage"),
                                             "use_fabric_endpoint": "true"})
        step(t, name, lh_id, "OK", f"Delta table {PREFIX}_sales: 1,000 rows")
    except Exception as exc:
        seed_fail(t, exc)
        step(t, name, lh_id, "Failed", f"Delta table: {exc}")
        if LARGE_DATA_MIB or MEASURE_DATA_SIZE:
            raise

    # Shortcut to the Lakehouse's own Files/raw folder: the backup must skip shortcut data
    try:
        fabric("POST", f"/workspaces/{WS}/items/{lh_id}/shortcuts?shortcutConflictPolicy=Abort",
               {"path": "Files", "name": f"{PREFIX}_shortcut",
                "target": {"oneLake": {"workspaceId": WS, "itemId": lh_id, "path": "Files/raw"}}})
        step(t, name, lh_id, "OK", f"shortcut Files/{PREFIX}_shortcut -> Files/raw")
    except HttpError as exc:
        if exc.status == 409 or "exist" in exc.message.lower():
            step(t, name, lh_id, "Exists", f"shortcut Files/{PREFIX}_shortcut")
        else:
            step(t, name, lh_id, "Failed", f"shortcut: {exc}")

    # SQL analytics endpoint: wait until provisioned, then add a view
    t2 = "SQLEndpoint"
    try:
        def endpoint():
            props = fabric("GET", f"/workspaces/{WS}/lakehouses/{lh_id}").get("properties") or {}
            sql = props.get("sqlEndpointProperties") or {}
            return sql if sql.get("id") and str(sql.get("provisioningStatus", "Success")).lower() == "success" else None
        sql = wait_until(endpoint, "The SQL analytics endpoint")
        IDS[t2] = sql["id"]
        conn = sql_connect(lh_id, "Lakehouse")
        sql_run(conn, f"CREATE OR ALTER VIEW dbo.{PREFIX}_view AS SELECT 1 AS id, 'backup test' AS note")
        ok = sql_scalar(conn, f"SELECT COUNT(*) FROM sys.views WHERE name = '{PREFIX}_view'")
        step(t2, name, sql["id"], "OK" if ok else "Failed", f"view dbo.{PREFIX}_view" + ("" if ok else " not found after creation"))
    except Exception as exc:
        step(t2, name, IDS.get(t2), "Failed", str(exc))

    if LARGE_DATA_MIB:
        seed_large_lakehouse(lh_id)
    elif MEASURE_DATA_SIZE:
        if "small_path" not in locals():
            raise SeedError("The original small Lakehouse fixture failed; persisted-size measurement is blocked.")
        seed_measure_delta(t, lh_id, f"{PREFIX}_sales", small_path, 1000)


if "Lakehouse" in CREATE_TYPES:
    try:
        create_lakehouse()
    except Exception as exc:
        seed_fail("Lakehouse", exc)
        step("Lakehouse", NAMES["Lakehouse"], IDS.get("Lakehouse"), "Failed", str(exc))
