# Fabric migration v2 – Git-first backup and restore (test version)

> **AS-IS - independent personal/community project.**
> These notebooks are provided **AS-IS**, without warranty of any kind, express or implied.
> They are a personal/community contribution, not a Microsoft product or service, and are not published on
> behalf of, endorsed, supported or warranted by Microsoft. They do not represent or bind Microsoft and
> create no obligation or commitment by Microsoft. Product names, APIs and documentation are technical
> references only. Use at your own risk and validate in your own environment before production or deletion.

Three copies of every definition: **Azure DevOps** (Fabric Git integration, primary), the **raw copy** taken with
the Git snapshot (ADLS, fallback if DevOps has a problem) and the V2_2 backup run (ADLS, secondary, plus the data).

## Diagnostic privacy
HTTP errors and recorded log details redact credential values; the agent runner also redacts console/table
output, transcripts, status facts and terminal failures. The shared redactor covers registered credentials,
authorization tokens, signed-URL signatures and secret-valued fields without truncating diagnostics or fixes.
It does not change API/backup payloads or source DataFrames. Keep all operational output private: names,
resource IDs, business data and unrecognized sensitive values are not guaranteed to be removed.

## Build ID: are you running the current notebooks?
Every notebook prints `<name> build <BUILD_ID>` when it starts. `BUILD_IDS.txt` in the package lists the IDs of this
release. A different ID means an older import: import the notebooks again before going on. (The ID covers all code,
not the settings cell, so it doesn't change when you fill in your settings.)

## Runbook

| Step | When | Notebook | Result |
|---|---|---|---|
| T | Before production, in your test tenant | `V2_T_Create_Test_Items` | Test workspace with one item of each type; run steps 0–6 on it as a rehearsal, then hard-delete the test items (see *Rehearsal*) |
| 0 | Weeks before | `V2_0_Permission_Check` | PASS/FAIL per permission and network path, with the fix. Temporary DevOps branch and ADLS file are removed |
| 1 | Weeks before | `V2_1_Connect_Workspaces_To_Git` | Checks every workspace's Git link (same as Workspace settings > Git integration); links the unlinked ones to the same repository and branch, one empty folder each, and makes the first commit. Shows your workspace role (Admin needed; `ADD_ME_AS_WORKSPACE_ADMIN` adds you); failures print the fix and the values to enter by hand (dry run + `CONFIRM_CONNECT_ID`) |
| – | Change freeze starts | – | Note the time (UTC): `FREEZE_STARTED_UTC` |
| 2 | After the freeze starts | `V2_2_Backup_Data` | Data + secondary definitions in ADLS; Eventhouse rows as Parquet, in ADX, or both (`KQL_BACKUP_MODE`) |
| 3 | After step 2 | `V2_3_Git_Snapshot` | Git sync check of every workspace (nothing is created until all are synced; commit in Source control, or `COMMIT_PENDING_CHANGES = True`), raw copy saved (`_migration/<id>/raw` + `/git`), one locked branch `migration-snapshot/<id>` + tag `migration-snapshot-<id>`, same name for all workspaces, manifest, READY/BLOCKED |
| 4 | ATM Stage 2.1 (trial capacities: Stage 1.1), at least 24 h (trial: 48 h) before the anchor time | `V2_4_Delete_Fabric_Items` | Git disconnected, Fabric items **hard-deleted** (`HARD_DELETE = True`; READY workspaces only, nothing changed since step 3, all three copies present, every Lakehouse/Warehouse/KQL database has an OK data backup) |
| – | Before capacities are deleted | Rerun the V2_4 dry run to catch auto-created items; delete the notebooks' own workspace content last (see *Assisted Tenant Migration*) | |
| – | ATM Stages 3–5 | Capacities deleted, Microsoft migrates, capacities reprovisioned and workspaces reattached; tenant setting *Users can create Fabric items* restored (Stage 5.10) | |
| 5 | After the move | `V2_5_Restore_From_Git` | Items recreated by Fabric from the snapshot commit; IDs of deleted items found and (on confirmation) fixed |
| 6 | After step 5 | `V2_6_Restore_Data` | Lakehouse files and tables, KQL tables, Warehouse tables and SQL endpoint objects loaded from the step 2 backup run and checked against it (dry run + `CONFIRM_DATA_ID`; settings printed by step 5) |
| – | Sign-off | – | Switch workspaces to the working branch; connections, gateways, item permissions, Direct Lake models |

The step number is the notebook number (`V2_<step>_…`).

## Rehearsal in your test tenant before production (`V2_T_Create_Test_Items`)
Run the whole chain once on test items in your test tenant (same tenant settings, capacity, Key Vault, ADLS, Azure
DevOps and ADX as the real run) before touching production:
1. Create a test workspace on a Fabric capacity, import `V2_T_Create_Test_Items` (Python notebook) and Run all. It
   creates one item of each type (`bkp_test_*`: Lakehouse with files, a Delta table and a shortcut, SQL endpoint
   view, Warehouse table/view/procedure, Eventhouse with a table and a function, Notebook, DataPipeline, Dataflow,
   DataAgent, GraphModel, Ontology) and prints the `WORKSPACE_IDS` line. Reruns don't duplicate anything.
2. Run V2_0 to V2_6 against that workspace. Expected along the way: the notebook's default Lakehouse listed under
   "References to deleted items" (V2_5 FIX ID), KQL Database / Graph Model shown as uncommitted after V2_5, and
   "ALL DATA VERIFIED" at the end of V2_6. Preview types the tenant doesn't have are reported and skipped.
3. Clean up before the ATM deadlines: `DELETE_TEST_ITEMS = True` in its last cell hard-deletes every `bkp_test_*`
   item and the workspace's Dataflow Gen2 staging items (`HARD_DELETE = True`: a soft-deleted item would block
   the migration). Then remove the notebook itself (Delete_Notebooks_By_Pattern) and the empty workspace.
