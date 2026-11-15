"""Python AST Code Map and structural symbol indexing.

Pure Python standard library implementation (zero runtime dependencies).
Extracts classes, methods, functions, async functions, decorators, parameters,
return types, docstrings, and imports using Python's built-in `ast` module.
Reduces token overhead by 80-90% compared to reading full raw source files.
"""
from __future__ import annotations

import ast
import collections
import os
import re
import threading
from pathlib import Path
from typing import Any

DEFAULT_EXCLUDES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".autoharness",
    "build", "dist", ".egg-info", "node_modules", ".playwright-mcp"
}

SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".mts": "typescript",
    ".cts": "typescript",
    ".java": "java",
}

_LANG_CACHE: dict[str, Any] = {}
_LANG_LOCK = threading.Lock()


def get_tree_sitter_parser(lang: str) -> Any | None:
    """Load or retrieve thread-safe cached Tree-sitter Language and return a fresh Parser."""
    language = _LANG_CACHE.get(lang)
    if not language:
        with _LANG_LOCK:
            if lang in _LANG_CACHE:
                language = _LANG_CACHE[lang]
            else:
                try:
                    from tree_sitter import Language
                    if lang in ("javascript", "js"):
                        import tree_sitter_javascript as ts_mod
                        language = Language(ts_mod.language())
                    elif lang in ("typescript", "ts"):
                        import tree_sitter_typescript as ts_mod
                        language = Language(ts_mod.language_typescript())
                    elif lang == "tsx":
                        import tree_sitter_typescript as ts_mod
                        language = Language(ts_mod.language_tsx())
                    elif lang == "java":
                        import tree_sitter_java as ts_mod
                        language = Language(ts_mod.language())
                    else:
                        return None
                    _LANG_CACHE[lang] = language
                except (ImportError, AttributeError):
                    return None
    if language:
        from tree_sitter import Parser
        return Parser(language)
    return None


def _clean_params(raw_params: str) -> str:
    raw = raw_params.strip()
    if raw.startswith("(") and raw.endswith(")"):
        return raw[1:-1].strip()
    return raw


def _clean_returns(raw_ret: str) -> str:
    ret = raw_ret.strip()
    if not ret:
        return ""
    if ret.startswith(":"):
        return f" -> {ret[1:].strip()}"
    if not ret.startswith("->"):
        return f" -> {ret}"
    return ret


