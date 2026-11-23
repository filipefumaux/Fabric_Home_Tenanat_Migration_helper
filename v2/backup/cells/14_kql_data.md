## 6. Eventhouse data (KQL databases)
Saves the database schema script and exports every table to compressed Parquet with
`.export async`. The Eventhouse writes to ADLS directly, so the data doesn't pass through this notebook.
