## After this notebook
- Keep the change freeze. Anything changed after the snapshot isn't in Git or ADLS; the delete notebook checks
  this and refuses.
- The snapshot branches stay locked. Don't delete them until the migration is signed off.
- If a workspace is BLOCKED, fix the reason, take a new secondary backup if data changed, and run this notebook again.
