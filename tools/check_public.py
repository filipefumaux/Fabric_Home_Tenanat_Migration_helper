"""Read-only checks for accidental configuration, saved output, or credential publication."""
import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PATHS = ("v2/agent/runs/", "v2/agent/e2e/", "v2/out_lab/", ".azure/")
SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"[?&]sig=[A-Za-z0-9%+/=]{16,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def publication_files():
    result = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if result.returncode == 0 and Path(result.stdout.strip()).resolve() == ROOT:
        names = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True).stdout
        return [ROOT / name for name in names.split("\0") if name]
    return [p for p in ROOT.rglob("*") if p.is_file()
            and not any(part in (".git", ".venv", "__pycache__") for part in p.relative_to(ROOT).parts)
            and p.suffix not in (".pyc", ".pyo")]


def check_notebook(path):
    nb = json.loads(path.read_text(encoding="utf-8"))
    code = [c for c in nb["cells"] if c["cell_type"] == "code"]
    if any(c.get("outputs") or c.get("execution_count") is not None for c in code):
        raise ValueError(f"{path.name}: saved output/execution state must be cleared")
    settings = [c for c in code if "parameters" in c.get("metadata", {}).get("tags", [])]
    if len(settings) != 1:
        raise ValueError(f"{path.name}: expected one parameters cell")
    values = {}
    for node in ast.parse("".join(settings[0]["source"])).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.startswith("CONFIRM_") or name in (
                    "DRY_RUN", "ALLOW_CROSS_TENANT", "WORKSPACE_IDS", "TARGET_WORKSPACE_ID",
                    "WAREHOUSE_LOAD", "SQL_OBJECTS", "LARGE_DATA_MIB", "ISOLATED_STRESS_RUNTIME"):
                values[name] = ast.literal_eval(node.value)
    if any(value for name, value in values.items() if name.startswith("CONFIRM_")):
        raise ValueError(f"{path.name}: populated confirmation ID")
    if "DRY_RUN" in values and values["DRY_RUN"] is not True:
        raise ValueError(f"{path.name}: DRY_RUN must be True in published settings")
    if values.get("ALLOW_CROSS_TENANT", False) is not False:
        raise ValueError(f"{path.name}: cross-tenant opt-in must not be published")
    if values.get("WORKSPACE_IDS") or values.get("TARGET_WORKSPACE_ID"):
        raise ValueError(f"{path.name}: real target workspace settings must not be published")
    if path.stem.startswith("V2_6") and (values["WAREHOUSE_LOAD"], values["SQL_OBJECTS"]) != ("script", "script"):
        raise ValueError(f"{path.name}: SQL execution opt-ins must stay script-only")
    if path.stem.startswith("V2_T") and (values["LARGE_DATA_MIB"] != 0 or values["ISOLATED_STRESS_RUNTIME"] is not False):
        raise ValueError(f"{path.name}: stress runtime/data opt-ins must stay off")


def main(private_markers=()):
    if not isinstance(private_markers, (list, tuple)) or any(
            not isinstance(marker, str) or not marker.strip() for marker in private_markers):
        raise ValueError("Private markers must be a list of nonempty strings")
    patterns = [re.compile(r"(?<![a-z0-9])" + re.escape(marker.strip()) + r"(?![a-z0-9])", re.IGNORECASE)
                for marker in private_markers]
    files = publication_files()
    notebooks = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        display_path = relative
        for pattern in patterns:
            display_path = pattern.sub("<private>", display_path)
        if any(pattern.search(relative) for pattern in patterns):
            raise ValueError(f"Private identifier remains in a file path: {display_path}")
        if (path.name == "lab_config.py" or path.name == ".env" or path.name.endswith(".env")
                or path.name.startswith(".env.") or "HANDOVER" in path.name.upper()
                or relative.startswith(FORBIDDEN_PATHS)):
            raise ValueError(f"Private/local file included in publication: {relative}")
        if path.suffix in (".py", ".md", ".txt", ".json", ".ipynb", ".yml", ".yaml") or path.name == "LICENSE":
            text = path.read_text(encoding="utf-8")
            if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                raise ValueError(f"Credential-shaped content found: {relative}")
            if any(pattern.search(text) for pattern in patterns):
                raise ValueError(f"Private identifier remains: {display_path}")
        if path.suffix == ".ipynb" and relative.startswith("v2/out/"):
            check_notebook(path)
            notebooks.append(path)
    if len(notebooks) != 8:
        raise ValueError(f"Expected eight published notebooks, found {len(notebooks)}")
    print("PUBLICATION CHECK: PASS (8 clean notebooks, safe defaults, no detected private files or credentials)")
    if patterns:
        print(f"PRIVATE IDENTIFIER CHECK: PASS ({len(patterns)} locally supplied markers; values not logged)")
    else:
        print("PRIVATE IDENTIFIER CHECK: NOT CONFIGURED; use --private-markers-file with a local-only list before sharing.")
    print("This check is not a substitute for human release/ownership review.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-markers-file", type=Path,
                        help="local JSON list of private names/IDs; keep this file outside the repository")
    args = parser.parse_args()
    markers = ()
    if args.private_markers_file:
        if args.private_markers_file.resolve().is_relative_to(ROOT):
            raise ValueError("Keep the private-marker file outside the repository")
        markers = json.loads(args.private_markers_file.read_text(encoding="utf-8"))
    main(markers)