def parse_treesitter_file(file_path: Path, lang: str) -> dict[str, Any]:
    """Parse a JS/TS/Java source file using Tree-sitter into uniform codemap structure."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"file": str(file_path), "error": str(e), "exists": False}

    lines = content.splitlines()
    total_lines = len(lines)
    code_lines = sum(1 for l in lines if l.strip() and not l.strip().startswith("//") and not l.strip().startswith("/*"))

    parser = get_tree_sitter_parser(lang)
    if not parser:
        return {
            "file": str(file_path).replace("\\", "/"),
            "exists": True,
            "error": f"Tree-sitter parser for {lang} unavailable",
            "total_lines": total_lines,
            "code_lines": code_lines,
            "imports": [],
            "classes": [],
            "functions": [],
            "policy_violations": [],
        }

    src_bytes = content.encode("utf-8")
    tree = parser.parse(src_bytes)
    root = tree.root_node

    imports: list[str] = []
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    consumed_comments: set[int] = set()

    def get_text(node) -> str:
        if not node:
            return ""
        return src_bytes[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def extract_doc(node) -> str:
        if not node:
            return ""
        prev = node.prev_named_sibling
        if prev and prev.type == "comment" and prev.start_byte not in consumed_comments:
            text = get_text(prev).strip()
            comment_lines = [l.lstrip("/*# \t").rstrip("*/ \t") for l in text.splitlines()]
            return next((l for l in comment_lines if l), "")
        return ""

    def extract_decorators(node) -> list[str]:
        decs = []
        nodes_to_check = list(node.children)
        for c in node.children:
            if c.type == "modifiers":
                nodes_to_check.extend(c.children)
        for child in nodes_to_check:
            if child.type in ("decorator", "annotation", "marker_annotation"):
                decs.append(get_text(child).strip())
        return decs

    # Extract module-level doc from first comment if present
    module_doc = ""
    if root.named_children and root.named_children[0].type == "comment":
        comment_node = root.named_children[0]
        consumed_comments.add(comment_node.start_byte)
        text = get_text(comment_node).strip()
        comment_lines = [l.lstrip("/*# \t").rstrip("*/ \t") for l in text.splitlines()]
        module_doc = next((l for l in comment_lines if l), "")

    def process_class_like(node, outer_node, is_interface: bool = False):
        name_node = node.child_by_field_name("name")
        if not name_node:
            return
        cls_name = get_text(name_node)
        bases: list[str] = []
        for child in node.children:
            if child.type in ("class_heritage", "extends_clause", "implements_clause", "superclass", "super_interfaces", "extends_interfaces"):
                for item_node in child.named_children:
                    if item_node.type in ("extends_clause", "implements_clause", "type_list"):
                        for id_node in item_node.named_children:
                            b_name = get_text(id_node).strip()
                            if b_name and b_name not in ("extends", "implements"):
                                bases.append(b_name)
                    else:
                        b_name = get_text(item_node).strip()
                        if b_name and b_name not in ("extends", "implements"):
                            bases.append(b_name)

        doc = extract_doc(outer_node) or extract_doc(node) or (f"interface {cls_name}" if is_interface else "")
        methods: list[dict[str, Any]] = []
        body_node = node.child_by_field_name("body")
        if body_node:
            for item in body_node.named_children:
                if item.type in ("method_definition", "method_declaration", "constructor_declaration", "method_signature"):
                    m_name_node = item.child_by_field_name("name")
                    if not m_name_node:
                        continue
                    m_name = get_text(m_name_node)
                    m_params = _clean_params(get_text(item.child_by_field_name("parameters")))
                    m_ret_node = item.child_by_field_name("return_type") or item.child_by_field_name("type")
                    m_ret = _clean_returns(get_text(m_ret_node)) if m_ret_node else ""
                    is_async = any(c.type == "async" or get_text(c) == "async" for c in item.children)
                    methods.append({
                        "name": m_name,
                        "is_async": is_async,
                        "args": m_params,
                        "returns": m_ret,
                        "doc": extract_doc(item),
                        "line": item.start_point.row + 1,
                        "decorators": extract_decorators(item)
                    })

        classes.append({
            "name": cls_name,
            "bases": bases,
            "doc": doc,
            "line": outer_node.start_point.row + 1,
            "methods": methods,
            "decorators": extract_decorators(node) or extract_decorators(outer_node)
        })

    def process_function_like(node, outer_node, explicit_name: str = "", is_async: bool = False):
        name_node = node.child_by_field_name("name")
        name = explicit_name or (get_text(name_node) if name_node else "")
        if not name:
            return
        params = _clean_params(get_text(node.child_by_field_name("parameters")))
        ret_node = node.child_by_field_name("return_type") or node.child_by_field_name("type")
        ret = _clean_returns(get_text(ret_node)) if ret_node else ""
        if not is_async:
            is_async = any(c.type == "async" or get_text(c) == "async" for c in node.children)
        doc = extract_doc(outer_node) or extract_doc(node)
        functions.append({
            "name": name,
            "is_async": is_async,
            "args": params,
            "returns": ret,
            "doc": doc,
            "line": outer_node.start_point.row + 1,
            "decorators": extract_decorators(node) or extract_decorators(outer_node)
        })

    for child in root.named_children:
        target = child
        if child.type == "export_statement":
            decl = child.child_by_field_name("declaration")
            if decl:
                target = decl

        if target.type in ("import_statement", "import_declaration"):
            imports.append(get_text(target).strip())
        elif target.type in ("package_declaration",):
            imports.insert(0, get_text(target).strip())
        elif target.type in ("class_declaration",):
            process_class_like(target, outer_node=child, is_interface=False)
        elif target.type in ("interface_declaration",):
            process_class_like(target, outer_node=child, is_interface=True)
        elif target.type in ("function_declaration",):
            process_function_like(target, outer_node=child)
        elif target.type in ("lexical_declaration", "variable_declaration"):
            for decl in target.named_children:
                if decl.type == "variable_declarator":
                    v_name_node = decl.child_by_field_name("name")
                    v_val_node = decl.child_by_field_name("value")
                    if v_name_node and v_val_node and v_val_node.type in ("arrow_function", "function_expression"):
                        is_async = any(c.type == "async" or get_text(c) == "async" for c in v_val_node.children)
                        process_function_like(v_val_node, outer_node=child, explicit_name=get_text(v_name_node), is_async=is_async)

    return {
        "file": str(file_path).replace("\\", "/"),
        "exists": True,
        "doc": module_doc,
        "total_lines": total_lines,
        "code_lines": code_lines,
        "imports": imports,
        "classes": classes,
        "functions": functions,
        "policy_violations": [],
    }


def parse_source_file(file_path: Path) -> dict[str, Any]:
    """Parse any supported source file (.py, .js, .ts, .java) into a uniform symbol map."""
    suffix = file_path.suffix.lower()
    if suffix == ".py":
        return parse_python_file(file_path)
    lang = SUPPORTED_EXTENSIONS.get(suffix)
    if lang:
        return parse_treesitter_file(file_path, lang)
    return {"file": str(file_path), "error": f"Unsupported extension {suffix}", "exists": file_path.exists()}


def _format_arg(arg: ast.arg) -> str:
    res = arg.arg
    if arg.annotation:
        try:
            res += f": {ast.unparse(arg.annotation)}"
        except Exception:
            pass
    return res


def _format_args(args: ast.arguments) -> str:
    parts = []
    # Positional only
    for a in getattr(args, "posonlyargs", []):
        parts.append(_format_arg(a))
    if getattr(args, "posonlyargs", []):
        parts.append("/")
    # Regular positional
    for a in args.args:
        parts.append(_format_arg(a))
    # Vararg *args
    if args.vararg:
        parts.append(f"*{_format_arg(args.vararg)}")
    elif args.kwonlyargs:
        parts.append("*")
    # Keyword only
    for a in args.kwonlyargs:
        parts.append(_format_arg(a))
    # Kwarg **kwargs
    if args.kwarg:
        parts.append(f"**{_format_arg(args.kwarg)}")
    return ", ".join(parts)


def _extract_ast_calls(node: ast.AST) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = ""
            if isinstance(child.func, ast.Name):
                name = child.func.id
            elif isinstance(child.func, ast.Attribute):
                val_name = child.func.value.id if isinstance(child.func.value, ast.Name) else ""
                name = f"{val_name}.{child.func.attr}" if val_name else child.func.attr
            if name:
                calls.append({"name": name, "line": getattr(child, "lineno", 0)})
    return calls


def parse_python_file(file_path: Path) -> dict[str, Any]:
    """Parse a single Python file into a structured symbol map using stdlib ast."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"file": str(file_path), "error": str(e), "exists": False}

    lines = content.splitlines()
    total_lines = len(lines)
    code_lines = sum(1 for l in lines if l.strip() and not l.strip().startswith("#"))

    try:
        tree = ast.parse(content, filename=str(file_path))
    except SyntaxError as e:
        return {
            "file": str(file_path),
            "exists": True,
            "syntax_error": f"{e.msg} at line {e.lineno}",
            "total_lines": total_lines,
            "code_lines": code_lines
        }

    docstring = ast.get_docstring(tree) or ""
    module_doc = docstring.strip().splitlines()[0] if docstring.strip() else ""

    imports: list[str] = []
    classes: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name if not alias.asname else f"{alias.name} as {alias.asname}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = [alias.name if not alias.asname else f"{alias.name} as {alias.asname}" for alias in node.names]
            imports.append(f"from {mod} import {', '.join(names)}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            f_doc = ast.get_docstring(node) or ""
            ret_type = ""
            if node.returns:
                try:
                    ret_type = f" -> {ast.unparse(node.returns)}"
                except Exception:
                    pass
            functions.append({
                "name": node.name,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
                "args": _format_args(node.args),
                "returns": ret_type,
                "doc": f_doc.strip().splitlines()[0] if f_doc.strip() else "",
                "line": node.lineno,
                "decorators": [ast.unparse(d) for d in node.decorator_list if hasattr(ast, "unparse")],
                "calls": _extract_ast_calls(node),
            })
        elif isinstance(node, ast.ClassDef):
            c_doc = ast.get_docstring(node) or ""
            bases = []
            for b in node.bases:
                try:
                    bases.append(ast.unparse(b))
                except Exception:
                    pass
            methods: list[dict[str, Any]] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_doc = ast.get_docstring(item) or ""
                    m_ret = ""
                    if item.returns:
                        try:
                            m_ret = f" -> {ast.unparse(item.returns)}"
                        except Exception:
                            pass
                    methods.append({
                        "name": item.name,
                        "is_async": isinstance(item, ast.AsyncFunctionDef),
                        "args": _format_args(item.args),
                        "returns": m_ret,
                        "doc": m_doc.strip().splitlines()[0] if m_doc.strip() else "",
                        "line": item.lineno,
                        "decorators": [ast.unparse(d) for d in item.decorator_list if hasattr(ast, "unparse")],
                        "calls": _extract_ast_calls(item),
                    })
            classes.append({
                "name": node.name,
                "bases": bases,
                "doc": c_doc.strip().splitlines()[0] if c_doc.strip() else "",
                "line": node.lineno,
                "methods": methods,
                "decorators": [ast.unparse(d) for d in node.decorator_list if hasattr(ast, "unparse")]
            })

    return {
        "file": str(file_path).replace("\\", "/"),
        "exists": True,
        "doc": module_doc,
        "total_lines": total_lines,
        "code_lines": code_lines,
        "imports": imports,
        "classes": classes,
        "functions": functions,
        "policy_violations": check_ast_policies(content, filename=str(file_path)),
    }


def generate_codemap(
    root_path: Path,
    max_depth: int = 5,
    exclude_dirs: set[str] | None = None
) -> dict[str, Any]:
    """Recursively scan root_path for supported source files (.py, .js, .ts, .java) and generate codebase symbol map."""
    excludes = DEFAULT_EXCLUDES | (exclude_dirs or set())
    root = root_path.resolve()
    modules: dict[str, dict[str, Any]] = {}

    if root.is_file():
        if root.suffix.lower() in SUPPORTED_EXTENSIONS:
            rel_name = root.name
            modules[rel_name] = parse_source_file(root)
        return {"root": str(root).replace("\\", "/"), "modules": modules}

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place so os.walk does not descend into them
        dirnames[:] = [d for d in dirnames if d not in excludes]
        rel_dir = Path(dirpath).relative_to(root)
        depth = len(rel_dir.parts)
        if depth >= max_depth:
            dirnames.clear()
            continue

        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
                rel_key = str(fpath.relative_to(root)).replace("\\", "/")
                parsed = parse_source_file(fpath)
                modules[rel_key] = parsed

    sorted_modules = {k: modules[k] for k in sorted(modules.keys())}
    return {
        "root": str(root).replace("\\", "/"),
        "modules_count": len(sorted_modules),
        "modules": sorted_modules
    }


def format_codemap_tree(codemap: dict[str, Any], include_docstrings: bool = True) -> str:
    """Format the codemap into an ultra-compact indented outline representation."""
    lines: list[str] = []
    modules = codemap.get("modules", {})
    lines.append(f"# Codebase Symbol Map ({len(modules)} modules)")
    lines.append("")

    for mod_path, mod_data in modules.items():
        if mod_data.get("syntax_error"):
            lines.append(f"## {mod_path} [SYNTAX ERROR: {mod_data['syntax_error']}]")
            continue
        if mod_data.get("error"):
            lines.append(f"## {mod_path} [ERROR: {mod_data['error']}]")
            continue
        total_l = mod_data.get("total_lines", 0)
        code_l = mod_data.get("code_lines", 0)
        lines.append(f"## {mod_path} ({code_l} code lines, {total_l} total)")
        if include_docstrings and mod_data.get("doc"):
            lines.append(f"  \"\"\"{mod_data['doc']}\"\"\"")

        for cls in mod_data.get("classes", []):
            bases_str = f"({', '.join(cls['bases'])})" if cls.get("bases") else ""
            lines.append(f"  class {cls['name']}{bases_str}:")
            if include_docstrings and cls.get("doc"):
                lines.append(f"    \"\"\"{cls['doc']}\"\"\"")
            for m in cls.get("methods", []):
                async_pfx = "async " if m.get("is_async") else ""
                lines.append(f"    {async_pfx}def {m['name']}({m['args']}){m['returns']}")
                if include_docstrings and m.get("doc"):
                    lines.append(f"      \"\"\"{m['doc']}\"\"\"")

        for fn in mod_data.get("functions", []):
            async_pfx = "async " if fn.get("is_async") else ""
            lines.append(f"  {async_pfx}def {fn['name']}({fn['args']}){fn['returns']}")
            if include_docstrings and fn.get("doc"):
                lines.append(f"    \"\"\"{fn['doc']}\"\"\"")

        lines.append("")

    return "\n".join(lines).strip()


def search_codemap(codemap: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """Search for matching classes, methods, or functions across the codemap."""
    q = query.strip().lower()
    matches: list[dict[str, Any]] = []

    for mod_path, mod_data in codemap.get("modules", {}).items():
        # Match module doc
        if q in mod_path.lower() or (mod_data.get("doc") and q in mod_data["doc"].lower()):
            matches.append({
                "type": "module",
                "file": mod_path,
                "name": mod_path,
                "doc": mod_data.get("doc", "")
            })

        for cls in mod_data.get("classes", []):
            if q in cls["name"].lower() or (cls.get("doc") and q in cls["doc"].lower()):
                matches.append({
                    "type": "class",
                    "file": mod_path,
                    "name": cls["name"],
                    "line": cls["line"],
                    "doc": cls.get("doc", "")
                })
            for m in cls.get("methods", []):
                if q in m["name"].lower() or (m.get("doc") and q in m["doc"].lower()):
                    matches.append({
                        "type": "method",
                        "file": mod_path,
                        "class": cls["name"],
                        "name": f"{cls['name']}.{m['name']}",
                        "line": m["line"],
                        "args": m["args"],
                        "returns": m["returns"],
                        "doc": m.get("doc", "")
                    })

        for fn in mod_data.get("functions", []):
            if q in fn["name"].lower() or (fn.get("doc") and q in fn["doc"].lower()):
                matches.append({
                    "type": "function",
                    "file": mod_path,
                    "name": fn["name"],
                    "line": fn["line"],
                    "args": fn["args"],
                    "returns": fn["returns"],
                    "doc": fn.get("doc", "")
                })

    return matches


def check_ast_policies(
    source_or_path: str | Path,
    filename: str = "<unknown>",
) -> list[dict[str, Any]]:
    """Deterministic structural AST policy verification (zero runtime dependencies).

    Checks code against reliable engineering invariants:
      - DEAD_PRIVATE_METHOD: private class methods that are never called/referenced within the class.
      - SILENCED_EXCEPTION: broad exceptions (Exception, BaseException, bare except) swallowed silently.
      - UNBOUNDED_RESOURCE_OPEN: standalone file open() without 'with' or try/finally cleanup.
    """
    if isinstance(source_or_path, Path):
        file_path = source_or_path
        filename = str(file_path).replace("\\", "/")
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return [{"rule": "READ_ERROR", "severity": "blocker", "file": filename, "line": 0, "symbol": "", "message": str(e)}]
    elif isinstance(source_or_path, str):
        content = source_or_path
        p = Path(source_or_path)
        if len(source_or_path) < 1024 and "\n" not in source_or_path and p.is_file():
            filename = str(p).replace("\\", "/")
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return [{"rule": "READ_ERROR", "severity": "blocker", "file": filename, "line": 0, "symbol": "", "message": str(e)}]
    else:
        return []

    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError as e:
        return [{
            "rule": "SYNTAX_ERROR",
            "severity": "blocker",
            "file": filename,
            "line": e.lineno or 0,
            "symbol": "",
            "message": f"SyntaxError: {e.msg} at line {e.lineno}",
        }]

    violations: list[dict[str, Any]] = []

    # 1. DEAD_PRIVATE_METHOD check
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            private_methods: dict[str, Any] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_name = item.name
                    if m_name.startswith("_") and not m_name.startswith("__"):
                        # Skip properties, overrides, abstractmethods
                        decs = {ast.unparse(d) if hasattr(ast, "unparse") else "" for d in item.decorator_list}
                        if any("property" in d or "abstractmethod" in d or "override" in d for d in decs):
                            continue
                        private_methods[m_name] = item

            if private_methods:
                referenced: set[str] = set()
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute) and child.attr in private_methods:
                        referenced.add(child.attr)
                    elif isinstance(child, ast.Name) and child.id in private_methods:
                        referenced.add(child.id)

                for m_name, m_node in private_methods.items():
                    if m_name not in referenced:
                        violations.append({
                            "rule": "DEAD_PRIVATE_METHOD",
                            "severity": "major",
                            "file": filename,
                            "line": m_node.lineno,
                            "symbol": f"{node.name}.{m_name}",
                            "message": f"Private method '{node.name}.{m_name}' declared at line {m_node.lineno} is never referenced in class '{node.name}'.",
                        })

    # 2. SILENCED_EXCEPTION check
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            is_broad = False
            exc_name = "bare except"
            if node.type is None:
                is_broad = True
            elif isinstance(node.type, ast.Name):
                exc_name = node.type.id
                if exc_name in ("Exception", "BaseException"):
                    is_broad = True
            elif isinstance(node.type, ast.Tuple):
                names = [elt.id for elt in node.type.elts if isinstance(elt, ast.Name)]
                if any(n in ("Exception", "BaseException") for n in names):
                    is_broad = True
                    exc_name = ", ".join(names)

            if is_broad:
                is_silenced = True
                for stmt in node.body:
                    if isinstance(stmt, (ast.Pass, ast.Continue)):
                        continue
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                        continue
                    is_silenced = False
                    break

                if is_silenced:
                    violations.append({
                        "rule": "SILENCED_EXCEPTION",
                        "severity": "major",
                        "file": filename,
                        "line": node.lineno,
                        "symbol": exc_name,
                        "message": f"Broad exception handler at line {node.lineno} ({exc_name}) silences error without handling, re-raising, or returning.",
                    })

    # 3. UNBOUNDED_RESOURCE_OPEN check
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls_open: list[ast.Call] = []
            has_with_or_finally = False
            for child in ast.walk(node):
                if isinstance(child, ast.With):
                    has_with_or_finally = True
                elif isinstance(child, ast.Try) and child.finalbody:
                    has_with_or_finally = True
                elif isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name) and child.func.id == "open":
                        calls_open.append(child)

            if calls_open and not has_with_or_finally:
                for c in calls_open:
                    violations.append({
                        "rule": "UNBOUNDED_RESOURCE_OPEN",
                        "severity": "major",
                        "file": filename,
                        "line": c.lineno,
                        "symbol": "open",
                        "message": f"Standalone 'open()' call at line {c.lineno} outside a 'with' context manager or try/finally block.",
                    })

    violations.sort(key=lambda x: (x.get("line", 0), x.get("rule", "")))
    return violations


