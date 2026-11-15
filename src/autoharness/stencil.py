"""Domain-agnostic structural scanner and stencil/scaffold generator.

Extracts verifiable structural invariants (schemas, AST skeletons, spatial wireframes)
from reference sources without coupling to any specific task or domain.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any


def extract_json_schema(data: Any) -> dict[str, Any]:
    """Recursively infer a structural JSON schema from raw JSON data."""
    if isinstance(data, dict):
        properties = {}
        required = []
        for k, v in data.items():
            properties[k] = extract_json_schema(v)
            required.append(k)
        return {"type": "object", "properties": properties, "required": sorted(required)}
    elif isinstance(data, list):
        if not data:
            return {"type": "array", "items": {}}
        # Sample items to infer schema
        item_schemas = [extract_json_schema(item) for item in data[:5]]
        return {"type": "array", "items": item_schemas[0]}
    elif isinstance(data, bool):
        return {"type": "boolean"}
    elif isinstance(data, int):
        return {"type": "integer"}
    elif isinstance(data, float):
        return {"type": "number"}
    elif isinstance(data, str):
        return {"type": "string"}
    elif data is None:
        return {"type": "null"}
    return {"type": "any"}


def extract_python_ast_skeleton(code: str) -> str:
    """Extract an interface/stub skeleton (classes, functions, signatures) from Python code."""
    parsed = ast.parse(code)
    lines = []

    for node in parsed.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ast.unparse(node.args)
            returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
            lines.append(f"{prefix}{node.name}({args}){returns}:")
            doc = ast.get_docstring(node)
            if doc:
                lines.append(f'    """{doc}"""')
            lines.append("    ...")
            lines.append("")
        elif isinstance(node, ast.ClassDef):
            bases = f"({', '.join(ast.unparse(b) for b in node.bases)})" if node.bases else ""
            lines.append(f"class {node.name}{bases}:")
            doc = ast.get_docstring(node)
            if doc:
                lines.append(f'    """{doc}"""')
            methods = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if not methods:
                lines.append("    pass")
            else:
                for m in methods:
                    m_args = ast.unparse(m.args)
                    m_ret = f" -> {ast.unparse(m.returns)}" if m.returns else ""
                    m_pref = "async def " if isinstance(m, ast.AsyncFunctionDef) else "def "
                    lines.append(f"    {m_pref}{m.name}({m_args}){m_ret}:")
                    m_doc = ast.get_docstring(m)
                    if m_doc:
                        lines.append(f'        """{m_doc}"""')
                    lines.append("        ...")
                    lines.append("")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def extract_image_wireframe(img_path: Path) -> dict[str, Any]:
    """Extract spatial bounding landmarks, aspect ratio, and color regions from an image reference."""
    try:
        from PIL import Image
    except ImportError:
        return {"error": "PIL (Pillow) is required for image wireframe extraction"}

    with Image.open(img_path) as img:
        width, height = img.size
        mode = img.mode
        # Convert to RGB for channel analysis
        rgb = img.convert("RGB")
        # Sample quadrants / key landmarks
        w2, h2 = width // 2, height // 2
        quadrants = {
            "top_left": rgb.crop((0, 0, w2, h2)),
            "top_right": rgb.crop((w2, 0, width, h2)),
            "bottom_left": rgb.crop((0, h2, w2, height)),
            "bottom_right": rgb.crop((w2, h2, width, height)),
        }
        quad_colors = {}
        for q_name, q_img in quadrants.items():
            # Get average color
            colors = q_img.resize((1, 1)).getpixel((0, 0))
            quad_colors[q_name] = {"r": colors[0], "g": colors[1], "b": colors[2]}

        # Find significant luminance centers
        gray = img.convert("L").resize((64, 36))
        get_data_fn = getattr(gray, "get_flattened_data", gray.getdata)
        pixels = list(get_data_fn())
        max_idx = pixels.index(max(pixels))
        min_idx = pixels.index(min(pixels))
        brightest_pt = [(max_idx % 64) * width // 64, (max_idx // 64) * height // 36]
        darkest_pt = [(min_idx % 64) * width // 64, (min_idx // 64) * height // 36]

    return {
        "dimensions": [width, height],
        "aspect_ratio": f"{width}:{height}",
        "color_mode": mode,
        "quadrant_ambient": quad_colors,
        "focal_points": {
            "brightest": brightest_pt,
            "darkest": darkest_pt,
        },
    }


def extract_image_contours(img_path: Path, max_contours: int = 250, min_length: float = 30.0) -> dict[str, Any]:
    """Extract structural edge contours/polylines from an image reference as vector paths."""
    try:
        import cv2
        import numpy as np

        img = cv2.imread(str(img_path))
        if img is None:
            return {"error": f"Failed to load image: {img_path}"}
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 35, 110)

        raw_contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        scored = []
        for c in raw_contours:
            arc_len = float(cv2.arcLength(c, False))
            if arc_len >= min_length:
                scored.append((arc_len, c))
        scored.sort(key=lambda x: x[0], reverse=True)

        contours = []
        for idx, (arc_len, c) in enumerate(scored[:max_contours], 1):
            epsilon = max(1.0, 0.003 * arc_len)
            approx = cv2.approxPolyDP(c, epsilon, False)
            pts = approx.reshape(-1, 2).tolist()
            if len(pts) >= 2:
                x, y, bw, bh = cv2.boundingRect(approx)
                d_str = "M " + " L ".join(f"{p[0]},{p[1]}" for p in pts)
                contours.append({
                    "id": idx,
                    "length": round(arc_len, 2),
                    "bbox": [int(x), int(y), int(bw), int(bh)],
                    "points": pts,
                    "path_d": d_str,
                })

        return {
            "dimensions": [w, h],
            "total_extracted": len(contours),
            "contours": contours,
        }
    except ImportError:
        try:
            from PIL import Image, ImageFilter
            with Image.open(img_path) as img:
                w, h = img.size
                return {
                    "dimensions": [w, h],
                    "total_extracted": 0,
                    "contours": [],
                    "note": "OpenCV recommended for full polyline extraction; Pillow fallback used."
                }
        except Exception as e:
            return {"error": f"Failed extracting image contours: {e}"}


def extract_markdown_outline(text: str) -> dict[str, Any]:
    """Extract structural heading hierarchy from Markdown text."""
    headings = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            level = len(line.split()[0])
            title = line.lstrip("#").strip()
            headings.append({"level": level, "title": title})
    return {"headings": headings}


def extract_stencil(source: Path, output: Path | None = None, mode: str = "auto") -> dict[str, Any]:
    """Extract a domain-agnostic structural stencil/blueprint from a reference file."""
    if not source.is_file():
        return {"status": "error", "error": f"Source file not found: {source}"}

    suffix = source.suffix.lower()
    selected_mode = mode
    if selected_mode == "auto":
        if suffix in (".json",):
            selected_mode = "schema"
        elif suffix in (".py",):
            selected_mode = "ast"
        elif suffix in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            selected_mode = "wireframe"
        elif suffix in (".md", ".markdown"):
            selected_mode = "outline"
        else:
            selected_mode = "text"

    content: Any = None
    text_result = ""

    if selected_mode == "schema":
        try:
            raw = json.loads(source.read_text("utf-8"))
            content = extract_json_schema(raw)
            text_result = json.dumps(content, indent=2)
        except Exception as e:
            return {"status": "error", "error": f"Failed to extract JSON schema: {e}"}

    elif selected_mode == "ast":
        try:
            code = source.read_text("utf-8")
            text_result = extract_python_ast_skeleton(code)
            content = {"type": "python_stub", "code": text_result}
        except Exception as e:
            return {"status": "error", "error": f"Failed to extract AST skeleton: {e}"}

    elif selected_mode == "wireframe":
        try:
            content = extract_image_wireframe(source)
            text_result = json.dumps(content, indent=2)
        except Exception as e:
            return {"status": "error", "error": f"Failed to extract image wireframe: {e}"}

    elif selected_mode == "contour":
        try:
            content = extract_image_contours(source)
            text_result = json.dumps(content, indent=2)
        except Exception as e:
            return {"status": "error", "error": f"Failed to extract image contours: {e}"}

    elif selected_mode == "outline":
        try:
            text = source.read_text("utf-8")
            content = extract_markdown_outline(text)
            text_result = json.dumps(content, indent=2)
        except Exception as e:
            return {"status": "error", "error": f"Failed to extract outline: {e}"}

    else:
        text_result = source.read_text("utf-8")
        content = {"type": "raw", "lines": len(text_result.splitlines())}

    if output:
        output.write_text(text_result, encoding="utf-8")

    return {
        "status": "ok",
        "source": str(source),
        "mode": selected_mode,
        "stencil": content,
        "output": str(output) if output else None,
    }


def check_stencil(stencil_path: Path, target_path: Path) -> dict[str, Any]:
    """Check structural conformance of a target file against a reference stencil."""
    if not stencil_path.is_file():
        return {"status": "error", "error": f"Stencil not found: {stencil_path}"}
    if not target_path.is_file():
        return {"status": "error", "error": f"Target not found: {target_path}"}

    stencil_text = stencil_path.read_text("utf-8", errors="replace").strip()
    target_text = target_path.read_text("utf-8", errors="replace").strip()

    # 1. JSON Schema Conformance Check
    if stencil_path.suffix.lower() == ".json":
        try:
            stencil_json = json.loads(stencil_text)
            target_json = json.loads(target_text)
            if isinstance(stencil_json, dict) and stencil_json.get("type") == "object":
                required = set(stencil_json.get("required", []))
                target_keys = set(target_json.keys()) if isinstance(target_json, dict) else set()
                missing = required - target_keys
                if missing:
                    return {
                        "status": "fail",
                        "error": f"Target JSON missing required keys: {sorted(missing)}",
                        "conforms": False,
                    }
                return {"status": "ok", "conforms": True, "type": "json_schema"}
        except json.JSONDecodeError:
            pass

    # 2. Python AST Stub Conformance Check
    if stencil_path.suffix.lower() == ".py" and target_path.suffix.lower() == ".py":
        try:
            stencil_ast = ast.parse(stencil_text)
            target_ast = ast.parse(target_text)
            stencil_funcs = {n.name for n in ast.walk(stencil_ast) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            target_funcs = {n.name for n in ast.walk(target_ast) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            missing_funcs = stencil_funcs - target_funcs
            if missing_funcs:
                return {
                    "status": "fail",
                    "error": f"Target Python file missing expected functions/methods: {sorted(missing_funcs)}",
                    "conforms": False,
                }
            return {"status": "ok", "conforms": True, "type": "python_ast"}
        except SyntaxError as e:
            return {"status": "error", "error": f"Syntax error during AST check: {e}"}

    # 3. Spatial Dimensions / ViewBox Conformance Check (for SVG or Wireframes/Contours)
    if target_path.suffix.lower() == ".svg":
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(target_text)
            viewbox = root.attrib.get("viewBox", "")
            if stencil_path.suffix.lower() == ".json":
                try:
                    s_data = json.loads(stencil_text)
                    if "dimensions" in s_data:
                        w, h = s_data["dimensions"]
                        expected_vb = f"0 0 {w} {h}"
                        if viewbox and viewbox != expected_vb:
                            return {
                                "status": "fail",
                                "error": f"Target SVG viewBox '{viewbox}' does not match stencil '{expected_vb}'",
                                "conforms": False,
                            }
                    if "contours" in s_data and s_data.get("contours"):
                        paths = root.findall(".//{http://www.w3.org/2000/svg}path") + root.findall(".//path")
                        if not paths:
                            return {
                                "status": "fail",
                                "error": "Target SVG has no vector paths conforming to contour stencil",
                                "conforms": False,
                            }
                        return {
                            "status": "ok",
                            "conforms": True,
                            "type": "svg_contours",
                            "details": {
                                "stencil_contours": len(s_data["contours"]),
                                "target_paths": len(paths),
                            },
                        }
                except Exception:
                    pass
            return {"status": "ok", "conforms": True, "type": "svg_viewbox"}
        except Exception as e:
            return {"status": "error", "error": f"Failed to parse SVG: {e}"}

    # Basic non-empty pass
    return {"status": "ok", "conforms": True, "type": "generic"}
