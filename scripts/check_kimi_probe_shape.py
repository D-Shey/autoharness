"""B2 check: to_kimi emits exactly the entry shape the live probe proved the real Kimi Code
CLI loads from .kimi-code/mcp.json (task evidence #1: a headless kimi -p run listed and called
mcp__envprobe__* with that JSON), and the bearer/SSE field names follow Kimi's documented schema."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from autoharness import agents  # noqa: E402

stdio, warnings = agents.to_kimi({"command": "python", "args": ["envprobe_server.py"]})
assert warnings == [], warnings
assert stdio == {"command": "python", "args": ["envprobe_server.py"]}, stdio
json.dumps({"mcpServers": {"envprobe": stdio}})   # what the adapter writes must stay parseable JSON

http, warnings = agents.to_kimi({"type": "http", "url": "https://mcp.example.com/mcp",
                                 "headers": {"Authorization": "Bearer ${TOKEN}"}})
assert warnings == [] and http == {"url": "https://mcp.example.com/mcp", "bearerTokenEnvVar": "TOKEN"}, http

sse, warnings = agents.to_kimi({"type": "sse", "url": "https://mcp.example.com/sse"})
assert warnings == [] and sse == {"url": "https://mcp.example.com/sse", "transport": "sse"}, sse

native, warnings = agents.to_kimi({"command": "npx", "args": ["-y", "c7"],
                                   "env": {"K": "${K}", "MODE": "ro", "T": "${OTHER}"}})
assert native.get("env") == {"MODE": "ro"}, native          # same-name ref dropped: servers inherit the env
assert native.get("args") == ["-y", "c7"], native
assert len(warnings) == 1 and "set T" in warnings[0], warnings
print("adapter emits the probe-verified kimi shape")
