"""Copy to ignored lab_config.py and enter only your approved test settings. Secret VALUES belong in Key Vault."""
LAB = {
    "WORKSPACE_IDS": [],
    "ADO_ORGANIZATION": "",
    "ADO_PROJECT": "",
    "ADO_REPOSITORY": "",
    "ADO_BRANCH": "",
    "KEY_VAULT_URL": "",
    "ADO_AUTH": "service_principal",
    "ADO_SP_TENANT_ID": "",
    "ADO_SP_CLIENT_ID": "",
    "ADO_SP_SECRET_NAME": "",
    "ADLS_ACCOUNT": "",
    "ADLS_CONTAINER": "",
    "ADLS_ROOT": "fabric-backups",
    "KQL_BACKUP_MODE": "parquet",
    "ALLOW_CROSS_TENANT": False,
}
KEEP = {"delete": {"WORKSPACE_IDS"}}
E2E = {
    "RUNNER_WORKSPACE_ID": "",
    "REHEARSAL_WORKSPACE_IDS": [],  # owner-approved test workspaces only; --reset permanently clears their items
    "MEMBERS": [],
    "LAB_MARKERS": [],  # add recognizable private test names/ID fragments for verify-build leak checks
}