The cleanup deletes by name prefix: never run it in a production workspace.

### Automated rehearsal and green light (`agent/e2e_rehearsal.py`, from VS Code)
The same rehearsal, run unattended from a PC (`az login` as the person who runs the notebooks) and ending in a
report to hand over. One command does everything above in a **new disposable workspace** it creates itself:

Without the optional `azure-identity` package, use Azure CLI 2.54 or newer: the runner needs its `expires_on`
timestamp to renew cached tokens during long rehearsals. It never gives a cached CLI token a new lifetime.

`python agent/e2e_rehearsal.py start --capacity <capacity id> --runner-workspace <id> --member ServicePrincipal:<sp object id>:Contributor`

Or reuse a test workspace you listed in `lab_config.py` (`E2E["REHEARSAL_WORKSPACE_IDS"]`, with the runner workspace
in `E2E["RUNNER_WORKSPACE_ID"]`): `start --workspace <id> --reset`. `--reset` disconnects it from Git and
hard-deletes every non-Power BI item first (without it, a workspace that isn't empty stops the rehearsal and lists
its items). Each rehearsal links the workspace to a new Git folder, `/e2e-rehearsals/<id>/<workspace>`. A reused
workspace is never deleted by `cleanup`.

| Step | What runs | Passes when |
|---|---|---|
| local | the five simulated test suites on this build | all end `RESULT: ALL PASSED` (otherwise nothing is created in Fabric) |
| ws | creates `E2E-REHEARSAL-<id>` on the capacity, adds the service principal | created |
| V2_T | test items and data | all steps succeeded |
| V2_0 | permission check (with write tests) | no FAIL |
| V2_1 | dry run → CONNECT ID → link + first commit | no failed row |
| V2_2 | backup; freeze = the moment this step starts | no failed/partial row |
| V2_3 | snapshot | the workspace is READY |
| V2_4 | dry run → DELETE ID → hard delete | no failed row; no migrated item left |
| V2_5 | dry run → PLAN ID → restore; FIX ID → reference fix; last dry run | nothing left to restore or fix automatically |
| V2_6 | dry run → DATA ID → data load | ALL DATA VERIFIED |
| compare | items before (after V2_T) vs after (after V2_6), by type and name | every item back |

The report, `agent/e2e/<id>/E2E_REPORT.md` (+ `.json`), is **GREEN** only if every check passes and the notebooks
still have the build IDs recorded at the start (listed in the report; each run also checks the build ID the notebook
printed). `verify-build <id> --notebooks out` confirms the release build (`out/`, no lab values) has the same code. Warnings (for example a manual
reference) don't block GREEN but are listed. It also lists what a rehearsal can't cover: the ATM move itself and
its waiting times, production data volumes and copy speed, and item types the test items don't have.
- **Safety:** real runs happen only in the workspace the rehearsal created (its name prefix and ID are checked
  before every real step); V2_4 to V2_6 also check that the snapshot holds only that workspace; inside each
  notebook a guard refuses any other workspace or a `WORKSPACE_MAP`. Confirmation IDs come from the dry run of
  the same step. The runner workspace must be a different one.
- **Stops and resumes:** a failed check stops the chain (`STOPPED at <step>`, with the transcript path). After
  fixing a tenant setting: `resume <id>`. After a code change: a new `start` (the build changed).
- **Clean up:** `cleanup <id> --yes` deletes the rehearsal workspace; then delete the `AGENT_*` notebooks in the
  runner workspace. The ADLS backup, Git branches/tags and ADX databases of the rehearsal stay (remove them by hand
  if you like). Clean up before the ATM deadlines.

### Size and multiple-item rehearsal (explicit opt-in)
The default remains one small set. For four independent sets, with about 1 GiB of persisted data in only the
first Lakehouse, Warehouse and Eventhouse, use:

`python agent\e2e_rehearsal.py start --workspace <allowlisted-workspace-id> --copies 4 --large-mib 1024 --seed-prefix bkp_stress`

Add existing test workspaces to the rehearsal allowlist only with their owner's explicit approval; the owner
grants the backup app Viewer or higher in each workspace. Creating another workspace does not copy those
grants. Run one separate rehearsal per workspace; never combine workspace IDs or change the single-workspace
guards. If a workspace is not empty, inspect the exact inventory and approve its reset before adding `--reset`.

- The four prefixes are `bkp_stress_01` through `bkp_stress_04`. Each has its own nine user-created item types,
  plus the primary KQL database and SQL endpoint: 44 distinct explicit item IDs. Fabric-managed Ontology and
  Dataflow staging items are additional; they never substitute for one of the four explicit copies.
- Copy 1 requests the large dataset. Copies 2-4 remain small. Every copy is measured: active Parquet file bytes
  for Lakehouse/Warehouse (`active_parquet_bytes`), persisted compressed extent bytes for Eventhouse
  (`compressed_extent_bytes`). Row counts and logical/uncompressed size are not accepted as GB-size proof.
- The large measurements must be 95%-110% of the requested target. Each small measurement must be positive
  and below 10% of the large target. Missing measurements, wrong item IDs, failed seed steps or missing/duplicate
  explicit item IDs stop the chain before backup/deletion.
- Completed copies and their measured evidence are saved individually. Resuming the same build retries only
  incomplete copies, rather than regenerating the successful large copy. A code change still needs a new rehearsal.
- The report includes the copy count, target, measured bytes and metric names. These are seed-time measurements;
  final data recovery uses V2_6's file-size, row-count and available NULL-count checks. The recorded profile does
  not prove unlimited scale, production concurrency or the ATM region move.

Two such workspaces start with about 6 GiB of seed data, before backup, staging and ADX copies. Allow hours and
additional compute/storage charges; this option does not resize or reassign the capacity. A previous small-data
GREEN does not certify this new profile or changed notebook code.

