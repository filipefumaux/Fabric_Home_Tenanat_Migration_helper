# Setup: Azure DevOps access for the v2 notebooks (PAT or service principal)

> **AS-IS - independent personal/community project.**
> These notebooks are provided **AS-IS**, without warranty of any kind, express or implied.
> They are a personal/community contribution, not a Microsoft product or service, and are not published on
> behalf of, endorsed, supported or warranted by Microsoft. They do not represent or bind Microsoft and
> create no obligation or commitment by Microsoft. Product names, APIs and documentation are technical
> references only. Use at your own risk and validate in your own environment before production or deletion.

The v2 notebooks talk to Azure DevOps in **two separate ways**. Set up both before running V2_0.

| | What it does | Who signs in | Set up in |
|---|---|---|---|
| **A. Fabric Git link** | Workspace settings > Git integration: connect, commit, update from Git (V2_1, V2_3, V2_5) | Chosen with `GIT_LINK_AUTH`: the **service principal** through a Fabric connection (default), the **person running the notebook**, or an existing Fabric connection | Section 3 |
| **B. Notebook DevOps calls** | Read the repository, create the snapshot branch and tag, lock branches (V2_0 to V2_5) | A **personal access token (PAT)** *or* a **service principal**, chosen with `ADO_AUTH` | Sections 1 and 2 |

Choose **one** option for B. Both work; the table helps to choose.

| | PAT (`ADO_AUTH = "pat"`) | Service principal (`ADO_AUTH = "service_principal"`) |
|---|---|---|
| Belongs to | One person | An app registration (not a person) |
| Expires | Per organization policy (often 30–90 days, max 1 year) | Client secret expiry you choose (up to 2 years) |
| Affected if the person's account changes during the migration | Yes | No |
| Setup effort | Low (the person creates it) | Medium (Entra app + DevOps organization admin) |
| Recommended for | Tests, short migrations | The production migration |

---

## 1. Option PAT

**Who:** the person whose DevOps account the notebooks will use (normally the person running them).

1. **DevOps permissions of that person** (Project Collection or Project Administrator grants them):
   - Access level **Basic** in the organization (Organization settings > Users).
   - Member of the project's **Contributors** group, which gives on the repository: Read, Contribute (push and lock
     branches), Create branch, Create tag.
2. **Create the token:** `https://dev.azure.com/<organization>` > User settings (top right) > **Personal access
   tokens** > New Token.
   - Organization: **the organization in `ADO_ORGANIZATION`** (not "All accessible organizations" unless needed).
   - Expiration: past the end of the migration (restore included), within the organization's policy.
   - Scopes: Custom defined > **Code: Read & write** and **Project and Team: Read**.
   - Copy the token once; it's shown only at creation. It has **52 or 84 characters**.
3. **Store it in Key Vault:** Secrets > Generate/Import (or a new version of an existing secret). Value = only the
   token: no quotes, spaces, `Bearer` or `Basic`.
4. **Notebook settings** (same in V2_0 to V2_5):
   ```python
   KEY_VAULT_URL = "https://<key-vault-name>.vault.azure.net/"
   ADO_AUTH = "pat"
   ADO_PAT_SECRET_NAME = "<secret name>"
   ```
5. **Renewal:** before it expires, regenerate it in DevOps (same page > Regenerate) and add it as a **new version** of
   the same secret. No notebook change.

**Common errors (V2_0 names them):**
- *HTTP 203 / sign-in page* — the token is expired, revoked, created in another organization, or the secret
  holds something else (V2_0 says what it looks like, for example a client secret or a GUID).
- *Organization policy* — "Restrict personal access token creation" or IP conditional access policy validation
  can block tokens: ask the DevOps organization admin.

## 2. Option service principal

This section covers Azure DevOps. The full list of what the app needs (DevOps, Fabric tenant setting, ADX,
storage) is in [README.md](README.md), section **"Service principal permissions (all in one place)"**.

**Who:** an Entra administrator (step 1), a DevOps Project Collection Administrator (steps 2–3), a Key Vault
owner (step 4).

1. **Entra app registration:** Entra admin center > App registrations > New registration (single tenant).
   - Note the **Application (client) ID** and the **Directory (tenant) ID** (Overview page).
   - Certificates & secrets > New client secret. Copy the **Value** right away (40 characters). **Not** the
     "Secret ID" (a GUID). Note the expiry date.
   - No API permissions are needed: Azure DevOps access is granted inside DevOps.
2. **DevOps organization is connected to the same Entra tenant:** Organization settings > Microsoft Entra.
   Check the policy **"Allow service principals"**/third-party application access is not blocked by an organization
   policy (Organization settings > Policies).
3. **Add the app to DevOps:** Organization settings > Users > **Add users** > search the app name > Access level
   **Basic** > Add to project: the migration project, group **Contributors**. (Contributors gives Read, Contribute,
   Create branch, Create tag on the repository; lock needs Contribute.)
4. **Store the client secret Value in Key Vault** (as in section 1, step 3).
5. **Notebook settings** (same in V2_0 to V2_5):
   ```python
   KEY_VAULT_URL = "https://<key-vault-name>.vault.azure.net/"
   ADO_AUTH = "service_principal"
   ADO_SP_TENANT_ID = "<directory (tenant) ID>"
   ADO_SP_CLIENT_ID = "<application (client) ID>"
   ADO_SP_SECRET_NAME = "<secret name>"
   ```
   The notebook requests an Entra token for Azure DevOps and renews it automatically during long runs.
6. **Renewal:** before the client secret expires, create a new one in the app registration and add it as a new
   version of the Key Vault secret.

