# ---------------------------------------------------------------- scope
# Workspace IDs to migrate (from the inventory workbook). Each must already be connected to Azure DevOps.
WORKSPACE_IDS = [
    # "00000000-0000-0000-0000-000000000000",
]
# In-scope Fabric item types: saved in Git AND in the secondary ADLS backup.
ITEM_TYPES = ["Dataflow", "DataAgent", "Eventhouse", "KQLDatabase", "GraphModel", "Lakehouse", "Ontology",
              "SQLEndpoint", "Warehouse", "DataPipeline", "Notebook"]
# Other Fabric item types found in the workspaces are deleted in the migration too. List a type here only after
# you've decided how it's recovered (for example: definition from Git, data rebuilt by its source). Until then
# the workspace stays BLOCKED.
ACKNOWLEDGED_ITEM_TYPES = []

# ---------------------------------------------------------------- Git (primary copy)
# Organization of the workspaces' Azure DevOps connections, and the token used to read the repository and create
# the snapshot branches.
ADO_ORGANIZATION = "<devops-organization>"
KEY_VAULT_URL = "https://<key-vault-name>.vault.azure.net/"
ADO_AUTH = "pat"                         # how notebooks sign in to Azure DevOps: "pat" or "service_principal"
ADO_PAT_SECRET_NAME = "<secret-name>"    # pat: Key Vault secret holding the personal access token
ADO_SP_TENANT_ID = ""                    # service_principal: Entra tenant ID (GUID) of the app registration
ADO_SP_CLIENT_ID = ""                    # service_principal: application (client) ID
ADO_SP_SECRET_NAME = ""                  # service_principal: Key Vault secret holding the client secret VALUE
# One snapshot branch with the SAME name for every workspace: <prefix>/<snapshot id> (one per repository), at the
# branch head after all commits, plus an annotated tag <prefix>-<snapshot id> on the same commit.
SNAPSHOT_BRANCH_PREFIX = "migration-snapshot"
CREATE_SNAPSHOT_TAG = True
LOCK_SNAPSHOT_BRANCHES = True                    # locked branches can't be changed by anyone without unlocking
# Git sync check: a workspace is synced when it's connected, nothing is uncommitted in the workspace, nothing is
# incoming from Git and there are no conflicts (Source control shows 0 changes and 0 updates).
# True = the snapshot is taken only when EVERY workspace is synced; otherwise nothing is created (no commit, no
# branch, no raw copy, no manifest) and the sync table says what to do in each workspace.
REQUIRE_ALL_WORKSPACES_SYNCED = True
# False = this notebook never commits: commit in each workspace's Source control. True = it commits uncommitted
# workspace changes for you, only in a real run and only when nothing else is out of sync.
COMMIT_PENDING_CHANGES = False
# A workspace change of type "Deleted" means the item exists in Git but not in the workspace. Committing it would
# remove it from Git. Blocked unless you set this to True after confirming the item was deleted on purpose.
ALLOW_COMMITTING_DELETIONS = False

# ---------------------------------------------------------------- secondary backup (ADLS, from V2_2_Backup_Data)
ADLS_ACCOUNT = "<storage-account-name>"
ADLS_CONTAINER = "fabric-backup"
ADLS_ROOT = "fabric-backups"
SECONDARY_BACKUP_RUN_ID = "<run id, e.g. 20261120T190000Z>"   # the backup run taken after the change freeze
# When the change freeze started (UTC, ISO 8601, e.g. "2026-11-20T18:00:00Z"). The backup run must start after it.
FREEZE_STARTED_UTC = ""
# Items in a Git-linked workspace that Git doesn't hold (for example item types Fabric Git doesn't support) count as
# covered when True: V2_5 recreates them from their definition in ADLS (raw copy of this snapshot, else the V2_2
# backup) after the Git update, and V2_4 checks they didn't change since the snapshot. Needs
# SAVE_RAW_COPY_TO_ADLS = True. False = such items block the workspace.
ALLOW_SECONDARY_ONLY_ITEMS = False

# ---------------------------------------------------------------- workspaces without Git
# Workspaces that stay WITHOUT a Git link (it can't or shouldn't be made). They skip the Git checks; their items are
# recreated by V2_5 from the raw definitions saved with this snapshot in ADLS, and V2_4 proves nothing changed by
# comparing each item's definition with that copy. Needs SAVE_RAW_COPY_TO_ADLS = True.
NO_GIT_WORKSPACE_IDS = []

# ---------------------------------------------------------------- raw copy (fallback if Azure DevOps has a problem)
# True = also save, at the moment of the snapshot, every item's raw definition from the Fabric API and a file copy
# of the snapshot commit, under <ADLS_ROOT>/_migration/<snapshot id>/raw/ and .../git/. The raw/ folder uses the
# backup layout; V2_5 recreates items from it when Azure DevOps is unavailable (RESTORE_SOURCE = "adls"), for
# workspaces without Git, and for items Git doesn't hold.
SAVE_RAW_COPY_TO_ADLS = True

# ---------------------------------------------------------------- safety
# True = read and report only: no commit, no branch, no manifest.
DRY_RUN = True