def detect_anti_shims(
    source_or_path: str | Path,
    filename: str = "<unknown>",
) -> list[dict[str, Any]]:
    """Deterministic Anti-Shim & Fake Implementation Sentinel (inspired by mgantlett/nomos and xenups/Asha).

    Detects fake agent shortcuts, stubs, and mock cheats:
      - EMPTY_STUB_BODY: functions/methods consisting only of pass, ellipsis, or docstring without logic.
      - TRIVIAL_RETURN_STUB: non-predicate functions returning hardcoded constants (True, False, None, "") without computation.
      - NOT_IMPLEMENTED_STUB: functions raising NotImplementedError or Exception("TODO").
      - TRIVIAL_ASSERTION_CHEAT: test functions with trivial tautological assertions (assert True, assert 1 == 1).
    """
    if isinstance(source_or_path, Path):
        file_path = source_or_path
        filename = str(file_path).replace("\\", "/")
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return [{"rule": "READ_ERROR", "severity": "blocker", "file": filename, "line": 0, "symbol": "", "message": str(e)}]
    elif isinstance(source_or_path, str):
        content = source_or_path
        p = Path(source_or_path)
        if len(source_or_path) < 1024 and "\n" not in source_or_path and p.is_file():
            filename = str(p).replace("\\", "/")
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                return [{"rule": "READ_ERROR", "severity": "blocker", "file": filename, "line": 0, "symbol": "", "message": str(e)}]
    else:
        return []

    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError:
        return []

    violations: list[dict[str, Any]] = []
    is_test_file = "test" in filename.lower()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_name = node.name
            decs = {ast.unparse(d) if hasattr(ast, "unparse") else "" for d in node.decorator_list}
            if any("abstractmethod" in d or "overload" in d for d in decs):
                continue

            # Filter docstring
            body_stmts = [
                s for s in node.body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
            ]

            # 1. EMPTY_STUB_BODY: only pass or ellipsis
            if not body_stmts:
                violations.append({
                    "rule": "EMPTY_STUB_BODY",
                    "severity": "major",
                    "file": filename,
                    "line": node.lineno,
                    "symbol": fn_name,
                    "message": f"Function '{fn_name}' has an empty body without implementation.",
                })
            elif len(body_stmts) == 1:
                stmt = body_stmts[0]
                if isinstance(stmt, ast.Pass):
                    violations.append({
                        "rule": "EMPTY_STUB_BODY",
                        "severity": "major",
                        "file": filename,
                        "line": node.lineno,
                        "symbol": fn_name,
                        "message": f"Function '{fn_name}' consists solely of a 'pass' stub.",
                    })
                elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
                    violations.append({
                        "rule": "EMPTY_STUB_BODY",
                        "severity": "major",
                        "file": filename,
                        "line": node.lineno,
                        "symbol": fn_name,
                        "message": f"Function '{fn_name}' consists solely of an Ellipsis (...) stub.",
                    })

                # 2. NOT_IMPLEMENTED_STUB
                elif isinstance(stmt, ast.Raise):
                    exc_name = ""
                    if isinstance(stmt.exc, ast.Name):
                        exc_name = stmt.exc.id
                    elif isinstance(stmt.exc, ast.Call) and isinstance(stmt.exc.func, ast.Name):
                        exc_name = stmt.exc.func.id
                    if exc_name in ("NotImplementedError", "NotImplemented"):
                        violations.append({
                            "rule": "NOT_IMPLEMENTED_STUB",
                            "severity": "major",
                            "file": filename,
                            "line": node.lineno,
                            "symbol": fn_name,
                            "message": f"Function '{fn_name}' raises NotImplementedError stub.",
                        })

                # 3. TRIVIAL_RETURN_STUB
                elif isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant):
                    # Exclude boolean predicates and dunder methods
                    is_predicate = any(fn_name.startswith(pfx) for pfx in ("is_", "has_", "can_", "supports_"))
                    if not is_predicate and not fn_name.startswith("__") and not fn_name.startswith("test_"):
                        val_repr = repr(stmt.value.value)
                        violations.append({
                            "rule": "TRIVIAL_RETURN_STUB",
                            "severity": "major",
                            "file": filename,
                            "line": node.lineno,
                            "symbol": fn_name,
                            "message": f"Function '{fn_name}' trivially returns constant {val_repr} without logic.",
                        })

            # 4. TRIVIAL_ASSERTION_CHEAT (in tests)
            if is_test_file or fn_name.startswith("test_"):
                for child in ast.walk(node):
                    if isinstance(child, ast.Assert):
                        if isinstance(child.test, ast.Constant) and child.test.value is True:
                            violations.append({
                                "rule": "TRIVIAL_ASSERTION_CHEAT",
                                "severity": "blocker",
                                "file": filename,
                                "line": child.lineno,
                                "symbol": f"{fn_name}:assert True",
                                "message": f"Test '{fn_name}' contains tautological 'assert True' cheat.",
                            })
                        elif (
                            isinstance(child.test, ast.Compare)
                            and len(child.test.ops) == 1
                            and isinstance(child.test.ops[0], ast.Eq)
                            and hasattr(ast, "unparse")
                            and ast.unparse(child.test.left) == ast.unparse(child.test.comparators[0])
                        ):
                            expr_repr = ast.unparse(child.test.left)
                            violations.append({
                                "rule": "TRIVIAL_ASSERTION_CHEAT",
                                "severity": "blocker",
                                "file": filename,
                                "line": child.lineno,
                                "symbol": f"{fn_name}:assert {expr_repr} == {expr_repr}",
                                "message": f"Test '{fn_name}' contains tautological equality assertion cheat.",
                            })

    violations.sort(key=lambda x: (x.get("line", 0), x.get("rule", "")))
    return violations


