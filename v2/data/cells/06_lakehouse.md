## Lakehouse data
Copies `Files/` and `Tables/` back into OneLake byte for byte (Delta history kept): data files first, Delta logs last, so a table only appears once all its files are there. Then checks every file.
With `LAKEHOUSE_COPY_METHOD = "fastcp"` each table folder is copied in bulk by azcopy (`notebookutils.fs.fastcp`), after a tiny test copy in ADLS that checks how fastcp lays out folders. Anything fastcp didn't place is copied file by file. Nothing that is already in the Lakehouse is replaced, and the backup in ADLS is never moved or changed.
