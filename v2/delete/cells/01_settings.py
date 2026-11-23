SNAPSHOT_ID = "<snapshot id printed by V2_3_Git_Snapshot>"
# Workspaces to clear now (must be READY in the manifest). Empty = every READY workspace in the manifest.
WORKSPACE_IDS = []

ADLS_ACCOUNT = "<storage-account-name>"
ADLS_CONTAINER = "fabric-backup"
ADLS_ROOT = "fabric-backups"
KEY_VAULT_URL = "https://<key-vault-name>.vault.azure.net/"
ADO_AUTH = "pat"                         # how notebooks sign in to Azure DevOps: "pat" or "service_principal"
ADO_PAT_SECRET_NAME = "<secret-name>"    # pat: Key Vault secret holding the personal access token
ADO_SP_TENANT_ID = ""                    # service_principal: Entra tenant ID (GUID) of the app registration
ADO_SP_CLIENT_ID = ""                    # service_principal: application (client) ID
ADO_SP_SECRET_NAME = ""                  # service_principal: Key Vault secret holding the client secret VALUE

# Refuse a snapshot older than this: a stale snapshot means more risk that something changed.
MAX_SNAPSHOT_AGE_HOURS = 24

# Assisted Tenant Migration needs HARD deletes: a soft-deleted item stays in the workspace recycle bin for up to
# 30 days and blocks the migration. Hard delete needs the workspace Admin role and can't be undone.
HARD_DELETE = True

# 1. DRY_RUN = True prints the deletion plan and a DELETE ID. 2. Review it, then DRY_RUN = False and paste the ID.
DRY_RUN = True
CONFIRM_DELETE_ID = ""