def format_anti_shim_report(violations: list[dict[str, Any]]) -> str:
    """Format detected anti-shim stub violations into clean Markdown."""
    lines = [
        f"# Anti-Shim Sentinel Report ({len(violations)} issues found)",
        "",
        "## Summary",
        f"- Total Stub Invariants Checked: {len(violations)}",
        f"- Status: {'CLEAN (No shims detected)' if not violations else 'VIOLATIONS DETECTED'}",
        "",
    ]
    if not violations:
        lines.append("- No stub functions, trivial return values, or mock cheats detected.")
    else:
        lines.append("## Detected Shims & Stubs")
        for idx, v in enumerate(violations, 1):
            lines.append(f"{idx}. [{v.get('severity', 'major').upper()}] `{v.get('symbol')}` ({v.get('file')}:{v.get('line')})")
            lines.append(f"   *Rule:* `{v.get('rule')}` — {v.get('message')}")
    return "\n".join(lines).strip()


def scan_ast_policies(
    root_path: Path,
    max_depth: int = 5,
    exclude_dirs: set[str] | None = None,
    include_anti_shims: bool = False,
) -> list[dict[str, Any]]:
    """Scan all Python modules under root_path for AST policy violations."""
    excludes = DEFAULT_EXCLUDES | (exclude_dirs or set())
    root = root_path.resolve()
    all_violations: list[dict[str, Any]] = []

    if root.is_file():
        if root.suffix == ".py":
            res = check_ast_policies(root)
            if include_anti_shims:
                res.extend(detect_anti_shims(root))
            return res
        return []

    for path in sorted(root.rglob("*.py")):
        parts = set(path.relative_to(root).parts[:-1])
        if parts & excludes:
            continue
        if len(path.relative_to(root).parts) > max_depth:
            continue
        all_violations.extend(check_ast_policies(path))
        if include_anti_shims:
            all_violations.extend(detect_anti_shims(path))

    return all_violations