The producer uses bounded batches and high-entropy Parquet sources retained under the seeded Lakehouse's
`Files` folder for safe retries; actual workspace storage is therefore higher than the measured stress-table
bytes. It requires compatible Fabric Python Delta transaction APIs and PyArrow, supports classic Delta
checkpoints, and fails explicitly for unsupported V2 checkpoints. A family has a two-hour limit, a maximum of
256 source batches and at most 90 storage-publishing polls; reaching a bound is a failure, not a smaller success.
Serialize runs using the same prefix; use a new prefix for a changed data profile.

If the Fabric kernel lacks the required Delta transaction API, an explicitly approved stress run can add
`--isolated-stress-runtime` to the coordinator command (`ISOLATED_STRESS_RUNTIME = True` in V2_T). This downloads
the pinned `deltalake==0.22.3` wheel from its fixed public URL, checks its SHA-256 before installation, and uses
`pip --no-index --no-deps --target` in a temporary job directory. No shared environment is updated and no
notebook credentials are sent to the package host. Existing PyArrow 16+ and Linux/Windows x86-64 are required.
It refuses to replace an already imported native Delta module. Default is False; small copies do not install
anything. A TLS/download/hash/dependency error stops the job; never bypass certificate or hash checks.

## Service principal permissions (all in one place)
One app registration (`ADO_SP_CLIENT_ID`; Kusto Copy uses the same app unless `KC_CLIENT_ID` names another one).
It signs in only for Azure DevOps and for Kusto Copy. Every other step runs as **you**, the person running the
notebook (Fabric administrator, Admin on each workspace, Key Vault secret reader, ADLS Storage Blob Data
Contributor).

| # | Where | Grant | Needed for | How |
|---|---|---|---|---|
| 1 | Entra | App registration + client secret; **no API permissions** | Everything below | SETUP_DevOps_Access_PAT_or_SP.md, section 2 step 1 |
| 2 | Key Vault | The client secret **Value** as a secret (`ADO_SP_SECRET_NAME` / `KC_CLIENT_SECRET_NAME`) | The notebooks read it as you; the app itself needs no Key Vault role | Key Vault > Secrets > Generate/Import |
| 3 | Azure DevOps organization | User with access level **Basic**; org policies allow service principals; org connected to the same Entra tenant | V2_0–V2_5 DevOps calls (`ADO_AUTH = "service_principal"`) and the Fabric Git link (`GIT_LINK_AUTH = "service_principal"`) | Organization settings > Users > Add users |
| 4 | Azure DevOps project | Member of **Contributors** (Read, Contribute, Create branch, Create tag on the repository) | Snapshot branch and tag (V2_3), branch lock, Fabric commit/update | Same dialog: Add to project > Contributors |
| 5 | Fabric tenant settings | **Developer settings > Service principals can call Fabric public APIs**: Enabled, for the whole organisation or a security group that contains the app | Kusto Copy reading the Eventhouse as the app (V2_2, `KQL_BACKUP_MODE` "adx"/"both") | Admin portal > Tenant settings (Fabric administrator) |
| 6 | Each Fabric workspace with an Eventhouse | **Viewer** (or Contributor) | Kusto Copy export | Workspace > Manage access > Add people or groups > the app |
| 7 | ADX cluster | **Contributor** (Azure role) | V2_2 creates one ADX database per KQL database and becomes its Admin | Cluster > Access control (IAM). Without it: create the databases yourself and add the app as database **Admin** (see the ADX prerequisites) |
| 8 | Storage account `ADLS_ACCOUNT` | **Storage Blob Data Contributor** (container or account) and **Storage Blob Delegator** (account level only) | Kusto Copy staging files and the user-delegation SAS for export and ingest | Storage account > Access control (IAM) |

Not needed: Fabric administrator, workspace Admin/Member, Microsoft Graph or Power BI API permissions, any role on
Key Vault. Rows 5–8 only apply when `KQL_BACKUP_MODE` is `"adx"` or `"both"`; with a PAT (`ADO_AUTH = "pat"`) and
`GIT_LINK_AUTH = "my_account"`, rows 3–4 aren't needed either. V2_0 checks rows 3–4; the first V2_2 run with
Kusto Copy shows any gap in rows 5–8 (its error names the missing role).

## Do you need the Fabric administrator role?
Not for the notebooks themselves. V2_0's check "Fabric administrator (admin APIs)" can FAIL without stopping the
others. The admin APIs are used only for three conveniences:

| Where | What the admin API does | Without Fabric administrator |
|---|---|---|
| V2_0, V2_1 | Shows your role in each workspace | The role shows as unknown; a call you're not allowed to make fails later with a clear error |
| V2_1 `ADD_ME_AS_WORKSPACE_ADMIN` | Adds you as Admin to workspaces you aren't in | Not available: a workspace Admin adds you by hand |
| V2_0 | Reads the Git tenant settings (sync, cross-geography export) | A WARN tells you to check them by hand in the Admin portal |

Needed whatever your role: **Admin on every migrated workspace** (hard delete in V2_4, Git link and disconnect),
read access to the Key Vault secrets and **Storage Blob Data Contributor** on the ADLS container.

For the migration itself a Fabric administrator is still needed for the tenant-level steps: record and disable
"Users can create Fabric items" before Stage 1 (restore it in Stage 5.10), enable the cross-geography Git setting if
needed, clear personal workspaces, and the tenant-wide inventory (admin item list). Simplest: the person who runs
the notebooks is a Fabric administrator. Otherwise a Fabric administrator first makes that person Admin of every
workspace and does the tenant settings.

## Assisted Tenant Migration (ATM): how the notebooks fit
Confirm these requirements against the assisted tenant migration plan approved for your tenant. ATM moves
Power BI items; it **does not keep Fabric items**, and a single remaining Fabric item, including a soft-deleted
one, blocks the migration.

