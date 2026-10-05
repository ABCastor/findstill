"""Fail on invalid Python source or browser JavaScript."""
import ast
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
for directory in ("src", "tests", "scripts"):
    for path in (root / directory).rglob("*.py"):
        ast.parse(path.read_text(), filename=str(path.relative_to(root)))
for path in (root / "src/media_search/static").glob("*.js"):
    subprocess.run(["node", "--check", str(path)], check=True)
print("Python and browser JavaScript syntax passed")
