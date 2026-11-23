# Fabric migration (v2) – step 5: restore the Fabric items from Git

> **AS-IS - independent personal/community project.**
> These notebooks are provided **AS-IS**, without warranty of any kind, express or implied.
> They are a personal/community contribution, not a Microsoft product or service, and are not published on
> behalf of, endorsed, supported or warranted by Microsoft. They do not represent or bind Microsoft and
> create no obligation or commitment by Microsoft. Product names, APIs and documentation are technical
> references only. Use at your own risk and validate in your own environment before production or deletion.

Run after the workspaces have moved. Fabric recreates the items itself from the locked snapshot commit (Git
integration), so references between items in the same workspace are rebuilt by Fabric. Data comes back in
step 6 with `V2_6_Restore_Data`; this notebook prints its settings.

**Which workspace:** each one is found by its ID (a home-region move keeps workspace IDs) or, if that ID no longer
exists, by its name; the plan's *Found by* column shows which, and the PLAN ID covers it. `WORKSPACE_MAP` is only
for a workspace recreated under another name.

**How it protects the workspace**
- Restores only from the snapshot commit recorded in the manifest (hash-checked); refuses if the locked snapshot
  branch moved or the commit no longer contains the expected items.
- Uses a separate restore branch per workspace, created from that commit. Items that already exist in the workspace
  (the Power BI reports and semantic models that moved with it) are removed from the restore branch, so the Git
  update can only **add** items: it runs with `allowOverrideItems = false` and no conflict resolution, so Fabric
  refuses to start rather than overwrite anything.
- Refuses a workspace that already has a Fabric item with the same name and type, or that is connected to another
  Git branch.
- Dry run first; a real run needs `CONFIRM_PLAN_ID`. Progress is saved in ADLS, so a rerun continues where it stopped.
- After the update: checks every item is back, the workspace is in sync with the restore branch, and scans the
  restored definitions (and semantic models) for IDs of the deleted items. Fixing them is a separate, confirmed step
  (`FIX_STALE_REFERENCES` + `CONFIRM_FIX_ID`); semantic models are only reported.

**Without Git (ADLS route)**: workspaces V2_3 kept without Git (`NO_GIT_WORKSPACE_IDS`) are always recreated from
the raw definitions saved with the snapshot in ADLS. If Azure DevOps is unavailable, `RESTORE_SOURCE = "adls"` does
the same for every workspace. Items are created in order (Lakehouse, Warehouse, Eventhouse, KQL database, then
Notebook, Dataflow, pipeline, GraphModel, Ontology, DataAgent), and the old IDs inside each definition are
replaced with the new items' IDs as they are created. The KQL database Fabric creates with an Eventhouse gets
the saved definition. Same dry run, PLAN ID and resume as the Git route; the workspaces end up without a Git link.

**Requirements**
- Contributor or higher on the target workspaces (now on a capacity in the new region); Fabric administrator for
  the item reads.
- Azure DevOps credentials in Fabric: your own (leave `GIT_CONNECTION_ID` empty) or a Fabric connection that stores
  a service principal (its ID in `GIT_CONNECTION_ID`). After a tenant remap, connections must be recreated.
- Azure DevOps token in Key Vault with *Create branch* and *Contribute* on the repository.
