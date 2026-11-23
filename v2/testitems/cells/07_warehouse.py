def create_warehouse():
    t, name = "Warehouse", NAMES["Warehouse"]
    wh_id, status = ensure_item(t, name)
    IDS[t] = wh_id
    step(t, name, wh_id, status)
    conn = wait_until(lambda: _try_connect(wh_id), "The Warehouse", every=15)
    table = f"dbo.{PREFIX}_orders"
    sql_run(conn, f"IF OBJECT_ID('{table}') IS NULL CREATE TABLE {table} "
                  "(OrderId INT, Customer VARCHAR(50), Amount DECIMAL(10,2), OrderDate DATE)")
    if int(sql_scalar(conn, f"SELECT COUNT(*) FROM {table}") or 0) == 0:
        values = ",".join(f"({i}, 'customer-{i % 25}', {i * 3.75:.2f}, DATEADD(day, {i % 365}, '2026-01-01'))"
                          for i in range(1, 501))
        sql_run(conn, f"INSERT INTO {table} (OrderId, Customer, Amount, OrderDate) VALUES {values}")
    sql_run(conn, f"CREATE OR ALTER VIEW dbo.{PREFIX}_orders_by_customer AS "
                  f"SELECT Customer, COUNT(*) AS Orders, SUM(Amount) AS Total FROM {table} GROUP BY Customer")
    sql_run(conn, f"CREATE OR ALTER PROCEDURE dbo.{PREFIX}_top_customers AS "
                  f"SELECT TOP 5 * FROM dbo.{PREFIX}_orders_by_customer ORDER BY Total DESC")
    rows = int(sql_scalar(conn, f"SELECT COUNT(*) FROM {table}") or 0)
    step(t, name, wh_id, "OK" if rows == 500 else "Check", f"{table}: {rows} rows; view and procedure created")
    if LARGE_DATA_MIB or MEASURE_DATA_SIZE:
        if rows != 500:
            raise SeedError(f"Original small Warehouse fixture has {rows} rows, expected 500; no stress load is safe.")
        if LARGE_DATA_MIB:
            seed_large_warehouse(wh_id, conn)
        else:
            seed_measure_delta(t, wh_id, table, f"Tables/dbo/{PREFIX}_orders", rows)


def _try_connect(wh_id):
    try:
        conn = sql_connect(wh_id, "Warehouse")
        conn.query("SELECT 1 AS ready")
        return conn
    except RuntimeError:
        raise
    except Exception:
        return None


if "Warehouse" in CREATE_TYPES:
    try:
        create_warehouse()
    except Exception as exc:
        seed_fail("Warehouse", exc)
        step("Warehouse", NAMES["Warehouse"], IDS.get("Warehouse"), "Failed", str(exc))
