"""TypeScript definition generator for MCP tool schemas in AutoHarness Code Mode.

Converts JSON Schema definitions into compact, idiomatic TypeScript definitions (.d.ts),
allowing models to inspect tool interfaces and write typed scripts with minimal context usage.
"""

from __future__ import annotations

import re
from typing import Any


def _sanitize_ident(name: str) -> str:
    """Ensure a string is a valid JavaScript/TypeScript identifier."""
    cleaned = re.sub(r"[^a-zA-Z0-9_$]", "_", name)
    if cleaned and cleaned[0].isdigit():
        cleaned = f"_{cleaned}"
    return cleaned or "_unnamed"


def _format_jsdoc(description: str | None, indent: str = "    ") -> str:
    """Format a description into a JSDoc comment block."""
    if not description or not description.strip():
        return ""
    lines = description.strip().splitlines()
    if len(lines) == 1:
        return f"{indent}/** {lines[0].strip()} */\n"
    res = [f"{indent}/**"]
    for line in lines:
        res.append(f"{indent} * {line.strip()}")
    res.append(f"{indent} */\n")
    return "\n".join(res)


MAX_RECURSION_DEPTH = 10


def json_schema_to_ts(schema: dict[str, Any] | None, indent_level: int = 1, depth: int = 0) -> str:
    """Convert a JSON Schema structure into a TypeScript type representation."""
    if not schema or not isinstance(schema, dict) or depth > MAX_RECURSION_DEPTH:
        return "any"

    # Handle enum
    if "enum" in schema and isinstance(schema["enum"], list):
        literals = []
        for v in schema["enum"]:
            if isinstance(v, str):
                literals.append(f'"{v}"')
            elif isinstance(v, (int, float, bool)) or v is None:
                literals.append(str(v).lower() if isinstance(v, bool) or v is None else str(v))
        return " | ".join(literals) if literals else "any"

    # Handle oneOf / anyOf
    for key in ("oneOf", "anyOf"):
        if key in schema and isinstance(schema[key], list):
            types = [json_schema_to_ts(s, indent_level, depth + 1) for s in schema[key] if isinstance(s, dict)]
            return " | ".join(types) if types else "any"

    # Handle allOf
    if "allOf" in schema and isinstance(schema["allOf"], list):
        types = [json_schema_to_ts(s, indent_level, depth + 1) for s in schema["allOf"] if isinstance(s, dict)]
        return " & ".join(types) if types else "any"

    stype = schema.get("type")

    # Arrays
    if stype == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            elem_type = json_schema_to_ts(items, indent_level, depth + 1)
            if " " in elem_type and not (elem_type.startswith("{") or elem_type.startswith("(")):
                return f"({elem_type})[]"
            return f"{elem_type}[]"
        return "any[]"

    # Primitives
    if stype == "string":
        return "string"
    if stype in ("number", "integer"):
        return "number"
    if stype == "boolean":
        return "boolean"
    if stype == "null":
        return "null"

    # Objects
    if stype == "object" or "properties" in schema:
        properties = schema.get("properties")
        if not properties or not isinstance(properties, dict):
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                return f"{{ [key: string]: {json_schema_to_ts(additional, indent_level, depth + 1)} }}"
            return "Record<string, any>"

        required = set(schema.get("required") or [])
        indent = "    " * indent_level
        inner_indent = "    " * (indent_level + 1)
        lines = ["{"]

        for prop_name, prop_schema in sorted(properties.items()):
            if not isinstance(prop_schema, dict):
                prop_schema = {}
            opt = "" if prop_name in required else "?"
            safe_name = _sanitize_ident(prop_name)
            if safe_name != prop_name:
                prop_repr = f'"{prop_name}"'
            else:
                prop_repr = safe_name

            jsdoc = _format_jsdoc(prop_schema.get("description"), inner_indent)
            prop_type = json_schema_to_ts(prop_schema, indent_level + 1, depth + 1)
            lines.append(f"{jsdoc}{inner_indent}{prop_repr}{opt}: {prop_type};")

        lines.append(f"{indent}}}")
        return "\n".join(lines)

    return "any"


def generate_tool_method(tool: dict[str, Any], indent_level: int = 1) -> str:
    """Generate a TypeScript method definition for a single MCP tool."""
    name = tool.get("name", "unnamed")
    safe_name = _sanitize_ident(name)
    prop_repr = safe_name if safe_name == name else f'"{name}"'
    desc = tool.get("description")
    schema = tool.get("inputSchema") or {}

    indent = "    " * indent_level
    jsdoc = _format_jsdoc(desc, indent)

    args_type = json_schema_to_ts(schema, indent_level)
    # Check if empty object or optional
    is_empty_or_optional = not schema.get("properties") or not schema.get("required")

    arg_spec = f"args?: {args_type}" if is_empty_or_optional else f"args: {args_type}"
    return f"{jsdoc}{indent}{prop_repr}({arg_spec}): Promise<any>;"


def generate_server_interface(server_name: str, tools: list[dict[str, Any]]) -> str:
    """Generate a TypeScript interface for all tools in a specific MCP server."""
    iface_name = f"{_sanitize_ident(server_name).capitalize()}Tools"
    lines = [f"export interface {iface_name} {{"]
    for tool in sorted(tools, key=lambda t: t.get("name", "")):
        lines.append(generate_tool_method(tool, indent_level=1))
    lines.append("}\n")
    return "\n".join(lines)


def generate_definitions(tools_by_server: dict[str, list[dict[str, Any]]]) -> str:
    """Generate complete ambient TypeScript definitions for all connected MCP servers."""
    if not tools_by_server:
        return "// No MCP servers connected.\ndeclare const mcp: Record<string, any>;\n"

    parts = [
        "// AutoHarness Code Mode ambient type definitions",
        "// Tools are exposed via the global `mcp` object.\n",
    ]

    mcp_props = []
    for server_name in sorted(tools_by_server):
        tools = tools_by_server[server_name]
        iface_name = f"{_sanitize_ident(server_name).capitalize()}Tools"
        parts.append(generate_server_interface(server_name, tools))
        mcp_props.append(f"    {_sanitize_ident(server_name)}: {iface_name};")

    parts.append("export interface McpRegistry {")
    parts.extend(mcp_props)
    parts.append("}\n")
    parts.append("declare global {")
    parts.append("    const mcp: McpRegistry;")
    parts.append("}\n")
    parts.append("export {};")

    return "\n".join(parts)


def search_definitions(query: str | None, tools_by_server: dict[str, list[dict[str, Any]]]) -> str:
    """Search tools across servers and return compact TypeScript definitions for matches."""
    q = (query or "").strip().lower()
    if not q or q == "*":
        return generate_definitions(tools_by_server)

    filtered: dict[str, list[dict[str, Any]]] = {}
    for server_name, tools in tools_by_server.items():
        if q in server_name.lower():
            filtered[server_name] = tools
            continue
        matched_tools = [
            t for t in tools
            if q in t.get("name", "").lower() or q in (t.get("description") or "").lower()
        ]
        if matched_tools:
            filtered[server_name] = matched_tools

    if not filtered:
        return f"// No MCP tools matching query: {query!r}\n"

    return generate_definitions(filtered)
