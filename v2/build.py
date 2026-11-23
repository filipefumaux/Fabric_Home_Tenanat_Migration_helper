"""Build the v2 notebooks. Each gets the shared helpers (common/base.py + common/extra.py) as cell 03."""
import ast, hashlib, json, os, sys, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
OUT = ARGS[0] if ARGS else os.path.join(HERE, "out")
LAB_MODE = "--lab" in sys.argv
os.makedirs(OUT, exist_ok=True)
HELPERS = open(os.path.join(HERE, "common/base.py"), encoding="utf-8").read().rstrip() + "\n" + open(os.path.join(HERE, "common/extra.py"), encoding="utf-8").read()
REDACTION = open(os.path.join(HERE, "common/redaction.py"), encoding="utf-8").read().rstrip() + "\n"
NOTEBOOKS = {"permcheck": "V2_0_Permission_Check.ipynb", "connect": "V2_1_Connect_Workspaces_To_Git.ipynb",
             "backup": "V2_2_Backup_Data.ipynb", "snapshot": "V2_3_Git_Snapshot.ipynb", "delete": "V2_4_Delete_Fabric_Items.ipynb",
             "restore": "V2_5_Restore_From_Git.ipynb", "data": "V2_6_Restore_Data.ipynb",
             "testitems": "V2_T_Create_Test_Items.ipynb"}
DATA_HELPERS = open(os.path.join(HERE, "common/data.py"), encoding="utf-8").read()
CELL_META = {"microsoft": {"language": "python", "language_group": "jupyter_python"}}


def apply_lab(folder, text):
    """Write the test-tenant values of lab_config.py into a settings cell (only settings the cell already has)."""
    import re
    sys.path.insert(0, HERE)
    from lab_config import LAB, KEEP
    for name, value in LAB.items():
        if name in KEEP.get(folder, set()):
            continue
        if isinstance(value, list):
            lit = "[\n" + "".join(f"    {v!r},\n".replace("'", '"') for v in value) + "]"
            text, n = re.subn(rf"^{name} = \[.*?^\]", lambda m: f"{name} = {lit}", text, count=1, flags=re.M | re.S)
            if n:
                continue
        lit = json.dumps(value) if isinstance(value, str) else repr(value)

        def put(m, lit=lit):
            head = m.group(1) + lit
            if not m.group(3):
                return head
            col = len(m.group(1) + m.group(2)) + len(m.group(3)) - len(m.group(3).lstrip(" \t"))
            return head + " " * max(2, col - len(head)) + m.group(3).lstrip(" \t")
        text = re.sub(rf"^({name} = )([^#\n]*?)([ \t]+#[^\n]*)?$", put, text, count=1, flags=re.M)
    return text


def lines(text):
    parts = text.rstrip("\n").split("\n")
    return [p + "\n" for p in parts[:-1]] + [parts[-1]]


BUILD_IDS = {}
for folder, fname in NOTEBOOKS.items():
    cdir = os.path.join(HERE, folder, "cells2" if folder == "snapshot" else "cells")
    own = folder in ("backup", "testitems")     # V2_2 and V2_T keep their own helpers
    files = sorted(set(os.listdir(cdir) + ["03_helpers.py"]))
    cells = []
    for name in files:
        text = (HELPERS + (DATA_HELPERS if folder in ("data", "permcheck") else "")) if name == "03_helpers.py" and not own else open(os.path.join(cdir, name), encoding="utf-8").read()
        if name == "03_helpers.py":
            text = REDACTION + text
        if LAB_MODE and name.startswith("01_settings"):
            text = apply_lab(folder, text)
        cid = uuid.uuid5(uuid.NAMESPACE_URL, f"fabric-item-migration-v2/{folder}/{name}").hex[:12]
        if name.endswith(".md"):
            cells.append({"cell_type": "markdown", "id": cid, "metadata": {}, "source": lines(text)})
        else:
            ast.parse(text, filename=name, feature_version=(3, 10))
            meta = json.loads(json.dumps(CELL_META))
            if name.startswith("01_settings"):
                meta["tags"] = ["parameters"]
            cells.append({"cell_type": "code", "id": cid, "metadata": meta, "execution_count": None, "outputs": [],
                          "source": lines(text)})
    # Build ID: hash of every cell except the settings cell, so the lab build (out_lab) and the release build (out)
    # of the same code share it. Printed when the notebook runs: tells which build ran (re-import after updates).
    digest = hashlib.sha256("\n".join("".join(c["source"]) for c in cells
                                       if "parameters" not in c["metadata"].get("tags", [])).encode()).hexdigest()[:12]
    BUILD_IDS[fname] = digest
    stamp = (f'BUILD_ID = "{digest}"\n'
             f'print("{fname[:-6]} build {digest} (compare with BUILD_IDS.txt of the package you meant to run)")\n')
    helper = next(c for c in cells if c["id"] == uuid.uuid5(uuid.NAMESPACE_URL, f"fabric-item-migration-v2/{folder}/03_helpers.py").hex[:12])
    helper["source"] = lines(stamp + "".join(helper["source"]))
    nb = {"cells": cells, "metadata": {
        "kernel_info": {"name": "jupyter", "jupyter_kernel_name": "python3.11"},
        "kernelspec": {"name": "jupyter", "display_name": "Jupyter", "language": "Jupyter"},
        "language_info": {"name": "python"},
        "microsoft": {"language": "python", "language_group": "jupyter_python",
                      "ms_spell_check": {"ms_spell_check_language": "en"}},
        "nteract": {"version": "nteract-front-end@1.0.0"}, "dependencies": {}},
        "nbformat": 4, "nbformat_minor": 5}
    path = os.path.join(OUT, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(nb, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"{fname}: {len(cells)} cells ({sum(c['cell_type'] == 'code' for c in cells)} code)")

with open(os.path.join(OUT, "BUILD_IDS.txt"), "w", encoding="utf-8") as f:
    f.write("Build ID of each notebook (printed when it runs; same in the lab and the release build):\n")
    f.writelines(f"{k[:-6]}  {v}\n" for k, v in BUILD_IDS.items())