DEFAULT_BOUNDARY_RULES: list[dict[str, Any]] = [
    {
        "name": "CORE_CANNOT_IMPORT_PRESENTATION",
        "description": "Core engines and domain logic must not import CLI/presentation modules",
        "source_regex": r"(^|/)(evidence|runs|env|memory|vector|codemap|answers|audit|sandbox|codegen)\.py$",
        "forbidden_regex": r"(^|/)(cli|gateway|server)\.py$",
        "severity": "major",
    },
    {
        "name": "PROD_CANNOT_IMPORT_TESTS",
        "description": "Production modules must not import test files",
        "source_regex": r"^src/",
        "forbidden_regex": r"^tests/",
        "severity": "blocker",
    },
]

_FROM_IMPORT_RE = re.compile(r"^from\s+([.\w]+)\s+import\s+(.+)$")
_IMPORT_RE = re.compile(r"^import\s+([.\w\s,]+)$")
_JS_IMPORT_RE = re.compile(r"""(?:import|from|require\s*\()\s*['"]([.][^'"]+)['"]""")


def build_dependency_graph(codemap: dict[str, Any]) -> dict[str, Any]:
    """Build directed module dependency graph from codemap symbol definitions (zero tokens)."""
    modules = codemap.get("modules", {})
    all_modules = sorted(modules.keys())

    # Build multi-resolution module lookup table
    module_lookup: dict[str, str] = {}
    stem_counts: dict[str, int] = collections.defaultdict(int)
    stem_to_mod: dict[str, str] = {}

    for m_path in all_modules:
        norm_path = m_path.replace("\\", "/")
        module_lookup[norm_path] = norm_path
        p = Path(norm_path)
        no_ext = str(p.with_suffix("")).replace("\\", "/")
        module_lookup[no_ext] = norm_path

        # Stem lookup if unique
        stem = p.stem
        stem_counts[stem] += 1
        stem_to_mod[stem] = norm_path

        # Python dotted path resolution
        parts = list(p.parts)
        if parts and parts[-1].endswith(p.suffix) and p.suffix:
            parts[-1] = p.stem
        if parts and parts[-1] == "__init__":
            parts.pop()

        if parts:
            dotted_full = ".".join(parts)
            module_lookup[dotted_full] = norm_path
            # If path starts with src/ or lib/, also index package root directly
            if len(parts) > 1 and parts[0] in ("src", "lib"):
                dotted_sub = ".".join(parts[1:])
                module_lookup[dotted_sub] = norm_path

    # Only map unique stems to prevent ambiguous collisions
    for stem, count in stem_counts.items():
        if count == 1:
            module_lookup[stem] = stem_to_mod[stem]

    def _resolve_candidate(cand: str) -> str | None:
        if cand in module_lookup:
            return module_lookup[cand]
        if "." in cand:
            parts = cand.split(".")
            for k in range(len(parts), 0, -1):
                sub_slash = "/".join(parts[-k:])
                if sub_slash in module_lookup:
                    return module_lookup[sub_slash]
            last = parts[-1]
            if last in stem_to_mod:
                return stem_to_mod[last]
        if cand in stem_to_mod:
            return stem_to_mod[cand]
        return None

    adjacency: dict[str, set[str]] = {m: set() for m in all_modules}
    reverse_adj: dict[str, set[str]] = {m: set() for m in all_modules}
    external_imports: dict[str, set[str]] = {m: set() for m in all_modules}

    for src_mod, mod_data in modules.items():
        norm_src = src_mod.replace("\\", "/")
        src_dir = Path(norm_src).parent
        imports = mod_data.get("imports", [])

        for imp in imports:
            raw_imp = imp.strip()
            matched = False

            # 1. Python "from <module> import <names>"
            m_from = _FROM_IMPORT_RE.match(raw_imp)
            if m_from:
                mod_name, names_str = m_from.group(1), m_from.group(2)
                if mod_name.startswith("."):
                    dot_count = len(mod_name) - len(mod_name.lstrip("."))
                    remainder = mod_name.lstrip(".")
                    target_dir = src_dir
                    for _ in range(dot_count - 1):
                        target_dir = target_dir.parent

                    if remainder:
                        cand1 = str(target_dir / f"{remainder}.py").replace("\\", "/")
                        cand2 = str(target_dir / remainder / "__init__.py").replace("\\", "/")
                        if cand1 in modules:
                            adjacency[norm_src].add(cand1)
                            matched = True
                        elif cand2 in modules:
                            adjacency[norm_src].add(cand2)
                            matched = True
                    else:
                        names = [n.strip().split()[0] for n in names_str.split(",") if n.strip()]
                        for n in names:
                            cand1 = str(target_dir / f"{n}.py").replace("\\", "/")
                            cand2 = str(target_dir / n / "__init__.py").replace("\\", "/")
                            if cand1 in modules:
                                adjacency[norm_src].add(cand1)
                                matched = True
                            elif cand2 in modules:
                                adjacency[norm_src].add(cand2)
                                matched = True
                else:
                    direct_target = _resolve_candidate(mod_name)
                    if direct_target:
                        adjacency[norm_src].add(direct_target)
                        matched = True

                    names = [n.strip().split()[0] for n in names_str.split(",") if n.strip()]
                    for n in names:
                        combined = f"{mod_name}.{n}"
                        sub_target = _resolve_candidate(combined)
                        if sub_target:
                            adjacency[norm_src].add(sub_target)
                            matched = True
                        elif n in stem_to_mod and stem_counts[n] == 1:
                            adjacency[norm_src].add(stem_to_mod[n])
                            matched = True

                if not matched and not mod_name.startswith("."):
                    external_imports[norm_src].add(mod_name.split(".")[0])
                continue

            # 2. Python "import <modules>"
            m_imp = _IMPORT_RE.match(raw_imp)
            if m_imp:
                items = [x.strip().split()[0] for x in m_imp.group(1).split(",") if x.strip()]
                for item in items:
                    target = _resolve_candidate(item)
                    if target:
                        adjacency[norm_src].add(target)
                        matched = True
                    else:
                        external_imports[norm_src].add(item.split(".")[0])
                continue

            # 3. JS/TS relative imports
            m_js = _JS_IMPORT_RE.search(raw_imp)
            if m_js:
                rel_cand = m_js.group(1)
                base = (src_dir / rel_cand).as_posix()
                for ext in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                    attempt = base + ext
                    if attempt in modules:
                        adjacency[norm_src].add(attempt)
                        matched = True
                        break
                continue

            # 4. Bare Python module name from ast.Import (e.g. "beta" or "beta as b" or "os")
            mod_item = raw_imp.split()[0]
            if not mod_item.startswith((".", "'", '"', "/", "\\")):
                target = _resolve_candidate(mod_item)
                if target:
                    adjacency[norm_src].add(target)
                    matched = True
                else:
                    external_imports[norm_src].add(mod_item.split(".")[0])

        # Remove self-loops
        adjacency[norm_src].discard(norm_src)

    # Populate reverse adjacency
    for src, tgts in adjacency.items():
        for tgt in tgts:
            if tgt in reverse_adj:
                reverse_adj[tgt].add(src)

    edges = [
        {"source": src, "target": tgt}
        for src in all_modules
        for tgt in sorted(adjacency[src])
    ]

    return {
        "modules": all_modules,
        "edges": edges,
        "adjacency": {m: sorted(adjacency[m]) for m in all_modules},
        "reverse_adjacency": {m: sorted(reverse_adj[m]) for m in all_modules},
        "in_degree": {m: len(reverse_adj[m]) for m in all_modules},
        "out_degree": {m: len(adjacency[m]) for m in all_modules},
        "external_imports": {m: sorted(external_imports[m]) for m in all_modules},
    }


