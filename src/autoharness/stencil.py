"""Domain-agnostic structural scanner and stencil/scaffold generator.

Extracts verifiable structural invariants (schemas, AST skeletons, spatial wireframes,
geometric landmarks, and contour frameworks) from reference sources across any domain
into domain-agnostic geometric stamps (.autoharness/stencils/) for high-fidelity alignment
and ground-truth validation.
"""

from __future__ import annotations

import ast
import datetime
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
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
        # Fallback to system python if isolated environment lacks Pillow
        sys_res = _run_system_python_image_extract(img_path, mode="wireframe")
        if sys_res and "dimensions" in sys_res:
            return sys_res
        return {"error": "PIL (Pillow) is required for image wireframe extraction"}

    with Image.open(img_path) as img:
        width, height = img.size
        mode = img.mode
        rgb = img.convert("RGB")
        w2, h2 = width // 2, height // 2
        quadrants = {
            "top_left": rgb.crop((0, 0, w2, h2)),
            "top_right": rgb.crop((w2, 0, width, h2)),
            "bottom_left": rgb.crop((0, h2, w2, height)),
            "bottom_right": rgb.crop((w2, h2, width, height)),
        }
        quad_colors = {}
        for q_name, q_img in quadrants.items():
            colors = q_img.resize((1, 1)).getpixel((0, 0))
            quad_colors[q_name] = {"r": colors[0], "g": colors[1], "b": colors[2]}

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


def _do_cv2_contour_extract(
    img_path: Path,
    max_contours: int = 250,
    min_length: float = 30.0,
    threshold: float = 0.5,
    max_points: int = 250,
) -> dict[str, Any]:
    """Perform OpenCV polyline and landmark contour extraction."""
    import cv2
    import numpy as np

    thresh_val = max(0.01, min(0.99, float(threshold)))
    img = cv2.imread(str(img_path))
    if img is None:
        return {"error": f"Failed to load image: {img_path}"}
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # Map threshold (0.0-1.0) to Canny edge detector sensitivity
    low_t = int(max(10, 80 * (1.0 - thresh_val)))
    high_t = int(max(25, 200 * (1.0 - thresh_val)))
    edges = cv2.Canny(blurred, low_t, high_t)

    raw_contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    scored = []
    for c in raw_contours:
        arc_len = float(cv2.arcLength(c, False))
        if arc_len >= min_length:
            scored.append((arc_len, c))
    scored.sort(key=lambda x: x[0], reverse=True)

    contours = []
    landmarks = []
    limit_contours = min(max_contours, max_points)
    for idx, (arc_len, c) in enumerate(scored[:limit_contours], 1):
        epsilon = max(1.0, 0.003 * arc_len)
        approx = cv2.approxPolyDP(c, epsilon, False)
        pts = approx.reshape(-1, 2).tolist()
        if len(pts) >= 2:
            x, y, bw, bh = cv2.boundingRect(approx)
            M = cv2.moments(c)
            cx = int(M["m10"] / M["m00"]) if M["m00"] != 0 else x + bw // 2
            cy = int(M["m01"] / M["m00"]) if M["m00"] != 0 else y + bh // 2
            d_str = "M " + " L ".join(f"{p[0]},{p[1]}" for p in pts)

            contour_obj = {
                "id": idx,
                "length": round(arc_len, 2),
                "bbox": [int(x), int(y), int(bw), int(bh)],
                "centroid": [cx, cy],
                "points": pts[:max_points],
                "path_d": d_str,
            }
            contours.append(contour_obj)
            if idx <= 15:
                landmarks.append({
                    "id": idx,
                    "label": f"landmark_{idx}",
                    "bbox": [int(x), int(y), int(bw), int(bh)],
                    "center": [cx, cy],
                    "normalized_center": [round(cx / w, 4), round(cy / h, 4)],
                })

    wireframe = extract_image_wireframe(img_path)

    return {
        "dimensions": [w, h],
        "aspect_ratio": f"{w}:{h}",
        "threshold_used": thresh_val,
        "total_extracted": len(contours),
        "landmarks": landmarks,
        "contours": contours,
        "wireframe_metadata": wireframe,
    }


