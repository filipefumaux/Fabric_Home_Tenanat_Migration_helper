# V2_2 – Back up definitions and data to ADLS (secondary copy)

> **AS-IS - independent personal/community project.**
> These notebooks are provided **AS-IS**, without warranty of any kind, express or implied.
> They are a personal/community contribution, not a Microsoft product or service, and are not published on
> behalf of, endorsed, supported or warranted by Microsoft. They do not represent or bind Microsoft and
> create no obligation or commitment by Microsoft. Product names, APIs and documentation are technical
> references only. Use at your own risk and validate in your own environment before production or deletion.

Run this **after the change freeze starts and before `V2_3_Git_Snapshot`**. The workspace's own Git link
(V2_1) is the primary copy of the definitions; this notebook saves the **secondary copy of the definitions and
all the data** in ADLS. V2_3 checks this run, V2_4 refuses to delete an item whose backup isn't OK, and V2_6
loads the data back after the move.

| Item type | Saved in ADLS | Restored by |
|---|---|---|
| Notebook, DataPipeline, Dataflow, DataAgent, GraphModel, Ontology | definition | V2_5 (Git); this copy is the fallback |
| Lakehouse | definition, `Files/` and `Tables/` (Delta, including every schema of a schema-enabled Lakehouse; the layout is saved in `lakehouse.json`). Shortcuts: definition only | V2_6 |
| Warehouse | definition (SQL project), tables as Delta folders | V2_6 |
| Eventhouse / KQL database | definition, schema script, and the rows as chosen in `KQL_BACKUP_MODE` | V2_6 |
| SQLEndpoint | views, functions, procedures and permissions as scripts | V2_6 |

### Eventhouse data: `KQL_BACKUP_MODE`
| Mode | Where the rows go | Restore (V2_6) | Needs |
|---|---|---|---|
| `"parquet"` (default) | Parquet files in ADLS, exported by the Eventhouse | `.ingest` from ADLS | Runner: Storage Blob Data Contributor (the Eventhouse writes with your identity) |
| `"adx"` | An Azure Data Explorer cluster, with Kusto Copy (reruns copy only new rows) | `.set-or-append` from ADX into the new Eventhouse | ADX cluster and databases; service principal (step 7); runner: Viewer on the ADX databases |
| `"both"` | Both | Parquet first, ADX as fallback | Both |

### Before the first run
1. **Python notebook** (not PySpark), in a workspace that is **not** being migrated. Run it as a person with
   **Contributor or higher** on every listed workspace.
2. **ADLS Gen2**: same storage account and container as V2_3–V2_6. Runner: **Storage Blob Data Contributor**.
3. Set `FREEZE_STARTED_UTC` (when nobody changes items any more). The run refuses to start before it.
4. Keep `DRY_RUN = True` first: it checks access and reports sizes without writing. Then `DRY_RUN = False`.
5. At the end the notebook prints the settings to paste into V2_3 (`SECONDARY_BACKUP_RUN_ID`, `FREEZE_STARTED_UTC`).

### What isn't backed up
* Data behind shortcuts (only the shortcut definition), data in Dataflow staging.
* KQL materialized views' stored results (their definitions are in the schema script), and rows ingested while
  the export runs (that's why the freeze comes first).
* Item permissions, workspace roles, connections and gateway credentials, sensitivity labels.
