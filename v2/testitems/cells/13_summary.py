result = pd.DataFrame(STEPS)
try:
    display(result)
except NameError:
    print(result.to_string())
failed = result[result.Status.isin(["Failed", "Check"])]
print(f"\n{len(failed)} step(s) need attention." if len(failed) else "\nAll steps succeeded.")
print("\nPaste this into the settings cell of V2_0 to V2_3 (V2_4 to V2_6 take the workspaces from the snapshot):")
print(f'WORKSPACE_IDS = ["{WS}"]')
if LARGE_DATA_MIB or MEASURE_DATA_SIZE:
    print("\nSEED_METRICS (physical bytes, no logical-size estimates):")
    print(json.dumps(SEED_METRICS, indent=2, sort_keys=True))
    incomplete = {k: v for k, v in SEED_METRICS.items() if v["status"] != "OK"}
    if incomplete or len(failed):
        raise SeedError("Test-item seeding/measurement is incomplete. "
                        + json.dumps(incomplete, sort_keys=True) + "\n"
                        + "\n".join(str(s["Detail"]) for s in STEPS if s["Status"] in ("Failed", "Check"))
                        + "\nFix the reported publishing/ingestion/ownership error and rerun the same parameters. "
                          "No size waiver, cleanup, permission or policy change was performed.")
