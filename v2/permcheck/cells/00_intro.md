# Fabric migration (v2) – step 0: permission and connectivity check

Run this first, as the person (or identity) who will run the other v2 notebooks. It checks every permission and
network path they need and tells you how to fix what's missing. It changes nothing in Fabric.

With `WRITE_TESTS = True` it also proves write access, then cleans up:
- Azure DevOps: creates `permission-check/<run id>`, pushes one file, locks and unlocks it, deletes the branch.
- ADLS: writes, reads and deletes one small file under `<ADLS_ROOT>/_migration/_permission_check/`.
- Data restore (step 5): writes and deletes one file in a Lakehouse's `Files/`; has a KQL database and a Warehouse
  read a small probe file from the backup container with your identity (as ingestion, `COPY INTO` and `OPENROWSET` will).

If Azure DevOps refuses the token, extra *diagnose* rows say whether the token, the organization, the project or the
repository is the problem.

Result values: **PASS**, **FAIL** (the migration notebooks will stop), **WARN** (works, but check it), **INFO**.

What it can't prove without changing a workspace: the Git commit, connect and update calls themselves. The Git status
call it makes uses the same credentials and role, so a PASS there is the best available signal; the dry runs of the
other notebooks confirm the rest.
