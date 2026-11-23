SNAPSHOT_ID = "<snapshot id printed by V2_3_Git_Snapshot>"
# Only workspaces that were READY in the manifest are restored. Each is found by its ID (a home-region move keeps
# the IDs) or, if that ID is gone, by its name; the plan shows which. WORKSPACE_MAP is at the end (rarely needed).

ADLS_ACCOUNT = "<storage-account-name>"
ADLS_CONTAINER = "fabric-backup"
ADLS_ROOT = "fabric-backups"
KEY_VAULT_URL = "https://<key-vault-name>.vault.azure.net/"
ADO_AUTH = "pat"                         # how notebooks sign in to Azure DevOps: "pat" or "service_principal"
ADO_PAT_SECRET_NAME = "<secret-name>"    # pat: Key Vault secret holding the personal access token
ADO_SP_TENANT_ID = ""                    # service_principal: Entra tenant ID (GUID) of the app registration
ADO_SP_CLIENT_ID = ""                    # service_principal: application (client) ID
ADO_SP_SECRET_NAME = ""                  # service_principal: Key Vault secret holding the client secret VALUE

# ---------------------------------------------------------------- where the items come from
#   "git"  = the snapshot branch in Azure DevOps (normal). Workspaces V2_3 kept WITHOUT Git (NO_GIT_WORKSPACE_IDS)
#            are always recreated from the raw definitions in ADLS.
#   "adls" = Azure DevOps is unavailable: EVERY workspace is recreated from the raw definitions saved with the
#            snapshot in ADLS (IDs inside the definitions are replaced with the new ones as items are created).
#            The workspaces end up without a Git link; link them later with V2_1.
RESTORE_SOURCE = "git"

# ---------------------------------------------------------------- how Fabric signs in to Azure DevOps for the Git link
# (separate from ADO_AUTH, which is only how this notebook itself calls DevOps)
#   "service_principal" = Fabric connection "Azure DevOps - Source control" with the service principal in ADO_SP_*
#                         (fill those even if ADO_AUTH = "pat"). Same connection as V2_1 (found by
#                         GIT_CONNECTION_NAME), or created when you confirm the plan. Recommended: not tied to one
#                         person, works when the notebook runs as a service principal. The service principal needs
#                         Basic access + project Contributors.
#   "my_account"        = your own Azure DevOps sign-in (you must be a member of the DevOps organization).
#   "connection_id"     = an existing Fabric connection: put its ID in GIT_CONNECTION_ID.
GIT_LINK_AUTH = "service_principal"
GIT_CONNECTION_NAME = ""                  # "" = fabric-migration-<organization>-<project>-<repository>
GIT_CONNECTION_ID = ""                    # "connection_id": its ID (with "service_principal": force this one)
ALLOW_CROSS_TENANT = False                # this migration keeps one Entra tenant

RESTORE_BRANCH_PREFIX = "migration-restore"   # branch: <prefix>/<snapshot id>/<workspace>
LOCK_RESTORE_BRANCHES = True

# 1. DRY_RUN = True: prints the plan and a PLAN ID. 2. DRY_RUN = False with CONFIRM_PLAN_ID = that ID.
DRY_RUN = True
CONFIRM_PLAN_ID = ""

# After the restore, IDs of deleted items still written inside definitions are listed with a FIX ID.
# To replace them with the new IDs, run again with FIX_STALE_REFERENCES = True and CONFIRM_FIX_ID = that ID.
FIX_STALE_REFERENCES = False
CONFIRM_FIX_ID = ""
# Power BI types scanned for old IDs (reported only, never changed).
SCAN_POWER_BI_TYPES = ["SemanticModel"]

# ---------------------------------------------------------------- rarely needed
# Only if a workspace was recreated under ANOTHER name: {"source workspace ID (manifest)": "workspace ID now"}.
WORKSPACE_MAP = {}
