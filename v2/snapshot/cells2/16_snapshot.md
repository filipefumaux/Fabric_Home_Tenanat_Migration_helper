## 7. Snapshot branches and manifest
Creates one locked branch `migration-snapshot/<snapshot id>` (same name for every workspace) and the tag `migration-snapshot-<snapshot id>` at the branch head after all commits, after checking that no workspace's folder changed after its own commit.
Then writes the manifest that the delete and restore notebooks require. Blocked workspaces are recorded but get no
snapshot branch: fix them and run this notebook again (a new snapshot ID).