| ATM requirement | What it means here |
|---|---|
| Hard-delete every Fabric item (Stage 1.1 trial, 2.1 production) | V2_4 deletes with `hardDelete=true` (needs workspace **Admin**; permanent). A soft delete keeps the item in the recycle bin for up to 30 days and blocks the migration. The DELETE ID changes if you switch `HARD_DELETE` |
| Inventory at least 7 days before the anchor time | V2_0 and V2_1 weeks before; V2_3 READY/BLOCKED list per workspace |
| Delete on trial capacities ≥ 48 h, production ≥ 24 h before the anchor time; always before the capacity is cancelled or deleted | Plan V2_2 → V2_3 → V2_4 to finish by then. V2_4 refuses a snapshot older than `MAX_SNAPSHOT_AGE_HOURS` (24) |
| Never delete a workspace, or move it off its capacity, before its Fabric items are gone | The notebooks never delete or move workspaces. Don't do it by hand either: the items go to soft delete for 30 days |
| Record, then disable, *Users can create Fabric items* before Stage 1; restore it in Stage 5.10 | This is the change freeze: set `FREEZE_STARTED_UTC` to that time. Restore (V2_5, V2_6) only after Stage 5.10, or with an account in the setting's allowed group, otherwise creating items fails |
| Some items are auto-created even with the setting off (for example DataExploration, Dataflow staging items) | Rerun the V2_4 dry run just before the capacities are deleted: it flags every item created after the snapshot (back it up or hard-delete it by hand). After a real run, its Verify row lists any Fabric item still left |
| Personal workspaces (My workspace) must be cleared too; the Fabric admin may need to take them over | Out of scope for these notebooks; add them to the inventory and clear them by hand |
| Workspace and item IDs are kept by ATM; capacity IDs change | V2_5 finds the same workspace by ID (`TARGET_WORKSPACE_MAP` only if one was recreated) |
| Workspace VNets, private links and managed private endpoints are removed before and recreated after | Storage firewalls that allow Fabric through a managed private endpoint need it recreated before V2_6 reads the backup |
| ADX cluster and ADLS account are Azure resources | Not affected by ATM; keep them until V2_6 has verified the data |


Git is the normal route. Two cases use the raw definitions V2_3 saves in ADLS (`_migration/<snapshot id>/raw`)
instead:

| Case | Setting | What changes |
|---|---|---|
| A workspace can't or shouldn't be linked to Git (cross-geography rule, policy, ownership) | V2_3: `NO_GIT_WORKSPACE_IDS = ["<workspace id>"]` (also in `WORKSPACE_IDS`) | V2_3 skips its Git checks (sync table: *No Git (ADLS route)*), needs an OK V2_2 definition and the raw copy for every item, and creates no branch for it. V2_4 proves nothing changed by comparing each item's live definition with the raw copy's fingerprint (`rawHash`); any difference blocks. V2_5 recreates it from ADLS |
| Azure DevOps is unavailable at restore time | V2_5: `RESTORE_SOURCE = "adls"` | Every workspace is recreated from ADLS; no DevOps calls |

