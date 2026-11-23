# ---------------------------------------------------------------- what to back up
# Workspace IDs (GUIDs) to back up: the same workspaces as V2_1 and V2_3.
WORKSPACE_IDS = [
    # "00000000-0000-0000-0000-000000000000",
]
ITEM_TYPES = ["Dataflow", "DataAgent", "Eventhouse", "GraphModel", "Lakehouse", "Ontology",
              "SQLEndpoint", "Warehouse", "DataPipeline", "Notebook"]
INCLUDE_KQL_DATABASES = True        # an Eventhouse's data lives in its KQL databases: keep True
ONLY_ITEM_IDS = []                  # optional: only these item IDs

# When the change freeze started (UTC, e.g. "2026-11-16T10:00:00Z"). This run must start after it; V2_3 checks the
# same value. Leave empty only for a test run (V2_3 will then block).
FREEZE_STARTED_UTC = ""

# True = report what would be backed up and how big it is, without writing anything.
DRY_RUN = True

# ---------------------------------------------------------------- ADLS Gen2 (same account as V2_3-V2_6)
ADLS_ACCOUNT = "<storage-account-name>"   # hierarchical namespace enabled
ADLS_CONTAINER = "fabric-backup"          # must already exist
ADLS_ROOT = "fabric-backups"              # each run writes to <ADLS_ROOT>/<run id>/
KEY_VAULT_URL = "https://<key-vault-name>.vault.azure.net/"

COPY_LAKEHOUSE_FILES = True         # Lakehouse Files/ (shortcuts are skipped, not copied)
COPY_LAKEHOUSE_TABLES = True        # Lakehouse Tables/ as Delta folders (shortcuts skipped)
COPY_WAREHOUSE_TABLES = True        # Warehouse tables as Delta folders, read from OneLake
SCRIPT_SQL_ITEM_TYPES = ["SQLEndpoint"]  # script views/functions/procedures + permissions
MAX_PARALLEL_FILES = 8              # concurrent file copies per item

# ---------------------------------------------------------------- Eventhouse (KQL database) data
# "parquet" = every table exported to Parquet files in ADLS; V2_6 ingests them into the new Eventhouse.
# "adx"     = every table copied into an Azure Data Explorer cluster with Kusto Copy; V2_6 copies them back
#             from ADX into the new Eventhouse. Needs the ADX settings below.
# "both"    = both copies (safest; V2_6 uses the Parquet files and falls back to ADX).
# The KQL schema script (database_schema.kql) is saved in ADLS in every mode.
KQL_BACKUP_MODE = "parquet"

# ---------------------------------------------------------------- Azure Data Explorer (only "adx" or "both")
ADX_CLUSTER_URI = "https://<cluster>.<region>.kusto.windows.net"
# One ADX database per KQL database. ADX_DATABASE_PATTERN names it: {workspace} = workspace name,
# {database} = KQL database name, {id8} = first 8 characters of the KQL database item ID ({id} = all of it).
# The default is readable and unique across workspaces, e.g. "Sales_WS__SalesKQL__1b2c3d4e". The result is made
# safe for ADX (letters, digits, _ . - only). V2_6 doesn't need the names: each item records where its rows went.
ADX_DATABASE_PATTERN = "{workspace}__{database}__{id8}"
ADX_DATABASE_MAP = {}               # rarely needed: fixed name per KQL database {"name or item ID": "adx db"}
# True = V2_2 creates missing ADX databases (Azure Resource Manager, as the Kusto Copy service principal, which
# then is Admin on them). Needs Contributor on the ADX cluster. False = create them yourself first.
ADX_CREATE_DATABASES = True
ADX_SUBSCRIPTION_ID = ""            # "" = found from ADX_CLUSTER_URI (the app must see the cluster in Azure)
ADX_RESOURCE_GROUP = ""             # "" = found from ADX_CLUSTER_URI
ADX_SOFT_DELETE_PERIOD = ""         # retention of created databases; "" = unlimited (keep: Kusto Copy keeps the
                                    # original ingestion times, a short retention would drop old rows at once)
ADX_HOT_CACHE_PERIOD = "P1D"        # hot cache of created databases (backup data is rarely queried)
# Added as Viewer to each database V2_2 creates, so V2_6 can read it back. "me" = the person running this
# notebook; others as "aaduser=<upn>" or "aadgroup=<object id>;<tenant id>".
ADX_VIEWERS = ["me"]
ADX_CREATE_TABLES = True            # create missing destination tables (and add missing columns)

# Kusto Copy signs in as a service principal. Empty = the same app as the DevOps service principal (ADO_SP_*).
ADO_SP_TENANT_ID = ""
ADO_SP_CLIENT_ID = ""
ADO_SP_SECRET_NAME = ""
KC_TENANT_ID = ""
KC_CLIENT_ID = ""
KC_CLIENT_SECRET_NAME = ""

# The Kusto Copy Linux release downloaded from GitHub. It runs only if its SHA-256 matches.
KC_VERSION = "1.11.3.144"
KC_SHA256 = "dd546b3fcc2320085a0e2aff9af16ffe314df317fe95742628c161fcbc2a0b77"
KC_STAGING_FOLDER = "kusto-copy"    # in ADLS_CONTAINER. Keep it: it holds the progress used for the next run.
KC_COPY_MODE = "BackfillAndNew"     # first run copies history, later runs copy new arrivals only
KC_PARALLEL_JOBS = 2
KC_EXPORT_COUNT = 0
KC_TIMEOUT_HOURS = 8
