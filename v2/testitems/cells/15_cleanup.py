DELETE_TEST_ITEMS = False
HARD_DELETE = True               # False = soft delete (recoverable, but a soft-deleted item blocks the migration)
DELETE_DATAFLOW_STAGING = True   # also StagingLakehouseForDataflows_* / StagingWarehouseForDataflows_* of this workspace

STAGING = re.compile(r"^Staging(Lakehouse|Warehouse)ForDataflows(_\d+)?$", re.IGNORECASE)
if DELETE_TEST_ITEMS:
    # SQL endpoints and KQL databases go with their Lakehouse and Eventhouse; pipelines first, they reference notebooks.
    order = ["DataPipeline", "Notebook", "Dataflow", "DataAgent", "GraphModel", "Ontology", "Warehouse",
             "Lakehouse", "Eventhouse"]
    items = [i for i in fabric_list(f"/workspaces/{WS}/items") if i.get("type") in order and (
        i.get("displayName", "").lower().startswith(PREFIX.lower() + "_")
        or (DELETE_DATAFLOW_STAGING and STAGING.match(i.get("displayName", ""))))]
    mode = "hard" if HARD_DELETE else "soft"
    for item in sorted(items, key=lambda i: order.index(i["type"])):
        try:
            fabric("DELETE", f"/workspaces/{WS}/items/{item['id']}" + ("?hardDelete=true" if HARD_DELETE else ""))
            print(f"  deleted ({mode}) {item['type']} {item['displayName']}")
        except HttpError as exc:
            print(f"  ! {item['type']} {item['displayName']}: {exc}")
    print(f"{len(items)} test item(s) processed ({mode} delete).")
else:
    print("Nothing deleted. Set DELETE_TEST_ITEMS = True in this cell and run it to remove the test items.")
