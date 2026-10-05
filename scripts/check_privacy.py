"""Reject personal home paths and email addresses in product source."""
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
# Third-party licence notices must retain their original authors and addresses.
paths = [root / "README.md", root / "pyproject.toml"]
for directory in ("src", "tests", "scripts", "agent-skill", ".github"):
    paths += [p for p in (root / directory).rglob("*") if p.suffix in (".py", ".js", ".html", ".css", ".md", ".yml")]
patterns = (re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/"), re.compile(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"))
failed = []
for path in paths:
    if any(pattern.search(path.read_text()) for pattern in patterns):
        failed.append(str(path.relative_to(root)))
if failed:
    print("Private identifier pattern in: " + ", ".join(failed), file=sys.stderr)
    sys.exit(1)
print("Product source privacy patterns passed; third-party licence notices excluded")
