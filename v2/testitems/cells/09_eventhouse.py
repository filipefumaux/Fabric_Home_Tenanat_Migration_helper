def create_eventhouse():
    t, name = "Eventhouse", NAMES["Eventhouse"]
    eh_id, status = ensure_item(t, name)
    IDS[t] = eh_id
    step(t, name, eh_id, status)

    def database():
        for db in fabric_list(f"/workspaces/{WS}/kqlDatabases"):
            props = db.get("properties") or {}
            if (props.get("parentEventhouseItemId") or "").lower() == eh_id.lower() and props.get("queryServiceUri"):
                return db
        return None
    db = wait_until(database, "The Eventhouse's KQL database")
    IDS["KQLDatabase"] = db["id"]
    uri, dbname = db["properties"]["queryServiceUri"], db["properties"].get("databaseName") or db["displayName"]
    table = f"{PREFIX}_events"
    kusto(uri, dbname, f".create-merge table {table} (Timestamp:datetime, DeviceId:string, Value:real)")
    count = int((kusto(uri, dbname, f"{table} | count", "query") or [{"Count": 0}])[0].get("Count") or 0)
    if count == 0:
        kusto(uri, dbname, f".set-or-append {table} <| range i from 1 to 1000 step 1 "
                           "| project Timestamp = datetime(2026-01-01) + i * 1m, DeviceId = strcat('device-', i % 10), "
                           "Value = todouble(i % 100) / 3.0")
        count = int(kusto(uri, dbname, f"{table} | count", "query")[0].get("Count") or 0)
    kusto(uri, dbname, f".create-or-alter function with (folder = 'backup-test') {PREFIX}_avg_by_device() "
                       f"{{ {table} | summarize AvgValue = avg(Value) by DeviceId }}")
    step("KQLDatabase", db["displayName"], db["id"], "OK" if count == 1000 else "Check",
         f"table {table}: {count:,} rows; function {PREFIX}_avg_by_device")
    if LARGE_DATA_MIB or MEASURE_DATA_SIZE:
        if count != 1000:
            raise SeedError(f"Original small KQL fixture has {count} rows, expected 1000; no stress load is safe.")
        if LARGE_DATA_MIB:
            seed_large_eventhouse(eh_id, db["id"], uri, dbname)
        else:
            seed_measure_kql(eh_id, uri, dbname, table, count)


if "Eventhouse" in CREATE_TYPES:
    try:
        create_eventhouse()
    except Exception as exc:
        seed_fail("Eventhouse", exc)
        step("Eventhouse", NAMES["Eventhouse"], IDS.get("Eventhouse"), "Failed", str(exc))
