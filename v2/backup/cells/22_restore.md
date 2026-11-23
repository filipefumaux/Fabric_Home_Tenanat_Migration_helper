## Next step
Paste the printed settings into **V2_3_Git_Snapshot** and run it (dry run first). Restore after the move is done
by V2_5 (definitions from Git) and V2_6 (data from this run). If Azure DevOps is unavailable, V2_5 recreates the
items from ADLS instead (`RESTORE_SOURCE = "adls"`: the snapshot's raw copy, else this run's `definition/` folders).

### Troubleshooting
| Message | Meaning |
|---|---|
| `Access denied` on definitions or data | The runner isn't Contributor or higher on that workspace. |
| `Not supported` on a definition | Fabric has no definition API for that item (expected for SQLEndpoint). |
| `Can't write to ADLS` | Missing Storage Blob Data Contributor role, wrong container, or the storage firewall blocks Fabric. |
| KQL export fails with *access denied* or *forbidden* | The Eventhouse writes with the runner's identity (`impersonate`): check the runner's role on the container and the storage firewall. |
| Kusto Copy: *ADX database ... Create the database first* | The ADX database doesn't exist, or the service principal isn't an Admin on it. Map names in `ADX_DATABASE_MAP`. |
| Kusto Copy: *SHA-256 ... Not running it* | The download doesn't match `KC_SHA256`. Copy the release's SHA-256 from GitHub together with `KC_VERSION`. |
| Kusto Copy exit code with *unauthorized* or *forbidden* | Check the service principal: workspace member, ADX database Admin, Storage Blob Data Contributor on the container and Storage Blob Delegator on the account. |
| Kusto Copy: *configuration mismatch* | A staging folder's job changed (for example `KC_COPY_MODE`). Restore the setting, or change `KC_STAGING_FOLDER` to start over (which copies history again and duplicates rows already in ADX). |
| Kusto Copy: *rows in ADX vs ... in the source* | Fewer rows in ADX than the source had at the start. Check ADX retention: Kusto Copy keeps the original extent dates, so old rows can expire at once. |
