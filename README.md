# Fabric Item Migration Toolkit

Community-maintained Python notebooks for backing up, safely removing, and restoring Microsoft Fabric items
around an approved tenant migration.

**Warning: this toolkit can permanently delete Fabric items. Start with dry runs and a disposable test
workspace. A successful lab test is not approval to run against production.**

This is an independent project, not an official Microsoft product or a replacement for your migration
programme's requirements. GitHub hosts this toolkit's source; the notebooks' workspace Git integration and
snapshot implementation use **Azure DevOps**, not the GitHub provider.

## Notebook sequence

| Step | Notebook | Purpose |
|---|---|---|
| T | V2_T_Create_Test_Items | Create isolated test fixtures; optional measured GB-scale fixtures |
| 0 | V2_0_Permission_Check | Check permissions, connectivity, and required services |
| 1 | V2_1_Connect_Workspaces_To_Git | Connect approved workspaces to Azure DevOps Git |
| 2 | V2_2_Backup_Data | Back up data and secondary definitions to ADLS/ADX |
| 3 | V2_3_Git_Snapshot | Check synchronization and capture the recoverable snapshot |
| 4 | V2_4_Delete_Fabric_Items | Hard-delete only approved, fully backed-up items |
| 5 | V2_5_Restore_From_Git | Restore definitions and repair references on confirmation |
| 6 | V2_6_Restore_Data | Restore and verify supported data |

Read the [runbook](v2/out/README_v2.md) and [authentication/setup guide](v2/out/SETUP_DevOps_Access_PAT_or_SP.md)
before importing or running notebooks.

## Safety boundaries

- Dry run by default. Real changes require the confirmation ID produced by the matching dry run.
- Snapshots require synchronized workspaces unless an explicitly configured protected ADLS recovery route applies.
- Deletion requires READY workspaces and successful Git disconnection.
- No forced Git synchronization/sign-in, semantic model modification, or overwrite of populated destination tables.
- One Entra tenant; secrets are obtained from Key Vault, not source files.
- Downloaded runtime executables/wheels require pinned SHA-256 verification.
- Migration/control notebooks must run outside the workspaces being migrated.
- Automated real rehearsals are guarded to one disposable or explicitly allowlisted workspace per run.

## Diagnostic privacy

Shared redaction masks registered credentials, authorization tokens, signed-URL signatures and secret-valued
fields in runner console output, displayed table copies, transcripts, status facts, rehearsal reports and HTTP error messages.
Key Vault secrets and authentication tokens are registered locally when obtained; their values are still sent
unchanged to the intended service. API responses, backup definitions and restored data are not redacted.

Complete diagnostics and fixes remain visible apart from credential values. A failure whose exception contains
a credential is rethrown as a redacted RuntimeError with the original exception type in its message; ordinary
failures retain their type. Non-newline console output is buffered until a complete line or the end of the run
so a credential split across writes is not printed piecemeal.

Redaction is defense in depth, not permission to publish operational data. Workspace names, IDs, schema/data
and unknown sensitive values can still occur in output. Keep configured notebooks, transcripts and backups
private and inspect any excerpt before sharing it.

## Build and test locally

Use Python 3.10 or newer. CI tests Python 3.12. The local fake suites need pandas and requests; Fabric's Python
kernel supplies the data-plane runtime used by the generated notebooks.

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python v2\build.py
python v2\test\tests.py
python v2\test\backup_tests.py
python v2\test\agent_tests.py
python v2\test\testitems_tests.py
python v2\test\e2e_tests.py
python tools\test_check_public.py
python tools\check_public.py
```

Every suite must finish with `RESULT: ALL PASSED`; a traceback or a missing result line is a failure even if
the process reports success. The CI workflow enforces this.

Edit source cells and shared helpers, **not generated notebooks**. Build output is deterministic and each
notebook prints its build ID.

## Fabric setup

1. Select a separate control workspace and the Fabric **Python** notebook kernel, not PySpark.
2. Configure your own workspace IDs, Azure DevOps repository, Key Vault, ADLS, and optional ADX resources.
3. Import the generated notebooks from [v2/out](v2/out) and configure their settings in Fabric.
4. Run V2_0 and rehearse T -> 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 in your test tenant before production.
5. Follow the runbook's freeze, confirmation, verification, and sign-off requirements.

The toolkit does not move the tenant region or reprovision capacities.

### Optional local rehearsal automation

The runner needs Azure CLI 2.54+ when using the CLI token provider. Configure your own private lab values:

```powershell
Copy-Item v2\lab_config.example.py v2\lab_config.py
# Edit the ignored file with your approved test resource settings; do not put secret values in it.
Set-Location v2
python build.py out_lab --lab
python agent\e2e_rehearsal.py start --workspace <allowlisted-test-workspace-id>
```

Only the workspace owner approves the allowlist and every reset inventory. Keep any local Azure CLI profile,
credentials, configured notebooks, transcripts, backup manifests, and rehearsal state out of Git.
Do not run two coordinators concurrently against the same control notebooks.

## Validation provenance and limits

The toolkit derives from the baseline that passed five simulated suites and two separate
real same-region rehearsals with four copies per supported type and about 1 GiB in one Lakehouse, Warehouse,
and Eventhouse per workspace. Those runs exercised backup, snapshot, hard deletion, definition/reference
restore, and data verification.

This public edition generalizes documentation and test-only identifiers and adds diagnostic redaction and
cross-drive report handling. Its public build IDs are regenerated;
it has **not** had a separate live tenant rehearsal. [SOURCE_PROVENANCE.json](SOURCE_PROVENANCE.json) records
current source hashes, changed baseline hashes and the unchanged protected runner guards. Always verify the
build you actually run. CI checks publication safety before the fake suites, so a suite failure does not skip
that check.

Known limits include populated preview bindings, production/cross-region throughput, the actual tenant move,
and routes/data layouts not exercised by the baseline. Ontology can create additional backing items during
restore; they are reported, not silently removed. Review bindings before production.

The publication guard accepts a local-only JSON list of private names/IDs using `--private-markers-file`.
Keep that file outside the repository. Supplied markers are checked case-insensitively in file paths and text,
including hyphen/underscore labels and example email domains; their values are not logged.
No actual organization names are embedded in the public checker or its test fixtures. CI always checks
credentials, notebook outputs and safety defaults; without a private-marker file it explicitly reports the
organization-specific check as NOT CONFIGURED. Review new examples/documentation and run the locally
configured marker check before publishing. Test identities use neutral labels and reserved example domains.

## Contributing

Preserve every safety gate. Add a focused fake scenario for behavior changes, rebuild, and run the five suites.
Live validation needs explicit owner approval and must never use production workspaces as test fixtures.
Do not publish credentials or tenant/customer identifiers in code, issues, screenshots, or logs.

## License

[MIT](LICENSE). Copyright (c) 2026 filipefumaux.
