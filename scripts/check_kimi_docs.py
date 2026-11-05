"""B5 check: documentation lists the kimi agent everywhere the other agents are listed."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
checks = [
    ("README.md", "| Kimi Code | `kimi` |"),
    ("docs/installation.md", "| `kimi` (Kimi Code) | `.kimi-code/mcp.json` | `.agents/skills` |"),
    ("docs/installation.md", "| Kimi Code | `kimi` | `.agents/skills/autoharness/` | `~/.agents/skills/autoharness/` |"),
    ("docs/installation.md", "| Kimi Code | `.kimi-code/mcp.json` | restart Kimi Code (or run `/mcp`) |"),
    ("llms-full.txt", ".kimi-code/mcp.json"),
]
failed = [f"{f}: {needle!r} missing" for f, needle in checks if needle not in (root / f).read_text("utf-8")]
src = (root / "src/autoharness/agents.py").read_text("utf-8")
if 'Agent("kimi", "Kimi Code"' not in src:
    failed.append("agents.py: kimi agent entry missing")
if failed:
    print("\n".join(failed))
    raise SystemExit(1)
print("docs list the kimi agent")
