"""A tiny stdio MCP server for tests: echo, slow, add_tool (emits list_changed), crash, client_reply."""
import json
import sys
import time

TOOLS = {n: {"name": n, "description": d, "inputSchema": {"type": "object"}} for n, d in [
    ("echo", "Echo text back"), ("slow", "Sleep, then answer"), ("add_tool", "Add a tool and announce it"),
    ("crash", "Exit without answering"), ("client_reply", "What the client answered to our request")]}
state = {"reply": None}


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def text(t):
    return {"content": [{"type": "text", "text": t}]}


while True:
    line = sys.stdin.readline()
    if not line:
        break
    msg = json.loads(line)
    if "method" not in msg:          # the client's answer to our roots/list request
        state["reply"] = msg
        continue
    method, mid = msg["method"], msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "notifications/initialized":
        send({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": list(TOOLS.values())}})
    elif method == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
        if name == "echo":
            result = text(args.get("text", ""))
        elif name == "slow":
            time.sleep(float(args.get("seconds", 5)))
            result = text("done")
        elif name == "add_tool":
            TOOLS["extra"] = {"name": "extra", "description": "Added later", "inputSchema": {"type": "object"}}
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            result = text("added")
        elif name == "crash":
            sys.exit(1)
        elif name == "client_reply":
            result = text(json.dumps(state["reply"]))
        elif name == "extra":
            result = text("extra")
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}})
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": result})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not supported"}})
