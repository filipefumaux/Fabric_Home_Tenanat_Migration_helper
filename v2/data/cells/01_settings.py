SNAPSHOT_ID = "<snapshot id printed by V2_3_Git_Snapshot>"
# Each workspace is found where V2_5 restored it (restore_state.json), else by ID, else by name.

ADLS_ACCOUNT = "<storage-account-name>"
ADLS_CONTAINER = "fabric-backup"
ADLS_ROOT = "fabric-backups"

# Step 5 (V2_5) must have finished for a workspace before its data is loaded (from Git or from ADLS, both count).
# False only if the items were recreated some other way (by hand).
REQUIRE_GIT_RESTORE_DONE = True

RESTORE_LAKEHOUSE_DATA = True
RESTORE_KQL_DATA = True
# Where the KQL rows come from (V2_2 KQL_BACKUP_MODE): "auto" = the Parquet files if they were exported OK, else
# the Azure Data Explorer copy (Kusto Copy); "parquet" or "adx" = only that one. The ADX copy is read by the new
# Eventhouse with YOUR identity (cross-cluster query): you need Viewer on the ADX database.
KQL_RESTORE_SOURCE = "auto"
# Warehouse tables and SQL endpoint objects: "script" = write a .sql file to ADLS to run in the SQL editor,
# "run" = also run it here through Fabric's SQL connector with your identity, "off" = skip.
WAREHOUSE_LOAD = "script"
SQL_OBJECTS = "script"
# Lakehouse files: "fastcp" = bulk copy of each table folder (data first, _delta_log last) with azcopy
# (notebookutils.fs.fastcp), much faster for GBs; any file it doesn't place is copied one by one. "per_file" = every
# file through this notebook only. Both copy the backup byte for byte (Delta history kept) and never replace a file
# that is already there; the backup in ADLS is never moved or changed.
LAKEHOUSE_COPY_METHOD = "fastcp"
MAX_PARALLEL_FILES = 8                     # per_file copy (and the fallback)

# 1. DRY_RUN = True: prints the plan and a DATA ID. 2. DRY_RUN = False with CONFIRM_DATA_ID = that ID.
DRY_RUN = True
CONFIRM_DATA_ID = ""

# ---------------------------------------------------------------- rarely needed
# Only if a workspace was recreated under ANOTHER name: {"source workspace ID (manifest)": "workspace ID now"}.
WORKSPACE_MAP = {}
