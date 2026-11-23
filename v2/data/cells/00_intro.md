# Fabric migration (v2) – step 6: restore the data

> **AS-IS - independent personal/community project.**
> These notebooks are provided **AS-IS**, without warranty of any kind, express or implied.
> They are a personal/community contribution, not a Microsoft product or service, and are not published on
> behalf of, endorsed, supported or warranted by Microsoft. They do not represent or bind Microsoft and
> create no obligation or commitment by Microsoft. Product names, APIs and documentation are technical
> references only. Use at your own risk and validate in your own environment before production or deletion.

Run after step 5 (`V2_5_Restore_From_Git`) has brought the items back (from Git, or from the raw copy if Azure DevOps was unavailable).
Git holds only definitions, so the restored Lakehouses, Warehouses and KQL databases are **empty**. This notebook
loads their data from the backup run that the snapshot recorded (`SECONDARY_BACKUP_RUN_ID`, written by
`V2_2_Backup_Data` after the freeze) and proves it arrived.

| Item | Saved in ADLS by the backup | Restored by this notebook | Checked by |
|---|---|---|---|
| Lakehouse | `Files/` and `Tables/` (Delta; with schemas `Tables/<schema>/<table>`) | file copy into OneLake, Delta logs last; only into a Lakehouse with the same layout (schemas or not) | every file present with the same size |
| KQL database | Parquet export per table + schema script | `.ingest` per table (update policies paused) | row count = exported rows |
| Warehouse | `Tables/` (Delta) | `COPY INTO`, or typed physical-column `OPENROWSET` + `INSERT` for mapped tables; script or run here (`WAREHOUSE_LOAD = "run"`) | row count and available column NULL counts match the Delta log |
| SQL analytics endpoint | views, functions, procedures | a script, or run here (`SQL_OBJECTS = "run"`) | objects present |

**How it protects the data**
- Reads the manifest (hash-checked) and only the backup run it names; refuses items whose backup step wasn't OK.
- Finds each workspace where V2_5 restored it (else by ID, else by name) and each restored item by type and name;
  the IDs in the backup are not trusted.
- Never overwrites or deletes: files are created with *if-none-match*, a Delta table or KQL table that already
  holds data in the target is left alone and reported, Warehouse tables are loaded only when empty.
- Dry run first (plan + `DATA_ID`); a real run needs `CONFIRM_DATA_ID`. Progress is saved in ADLS, so a rerun
  continues where it stopped and never loads a table twice.

**Requirements**
- Contributor or higher on the target workspaces; Storage Blob Data Reader on the backup container (the Eventhouse
  and the Warehouse read the backup files with your identity).