How V2_5 recreates from ADLS:
* Order: Lakehouse, Warehouse, Eventhouse, KQL database, then Notebook, Dataflow, DataPipeline, GraphModel,
  Ontology, DataAgent. Old item IDs inside each definition are replaced with the new IDs as items are created (a
  notebook's default Lakehouse, a pipeline's notebook, the KQL database's Eventhouse). IDs still left over are
  listed by the usual reference scan and fixed with `FIX_STALE_REFERENCES`.
* Creating an Eventhouse also creates its first KQL database (same name): V2_5 applies the saved definition to it.
* If Fabric refuses to create a Lakehouse or Warehouse with its definition, it's created empty (a schema-enabled
  Lakehouse keeps schemas) and the row says *WITHOUT its definition*. A Warehouse created that way has no tables:
  create them (from the Warehouse's saved definition) before V2_6 loads the data.
* Dry run, PLAN ID and resume work as on the Git route. Recreated workspaces have no Git link; link them later
  with V2_1.
* `_migration/<snapshot id>/git/<workspace>/files/` still holds every file of each snapshot commit: push it to any
  Git repository to use Git integration again.

**Not verified in a real tenant yet:** creating each item type with its definition through the API (the Lakehouse
and Warehouse fallbacks exist for that reason), and whether a definition read twice gives the same fingerprint
for every type. If V2_4 reports a change you didn't make, take a new snapshot and check that item.

## Items Git doesn't hold, in a Git-linked workspace (`ALLOW_SECONDARY_ONLY_ITEMS` in V2_3)
Default `False`: such an item blocks its workspace in V2_3 ("not saved in Git"). With `True` (needs
`SAVE_RAW_COPY_TO_ADLS = True`) it's READY with recovery *ADLS definition (V2_5, not in Git)*:
- **V2_4** compares its live definition with the raw copy taken by V2_3 (Git status can't see it) and blocks if it
  changed.
- **V2_5** restores the workspace from Git first, then creates these items from their ADLS definition (raw copy,
  else the V2_2 backup), with IDs inside the definition pointing to the restored items. A rerun creates only what's
  missing and doesn't repeat the Git update.
Everything is in v2: no v1 notebook is used anywhere in the process.

## Dataflow Gen2 staging items (`StagingLakehouseForDataflows_…`, `StagingWarehouseForDataflows_…`)
Fabric creates these for Dataflows Gen2 and creates them again when a Dataflow Gen2 runs. They're not in Git and
hold only temporary data, so:
- **V2_2** leaves them out of the backup (it lists them once).
- **V2_3** marks them READY ("Dataflow Gen2 staging: Fabric recreates it"); they never block a workspace and get no
  raw copy.
- **V2_4** still **hard-deletes** them: under ATM any Fabric item left blocks the migration. A staging item that
  appears after the snapshot (a Dataflow ran) is added to the deletion plan instead of blocking it.
- **V2_5** doesn't restore them; **V2_6** has nothing to load. They come back the first time a restored Dataflow
  Gen2 runs.

## Data
Git never holds data. The only copy of the data is the backup run of step 2 (`V2_2_Backup_Data`, after the
freeze): Lakehouse `Files/` + `Tables/`, Warehouse tables (Delta), KQL tables (see below), SQL endpoint
views/functions/procedures. Step 3 refuses READY unless each of these backed up OK; step 4 checks again
that the files are still in ADLS (`data_manifest.csv`); step 6 loads them and compares files, sizes and row counts.
Warehouse tables and SQL objects are written as scripts by default (`WAREHOUSE_LOAD` / `SQL_OBJECTS = "run"` runs
them from the notebook through Fabric's SQL connector; no separate ODBC driver or SQL token is needed).
Warehouse column-mapped tables use their physical Parquet names with an explicit logical-column INSERT.
The loader reads only the Delta log's active files, leaves nonempty tables alone, and verifies row counts plus
column NULL counts when the backup provides them.

### Lakehouse copy speed (`LAKEHOUSE_COPY_METHOD` in V2_6)
- `"fastcp"` (default): each Delta table folder (and `Files/` when it holds no Delta table) is copied in bulk with
  azcopy through `notebookutils.fs.fastcp`, straight from ADLS into OneLake: first everything except `_delta_log`,
  then the `_delta_log` folders, so a table only becomes visible when its data is complete. Built for tables of
  several GB; the per-file copy sends every byte through the notebook VM.
- Copy, not move: the backup stays in ADLS until the restore is signed off (it's the only copy of the data).
- Byte for byte: Delta history, versions and file layout are kept, and V2_6 still checks every file and size.
  No Spark rewrite: re-reading and rewriting the tables would be slower per byte and lose the Delta history.
- Safe on reruns: `--overwrite=false`, so a file that is already in the Lakehouse is never replaced; a different-size
  leftover of an interrupted run of this notebook is replaced by the per-file copy.
- Before the first copy V2_6 makes a tiny test copy inside the ADLS migration folder (removed straight away) to learn
  how fastcp lays out folders and to check the `_delta_log` exclusion. If fastcp isn't usable, an Info row says why and
  the whole Lakehouse is copied file by file. Files fastcp didn't place are also copied file by file (the log row
  says "N by fastcp, M file by file").
- `"per_file"`: the original copy only (`MAX_PARALLEL_FILES` at a time).
- Not verified yet in a tenant: fastcp from ADLS into OneLake in another region (Microsoft documents that fastcp
  doesn't copy between OneLake regions; ADLS to OneLake should not be affected). Test it in the lab with the ADLS
  account and the workspace capacity in different regions; the fallback covers a failure.

### What each item's backup folder holds (V2_2 run folder)
| Folder | Holds | Notes |
|---|---|---|
| `<name>.Lakehouse__<id>` | definition, `data/Files`, `data/Tables`, `lakehouse.json`, `data_manifest.csv` | Shortcuts are not copied (their targets are backed up where they live) |
| `<name>.Warehouse__<id>` | definition, `data/Tables/<schema>/<table>` (Parquet + `_delta_log`) | V2_2 reads each table's Delta log and copies every data file it lists, also files a folder listing doesn't show. A table whose log lists no data files is reported: it is empty, or Delta log publishing is behind or paused (`SELECT name, DATA_LAKE_LOG_PUBLISHING_DESC FROM sys.databases` must say AUTO) |
| `<name>.Eventhouse__<id>` | definition only | The Eventhouse is the container; its data lives in the KQL databases |
| `<name>.KQLDatabase__<id>` | definition, `database_schema.kql`, `data/<table>/` Parquet (parquet/both), `kusto_copy/` logs and `_destination.json` (adx/both) | One folder per KQL database of the Eventhouse |
| `<name>.GraphModel__<id>`, `Ontology`, `DataAgent`, `Notebook`, `DataPipeline`, `Dataflow` | definition only | No data of their own. A graph model is built from Lakehouse tables: after V2_6 restores those tables, open the graph model and load (refresh) it again |
| `<name>.SQLEndpoint__<id>` | `sql/` one `.sql` file per view, function and procedure, plus `permissions.csv` | The endpoint itself comes back with its Lakehouse |

### Eventhouse data: Parquet or Azure Data Explorer (`KQL_BACKUP_MODE` in V2_2)
| V2_2 setting | Where the rows are | V2_6 (`KQL_RESTORE_SOURCE = "auto"`) |
|---|---|---|
| `"parquet"` (default) | Parquet files in the run folder (`<item>/data/<table>/`), exported by the Eventhouse with your identity | `.ingest` of the files into the new Eventhouse |
| `"adx"` | An Azure Data Explorer cluster, copied by Kusto Copy as the service principal (reruns copy only new rows); `<item>/kusto_copy/_destination.json` records cluster, database and tables | `.set-or-append` from ADX into the new Eventhouse (cross-cluster query with **your** identity: Viewer on the ADX database) |
| `"both"` | Both | Parquet; ADX if the export isn't OK (or force with `KQL_RESTORE_SOURCE = "adx"`) |

In every mode the schema script (`database_schema.kql`) is saved too. V2_3 and V2_4 accept either copy as the KQL
data backup. ADX setup: see *Prerequisites for the ADX copy* below (also the markdown above step 7 in V2_2).
`.set-or-append` from another cluster suits tables up to a few GB each; for bigger tables copy back with Kusto Copy
(ADX as source, the new Eventhouse as destination) and rerun V2_6 (tables that already hold rows are left alone).

**ADX database created but empty?** Kusto Copy (`KC_COPY_MODE = "BackfillAndNew"`) keeps in its staging folder
(`<KC_STAGING_FOLDER>/<adx host>/<adx database>/<KQL database id>/<table>`) what it has already copied, and a
rerun copies only newer rows. If the ADX database was deleted and created again (or a table dropped or cleared),
an older V2_2 build copied nothing. V2_2 now resets the staging folder of every ADX table that is missing or
empty and copies the full history again (no duplicates: the table is empty). Just rerun V2_2 with the current
build; the Kusto Copy rows say "old Kusto Copy progress reset". Each table's full Kusto Copy log is in ADLS:
`<run>/<workspace>/<KQL database>/kusto_copy/<table>.log`.

### Prerequisites for the ADX copy (`KQL_BACKUP_MODE = "adx"` or `"both"`)
Do these once, before the first V2_2 run. "The Kusto Copy service principal" is the app registration in
`KC_CLIENT_ID`; when that setting is empty (default) it is the **same app as the DevOps service principal**
(`ADO_SP_CLIENT_ID`, secret `ADO_SP_SECRET_NAME` in Key Vault). Kusto Copy runs inside V2_2 and signs in as that
app, not as you, so the app needs these roles.

| # | Who | Role | Where (scope) | Why |
|---|---|---|---|---|
| 1 | Kusto Copy service principal | **Contributor** (Azure role) | The ADX cluster: Azure portal > cluster > Access control (IAM) | V2_2 creates one ADX database per KQL database (`ADX_CREATE_DATABASES = True`) and becomes Admin on each one it creates |
| 2 | Kusto Copy service principal | **Storage Blob Data Contributor** | Container `ADLS_CONTAINER` (or the account) | Writes the staged export files under `KC_STAGING_FOLDER` and its progress |
| 3 | Kusto Copy service principal | **Storage Blob Delegator** | Storage account `ADLS_ACCOUNT` (account level; a container-level assignment doesn't work) | Creates the user-delegation SAS URLs that the Eventhouse (export) and ADX (ingest) use. The delegation key is requested at account level, so a Contributor role on the container alone isn't enough |
| 4 | Kusto Copy service principal | **Viewer** (or Contributor) | Each Fabric workspace that holds an Eventhouse to copy | Reads the KQL databases and runs the export. The Fabric tenant setting **Service principals can call Fabric public APIs** (Developer settings) must include the app |
| 5 | The person who runs V2_6 | **Viewer** on each ADX database | Automatic: V2_2 adds `ADX_VIEWERS` (default `"me"` = whoever runs V2_2) to each database it creates | V2_6 reads ADX back into the new Eventhouse with your identity (cross-cluster query). If someone else runs V2_6, add them to `ADX_VIEWERS` |
| 6 | Network | — | Storage firewall, ADX, notebook | The Eventhouse and ADX must reach the storage account; the notebook must reach `github.com` (download), `management.azure.com` and the ADX cluster |

If Contributor on the cluster isn't allowed: set `ADX_CREATE_DATABASES = False`, run V2_2 as a dry run (its table
lists the database names), create those databases, and make the app **Admin** on each (Azure portal > cluster >
Databases > the database > Permissions > Add, or `.add database ['<db>'] admins ('aadapp=<client id>;<tenant id>')`)
and yourself Viewer (`.add database ['<db>'] viewers ('aaduser=<your UPN>')`).

### How the ADX side is organised
* **One ADX database per KQL database**, named by `ADX_DATABASE_PATTERN` (default
  `{workspace}__{database}__{id8}`: workspace name, KQL database name, first 8 characters of its item ID, made safe
  for ADX, e.g. `Sales_WS__SalesKQL__1b2c3d4e`). Readable, unique across workspaces, and the tables keep their
  original names, so functions and policies in `database_schema.kql` still match on restore.
* V2_2 finds the cluster in Azure from `ADX_CLUSTER_URI` (or `ADX_SUBSCRIPTION_ID` + `ADX_RESOURCE_GROUP`), checks
  the app may create databases, and creates missing ones with unlimited retention (`ADX_SOFT_DELETE_PERIOD = ""`:
  Kusto Copy keeps the original ingestion times, so a short retention would drop old rows at once) and a 1-day hot
  cache (`ADX_HOT_CACHE_PERIOD`). Reruns reuse the same database.
* Every real run saves `adx_inventory.csv` in its run folder: workspace, KQL database, ADX database, created or
  existing, tables, rows, status. The dry run shows the same table with *would create*.
* **Restore needs no names:** each KQL database's `kusto_copy/_destination.json` records cluster, database and
  tables, and V2_6 reads it.
* `ADX_DATABASE_MAP` is only for a fixed name for a specific KQL database.

## Git sync check (V2_3)
V2_3 starts with a read-only table: per workspace, the Git link, uncommitted changes, incoming changes and the
DevOps branch head, with a Fix column. **Synced** = Source control shows 0 changes and 0 updates. The branch is
shared, so a head that moved only through other workspaces' folders still counts as synced. With
`REQUIRE_ALL_WORKSPACES_SYNCED = True` (default) the snapshot is taken only when every workspace is synced;
otherwise nothing is created. V2_3 doesn't commit for you unless `COMMIT_PENDING_CHANGES = True`.

## After V2_5: uncommitted items, FIX ID and the Git connection name
**Items shown as uncommitted after the restore (for example KQL Database, Graph Model).** After "Update from Git"
Fabric rewrites some definitions with the new item IDs (a KQL database's embedded queryset holds its own database
ID; a Graph Model holds its data source IDs). The workspace then shows them as uncommitted. Expected and harmless:
V2_5 logs them as Info under "Verify Git sync" and they don't block V2_6. Don't update from Git again (it would put
the old IDs back). Once the reference fix and V2_6 are done, commit them in Source control to the restore branch,
then switch to the team's working branch.

**FIX ID.** V2_5's section 4 lists every ID of a deleted item still written in a restored definition, for example
a notebook's default Lakehouse (`notebook-content.py`) or a KQL database's own old ID
(`EmbeddedRealTimeQueryset.json`). Rows marked "automatic (with confirmation)" are fixed by running V2_5 again with
`DRY_RUN = False`, `FIX_STALE_REFERENCES = True` and `CONFIRM_FIX_ID = "<the FIX ID>"`. The Git restore steps are
skipped on that rerun (already done); only the listed definitions are updated with the new IDs. Those items then
also show as uncommitted (commit them as above). Rows marked "manual" (Power BI items) are rebound by hand. Run the
fix before V2_6, and rerun V2_5 in dry run afterwards: the list should be empty.

**`GIT_CONNECTION_NAME` empty (default).** With `GIT_LINK_AUTH = "service_principal"` the notebooks look for a
Fabric connection named `fabric-migration-<organization>-<project>-<repository>` (the one V2_1 created, for example
`fabric-migration-contoso-migration-fabric-repo`) and reuse it. If it doesn't exist (for example it was
removed with the old capacity setup), the plan says "created when you confirm the plan" and V2_5 creates it, signing
in as `ADO_SP_*`. If a connection with that name exists but isn't "Azure DevOps – Source control" with a service
principal, V2_5 stops and asks for another name. Connections aren't Fabric items, so V2_4 doesn't delete
them; the default name finds the same one again (V2_0 shows whether it exists). Not used with `"my_account"`; `"connection_id"` uses
`GIT_CONNECTION_ID` instead.

## Which workspace after the move (V2_5, V2_6)
A home-region move keeps the workspace IDs, so V2_5 uses the same ID. If that ID no longer exists, it looks for
the workspace with the same name; the plan's *Found by* column shows which, and the PLAN ID covers it. V2_5 records
the target in `restore_state.json` and V2_6 uses the same one. `WORKSPACE_MAP` (end of the settings) is only for a
workspace recreated under another name.

## Lakehouses with schemas
A schema-enabled Lakehouse keeps its tables in `Tables/<schema>/<table>`. V2_2 copies every schema and records the
layout in `<item>/lakehouse.json`. V2_3 blocks the Lakehouse if its Git definition lost `defaultSchema`
(`lakehouse.metadata.json`), because Git would then recreate it without schemas. V2_6 loads the tables only into a
Lakehouse with the same layout (schemas or no schemas) and blocks otherwise. Schema shortcuts are skipped like other
shortcuts.

## Azure DevOps sign-in (`ADO_AUTH`, same setting in V2_0 to V2_5)
Step-by-step setup for both options, and for the Fabric Git link: **`SETUP_DevOps_Access_PAT_or_SP.md`**.

The notebooks read the repository, create the snapshot branch and tag, and lock branches through the Azure DevOps
REST API. Both credentials come from Key Vault (`KEY_VAULT_URL`) and are never shown.
- `ADO_AUTH = "pat"` (default): `ADO_PAT_SECRET_NAME` holds a personal access token (52 or 84 characters) with
  Code (Read & write) and Project and Team (Read), created in the same organization.
- `ADO_AUTH = "service_principal"`: `ADO_SP_TENANT_ID`, `ADO_SP_CLIENT_ID` and `ADO_SP_SECRET_NAME` (the client
  secret **Value**, not its Secret ID). The app must be added to the DevOps organization (Organization settings >
  Users, Basic access) and to the project's Contributors group, and the organization must be connected to the
  app's Entra tenant. The Entra token is renewed automatically during long runs.
- This only covers the notebooks' DevOps calls. Fabric's own Git link (V2_1, V2_5) is chosen separately with
  `GIT_LINK_AUTH`: `"service_principal"` (default: a Fabric connection "Azure DevOps – Source control" signing in
  as the service principal in `ADO_SP_*`, reused by name or created when you confirm), `"my_account"` (your own
  DevOps sign-in) or `"connection_id"` (`GIT_CONNECTION_ID`). See SETUP_DevOps_Access_PAT_or_SP.md, section 3.
- V2_0 shows the sign-in method, recognises a wrong secret (for example a client secret stored as a PAT) and names
  the fix.

## Git folder must exist before an API link
`GitProviderResourceNotFound` on connect can mean the Git folder doesn't exist in the branch: the connect API
doesn't create it (the workspace settings screen does). V2_1 creates each missing folder first with a small
`README.md` in it (one commit per workspace, with the notebook's DevOps sign-in) and treats a folder holding only
that file as empty.

## Workspace screens after an API link ("Can't fetch branches")
Git sign-in in Fabric is per person and per workspace. After V2_1 links a workspace with the service principal
connection, your own sign-in for that workspace can still be "Automatic" (your personal Azure DevOps account). Then
Source control and Workspace settings > Git integration show "Can't fetch branches" or "connection ... was removed",
and you can't disconnect from the screen, although the link and the commit worked.

**Fix (tested in the lab):** open the workspace > Source control > account, and choose the Fabric connection
`fabric-migration-<organization>-<project>-<repository>` (or your `GIT_CONNECTION_NAME`). Each person who uses
Source control does this once per workspace.

The notebooks don't change your Git sign-in by themselves. V2_1 reports it (column "Your Git sign-in") and leaves a
linked, synced workspace alone. `SWITCH_MY_GIT_CREDENTIALS = True` lets V2_1 switch it through the API as part of
the confirmed plan; that path isn't proven to fix the screens. V2_3, V2_4 and V2_5 commit, disconnect and update
with YOUR OWN Git sign-in, so V2_0 checks it ("Git status" row) and each notebook stops with this fix if Fabric
rejects it. Nothing is changed in that case.

## Redo a link test
1. Fix your Git sign-in as above, then Workspace settings > Git integration > Disconnect workspace.
2. Give V2_1 an empty folder: delete `<folder>` in the branch (Azure DevOps > Repos > Files), or use a new
   `ADO_BRANCH`.
3. Rerun V2_1 (dry run, then `CONFIRM_CONNECT_ID`). The Fabric connection is found by name and reused.

## Where to run the notebooks
Keep the migration notebooks in a separate workspace that V2_4 doesn't clear. V2_1 warns and V2_4 stops if it runs
inside a workspace it would clear. With Assisted Tenant Migration that workspace is migrated too, and its notebooks
are Fabric items: hard-delete them last (`tools/Delete_Notebooks_By_Pattern.py` from another workspace, or by hand,
making sure the deletion is permanent and not a soft delete), keep the .ipynb files outside Fabric, and import them again after
the capacities are back.

## Cross-geography Git link
`CrossGeographyOperationIsNotAllowed` on connect: the workspace's capacity and the DevOps organization are in
different geographies. A Fabric administrator enables Tenant settings > Git integration > "Users can export items
to Git repositories in other geographical locations", then rerun V2_1 (it resumes). V2_0 reports this setting and
each capacity's region. Enable it before the move: the new home region can create the situation later.

## One Entra tenant
The home-region move keeps the same tenant, and so does the backup: Fabric, the Azure DevOps organization and the
service principal must all be in the Fabric tenant. V2_0 checks it; V2_1 stops before linking otherwise
(`GitProviderResourceNotFound` is the typical symptom). `ALLOW_CROSS_TENANT` is for a future scenario only.
Details: `SETUP_DevOps_Access_PAT_or_SP.md` 3b.

## Test first
Run T, then 0–6 (with real data in the Lakehouse, Warehouse and KQL database) on a test workspace connected to a test repository (`V2_T_Create_Test_Items` creates test items), moved
to a capacity in another region.

## Known limits: Warehouse data and SQL execution
- Flat Warehouse Delta tables with valid `name`/`id` column mapping and supported scalar types use typed
  `OPENROWSET` with physical column names, then `INSERT` into the logical target columns. Renamed `WITH` column
  paths are not used: Fabric can return NULLs instead of the expected Parquet values. The physical mapping,
  active files and verification statistics are covered by the DATA ID.
- Deletion vectors, partitioned mapped tables, unsupported types, incomplete mapping/statistics or an unreadable
  Delta snapshot remain **manual load** work and prevent `ALL DATA VERIFIED`. They are never treated as empty
  tables. Plain [COPY INTO requires matching Parquet column names](https://learn.microsoft.com/en-us/sql/t-sql/statements/copy-into-transact-sql)
  and is used only for the unmapped path.
- `WAREHOUSE_LOAD` and `SQL_OBJECTS` default to `"script"`: V2_6 writes SQL files for the operator, not executes
  them. `"run"` executes the eligible operations through Fabric's SQL connector after a new dry run and DATA ID;
  it does not make unsupported layouts safe to load. An unattended rehearsal must deliberately select both run
  modes; pending manual SQL work is not a completed end-to-end rehearsal. V2_0 probes both SQL connections with
  read-only SELECT batches, not just driver availability.
- Restoring an Ontology can create a new GraphModel, Eventhouse and KQL database while the ADLS-only route also
  recovers the original backing items. The rehearsal reports the extra items as a warning, not a deletion target.
  Review the Ontology's backing-store links before cleanup. The empty test Ontology does not validate populated
  Ontology bindings or its business data.

## Verified in the lab
The functional Python baseline completed the guarded chain with GREEN lab reports. It exercised permission
checks, Git linking and snapshot, hard deletion, Git plus non-Git definition restore, reference repair and data
restore. The seed dataset verified 500 Warehouse rows (including every seeded value), 1,000 Lakehouse rows,
1,000 KQL rows and the restored SQL endpoint view. Warehouse name-mode column mapping used integer, string,
decimal and date columns. This public edition generalizes documentation/test identifiers, adds diagnostic
redaction and cross-drive report handling, and regenerates
its [BUILD_IDS.txt](BUILD_IDS.txt). The private lab reports do not certify these new public build IDs. See the
[source provenance](../../SOURCE_PROVENANCE.json) and rehearse the public build in your own test tenant.

Two separate GB-scale workspace rehearsals also passed, each with four independent sets: 44 explicit item
IDs, one large Lakehouse, Warehouse and Eventhouse, and three small copies of each. Each large stress table
held 499,712 rows and about 1 GiB by its measured active-Parquet or compressed-extent metric. Backup, hard
deletion, definition/reference restore and V2_6 data verification passed in each run; all 56 non-staging
baseline items returned. Each workspace's four Ontologies generated 12 additional backing items, reported
as a warning rather than silently discarded. These are same-region, single-workspace rehearsals, not proof
of a combined multi-workspace manifest, populated Ontology bindings or unlimited scale.

## Not verified yet
- Populated preview items, application-specific bindings and references beyond those in the seed dataset.
  Preview item definitions round-tripped, but that is not proof of every preview feature's runtime behavior.
- Which additional references Fabric rebinds on update from Git; the reference scan covers what it doesn't.
- Switching a restored workspace from the restore branch back to the working branch.
- The ADX-to-Eventhouse restore route (the rehearsal restored the Parquet copy), the unmapped Warehouse
  `COPY INTO` route, and data types/layouts not covered by the seed dataset.
- Lakehouse tables in a non-`dbo` schema showing up after the file copy.
- `notebookutils.fs.fastcp` production-scale throughput and cross-region ADLS-to-OneLake copies. Its layout,
  exclusion flags and same-region GB-size recovery were exercised, not a production performance benchmark.
- The ATM tenant move, its waiting periods and your test tenant-specific configuration. your organization must rehearse in its
  own tenant before production.
