log_df = pd.DataFrame(RESULTS, columns=["Workspace", "WorkspaceId", "Item", "Type", "ItemId", "Step", "Status",
                                        "Files", "Bytes", "Rows", "Detail"])
print("Backup result by step:")
show(log_df.pivot_table(index="Step", columns="Status", values="ItemId", aggfunc="count", fill_value=0))
problems = log_df[~log_df.Status.isin(["OK", "Unchanged", "Dry run", "Not applicable"])]
print(f"\n{len(problems)} rows need attention:")
show(problems.drop(columns=["WorkspaceId", "ItemId"]))
data_bytes = human(log_df[log_df.Step.isin(["Data", "KQL export"])].Bytes.sum())
print(f"\nData {'to copy (estimate, KQL uncompressed)' if DRY_RUN else 'copied'}: {data_bytes}")
print("API calls:", dict(CALLS), "| retried:", dict(RETRIES))

if not DRY_RUN:
    adls_write(f"{RUN_PREFIX}/backup_log.csv", log_df.to_csv(index=False))
    adls_write(f"{RUN_PREFIX}/_run_completed.json", json.dumps({
        "runId": RUN_ID, "finishedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "items": len(ITEMS), "problems": len(problems), "kqlBackupMode": KQL_BACKUP_MODE,
        "freezeStartedUtc": FREEZE_STARTED_UTC}, indent=2))
    print(f"\nBackup folder: abfss://{ADLS_CONTAINER}@{ADLS_ACCOUNT}.dfs.core.windows.net/{RUN_PREFIX}/")
    print("\nNext: V2_3_Git_Snapshot with these settings" + (" (fix the rows above first: V2_3 blocks items whose "
          "backup isn't OK)" if len(problems) else "") + ":\n")
    print(f'ADLS_ACCOUNT = "{ADLS_ACCOUNT}"\nADLS_CONTAINER = "{ADLS_CONTAINER}"\nADLS_ROOT = "{ADLS_ROOT}"\n'
          f'SECONDARY_BACKUP_RUN_ID = "{RUN_ID}"\nFREEZE_STARTED_UTC = "{FREEZE_STARTED_UTC or "<set the freeze time>"}"')
else:
    print("\nDry run finished. Review the sizes above, set DRY_RUN = False and run all cells again.")
