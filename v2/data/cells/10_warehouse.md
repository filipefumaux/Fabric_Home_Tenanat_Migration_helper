## Warehouse tables and SQL endpoint objects
Writes a `.sql` script per item to ADLS (`_migration/<snapshot>/data/scripts/`). With `"run"` it also runs it here: a Warehouse table is loaded only while it is empty, and SQL objects that already exist are skipped.
