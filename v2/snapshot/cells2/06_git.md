## 2. Git sync check (every workspace)
Read-only: for each workspace, the Git connection, Source control changes and updates, and the DevOps branch head.
**Synced** = connected to Azure DevOps, nothing uncommitted, nothing incoming, no conflicts. The branch is shared,
so a branch head that moved only through other workspaces' folders still counts as synced.
With `REQUIRE_ALL_WORKSPACES_SYNCED = True` (default) the snapshot is taken only when every workspace is synced.
This notebook doesn't commit or update for you unless `COMMIT_PENDING_CHANGES = True`; the Fix column says what to
do in the workspace's Source control.
