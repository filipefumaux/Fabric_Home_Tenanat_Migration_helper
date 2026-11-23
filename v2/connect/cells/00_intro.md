# Fabric migration (v2) – step 1: check and connect workspaces to Azure DevOps

> **AS-IS - independent personal/community project.**
> These notebooks are provided **AS-IS**, without warranty of any kind, express or implied.
> They are a personal/community contribution, not a Microsoft product or service, and are not published on
> behalf of, endorsed, supported or warranted by Microsoft. They do not represent or bind Microsoft and
> create no obligation or commitment by Microsoft. Product names, APIs and documentation are technical
> references only. Use at your own risk and validate in your own environment before production or deletion.

For each workspace: reports its Git connection (the same link as *Workspace settings > Git integration*), and
links the ones that aren't linked to the **same repository and branch** (`ADO_BRANCH`), each in its own folder,
then makes the first commit (workspace → Git). A workspace linked earlier but never synced is finished.

**Why linking can fail, and what this notebook does about it**
| Cause | Check / fix |
|---|---|
| You're Fabric administrator but not **Admin of the workspace** | Shows your role per workspace; `ADD_ME_AS_WORKSPACE_ADMIN = True` adds you (admin API) |
| Fabric has no Azure DevOps credentials for you | Open any workspace > Source control once and sign in, or use a stored connection (`GIT_CONNECTION_ID`) |
| Notebook runs as a service principal / workspace identity (scheduled or pipeline run) | Refused unless `GIT_CONNECTION_ID` is set |
| Anything else | Each failure prints the error and the fix; the workspaces that couldn't be linked are listed with the values to enter by hand |

**Safeguards**
- Dry run first: prints the plan and a CONNECT ID; a real run needs `CONFIRM_CONNECT_ID`.
- Workspaces already connected are never changed; connections to another organization, project or repository are
  reported for review.
- Connects only to an **empty** Git folder, so the first sync can only go from the workspace to Git, never the other
  way (nothing in the workspace can be overwritten). Two workspaces can't share a folder.
- Stops a workspace if Fabric asks to update the workspace from Git instead of committing.
- At the end, lists the Fabric items that are still not in Git (item types Git doesn't support): they rely on the
  ADLS copies.

**Requirements**
- Admin role on each workspace (or `ADD_ME_AS_WORKSPACE_ADMIN`); the workspace on a Fabric capacity.
- Your Git credentials for Azure DevOps (SSO) or a Fabric connection in `GIT_CONNECTION_ID`; *Contribute* on the
  branch. Use a branch without pull-request policies, or the commit is refused.
- Azure DevOps token in Key Vault (*Code: Read & write*) to check folders and create a missing branch.