def detect_architecture_cycles(
    dep_graph: dict[str, Any] | dict[str, list[str]],
    max_cycles: int = 200,
) -> list[list[str]]:
    """Detect all elementary directed cycles in the dependency graph using ordered Tiernan search."""
    if isinstance(dep_graph, dict) and "adjacency" in dep_graph:
        adj: dict[str, list[str]] = dep_graph["adjacency"]
    elif isinstance(dep_graph, dict):
        adj = dep_graph
    else:
        return []

    cycles: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    nodes = sorted(adj.keys())

    def dfs(start: str, curr: str, path: list[str]) -> None:
        if len(cycles) >= max_cycles:
            return
        for nxt in sorted(adj.get(curr, [])):
            if nxt == start and len(path) > 1:
                min_elem = min(path)
                min_idx = path.index(min_elem)
                can = tuple(path[min_idx:] + path[:min_idx])
                if can not in seen:
                    seen.add(can)
                    cycles.append(list(can))
            elif nxt > start and nxt not in path:
                dfs(start, nxt, path + [nxt])

    for n in nodes:
        dfs(n, n, [n])

    return sorted(cycles, key=lambda c: (len(c), c))


def detect_boundary_violations(
    dep_graph: dict[str, Any] | dict[str, list[str]],
    rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Detect architectural boundary and layer isolation violations across module dependencies."""
    if isinstance(dep_graph, dict) and "edges" in dep_graph:
        edges = dep_graph["edges"]
    elif isinstance(dep_graph, dict) and "adjacency" in dep_graph:
        edges = [{"source": s, "target": t} for s, targets in dep_graph["adjacency"].items() for t in targets]
    elif isinstance(dep_graph, dict):
        edges = [{"source": s, "target": t} for s, targets in dep_graph.items() for t in targets]
    else:
        edges = []

    active_rules = rules if rules is not None else DEFAULT_BOUNDARY_RULES
    compiled = []
    for r in active_rules:
        compiled.append((
            r,
            re.compile(r["source_regex"], re.IGNORECASE),
            re.compile(r["forbidden_regex"], re.IGNORECASE),
        ))

    violations: list[dict[str, Any]] = []
    for edge in edges:
        src = edge.get("source", "").replace("\\", "/")
        tgt = edge.get("target", "").replace("\\", "/")
        for rule_meta, s_pat, f_pat in compiled:
            if s_pat.search(src) and f_pat.search(tgt):
                violations.append({
                    "rule": rule_meta["name"],
                    "severity": rule_meta.get("severity", "major"),
                    "source": src,
                    "target": tgt,
                    "message": f"Boundary violation: '{src}' imports '{tgt}'. {rule_meta.get('description', '')}",
                })

    violations.sort(key=lambda x: (x.get("severity") != "blocker", x.get("source", ""), x.get("target", "")))
    return violations


def architecture_diff(
    base_graph: dict[str, Any],
    head_graph: dict[str, Any],
    rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Calculate deterministic architectural diff between base and head dependency graphs without tokens."""
    base_mods = set(base_graph.get("modules", []))
    head_mods = set(head_graph.get("modules", []))

    base_edges = {(e["source"], e["target"]) for e in base_graph.get("edges", [])}
    head_edges = {(e["source"], e["target"]) for e in head_graph.get("edges", [])}

    base_cycles = [tuple(c) for c in detect_architecture_cycles(base_graph)]
    head_cycles = [tuple(c) for c in detect_architecture_cycles(head_graph)]

    base_violations = detect_boundary_violations(base_graph, rules)
    head_violations = detect_boundary_violations(head_graph, rules)

    new_cycles = [list(c) for c in sorted(set(head_cycles) - set(base_cycles))]
    resolved_cycles = [list(c) for c in sorted(set(base_cycles) - set(head_cycles))]

    base_violation_keys = {(v["rule"], v["source"], v["target"]) for v in base_violations}
    new_violations = [v for v in head_violations if (v["rule"], v["source"], v["target"]) not in base_violation_keys]
    head_violation_keys = {(v["rule"], v["source"], v["target"]) for v in head_violations}
    resolved_violations = [v for v in base_violations if (v["rule"], v["source"], v["target"]) not in head_violation_keys]

    added_edges = sorted([{"source": s, "target": t} for s, t in (head_edges - base_edges)], key=lambda x: (x["source"], x["target"]))
    removed_edges = sorted([{"source": s, "target": t} for s, t in (base_edges - head_edges)], key=lambda x: (x["source"], x["target"]))

    has_regressions = bool(new_cycles or new_violations)
    verdict = "violation" if has_regressions else "clean"

    return {
        "verdict": verdict,
        "added_modules": sorted(list(head_mods - base_mods)),
        "removed_modules": sorted(list(base_mods - head_mods)),
        "added_edges": added_edges,
        "removed_edges": removed_edges,
        "new_cycles": new_cycles,
        "resolved_cycles": resolved_cycles,
        "new_violations": new_violations,
        "resolved_violations": resolved_violations,
        "total_head_cycles": len(head_cycles),
        "total_head_violations": len(head_violations),
    }


def format_architecture_report(arch_data: dict[str, Any]) -> str:
    """Format architecture dependency analysis into clean, structured Markdown."""
    dep_graph = arch_data.get("dep_graph", {})
    modules = dep_graph.get("modules", [])
    edges = dep_graph.get("edges", [])
    cycles = arch_data.get("cycles", [])
    violations = arch_data.get("violations", [])

    lines: list[str] = [
        f"# Architecture Dependency Graph ({len(modules)} modules, {len(edges)} dependencies)",
        "",
        "## Summary",
        f"- Modules: {len(modules)}",
        f"- Internal Dependencies: {len(edges)}",
        f"- Cycles Detected: {len(cycles)}",
        f"- Boundary Violations: {len(violations)}",
        "",
        "## Cycles",
    ]

    if not cycles:
        lines.append("- None detected (graph is a directed acyclic component)")
    else:
        for idx, cycle in enumerate(cycles, 1):
            cycle_str = " -> ".join(cycle) + f" -> {cycle[0]}"
            lines.append(f"{idx}. {cycle_str}")

    lines.append("")
    lines.append("## Boundary Violations")
    if not violations:
        lines.append("- None detected (all layer isolation invariants satisfied)")
    else:
        for idx, v in enumerate(violations, 1):
            lines.append(f"{idx}. [{v.get('severity', 'major').upper()}] {v.get('source')} -> {v.get('target')}: {v.get('message')}")

    in_degrees = dep_graph.get("in_degree", {})
    if in_degrees:
        sorted_central = sorted(in_degrees.items(), key=lambda kv: kv[1], reverse=True)[:5]
        top_refs = [f"- `{mod}`: {cnt} incoming references" for mod, cnt in sorted_central if cnt > 0]
        if top_refs:
            lines.append("")
            lines.append("## Most Referenced Modules")
            lines.extend(top_refs)

    return "\n".join(lines).strip()


def build_call_graph(codemap: dict[str, Any]) -> dict[str, Any]:
    """Extract symbol-level call graph across modules from AST codemap.

    Returns a deterministic graph representation containing:
      - nodes: dict[str, dict] with metadata (id, file, name, short_name, kind, line)
      - edges: list[dict] with (caller, callee, line)
      - callers: dict[str, list[str]] (callee_id -> list of caller_ids)
      - callees: dict[str, list[str]] (caller_id -> list of callee_ids)
    """
    nodes: dict[str, dict[str, Any]] = {}
    symbols_by_name: dict[str, list[str]] = {}
    symbols_by_short: dict[str, list[str]] = {}
    symbols_by_mod_and_name: dict[tuple[str, str], str] = {}
    symbols_by_mod_and_short: dict[tuple[str, str], list[str]] = {}
    stems_to_mods: dict[str, list[str]] = {}
    modules_imports: dict[str, dict[str, str]] = {}

    modules = codemap.get("modules", {})

    # 1. Register all symbol nodes
    for mod_path, mod_data in modules.items():
        norm_path = mod_path.replace("\\", "/")
        stem = Path(norm_path).stem
        stems_to_mods.setdefault(stem, []).append(norm_path)

        # Parse imports for this module
        mod_imp_map: dict[str, str] = {}
        for imp_str in mod_data.get("imports", []):
            imp_str = imp_str.strip()
            if imp_str.startswith("from "):
                # e.g., from autoharness.secrets import redact_secrets, mask as m
                parts = imp_str.split(" import ", 1)
                if len(parts) == 2:
                    from_mod = parts[0][5:].strip()
                    imported_names = [n.strip() for n in parts[1].split(",")]
                    for item in imported_names:
                        if " as " in item:
                            orig, alias = item.split(" as ", 1)
                            mod_imp_map[alias.strip()] = f"{from_mod}:{orig.strip()}"
                        else:
                            mod_imp_map[item] = f"{from_mod}:{item}"
            elif imp_str.startswith("import "):
                # e.g., import autoharness.secrets as sec
                imp_body = imp_str[7:].strip()
                for item in imp_body.split(","):
                    item = item.strip()
                    if " as " in item:
                        orig, alias = item.split(" as ", 1)
                        mod_imp_map[alias.strip()] = orig.strip()
                    else:
                        mod_imp_map[item] = item
        modules_imports[norm_path] = mod_imp_map

        # Classes and methods
        for cls in mod_data.get("classes", []):
            cls_name = cls["name"]
            for m in cls.get("methods", []):
                sym_id = f"{norm_path}:{cls_name}.{m['name']}"
                node_info = {
                    "id": sym_id,
                    "file": norm_path,
                    "class": cls_name,
                    "name": f"{cls_name}.{m['name']}",
                    "short_name": m["name"],
                    "kind": "method",
                    "line": m.get("line", 0),
                    "calls": m.get("calls", []),
                }
                nodes[sym_id] = node_info
                symbols_by_name.setdefault(node_info["name"], []).append(sym_id)
                symbols_by_short.setdefault(m["name"], []).append(sym_id)
                symbols_by_mod_and_name[(norm_path, node_info["name"])] = sym_id
                symbols_by_mod_and_name[(stem, node_info["name"])] = sym_id
                symbols_by_mod_and_short.setdefault((norm_path, m["name"]), []).append(sym_id)
                symbols_by_mod_and_short.setdefault((stem, m["name"]), []).append(sym_id)

        # Standalone functions
        for fn in mod_data.get("functions", []):
            sym_id = f"{norm_path}:{fn['name']}"
            node_info = {
                "id": sym_id,
                "file": norm_path,
                "class": None,
                "name": fn["name"],
                "short_name": fn["name"],
                "kind": "function",
                "line": fn.get("line", 0),
                "calls": fn.get("calls", []),
            }
            nodes[sym_id] = node_info
            symbols_by_name.setdefault(fn["name"], []).append(sym_id)
            symbols_by_short.setdefault(fn["name"], []).append(sym_id)
            symbols_by_mod_and_name[(norm_path, fn["name"])] = sym_id
            symbols_by_mod_and_name[(stem, fn["name"])] = sym_id
            symbols_by_mod_and_short.setdefault((norm_path, fn["name"]), []).append(sym_id)
            symbols_by_mod_and_short.setdefault((stem, fn["name"]), []).append(sym_id)

    # 2. Resolve calls into graph edges
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, int]] = set()

    for caller_id, caller_node in nodes.items():
        norm_path = caller_node["file"]
        stem = Path(norm_path).stem
        cls_name = caller_node.get("class")
        mod_imp_map = modules_imports.get(norm_path, {})

        for call in caller_node.get("calls", []):
            call_raw = call.get("name", "").strip()
            if not call_raw:
                continue

            callee_id: str | None = None

            # Case A: self.method or cls.method
            if call_raw.startswith("self.") or call_raw.startswith("cls."):
                m_target = call_raw.split(".", 1)[1]
                if cls_name:
                    callee_id = symbols_by_mod_and_name.get((norm_path, f"{cls_name}.{m_target}"))
                if not callee_id:
                    candidates = symbols_by_short.get(m_target, [])
                    if len(candidates) == 1:
                        callee_id = candidates[0]

            # Case B: module.attr or Class.attr or obj.method
            elif "." in call_raw:
                prefix, attr = call_raw.split(".", 1)
                # Check if prefix is imported alias or target
                if prefix in mod_imp_map:
                    target_info = mod_imp_map[prefix]
                    # Could be "autoharness.secrets:redact_secrets" or module "secrets"
                    if ":" in target_info:
                        m_mod, m_sym = target_info.split(":", 1)
                        callee_id = symbols_by_name.get(f"{attr}", [None])[0] or symbols_by_short.get(attr, [None])[0]
                    else:
                        callee_mod_stem = Path(target_info).stem
                        callee_id = symbols_by_mod_and_name.get((callee_mod_stem, attr)) or \
                                    symbols_by_mod_and_short.get((callee_mod_stem, attr), [None])[0]
                elif prefix in stems_to_mods:
                    callee_id = symbols_by_mod_and_name.get((prefix, attr)) or \
                                symbols_by_mod_and_short.get((prefix, attr), [None])[0]
                elif (norm_path, f"{prefix}.{attr}") in symbols_by_mod_and_name:
                    callee_id = symbols_by_mod_and_name[(norm_path, f"{prefix}.{attr}")]
                elif f"{prefix}.{attr}" in symbols_by_name:
                    callee_id = symbols_by_name[f"{prefix}.{attr}"][0]

            # Case C: simple function name
            else:
                # 1. Local symbol in the same module
                callee_id = symbols_by_mod_and_name.get((norm_path, call_raw))
                # 2. Imported symbol
                if not callee_id and call_raw in mod_imp_map:
                    target_info = mod_imp_map[call_raw]
                    if ":" in target_info:
                        _, target_sym = target_info.split(":", 1)
                        cands = symbols_by_name.get(target_sym, [])
                        if cands:
                            callee_id = cands[0]
                    elif target_info in symbols_by_name:
                        callee_id = symbols_by_name[target_info][0]
                # 3. Global unique symbol
                if not callee_id:
                    cands = symbols_by_name.get(call_raw, [])
                    if len(cands) == 1:
                        callee_id = cands[0]

            if callee_id and callee_id in nodes:
                edge_key = (caller_id, callee_id, call.get("line", 0))
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    edges.append({
                        "caller": caller_id,
                        "callee": callee_id,
                        "line": call.get("line", 0)
                    })

    # 3. Index callers and callees
    callers_map: dict[str, list[str]] = {nid: [] for nid in nodes}
    callees_map: dict[str, list[str]] = {nid: [] for nid in nodes}

    for e in edges:
        c_caller = e["caller"]
        c_callee = e["callee"]
        if c_callee not in callees_map[c_caller]:
            callees_map[c_caller].append(c_callee)
        if c_caller not in callers_map[c_callee]:
            callers_map[c_callee].append(c_caller)

    for k in callers_map:
        callers_map[k].sort()
    for k in callees_map:
        callees_map[k].sort()

    return {
        "nodes": nodes,
        "edges": edges,
        "callers": callers_map,
        "callees": callees_map,
        "nodes_count": len(nodes),
        "edges_count": len(edges),
    }


