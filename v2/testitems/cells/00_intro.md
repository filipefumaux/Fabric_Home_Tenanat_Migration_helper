# V2_T – Create test items (run in your test tenant before production)

> **AS-IS - independent personal/community project.**
> These notebooks are provided **AS-IS**, without warranty of any kind, express or implied.
> They are a personal/community contribution, not a Microsoft product or service, and are not published on
> behalf of, endorsed, supported or warranted by Microsoft. They do not represent or bind Microsoft and
> create no obligation or commitment by Microsoft. Product names, APIs and documentation are technical
> references only. Use at your own risk and validate in your own environment before production or deletion.

Creates one test item of each type the v2 notebooks cover, with a little data, in **one test workspace**. Run the
whole chain V2_0 to V2_6 against that workspace in your test tenant before touching production: same tenant
settings, capacity, Key Vault, ADLS, Azure DevOps and ADX as the real run.

| Item | Name | Test content |
|---|---|---|
| Lakehouse | `bkp_test_lakehouse` | `Files/raw/` (CSV, nested text file), Delta table `bkp_test_sales` (1,000 rows), a shortcut `Files/bkp_test_shortcut` (the backup must skip it) |
| SQLEndpoint | created with the Lakehouse | view `dbo.bkp_test_view` |
| Warehouse | `bkp_test_warehouse` | table `dbo.bkp_test_orders` (500 rows), a view and a stored procedure |
| Eventhouse + KQL database | `bkp_test_eventhouse` | table `bkp_test_events` (1,000 rows) and a function |
| Notebook | `bkp_test_notebook` | two cells, default Lakehouse = the test Lakehouse (V2_5 lists it under "References to deleted items") |
| DataPipeline | `bkp_test_datapipeline` | a Notebook activity that runs the test notebook |
| Dataflow | `bkp_test_dataflow` | one Power Query query (empty dataflow if the definition is rejected) |
| DataAgent, GraphModel, Ontology | `bkp_test_dataagent`, `bkp_test_graphmodel`, `bkp_test_ontology` | empty items (preview types; not available in every tenant) |

### How to use
1. Create a **test workspace on a Fabric capacity** (F SKU or trial) with a clear name, for example
   `migration-test-<date>`. Import this notebook into it (Language: **Python**, not PySpark). Leave
   `TARGET_WORKSPACE_ID` empty to use the notebook's own workspace.
2. **Run all.** About 5 to 15 minutes, mostly waiting for the Warehouse, the SQL endpoint and the Eventhouse.
   Running it again doesn't create duplicates or add rows twice.
3. The last cell prints the `WORKSPACE_IDS` line for the V2 notebooks. Then run, in order: V2_0, V2_1 (links the
   workspace to Git), V2_2, V2_3, V2_4 (dry run, then the real hard delete), V2_5, V2_6. Check that V2_6 ends
   with "ALL DATA VERIFIED".
4. Clean up **before the ATM deadlines**: every Fabric item in the tenant, test items included, must be
   hard-deleted (a soft-deleted item blocks the migration). Set `DELETE_TEST_ITEMS = True` in the last section and
   run that cell: it hard-deletes every item whose name starts with `bkp_test_`, plus the Dataflow Gen2 staging
   items of this workspace when `DELETE_DATAFLOW_STAGING = True`. Keep this notebook itself until the end, or delete
   it with Delete_Notebooks_By_Pattern.

Each step is independent: if one item type isn't available in your tenant, it's reported and the rest continue.
Don't run it in a production workspace: the cleanup deletes by name prefix.
