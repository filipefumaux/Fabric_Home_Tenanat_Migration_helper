## 6. Raw copy to ADLS (fallback)
For each READY workspace, with `SAVE_RAW_COPY_TO_ADLS = True` and `DRY_RUN = False`:
- `raw/`: every item's definition exactly as the Fabric API returns it, in the backup layout
  (`<workspace>__<id>/<item>.<Type>__<id>/item.json` + `definition/`), with `backup_log.csv` and run markers;
- `git/`: every file of the snapshot commit, same paths as in the repository, so it can be pushed to any Git repo.

A failed raw copy of an in-scope item blocks the workspace (no snapshot branch, nothing may be deleted).