def _resolve_symbol_query(call_graph: dict[str, Any], query: str) -> list[str]:
    """Find symbol IDs matching a search query with deterministic ranking."""
    q = query.strip()
    if not q:
        return []
    nodes = call_graph.get("nodes", {})
    if q in nodes:
        return [q]

    exact_name: list[str] = []
    exact_short: list[str] = []
    suffix_match: list[str] = []
    substr_match: list[str] = []

    q_lower = q.lower()

    for nid, node in nodes.items():
        name = node.get("name", "")
        short = node.get("short_name", "")
        if name == q:
            exact_name.append(nid)
        elif short == q:
            exact_short.append(nid)
        elif nid.endswith(f":{q}") or nid.endswith(f".{q}"):
            suffix_match.append(nid)
        elif q_lower in nid.lower() or q_lower in name.lower():
            substr_match.append(nid)

    res: list[str] = []
    for cand_group in (exact_name, exact_short, suffix_match, substr_match):
        for cid in sorted(cand_group):
            if cid not in res:
                res.append(cid)
    return res


def find_callers(call_graph: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """Return direct callers of symbols matching query."""
    resolved = _resolve_symbol_query(call_graph, query)
    if not resolved:
        return []
    target_id = resolved[0]
    callers = call_graph.get("callers", {}).get(target_id, [])
    nodes = call_graph.get("nodes", {})

    result: list[dict[str, Any]] = []
    for cid in callers:
        node = nodes.get(cid, {})
        result.append({
            "id": cid,
            "name": node.get("name", cid),
            "file": node.get("file", ""),
            "line": node.get("line", 0),
            "kind": node.get("kind", ""),
        })
    result.sort(key=lambda x: (x["file"], x["line"], x["name"]))
    return result


def find_callees(call_graph: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """Return direct callees called by symbols matching query."""
    resolved = _resolve_symbol_query(call_graph, query)
    if not resolved:
        return []
    target_id = resolved[0]
    callees = call_graph.get("callees", {}).get(target_id, [])
    nodes = call_graph.get("nodes", {})

    result: list[dict[str, Any]] = []
    for cid in callees:
        node = nodes.get(cid, {})
        result.append({
            "id": cid,
            "name": node.get("name", cid),
            "file": node.get("file", ""),
            "line": node.get("line", 0),
            "kind": node.get("kind", ""),
        })
    result.sort(key=lambda x: (x["file"], x["line"], x["name"]))
    return result


def analyze_impact(
    call_graph: dict[str, Any],
    query: str,
    max_depth: int = 5,
) -> dict[str, Any]:
    """Calculate blast radius and transitive upstream impact for a symbol without tokens."""
    resolved = _resolve_symbol_query(call_graph, query)
    if not resolved:
        return {
            "error": f"Symbol not found matching query: {query!r}",
            "query": query,
            "impact_radius": 0,
            "direct_callers": [],
            "direct_callees": [],
            "impacted_symbols": [],
            "impacted_tests": [],
            "impacted_entrypoints": [],
        }

    target_id = resolved[0]
    nodes = call_graph.get("nodes", {})
    callers_map = call_graph.get("callers", {})
    target_node = nodes.get(target_id, {})

    # Breadth-first search along reverse caller edges
    queue: list[tuple[str, int]] = [(target_id, 0)]
    visited: set[str] = {target_id}
    depth_map: dict[str, int] = {}

    while queue:
        curr, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        for caller in callers_map.get(curr, []):
            if caller not in visited:
                visited.add(caller)
                depth_map[caller] = depth + 1
                queue.append((caller, depth + 1))

    impacted_symbols: list[dict[str, Any]] = []
    impacted_tests: list[dict[str, Any]] = []
    impacted_entrypoints: list[dict[str, Any]] = []

    sorted_callers = sorted(depth_map.keys(), key=lambda x: (depth_map[x], nodes.get(x, {}).get("file", ""), x))

    for cid in sorted_callers:
        node = nodes.get(cid, {})
        d = depth_map[cid]
        fpath = node.get("file", "")
        name = node.get("name", "")
        item = {
            "id": cid,
            "name": name,
            "file": fpath,
            "line": node.get("line", 0),
            "kind": node.get("kind", ""),
            "depth": d,
        }
        impacted_symbols.append(item)

        # Classify tests
        if "test" in fpath.lower() or name.lower().startswith("test_"):
            impacted_tests.append(item)

        # Classify entrypoints (CLI entry, main, or zero incoming non-test callers)
        if "cli" in fpath.lower() or "__main__" in fpath.lower() or not callers_map.get(cid):
            impacted_entrypoints.append(item)

    direct_callers = find_callers(call_graph, target_id)
    direct_callees = find_callees(call_graph, target_id)

    return {
        "target": {
            "id": target_id,
            "name": target_node.get("name", target_id),
            "file": target_node.get("file", ""),
            "line": target_node.get("line", 0),
            "kind": target_node.get("kind", ""),
        },
        "impact_radius": len(impacted_symbols),
        "max_depth": max_depth,
        "direct_callers_count": len(direct_callers),
        "direct_callees_count": len(direct_callees),
        "direct_callers": direct_callers,
        "direct_callees": direct_callees,
        "impacted_symbols": impacted_symbols,
        "impacted_tests": impacted_tests,
        "impacted_entrypoints": impacted_entrypoints,
    }


def format_impact_report(impact: dict[str, Any]) -> str:
    """Format symbol impact blast radius into clean, structured Markdown."""
    if "error" in impact:
        return f"# Symbol Impact Analysis\n\nError: {impact['error']}"

    target = impact.get("target", {})
    t_id = target.get("id", "unknown")
    t_file = target.get("file", "")
    t_line = target.get("line", 0)
    radius = impact.get("impact_radius", 0)
    direct_callers = impact.get("direct_callers", [])
    direct_callees = impact.get("direct_callees", [])
    tests = impact.get("impacted_tests", [])
    entrypoints = impact.get("impacted_entrypoints", [])
    impacted = impact.get("impacted_symbols", [])

    lines = [
        f"# Symbol Impact Analysis for `{t_id}`",
        "",
        "## Summary",
        f"- Target Symbol: `{t_id}` ({t_file}:{t_line})",
        f"- Direct Callers: {len(direct_callers)}",
        f"- Direct Callees: {len(direct_callees)}",
        f"- Transitive Impact Radius: {radius} symbols",
        f"- Impacted Tests: {len(tests)}",
        f"- Impacted Entrypoints: {len(entrypoints)}",
        "",
        "## Direct Callers",
    ]
    if not direct_callers:
        lines.append("- None (no callers found in scanned codebase)")
    else:
        for c in direct_callers:
            lines.append(f"- `{c['id']}` (line {c.get('line', 0)})")

    lines.append("")
    lines.append("## Direct Callees")
    if not direct_callees:
        lines.append("- None (no callees resolved in scanned codebase)")
    else:
        for c in direct_callees:
            lines.append(f"- `{c['id']}` (line {c.get('line', 0)})")

    lines.append("")
    lines.append(f"## Transitive Impact (Depth <= {impact.get('max_depth', 5)})")
    if not impacted:
        lines.append("- No upstream symbols affected")
    else:
        for sym in impacted:
            lines.append(f"- [Depth {sym['depth']}] `{sym['id']}` ({sym['file']}:{sym['line']})")

    lines.append("")
    lines.append("## Impacted Tests")
    if not tests:
        lines.append("- No direct or indirect tests found calling this symbol")
    else:
        for t in tests:
            lines.append(f"- [Depth {t['depth']}] `{t['id']}` ({t['file']}:{t['line']})")

    lines.append("")
    lines.append("## Impacted Entrypoints")
    if not entrypoints:
        lines.append("- None")
    else:
        for ep in entrypoints:
            lines.append(f"- [Depth {ep['depth']}] `{ep['id']}` ({ep['file']}:{ep['line']})")

    return "\n".join(lines).strip()


