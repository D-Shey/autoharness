import json
import pytest
from pathlib import Path
from autoharness import stencil, evidence
from autoharness.env import Project


def test_stencil_kind_in_evidence_kinds():
    assert "stencil" in evidence.KINDS


def test_extract_json_schema(tmp_path):
    data = {
        "user_id": 42,
        "username": "ryan_gosling",
        "is_active": True,
        "score": 98.5,
        "tags": ["film", "cyberpunk"],
        "profile": {"location": "Los Angeles"}
    }
    src_file = tmp_path / "data.json"
    src_file.write_text(json.dumps(data), encoding="utf-8")
    out_file = tmp_path / "schema.json"

    res = stencil.extract_stencil(src_file, out_file, mode="schema")
    assert res["status"] == "ok"
    assert out_file.is_file()

    schema = json.loads(out_file.read_text("utf-8"))
    assert schema["type"] == "object"
    assert "username" in schema["properties"]
    assert schema["properties"]["username"]["type"] == "string"
    assert schema["properties"]["user_id"]["type"] == "integer"
    assert schema["properties"]["tags"]["type"] == "array"

    # Conformance check with valid target
    valid_target = tmp_path / "target_valid.json"
    valid_target.write_text(json.dumps(data), encoding="utf-8")
    check_res = stencil.check_stencil(out_file, valid_target)
    assert check_res["status"] == "ok"
    assert check_res["conforms"] is True

    # Conformance check with missing required field
    invalid_target = tmp_path / "target_invalid.json"
    invalid_target.write_text(json.dumps({"user_id": 1}), encoding="utf-8")
    fail_res = stencil.check_stencil(out_file, invalid_target)
    assert fail_res["status"] == "fail"
    assert fail_res["conforms"] is False


def test_extract_python_ast_skeleton(tmp_path):
    code = """
class CharacterService:
    \"\"\"Service managing movie characters.\"\"\"
    def get_character(self, char_id: int) -> str:
        # Complex business logic
        x = char_id * 2
        return f"Character_{x}"

    async def save_character(self, name: str) -> bool:
        return True

def standalone_helper(flag: bool) -> None:
    pass
"""
    src_py = tmp_path / "service.py"
    src_py.write_text(code, encoding="utf-8")
    out_py = tmp_path / "service_stencil.py"

    res = stencil.extract_stencil(src_py, out_py, mode="ast")
    assert res["status"] == "ok"
    assert out_py.is_file()

    stub_content = out_py.read_text("utf-8")
    assert "class CharacterService:" in stub_content
    assert "def get_character(self, char_id: int) -> str:" in stub_content
    assert "async def save_character(self, name: str) -> bool:" in stub_content
    assert "def standalone_helper(flag: bool) -> None:" in stub_content
    assert "x = char_id * 2" not in stub_content  # Method body removed!

    # Conformance check against implementation
    check_ok = stencil.check_stencil(out_py, src_py)
    assert check_ok["status"] == "ok"
    assert check_ok["conforms"] is True

    # Conformance check against incomplete implementation
    incomplete_code = "class CharacterService:\n    pass\n"
    inc_py = tmp_path / "incomplete.py"
    inc_py.write_text(incomplete_code, encoding="utf-8")
    check_fail = stencil.check_stencil(out_py, inc_py)
    assert check_fail["status"] == "fail"
    assert check_fail["conforms"] is False


def test_extract_image_wireframe(tmp_path):
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("PIL not available")

    img_path = tmp_path / "test_frame.png"
    img = Image.new("RGB", (640, 480), color=(128, 64, 200))
    img.save(img_path)

    out_json = tmp_path / "wireframe.json"
    res = stencil.extract_stencil(img_path, out_json, mode="wireframe")
    assert res["status"] == "ok"
    assert out_json.is_file()

    wireframe = json.loads(out_json.read_text("utf-8"))
    assert wireframe["dimensions"] == [640, 480]
    assert "quadrant_ambient" in wireframe
    assert "focal_points" in wireframe

    # SVG check against wireframe
    svg_ok = tmp_path / "scene.svg"
    svg_ok.write_text('<svg viewBox="0 0 640 480" xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    check_svg = stencil.check_stencil(out_json, svg_ok)
    assert check_svg["status"] == "ok"
    assert check_svg["conforms"] is True

    svg_wrong_dim = tmp_path / "wrong.svg"
    svg_wrong_dim.write_text('<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    check_svg_fail = stencil.check_stencil(out_json, svg_wrong_dim)
    assert check_svg_fail["status"] == "fail"
    assert check_svg_fail["conforms"] is False


def test_extract_image_contours(tmp_path):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        pytest.skip("PIL not available")

    img_path = tmp_path / "contour_src.png"
    img = Image.new("RGB", (400, 300), color=(0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([50, 50, 200, 150], outline=(255, 255, 255), width=3)
    draw.ellipse([220, 80, 320, 180], outline=(255, 255, 255), width=3)
    img.save(img_path)

    out_json = tmp_path / "contours.json"
    res = stencil.extract_stencil(img_path, out_json, mode="contour")
    assert res["status"] == "ok"
    assert out_json.is_file()

    data = json.loads(out_json.read_text("utf-8"))
    assert data["dimensions"] == [400, 300]
    assert "contours" in data

    # Conformance check with SVG containing paths
    target_svg = tmp_path / "target.svg"
    target_svg.write_text(
        '<svg viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M 50,50 L 200,50 L 200,150 L 50,150 Z"/>'
        '</svg>',
        encoding="utf-8"
    )
    check_res = stencil.check_stencil(out_json, target_svg)
    assert check_res["status"] == "ok"
    assert check_res["conforms"] is True
