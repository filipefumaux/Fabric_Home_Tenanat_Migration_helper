## KQL database data
Ingests each table's Parquet export from ADLS. Update policies are paused during ingestion (so rows aren't copied twice into tables that are also restored) and always put back.
