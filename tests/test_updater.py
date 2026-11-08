"""Unit tests for AutoHarness update checker and self-updater."""
import json
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

import pytest

from autoharness import __version__, cli, gateway, updater
from autoharness.env import Project


def test_parse_semver():
    assert updater.parse_semver("1.0.0") == (1, 0, 0)
    assert updater.parse_semver("v1.2.3") == (1, 2, 3)
    assert updater.parse_semver("V0.1.0-alpha") == (0, 1, 0)
    assert updater.parse_semver("2") == (2,)
    assert updater.parse_semver("1.10.0") > updater.parse_semver("1.9.9")
    assert updater.parse_semver("1.0.0") > updater.parse_semver("0.1.0")
    assert updater.parse_semver("1.0.0") == updater.parse_semver("v1.0.0")


def test_check_for_updates_release_api_newer(monkeypatch):
    fake_release = {
        "tag_name": "v1.1.0",
        "html_url": "https://github.com/D-Shey/autoharness/releases/tag/v1.1.0",
        "body": "New features in 1.1.0",
    }
    raw_bytes = json.dumps(fake_release).encode("utf-8")

    class FakeResponse(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def mock_urlopen(req, timeout=6.0):
        return FakeResponse(raw_bytes)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    res = updater.check_for_updates("D-Shey/autoharness")
    assert res["status"] == "ok"
    assert res["latest_version"] == "1.1.0"
    assert res["update_available"] is True
    assert res["source"] == "github_releases"
    assert "New features" in res["notes"]


def test_check_for_updates_release_api_same_version(monkeypatch):
    fake_release = {
        "tag_name": f"v{__version__}",
        "html_url": "https://github.com/D-Shey/autoharness/releases/tag/v1.0.0",
        "body": "Current release",
    }
    raw_bytes = json.dumps(fake_release).encode("utf-8")

    class FakeResponse(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def mock_urlopen(req, timeout=6.0):
        return FakeResponse(raw_bytes)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    res = updater.check_for_updates("D-Shey/autoharness")
    assert res["status"] == "ok"
    assert res["latest_version"] == __version__
    assert res["update_available"] is False


def test_check_for_updates_fallback_to_raw_pyproject(monkeypatch):
    # Simulate 404 on release API, then return raw pyproject.toml
    pyproject_text = '[project]\nname = "autoharness"\nversion = "1.2.0"\n'

    class FakeResponse(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def mock_urlopen(req, timeout=6.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "releases/latest" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return FakeResponse(pyproject_text.encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    res = updater.check_for_updates("D-Shey/autoharness")
    assert res["status"] == "ok"
    assert res["latest_version"] == "1.2.0"
    assert res["update_available"] is True
    assert res["source"] == "raw_pyproject"


def test_check_for_updates_network_error(monkeypatch):
    def mock_urlopen(req, timeout=6.0):
        raise urllib.error.URLError("DNS resolution failed")

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    res = updater.check_for_updates("D-Shey/autoharness")
    assert res["status"] == "error"
    assert "DNS resolution failed" in res["error"]
    assert res["update_available"] is False


def test_version_bump_is_1_0_0():
    """Verify that autoharness version is bumped to 1.0.0 in package and pyproject.toml."""
    assert __version__ == "1.0.0"
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    assert pyproject_path.is_file()
    text = pyproject_path.read_text("utf-8")
    assert 'version = "1.0.0"' in text


def test_detect_install_mode(tmp_path: Path):
    # Arbitrary user project with git and pyproject.toml must NOT be detected as autoharness git_repo
    user_repo = tmp_path / "user_repo"
    user_repo.mkdir()
    (user_repo / ".git").mkdir()
    (user_repo / "pyproject.toml").write_text('[project]\nname = "user-project"\n', "utf-8")
    assert updater.detect_install_mode(user_repo) != "git_repo"

    # Actual AutoHarness repository structure
    ah_repo = tmp_path / "autoharness"
    ah_repo.mkdir()
    (ah_repo / ".git").mkdir()
    (ah_repo / "pyproject.toml").write_text('[project]\nname = "autoharness"\nversion = "1.0.0"\n', "utf-8")
    pkg_dir = ah_repo / "src" / "autoharness"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text('__version__ = "1.0.0"\n', "utf-8")
    assert updater.detect_install_mode(ah_repo) == "git_repo"

    # Non-git directory
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    mode = updater.detect_install_mode(plain_dir)
    assert mode in ("uv_tool", "pipx", "pip")


def test_perform_update_dry_run_runner():
    executed_commands = []

    class MockResult:
        returncode = 0
        stdout = "Installed autoharness 1.0.0"
        stderr = ""

    def mock_runner(cmd, **kwargs):
        executed_commands.append(cmd)
        return MockResult()

    # Test uv_tool install
    res = updater.perform_update("D-Shey/autoharness", install_mode="uv_tool", runner=mock_runner)
    assert res["status"] == "ok"
    assert len(executed_commands) == 1
    assert executed_commands[0] == ["uv", "tool", "install", "--force", "git+https://github.com/D-Shey/autoharness.git"]


def test_cli_update_command(monkeypatch, capsys):
    fake_release = {
        "tag_name": "v1.0.0",
        "html_url": "https://github.com/D-Shey/autoharness",
        "body": "Up to date",
    }

    class FakeResponse(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=6.0: FakeResponse(json.dumps(fake_release).encode("utf-8")))

    # 1. autoharness update --check
    code = cli.main(["update", "--check"])
    assert code == 0
    out_check = json.loads(capsys.readouterr().out)
    assert out_check["status"] == "ok"
    assert out_check["latest_version"] == "1.0.0"
    assert out_check["update_available"] is False

    # 2. autoharness update (already up to date)
    code = cli.main(["update"])
    assert code == 0
    out_update = json.loads(capsys.readouterr().out)
    assert out_update["status"] == "ok"
    assert "up to date" in out_update["message"]

    # 3. autoharness update when update available without --yes requires approval (code 3)
    fake_newer = {
        "tag_name": "v1.1.0",
        "html_url": "https://github.com/D-Shey/autoharness",
        "body": "Version 1.1.0 available",
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=6.0: FakeResponse(json.dumps(fake_newer).encode("utf-8")))
    monkeypatch.setattr(updater, "perform_update", lambda **kwargs: {"status": "ok", "mocked": True})

    code_need_approval = cli.main(["update"])
    assert code_need_approval == cli.EXIT_APPROVAL
    out_approval = json.loads(capsys.readouterr().out)
    assert out_approval["status"] == "approval_required"

    # 4. autoharness update with --yes bypasses approval prompt
    code_yes = cli.main(["update", "--yes"])
    assert code_yes == 0
    out_yes = json.loads(capsys.readouterr().out)
    assert out_yes["status"] == "ok"
    assert out_yes["update"]["mocked"] is True


def test_gateway_harness_update_check_tool(tmp_path: Path, monkeypatch):
    fake_release = {
        "tag_name": "v1.5.0",
        "html_url": "https://github.com/D-Shey/autoharness",
        "body": "Big update",
    }

    class FakeResponse(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=6.0: FakeResponse(json.dumps(fake_release).encode("utf-8")))

    gw = gateway.Gateway(tmp_path, write=lambda s: None, log=lambda s: None)
    res = gw._call({"name": "harness_update_check", "arguments": {"repo": "D-Shey/autoharness"}})
    assert not res.get("isError")
    parsed = json.loads(res["content"][0]["text"])
    assert parsed["status"] == "ok"
    assert parsed["latest_version"] == "1.5.0"
    assert parsed["update_available"] is True