def _run_system_python_image_extract(
    img_path: Path,
    mode: str = "contour",
    threshold: float = 0.5,
    max_points: int = 250,
) -> dict[str, Any] | None:
    """Run extraction via system python subprocess when current interpreter lacks cv2/PIL."""
    src_dir = str(Path(__file__).resolve().parent.parent)
    candidates = [
        os.environ.get("AUTOHARNESS_PYTHON", ""),
        shutil.which("python") or "",
        shutil.which("python3") or "",
        shutil.which("py") or "",
    ]
    seen = set()
    for py_bin in candidates:
        if not py_bin or py_bin in seen:
            continue
        seen.add(py_bin)
        try:
            code = f"""
import json, sys
from pathlib import Path
sys.path.insert(0, {src_dir!r})
p = Path({str(img_path.resolve())!r})
thresh = {threshold}
mp = {max_points}

try:
    import cv2, numpy
    from autoharness.stencil import _do_cv2_contour_extract
    res = _do_cv2_contour_extract(p, threshold=thresh, max_points=mp)
    print(json.dumps(res))
    sys.exit(0)
except Exception:
    pass

try:
    from autoharness.stencil import extract_image_wireframe
    res = extract_image_wireframe(p)
    print(json.dumps(res))
    sys.exit(0)
except Exception:
    sys.exit(1)
"""
            proc = subprocess.run([py_bin, "-c", code], capture_output=True, text=True, timeout=15)
            if proc.returncode == 0 and proc.stdout.strip():
                return json.loads(proc.stdout.strip())
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


