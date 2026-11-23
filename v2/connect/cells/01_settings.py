WORKSPACE_IDS = [
    # "00000000-0000-0000-0000-000000000000",
]

ADO_ORGANIZATION = "<devops-organization>"
ADO_PROJECT = "<devops-project>"
ADO_REPOSITORY = "<repository-name>"
ADO_BRANCH = "fabric-workspaces"         # created from the default branch if it doesn't exist
CREATE_BRANCH_IF_MISSING = True
# Git folder per workspace. {workspace} = workspace name (made path-safe), {id} = workspace ID.
DIRECTORY_PATTERN = "/workspaces/{workspace}"
DIRECTORY_MAP = {}                        # optional per-workspace folder: {"workspace-guid": "/path"}

KEY_VAULT_URL = "https://<key-vault-name>.vault.azure.net/"
ADO_AUTH = "pat"                         # how notebooks sign in to Azure DevOps: "pat" or "service_principal"
ADO_PAT_SECRET_NAME = "<secret-name>"    # pat: Key Vault secret holding the personal access token
ADO_SP_TENANT_ID = ""                    # service_principal: Entra tenant ID (GUID) of the app registration
ADO_SP_CLIENT_ID = ""                    # service_principal: application (client) ID
ADO_SP_SECRET_NAME = ""                  # service_principal: Key Vault secret holding the client secret VALUE

# ---------------------------------------------------------------- how Fabric signs in to Azure DevOps for the Git link
# (separate from ADO_AUTH, which is only how this notebook itself calls DevOps)
#   "service_principal" = Fabric connection "Azure DevOps - Source control" with the service principal in ADO_SP_*
#                         (fill those even if ADO_AUTH = "pat"). Found by GIT_CONNECTION_NAME, or created when you
#                         confirm the plan. Recommended: not tied to one person, works when the notebook runs as a
#                         service principal. The service principal needs Basic access + project Contributors.
#   "my_account"        = your own Azure DevOps sign-in (you must be a member of the DevOps organization).
#   "connection_id"     = an existing Fabric connection: put its ID in GIT_CONNECTION_ID.
GIT_LINK_AUTH = "service_principal"
GIT_CONNECTION_NAME = ""                  # "" = fabric-migration-<organization>-<project>-<repository>
GIT_CONNECTION_ID = ""                    # "connection_id": its ID (with "service_principal": force this one)

# Your own Git sign-in per workspace (what the workspace screens and every later Git call use). False (default) =
# the notebook only reports it; you choose the connection yourself in the workspace: Source control > account >
# the Fabric connection used to link (this is what fixed "Can't fetch branches" in testing). True = the notebook
# switches it for you through the API (part of the confirmed plan; not proven to fix the screens).
SWITCH_MY_GIT_CREDENTIALS = False

# This migration keeps Fabric, Azure DevOps and the service principal in ONE Entra tenant. The notebook stops if
# the DevOps organization (or the service principal) belongs to another tenant. True only for a future cross-tenant
# scenario; not part of this migration.
ALLOW_CROSS_TENANT = False

# Linking needs the Admin role on each workspace (Fabric administrator isn't enough). True = workspaces where you
# aren't Admin get you added as Admin first (admin API, part of the confirmed plan). Remove it after the migration.
ADD_ME_AS_WORKSPACE_ADMIN = False

DRY_RUN = True
CONFIRM_CONNECT_ID = ""
