## 7. Copy Eventhouse tables to Azure Data Explorer (Kusto Copy)
Copies every table of the selected KQL databases into an Azure Data Explorer cluster with
[Kusto Copy](https://github.com/Azure/kusto-copy). The Eventhouse exports to the staging folder in
ADLS and ADX ingests from there, so the data doesn't pass through this notebook.

* **One ADX database per KQL database**, named by `ADX_DATABASE_PATTERN` (default
  `{workspace}__{database}__{id8}`, e.g. `Sales_WS__SalesKQL__1b2c3d4e`): readable, unique across workspaces,
  original table names kept. With `ADX_CREATE_DATABASES = True` (default) V2_2 creates missing databases through
  Azure Resource Manager as the service principal (which makes it Admin on them) and adds `ADX_VIEWERS` (you) as
  Viewer. The dry run lists the databases it would create. Every real run saves `adx_inventory.csv` (workspace,
  KQL database, ADX database, tables, rows) in the run folder; V2_6 doesn't need it, each item records its own
  destination.
* Each table is its own Kusto Copy job with a fixed staging folder
  (`<KC_STAGING_FOLDER>/<adx host>/<adx database>/<KQL database id>/<table>`). With
  `KC_COPY_MODE = "BackfillAndNew"` the first run copies the history and every later run copies
  only rows ingested since the previous run. **Don't delete or move these folders**: a new folder
  starts a new job and copies the history again, which duplicates rows in ADX.
* **The ADX database or a table was deleted (or emptied) after a copy?** The staging folder still says those
  rows were copied, so Kusto Copy would copy nothing. V2_2 checks this: for every ADX table that is missing or
  empty while its staging folder has progress, it resets that folder and copies the whole history again (no
  duplicates, the table is empty). The dry run lists these tables; the result says "old Kusto Copy progress reset".
* The notebook downloads Kusto Copy `KC_VERSION` (Linux) from GitHub releases, checks its SHA-256
  against `KC_SHA256`, and only then runs it. To move to a newer release, update both settings.
* Kusto Copy appends; it doesn't delete. Rows deleted or updated in the Eventhouse stay in ADX.
* A copy that hits `KC_TIMEOUT_HOURS`, or a notebook session that ends, isn't lost: the next run
  resumes the same job from its staging folder.

**Service principal permissions.** Kusto Copy signs in as an app, not as you: the app in `KC_CLIENT_ID`, or,
when that is empty, the DevOps service principal (`ADO_SP_CLIENT_ID`). Give **that app**:
* Fabric: member of each workspace (Viewer is enough to read the KQL databases).
* ADX: **Contributor** on the ADX cluster (Azure role, Access control (IAM)) so V2_2 can create the databases;
  it is then Admin on each database it creates. With `ADX_CREATE_DATABASES = False`: **Admin** on each destination
  database you created instead.
* Storage: **Storage Blob Data Contributor** on the container and **Storage Blob Delegator** on the
  storage account itself (account level) (Kusto Copy creates user-delegation SAS URLs for the Eventhouse and ADX).
* Network: the Eventhouse and the ADX ingestion service must reach the storage account, and this
  notebook must reach `github.com` (for the download) and the ADX cluster.


You (the person who later runs V2_6) need **Viewer** on each ADX database. Full checklist: README, *Prerequisites for the ADX copy*.
