WORKSPACE_IDS = [
    # "00000000-0000-0000-0000-000000000000",
]
ITEM_TYPES = ["Dataflow", "DataAgent", "Eventhouse", "KQLDatabase", "GraphModel", "Lakehouse", "Ontology",
              "Warehouse", "DataPipeline", "Notebook"]

ADO_ORGANIZATION = "<devops-organization>"
ADO_PROJECT = "<devops-project>"
ADO_REPOSITORY = "<repository-name>"
KEY_VAULT_URL = "https://<key-vault-name>.vault.azure.net/"
ADO_AUTH = "pat"                         # how notebooks sign in to Azure DevOps: "pat" or "service_principal"
ADO_PAT_SECRET_NAME = "<secret-name>"    # pat: Key Vault secret holding the personal access token
ADO_SP_TENANT_ID = ""                    # service_principal: Entra tenant ID (GUID) of the app registration
ADO_SP_CLIENT_ID = ""                    # service_principal: application (client) ID
ADO_SP_SECRET_NAME = ""                  # service_principal: Key Vault secret holding the client secret VALUE
# How Fabric signs in to DevOps for the Git link (V2_1/V2_5): "service_principal", "my_account" or "connection_id"
GIT_LINK_AUTH = "service_principal"
GIT_CONNECTION_NAME = ""                 # "" = fabric-migration-<organization>-<project>-<repository>
GIT_CONNECTION_ID = ""                   # only for "connection_id"

ADLS_ACCOUNT = "<storage-account-name>"
ADLS_CONTAINER = "fabric-backup"
ADLS_ROOT = "fabric-backups"
SECONDARY_BACKUP_RUN_ID = ""             # optional: check this backup run is readable

WRITE_TESTS = True                       # temporary DevOps branch + ADLS test file, both removed afterwards
