# Fabric migration (v2) – step 3: Git snapshot and readiness check

> **AS-IS - independent personal/community project.**
> These notebooks are provided **AS-IS**, without warranty of any kind, express or implied.
> They are a personal/community contribution, not a Microsoft product or service, and are not published on
> behalf of, endorsed, supported or warranted by Microsoft. They do not represent or bind Microsoft and
> create no obligation or commitment by Microsoft. Product names, APIs and documentation are technical
> references only. Use at your own risk and validate in your own environment before production or deletion.

Part of the tenant-migration runbook. **Azure DevOps (Fabric Git integration) is the primary copy of the item
definitions; V2_2's copy in ADLS is the secondary copy and the only copy of the data.**

| Step | Notebook | What it does |
|---|---|---|
| 0 | `V2_0_Permission_Check` | Checks every permission and network path the other notebooks need |
| 1 | `V2_1_Connect_Workspaces_To_Git` | Checks each workspace's Git connection; connects the ones that aren't (dry run first) |
| – | change freeze | Note the time (UTC): `FREEZE_STARTED_UTC` |
| 2 | `V2_2_Backup_Data` | Data + secondary definitions to ADLS (run after the change freeze) |
| **3** | **this notebook** | Checks that every workspace is synced with Git, freezes the synced commit in a locked snapshot branch, saves a raw copy to ADLS, checks every item is saved, writes the migration manifest |
| 4 | `V2_4_Delete_Fabric_Items` | Disconnects Git, deletes the Fabric items (only if step 3 said READY and nothing changed since) |
| – | migration | The workspaces move to the new home region |
| 5 | `V2_5_Restore_From_Git` | Reconnects each workspace to the snapshot and lets Fabric recreate the items from Git |
| 6 | `V2_6_Restore_Data` | Loads the data from the backup run back into the recreated items and checks it (settings printed by step 5) |

**Git sync check first (read-only).** A table shows, per workspace, whether it's **synced** with Git: connected,
nothing uncommitted, nothing incoming, no conflicts (Source control shows 0 changes and 0 updates), with the fix
for each one that isn't. With `REQUIRE_ALL_WORKSPACES_SYNCED = True` (default) nothing is created until **every**
workspace is synced. The notebook doesn't commit for you unless `COMMIT_PENDING_CHANGES = True`.

**What this notebook changes (only with `DRY_RUN = False` and every workspace synced)**
- Only with `COMMIT_PENDING_CHANGES = True`: commits pending workspace changes (additions and modifications;
  deletions block).
- Creates and locks ONE snapshot branch with the same name for all workspaces, `migration-snapshot/<snapshot id>`
  (one per repository, after every workspace's commit), and tags it `migration-snapshot-<snapshot id>`.
- With `SAVE_RAW_COPY_TO_ADLS`: saves every item's raw definition (Fabric API) and a file copy of the snapshot
  commit to `<ADLS_ROOT>/_migration/<snapshot id>/raw/` and `.../git/`. This is the fallback if Azure DevOps is
  unavailable at restore time: V2_5 with `RESTORE_SOURCE = "adls"` recreates the items from `raw/`.
- Workspaces in `NO_GIT_WORKSPACE_IDS` skip the Git checks and get no snapshot branch: their items count as
  saved through the raw copy (plus the V2_2 definition), and V2_5 recreates them from ADLS.
- Writes `manifest.json`, `manifest.sha256`, `items.csv` and a log to `<ADLS_ROOT>/_migration/<snapshot id>/`.

It never changes or deletes Fabric items.

**A workspace is READY only when**
- it's synced with Git (and, by default, so is every other workspace in scope);
- a schema-enabled Lakehouse keeps `defaultSchema` in its Git definition (`lakehouse.metadata.json`), so it comes
  back with schemas;
- every Fabric item is in the snapshot commit (SQL analytics endpoints are recreated with their Lakehouse);
- every in-scope item has an OK raw copy (when `SAVE_RAW_COPY_TO_ADLS`) and an OK definition in the secondary backup, and every Lakehouse, Warehouse and KQL
  database has an OK data backup (SQL endpoints: OK SQL scripts);
- every other Fabric item type is acknowledged in `ACKNOWLEDGED_ITEM_TYPES` (it's deleted with the rest);
- the secondary backup run is complete and started after the change freeze.

**Requirements**
- Fabric administrator, and Contributor or higher on every workspace; your Git credentials configured in Fabric
  (open Source control once in each workspace).
- Workspaces already connected to Azure DevOps by the operator.
- Azure DevOps personal access token in Key Vault, scope *Code (Read, write & manage)*, with *Create branch* and
  *Contribute* on the repository (locking a branch needs Contribute).
- *Storage Blob Data Contributor* on the backup container.
