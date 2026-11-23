# Fabric migration (v2) – step 4: delete the Fabric items

**This notebook permanently deletes Fabric items (and the data in Lakehouses, Warehouses and KQL databases).**
Use it only after step 3 (`V2_3_Git_Snapshot`) marked the workspace READY. Power BI items are never touched.

**It refuses to delete anything in a workspace unless**
- the manifest is intact (hash check), not older than `MAX_SNAPSHOT_AGE_HOURS`, and the workspace is READY in it;
- the locked snapshot branch still points at the snapshot commit;
- the workspace's Git status shows no change since the snapshot, and its Git head is still the snapshot commit;
- every Fabric item in the workspace now is in the manifest (nothing created since the snapshot);
- the secondary backup run is still in ADLS;
- you confirmed the exact plan: dry run first, then `CONFIRM_DELETE_ID`.

**Order of operations per workspace:** disconnect Git first (so the deletions can never be committed to the branch),
then delete items: consumers first (agents, pipelines, notebooks), storage last (Warehouse, KQL databases,
Eventhouses, Lakehouses). SQL analytics endpoints go with their Lakehouse.

Deleted items may stay in the workspace recycle bin. The tenant move requires them gone too; follow Microsoft's
guidance for the region move (purge soft-deleted items) after this notebook.