def extract_image_contours(
    img_path: Path,
    max_contours: int = 250,
    min_length: float = 30.0,
    threshold: float = 0.5,
    max_points: int = 250,
) -> dict[str, Any]:
    """Extract structural edge contours, geometric landmarks, and polylines from an image reference."""
    thresh_val = max(0.01, min(0.99, float(threshold)))

    # 1. Try in-process OpenCV extraction
    try:
        import cv2
        import numpy as np
        return _do_cv2_contour_extract(img_path, max_contours=max_contours, min_length=min_length, threshold=thresh_val, max_points=max_points)
    except ImportError:
        pass

    # 2. Try system Python subprocess extraction (where cv2 and PIL are installed)
    sys_res = _run_system_python_image_extract(img_path, mode="contour", threshold=thresh_val, max_points=max_points)
    if sys_res and ("contours" in sys_res or "dimensions" in sys_res):
        return sys_res

    # 3. Fallback to in-process Pillow
    try:
        from PIL import Image
        with Image.open(img_path) as img:
            w, h = img.size
            wf = extract_image_wireframe(img_path)
            return {
                "dimensions": [w, h],
                "aspect_ratio": f"{w}:{h}",
                "threshold_used": thresh_val,
                "total_extracted": 0,
                "landmarks": [
                    {"id": 1, "label": "brightest_focal", "center": wf.get("focal_points", {}).get("brightest", [w // 2, h // 2])},
                    {"id": 2, "label": "darkest_focal", "center": wf.get("focal_points", {}).get("darkest", [w // 2, h // 2])},
                ],
                "contours": [],
                "wireframe_metadata": wf,
                "note": "OpenCV polyline extraction fallback used.",
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
    return {"headings": headings, "total_sections": len(headings)}


def extract_svg_stencil(svg_path: Path) -> dict[str, Any]:
    """Extract structural viewBox, element hierarchy, and geometry bounds from an SVG reference."""
    import xml.etree.ElementTree as ET
    try:
        content = svg_path.read_text("utf-8")
        root = ET.fromstring(content)
        viewbox = root.attrib.get("viewBox", "")
        width = root.attrib.get("width", "")
        height = root.attrib.get("height", "")
        elements = list(root.iter())
        tags = [elem.tag.split("}")[-1] for elem in elements]
        defs_ids = []
        for defs in root.findall(".//{http://www.w3.org/2000/svg}defs") + root.findall(".//defs"):
            for child in defs:
                if "id" in child.attrib:
                    defs_ids.append(child.attrib["id"])

        return {
            "type": "svg_stencil",
            "viewBox": viewbox,
            "dimensions": [width, height],
            "total_elements": len(elements),
            "tag_counts": {t: tags.count(t) for t in sorted(set(tags))},
            "defs_ids": defs_ids,
        }
    except Exception as e:
        return {"error": f"Failed to extract SVG stencil: {e}"}


def extract_stencil(
    source: Path,
    output: Path | None = None,
    mode: str = "auto",
    threshold: float = 0.5,
    max_points: int = 250,
) -> dict[str, Any]:
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
        elif suffix in (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff"):
            selected_mode = "contour"
        elif suffix in (".svg",):
            selected_mode = "svg"
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
            content = extract_image_contours(source, threshold=threshold, max_points=max_points)
            text_result = json.dumps(content, indent=2)
        except Exception as e:
            return {"status": "error", "error": f"Failed to extract image contours: {e}"}

    elif selected_mode == "svg":
        try:
            content = extract_svg_stencil(source)
            text_result = json.dumps(content, indent=2)
        except Exception as e:
            return {"status": "error", "error": f"Failed to extract SVG stencil: {e}"}

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


def get_stencils_dir(project_root: Path) -> Path:
    """Ensure and return the domain-agnostic geometric stamps / stencil repository directory (.autoharness/stencils/)."""
    stencils_dir = project_root / ".autoharness" / "stencils"
    stencils_dir.mkdir(parents=True, exist_ok=True)
    return stencils_dir


def scan_stencil(
    source: Path,
    project_root: Path | None = None,
    mode: str = "auto",
    threshold: float = 0.5,
    max_points: int = 250,
    output: Path | None = None,
    task=None,
) -> dict[str, Any]:
    """Scan a reference file across any domain and persist it into .autoharness/stencils/."""
    if not source.is_file():
        return {"status": "error", "error": f"Source file not found: {source}"}

    proj = project_root
    if not proj and task:
        proj = getattr(task, "project", None).root if getattr(task, "project", None) else task.dir.parent.parent.parent
    if not proj:
        for parent in source.resolve().parents:
            if (parent / ".autoharness").exists():
                proj = parent
                break
    if not proj:
        proj = Path.cwd()
    stencils_dir = get_stencils_dir(proj)

    existing = list(stencils_dir.glob("*.json"))
    stem_clean = re.sub(r"[^a-zA-Z0-9_\-]", "_", source.stem)[:30]
    stencil_id = f"stc_{stem_clean}_{len(existing) + 1:02d}"

    target_file = output or (stencils_dir / f"{stencil_id}.json")

    extracted = extract_stencil(source, output=target_file, mode=mode, threshold=threshold, max_points=max_points)
    if extracted.get("status") != "ok":
        return extracted

    content = extracted["stencil"]
    summary: dict[str, Any] = {"mode": extracted["mode"]}
    if isinstance(content, dict):
        if "dimensions" in content:
            summary["dimensions"] = content["dimensions"]
        if "total_extracted" in content:
            summary["contours_count"] = content["total_extracted"]
        if "landmarks" in content:
            summary["landmarks_count"] = len(content["landmarks"])
        if "type" in content:
            summary["schema_type"] = content["type"]
        if "required" in content:
            summary["required_keys"] = content["required"]
        if "headings" in content:
            summary["headings_count"] = len(content["headings"])

    metadata = {
        "id": stencil_id,
        "source": str(source),
        "source_name": source.name,
        "mode": extracted["mode"],
        "threshold": threshold,
        "max_points": max_points,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "summary": summary,
        "blueprint": content,
    }
    target_file.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    if task:
        try:
            from autoharness import evidence
            evidence.add(task, target_file, "stencil", note=f"Auto-scanned stencil from {source.name}")
        except (OSError, ValueError, RuntimeError) as e:
            if hasattr(task, "log"):
                task.log(f"stencil evidence registration skipped: {e}")

    return {
        "status": "ok",
        "stencil_id": stencil_id,
        "file": str(target_file),
        "source": str(source),
        "mode": extracted["mode"],
        "summary": summary,
        "blueprint": content,
    }


def list_stencils(project_root: Path) -> dict[str, Any]:
    """List all extracted stencils in .autoharness/stencils/."""
    stencils_dir = get_stencils_dir(project_root)
    results = []
    for f in sorted(stencils_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text("utf-8"))
            results.append({
                "id": data.get("id", f.stem),
                "source": data.get("source_name") or data.get("source"),
                "mode": data.get("mode", "unknown"),
                "created_at": data.get("created_at", ""),
                "summary": data.get("summary", {}),
                "file": str(f),
            })
        except Exception:
            results.append({"id": f.stem, "file": str(f), "mode": "unreadable"})
    return {"status": "ok", "count": len(results), "stencils": results}


def show_stencil(stencil_id: str, project_root: Path) -> dict[str, Any]:
    """Retrieve full details of a stencil by id or filename."""
    stencils_dir = get_stencils_dir(project_root)
    cand = stencils_dir / f"{stencil_id}.json"
    if not cand.is_file():
        cand = stencils_dir / stencil_id
    if not cand.is_file():
        matches = [f for f in stencils_dir.glob("*.json") if stencil_id in f.stem]
        if matches:
            cand = matches[0]

    if not cand.is_file():
        return {"status": "error", "error": f"Stencil '{stencil_id}' not found in {stencils_dir}"}

    try:
        data = json.loads(cand.read_text("utf-8"))
        return {"status": "ok", "stencil": data}
    except Exception as e:
        return {"status": "error", "error": f"Failed reading stencil {cand}: {e}"}


def check_stencil(stencil_path_or_id: str | Path, target_path: Path, project_root: Path | None = None) -> dict[str, Any]:
    """Check structural conformance of a target file against a reference stencil."""
    proj = project_root or Path.cwd()
    s_path = Path(stencil_path_or_id)
    if not s_path.is_file():
        s_dir = get_stencils_dir(proj)
        cand = s_dir / f"{stencil_path_or_id}.json"
        if cand.is_file():
            s_path = cand
        else:
            matches = [f for f in s_dir.glob("*.json") if str(stencil_path_or_id) in f.stem]
            if matches:
                s_path = matches[0]

    if not s_path.is_file():
        return {"status": "error", "error": f"Stencil not found: {stencil_path_or_id}"}
    if not target_path.is_file():
        return {"status": "error", "error": f"Target not found: {target_path}"}

    stencil_text = s_path.read_text("utf-8", errors="replace").strip()
    target_text = target_path.read_text("utf-8", errors="replace").strip()

    blueprint_data = None
    try:
        loaded = json.loads(stencil_text)
        if isinstance(loaded, dict) and "blueprint" in loaded:
            blueprint_data = loaded["blueprint"]
            stencil_text = json.dumps(blueprint_data, indent=2)
    except (json.JSONDecodeError, ValueError, TypeError):
        blueprint_data = None

    # 1. JSON Schema Conformance Check
    if s_path.suffix.lower() == ".json":
        try:
            stencil_json = blueprint_data if blueprint_data is not None else json.loads(stencil_text)
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
    if (s_path.suffix.lower() == ".py" or (blueprint_data and isinstance(blueprint_data, dict) and blueprint_data.get("type") == "python_stub")) and target_path.suffix.lower() == ".py":
        try:
            stub_code = blueprint_data["code"] if (blueprint_data and isinstance(blueprint_data, dict) and "code" in blueprint_data) else stencil_text
            stencil_ast = ast.parse(stub_code)
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
            if s_path.suffix.lower() == ".json":
                try:
                    s_data = blueprint_data if blueprint_data is not None else json.loads(stencil_text)
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
                except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                    return {"status": "ok", "conforms": True, "type": "svg_viewbox"}
            return {"status": "ok", "conforms": True, "type": "svg_viewbox"}
        except Exception as e:
            return {"status": "error", "error": f"Failed to parse SVG: {e}"}

    return {"status": "ok", "conforms": True, "type": "generic"}


# --- Machine-Native Runtime Perception (Reticle Model) -------------------------

INTERACTIVE_TAGS = {"button", "a", "input", "select", "textarea"}
INTERACTIVE_ROLES = {"button", "link", "checkbox", "textbox", "radio", "combobox", "menuitem", "tab"}
LANDMARK_TAGS = {"main", "nav", "header", "footer", "aside", "form"}
LANDMARK_ROLES = {"main", "navigation", "banner", "contentinfo", "complementary", "form", "search"}


class HTMLPerceptionParser(HTMLParser):
    """Parses HTML into machine-native perception landmarks, accessibility checks, and interactive controls."""

    def __init__(self):
        super().__init__()
        self.elements: list[dict[str, Any]] = []
        self.interactive: list[dict[str, Any]] = []
        self.landmarks: list[dict[str, Any]] = []
        self.violations: list[dict[str, Any]] = []
        self.headings: list[tuple[int, str]] = []
        self.title: str = ""
        self._tag_stack: list[dict[str, Any]] = []
        self._max_depth = 0
        self._total_checks = 0
        self._passed_checks = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_dict = {k.lower(): (v or "") for k, v in attrs}
        depth = len(self._tag_stack) + 1
        if depth > self._max_depth:
            self._max_depth = depth

        elem_id = attr_dict.get("id", "")
        classes = attr_dict.get("class", "").split()
        role = attr_dict.get("role", "")
        aria_label = attr_dict.get("aria-label", "") or attr_dict.get("aria-labelledby", "")

        elem_data: dict[str, Any] = {
            "tag": tag,
            "id": elem_id,
            "classes": classes,
            "role": role,
            "aria_label": aria_label,
            "depth": depth,
            "attributes": attr_dict,
            "text": "",
        }
        self.elements.append(elem_data)
        self._tag_stack.append(elem_data)

        # Check landmarks
        if tag in LANDMARK_TAGS or role in LANDMARK_ROLES:
            self.landmarks.append({
                "type": role or tag,
                "tag": tag,
                "id": elem_id,
                "label": aria_label or attr_dict.get("title", ""),
                "depth": depth,
            })

        # Check interactive
        is_interactive = tag in INTERACTIVE_TAGS or role in INTERACTIVE_ROLES or "onclick" in attr_dict
        if is_interactive:
            self.interactive.append(elem_data)

        # Accessibility checks
        if tag == "img":
            self._total_checks += 1
            if "alt" not in attr_dict:
                self.violations.append({
                    "rule": "IMG_MISSING_ALT",
                    "severity": "high",
                    "element": tag,
                    "id": elem_id,
                    "message": f"Image element is missing an 'alt' attribute: id='{elem_id}'",
                })
            else:
                self._passed_checks += 1

        if tag == "input" and attr_dict.get("type") not in ("hidden", "submit", "button"):
            self._total_checks += 1
            has_label = bool(aria_label or attr_dict.get("placeholder") or attr_dict.get("title") or elem_id)
            if not has_label:
                self.violations.append({
                    "rule": "INPUT_MISSING_LABEL",
                    "severity": "medium",
                    "element": tag,
                    "id": elem_id,
                    "message": f"Form input field '{attr_dict.get('name') or elem_id}' has no accessible label or placeholder",
                })
            else:
                self._passed_checks += 1

    def handle_endtag(self, tag: str):
        if self._tag_stack and self._tag_stack[-1]["tag"] == tag:
            elem = self._tag_stack.pop()
            # Post-check button text/aria
            if elem["tag"] == "button" or elem.get("role") == "button":
                self._total_checks += 1
                has_accessible_name = bool(elem["text"].strip() or elem["aria_label"] or elem["attributes"].get("title"))
                if not has_accessible_name:
                    self.violations.append({
                        "rule": "BUTTON_MISSING_NAME",
                        "severity": "high",
                        "element": elem["tag"],
                        "id": elem["id"],
                        "message": f"Button element has no readable text, aria-label, or title (id='{elem['id']}')",
                    })
                else:
                    self._passed_checks += 1

            if re.match(r"^h([1-6])$", tag):
                level = int(tag[1])
                self.headings.append((level, elem["text"].strip()))

    def handle_data(self, data: str):
        clean = data.strip()
        if not clean:
            return
        if self._tag_stack:
            self._tag_stack[-1]["text"] += (" " + clean if self._tag_stack[-1]["text"] else clean)
        if self._tag_stack and self._tag_stack[-1]["tag"] == "title":
            self.title = clean


def extract_dom_perception(html_or_path: str | Path) -> dict[str, Any]:
    """Extract machine-native runtime perception tree, interactive elements, and a11y metrics (Reticle model)."""
    text = ""
    if isinstance(html_or_path, Path):
        text = html_or_path.read_text(encoding="utf-8", errors="replace")
    elif isinstance(html_or_path, str):
        p = Path(html_or_path)
        if len(html_or_path) < 1024 and p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
        else:
            text = html_or_path

    parser = HTMLPerceptionParser()
    parser.feed(text)

    # Check heading order hierarchy
    for i in range(1, len(parser.headings)):
        prev_lvl, _ = parser.headings[i - 1]
        curr_lvl, heading_text = parser.headings[i]
        parser._total_checks += 1
        if curr_lvl > prev_lvl + 1:
            parser.violations.append({
                "rule": "HEADING_HIERARCHY_SKIP",
                "severity": "low",
                "message": f"Heading hierarchy skipped from h{prev_lvl} to h{curr_lvl} ('{heading_text}')",
            })
        else:
            parser._passed_checks += 1

    total_checks = max(1, parser._total_checks)
    a11y_score = round(max(0.0, min(1.0, parser._passed_checks / total_checks)), 4)

    # Compact skeleton
    skeleton_lines = []
    for elem in parser.elements[:150]:
        indent = "  " * (elem["depth"] - 1)
        ident = f"#{elem['id']}" if elem["id"] else ""
        classes = ("." + ".".join(elem["classes"][:2])) if elem["classes"] else ""
        role_info = f" [role={elem['role']}]" if elem["role"] else ""
        txt = f" \"{elem['text'][:30]}...\"" if elem["text"] else ""
        skeleton_lines.append(f"{indent}<{elem['tag']}{ident}{classes}{role_info}>{txt}")

    return {
        "title": parser.title,
        "total_elements": len(parser.elements),
        "max_depth": parser._max_depth,
        "interactive_count": len(parser.interactive),
        "landmarks": parser.landmarks,
        "interactive_elements": [
            {
                "tag": e["tag"],
                "id": e["id"],
                "role": e["role"],
                "aria_label": e["aria_label"],
                "text": e["text"][:50],
                "type": e["attributes"].get("type", ""),
            }
            for e in parser.interactive
        ],
        "accessibility": {
            "score": a11y_score,
            "passed_checks": parser._passed_checks,
            "total_checks": total_checks,
            "violations_count": len(parser.violations),
            "violations": parser.violations,
        },
        "dom_skeleton": "\n".join(skeleton_lines),
    }


def verify_runtime_perception(
    target_html: str | Path,
    requirements: dict[str, Any],
) -> dict[str, Any]:
    """Verify runtime UI invariants against machine-native perception requirements (Reticle model)."""
    perc = extract_dom_perception(target_html)
    missing: list[str] = []
    matched: list[str] = []

    # 1. Check required tags / landmarks
    req_landmarks = requirements.get("required_landmarks", [])
    found_landmarks = {l["type"] for l in perc["landmarks"]}
    for lm in req_landmarks:
        if lm in found_landmarks:
            matched.append(f"landmark:{lm}")
        else:
            missing.append(f"missing landmark '{lm}'")

    # 2. Check required interactive elements
    req_interactive = requirements.get("required_interactive", [])
    for req in req_interactive:
        req_tag = req.get("tag")
        req_text = req.get("text")
        req_id = req.get("id")
        found = False
        for el in perc["interactive_elements"]:
            tag_ok = not req_tag or el["tag"] == req_tag
            id_ok = not req_id or el["id"] == req_id
            text_ok = not req_text or (req_text.lower() in el["text"].lower() or req_text.lower() in el["aria_label"].lower())
            if tag_ok and id_ok and text_ok:
                found = True
                break
        if found:
            matched.append(f"interactive:{req_tag or req_id or req_text}")
        else:
            missing.append(f"missing interactive element matching {req}")

    # 3. Check minimum accessibility score
    min_score = float(requirements.get("min_a11y_score", 0.0))
    a11y_passed = perc["accessibility"]["score"] >= min_score
    if not a11y_passed:
        missing.append(f"a11y score {perc['accessibility']['score']} is below required {min_score}")

    # 4. Check critical a11y violations
    if requirements.get("no_critical_violations"):
        high_v = [v for v in perc["accessibility"]["violations"] if v.get("severity") == "high"]
        if high_v:
            missing.append(f"{len(high_v)} critical accessibility violation(s) detected")

    passed = len(missing) == 0
    return {
        "passed": passed,
        "matched": matched,
        "missing": missing,
        "perception": perc,
    }


def format_perception_report(perception: dict[str, Any]) -> str:
    """Format perception statistics into a human-readable outline."""
    a11y = perception["accessibility"]
    lines = [
        f"## Runtime Perception Report: {perception.get('title') or 'Untitled'}",
        f"- **Elements:** {perception['total_elements']} (max depth: {perception['max_depth']})",
        f"- **Interactive Elements:** {perception['interactive_count']}",
        f"- **Landmarks:** {len(perception['landmarks'])} ({', '.join(l['type'] for l in perception['landmarks'])})",
        f"- **Accessibility Score:** {a11y['score'] * 100:.1f}% ({a11y['passed_checks']}/{a11y['total_checks']} passed, {a11y['violations_count']} violations)",
    ]
    if a11y["violations"]:
        lines.append("### Accessibility Violations:")
        for v in a11y["violations"]:
            lines.append(f"- **[{v['severity'].upper()}] {v['rule']}:** {v['message']}")
    return "\n".join(lines)

