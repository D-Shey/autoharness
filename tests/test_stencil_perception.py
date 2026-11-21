"""Tests for Machine-Native Runtime Perception and UI Validation (Reticle model)."""
from __future__ import annotations

from pathlib import Path
from autoharness import stencil


SAMPLE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <title>AutoHarness Console</title>
</head>
<body>
    <header>
        <nav role="navigation">
            <a href="/home" aria-label="Home Link">Home</a>
            <a href="/docs" aria-label="Documentation">Docs</a>
        </nav>
    </header>
    <main role="main">
        <h1>Dashboard</h1>
        <h3>Deep Heading</h3> <!-- Hierarchy skip violation -->
        <section>
            <form id="login-form">
                <input type="text" name="username" placeholder="Username" id="user-input" />
                <button type="submit" id="submit-btn">Log In</button>
            </form>
            <img src="/logo.png" /> <!-- Missing alt violation -->
        </section>
    </main>
    <footer>
        <p>&copy; 2026 AutoHarness</p>
    </footer>
</body>
</html>
"""


def test_extract_dom_perception():
    perc = stencil.extract_dom_perception(SAMPLE_HTML)
    assert perc["title"] == "AutoHarness Console"
    assert perc["total_elements"] >= 10
    assert perc["max_depth"] >= 4

    # Check interactive controls
    interactive_tags = [e["tag"] for e in perc["interactive_elements"]]
    assert "button" in interactive_tags
    assert "input" in interactive_tags
    assert "a" in interactive_tags

    # Check landmarks
    landmark_types = {l["type"] for l in perc["landmarks"]}
    assert "navigation" in landmark_types
    assert "main" in landmark_types
    assert "header" in landmark_types

    # Check accessibility checks & violations
    a11y = perc["accessibility"]
    assert a11y["violations_count"] >= 2
    rule_names = {v["rule"] for v in a11y["violations"]}
    assert "IMG_MISSING_ALT" in rule_names
    assert "HEADING_HIERARCHY_SKIP" in rule_names
    assert a11y["score"] < 1.0


def test_verify_runtime_perception():
    # 1. Matching requirements
    reqs_pass = {
        "required_landmarks": ["main", "navigation"],
        "required_interactive": [
            {"tag": "button", "text": "Log In"},
            {"tag": "input", "id": "user-input"},
        ],
        "min_a11y_score": 0.5,
    }
    res_pass = stencil.verify_runtime_perception(SAMPLE_HTML, reqs_pass)
    assert res_pass["passed"] is True
    assert len(res_pass["missing"]) == 0

    # 2. Failing requirements (missing element and strict a11y)
    reqs_fail = {
        "required_landmarks": ["nonexistent_landmark"],
        "required_interactive": [{"tag": "button", "text": "Delete Account"}],
        "no_critical_violations": True,  # IMG_MISSING_ALT is high/critical
    }
    res_fail = stencil.verify_runtime_perception(SAMPLE_HTML, reqs_fail)
    assert res_fail["passed"] is False
    assert any("nonexistent_landmark" in m for m in res_fail["missing"])
    assert any("Delete Account" in m for m in res_fail["missing"])
    assert any("critical" in m for m in res_fail["missing"])


def test_format_perception_report():
    perc = stencil.extract_dom_perception(SAMPLE_HTML)
    report = stencil.format_perception_report(perc)
    assert "AutoHarness Console" in report
    assert "Accessibility Violations:" in report
    assert "IMG_MISSING_ALT" in report
