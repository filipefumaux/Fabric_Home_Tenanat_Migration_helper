## 6. Delete the test items (optional, required before the migration)
Hard-deletes **every item in the target workspace whose name starts with the prefix** (`bkp_test_` by default) and,
with `DELETE_DATAFLOW_STAGING = True`, the Dataflow Gen2 staging items Fabric created in this workspace. It doesn't
run unless you set `DELETE_TEST_ITEMS = True`. With `HARD_DELETE = True` (default) the items are gone for good and
don't sit in the recycle bin, where they would block the tenant migration. Hard delete needs the workspace Admin
role.