**Common errors (V2_0 names them):**
- `AADSTS7000215` — wrong or expired secret, or the Secret ID was stored instead of the Value.
- `AADSTS700016` — the client ID isn't an app in that tenant.
- `AADSTS90002` — wrong tenant ID.
- HTTP 401 / TF400813 or HTTP 203 — the app isn't added to the DevOps organization/project, or the
  organization is connected to another Entra tenant.

## 3. Fabric Git link (both options)

Needed by V2_1 (link), V2_3 (commit) and V2_5 (restore), whatever you chose for B.

1. **Fabric tenant settings** (Fabric administrator: Admin portal > Tenant settings > Git integration):
   - **Users can synchronize workspace items with their Git repositories:** Enabled.
   - **Users can export items to Git repositories in other geographical locations:** Enabled, when a workspace's
     capacity is in another geography than the Azure DevOps organization. Without it the link fails with
     `CrossGeographyOperationIsNotAllowed`. Only item definitions cross geographies, never data.
     **The move to the new home region can create this situation even if it isn't needed today**, so enable it
     before the migration. If scoped to security groups, include everyone who runs V2_1 to V2_5: the setting
     applies to the person doing the Git operation, not to the DevOps account. Allow a few minutes after Apply.
   - If items have sensitivity labels: **Users can export workspace items with applied sensitivity labels to Git
     repositories:** Enabled.
2. **How Fabric signs in to Azure DevOps for the link: `GIT_LINK_AUTH`** (V2_0, V2_1, V2_5). Independent of
   `ADO_AUTH`, which is only how the notebooks themselves call DevOps.

   | `GIT_LINK_AUTH` | Fabric links as | Needs |
   |---|---|---|
   | `"service_principal"` (default, recommended) | The service principal in `ADO_SP_*`, through a Fabric connection "Azure DevOps – Source control" named `GIT_CONNECTION_NAME` (default `fabric-migration-<org>-<project>-<repo>`). V2_1 reuses it if it exists, otherwise creates it when you confirm the plan; V2_5 finds the same one (or creates it again after the move). | `ADO_SP_TENANT_ID`, `ADO_SP_CLIENT_ID`, `ADO_SP_SECRET_NAME` filled (also when `ADO_AUTH = "pat"`); the service principal in the DevOps organization with Basic access and project Contributors (section 2). Not tied to a person; works when the notebook runs as a service principal. When you renew the client secret, update it in the Fabric connection too (Settings > Manage connections and gateways). |
   | `"my_account"` | The person running the notebook | Their own DevOps access (Basic, project Contributors) in the same Entra tenant, and having opened any workspace > **Source control** once and signed in. Not possible when the notebook runs as a service principal. |
   | `"connection_id"` | An existing Fabric connection | Its ID in `GIT_CONNECTION_ID`; you need User permission on it. |

   Before linking, V2_1 and V2_5 check that the service principal can read the repository and stop if not.
   V2_1 also creates each workspace's Git folder (a `README.md` inside it) because the connect API fails with
   `GitProviderResourceNotFound` when the folder doesn't exist yet.
   **Your own Git sign-in per workspace (needed with every option).** Fabric runs commit, update and disconnect,
   and the Source control screens, with the Git sign-in of the person doing them, per workspace. With
   `"service_principal"` or `"connection_id"`, each person who runs V2_3–V2_5 or opens Source control chooses the
   connection once: workspace > **Source control** > account > the connection above. If that's skipped, the screens
   show "Can't fetch branches" and the notebooks stop with this fix. The notebooks don't change it for you
   (`SWITCH_MY_GIT_CREDENTIALS = True` in V2_1 is an opt-in, unproven API alternative). People need **User**
   permission on the connection (Manage connections and gateways > Manage users).
   The person running the notebooks is also **Admin** of every workspace (Fabric administrator isn't enough;
   V2_1 can add you with `ADD_ME_AS_WORKSPACE_ADMIN = True`).

   **3b. One Entra tenant (rule for this migration).** The home-region move keeps the same Entra tenant; only
   where Fabric stores its metadata changes. The backup follows the same rule: **Fabric, the Azure DevOps
   organization and the service principal are all in the Fabric tenant.**
   - The DevOps organization must be connected to the Fabric tenant's Entra ID (Azure DevOps > Organization
     settings > Microsoft Entra). If it isn't, connect it ("Connect directory", Project Collection Administrator;
     plan it with the DevOps admin because users outside the tenant lose access) or use an organization that is.
   - With your own sign-in, Fabric only sees organizations in its tenant where you are a member; otherwise the link
     fails with `HTTP 404 GitProviderResourceNotFound`, even though the PAT/service principal checks pass (those
     are only for the notebooks' own DevOps calls).
   - V2_0 shows "organization in the same Entra tenant as Fabric" (and the service principal's tenant). V2_1 stops
     before linking if either is in another tenant. `ALLOW_CROSS_TENANT = True` exists only for a possible future
     cross-tenant scenario and isn't part of this migration.
   - Which tenant the organization uses: Azure DevOps normally names it to an anonymous call. If it doesn't, the
     notebooks confirm it through the service principal: Azure DevOps only accepts service principals of the
     tenant it's connected to, so if the service principal of the Fabric tenant can read the repository, the
     organization is in the Fabric tenant.
3. **Key Vault:** the person running the notebooks has **Key Vault Secrets User** (RBAC) or Get on secrets
   (access policy).

## 4. Check

Run **V2_0_Permission_Check**. Expected PASS rows: sign-in method, Key Vault secret, service principal sign-in
(option SP), read repository, create/lock/unlock/delete test branch, the Git tenant settings, workspace role Admin,
Git link sign-in (`GIT_LINK_AUTH`), service principal can read the repository (option SP link). Every FAIL/WARN row has its fix in the "How to fix" column.
