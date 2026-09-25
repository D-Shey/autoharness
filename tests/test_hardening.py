import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autoharness.agents import AgentError, TomlMcpStore
from autoharness.mcp_client import HttpClient, McpError


def test_curated_processes_use_exact_versions():
    import re
    from autoharness.catalog import Catalog
    for entry in Catalog.load().mcp_servers.values():
        cfg = entry['config']
        if cfg.get('command') == 'npx':
            assert re.search(r'@\d+\.\d+\.\d+$', cfg['args'][1])
        elif cfg.get('command') == 'uvx':
            assert re.search(r'==\d+\.\d+\.\d+$', cfg['args'][0])


@pytest.mark.parametrize('declaration', [
    "[mcp_servers.'demo']\ncommand = 'original'\n",
    '[mcp_servers]\ndemo = { command = "original" }\n',
    'mcp_servers.demo.command = "original"\n',
    '[ "mcp_servers" . "demo" ]\ncommand = "original"\n',
])
def test_toml_detects_existing_server(tmp_path, declaration):
    path = tmp_path / 'config.toml'
    path.write_text(declaration, encoding='utf-8')
    store = TomlMcpStore(path)
    assert store.has('demo')
    with pytest.raises(AgentError):
        store.add('demo', {'command': 'replacement'})
    assert path.read_text('utf-8') == declaration


def test_toml_invalid_input_is_preserved(tmp_path):
    path = tmp_path / 'config.toml'
    original = '[broken\n'
    path.write_text(original, encoding='utf-8')
    with pytest.raises(AgentError):
        TomlMcpStore(path).add('demo', {'command': 'python'})
    assert path.read_text('utf-8') == original


def test_cli_reports_invalid_toml_as_json(tmp_path, monkeypatch, capsys):
    from autoharness import cli
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'project'
    path = root / '.codex' / 'config.toml'
    path.parent.mkdir(parents=True)
    path.write_text('[broken\n', encoding='utf-8')
    code = cli.main(['--project', str(root), '--agent', 'codex', 'gateway', 'install'])
    assert code == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'error'
    assert 'not valid TOML' in result['error']
    assert path.read_text('utf-8') == '[broken\n'


@pytest.mark.parametrize('operation', ['add', 'remove'])
@pytest.mark.parametrize('quote', ['"""', "'''"])
def test_toml_marker_text_inside_strings_is_preserved(tmp_path, operation, quote):
    from autoharness.agents import tomllib
    path = tmp_path / 'config.toml'
    original = (f'instructions = {quote}\n# >>> autoharness:demo\n'
                f'keep this user text\n# <<< autoharness:demo\n{quote}\n')
    path.write_text(original, encoding='utf-8')
    store = TomlMcpStore(path)
    if operation == 'add':
        store.add('demo', {'command': 'python'})
    else:
        store.remove('demo', created_file=False, created_dir=False)
    assert tomllib.loads(path.read_text('utf-8'))['instructions'] == tomllib.loads(original)['instructions']


def test_other_agent_updates_installed_codex_gateway_env(tmp_path, monkeypatch):
    from autoharness import cli
    from autoharness.agents import tomllib
    from autoharness.env import Project
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'project'
    root.mkdir()
    for agent in ['codex', 'claude']:
        assert cli.main(['--project', str(root), '--agent', agent, 'gateway', 'install']) == 0
    task = Project(root, 'claude').init_task('shared gateway')
    warnings = task.add_mcp('secret-server', {'command': 'python', 'env': {'TOKEN': '${MY_SECRET}'}},
                           source='test', trust={}, permissions={})
    cfg = tomllib.loads((root / '.codex/config.toml').read_text('utf-8'))
    assert 'MY_SECRET' in cfg['mcp_servers']['autoharness']['env_vars']
    assert any('Restart Codex' in warning for warning in warnings)


@pytest.mark.parametrize('user_config', [None, 'model = "example"\n'])
def test_other_agent_adds_when_codex_registry_is_from_another_clone(tmp_path, monkeypatch, user_config):
    from autoharness import cli
    from autoharness.env import Project
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'project'
    root.mkdir()
    project = Project(root, 'claude')
    project.save_gateway_registry({'installed': ['codex'], 'servers': {}})
    path = root / '.codex/config.toml'
    if user_config is not None:
        path.parent.mkdir()
        path.write_text(user_config, encoding='utf-8')
    assert cli.main(['--project', str(root), '--agent', 'claude', 'gateway', 'install']) == 0
    task = project.init_task('cloned registry')
    warnings = task.add_mcp('demo', {'command': 'python'}, source='test', trust={}, permissions={})
    assert 'demo' in task.mcp_lock()
    assert any('gateway install' in warning for warning in warnings)
    assert (path.read_text('utf-8') if path.exists() else None) == user_config


def test_gateway_codex_forwards_named_env_and_binds_project(tmp_path, monkeypatch, capsys):
    from autoharness import cli
    from autoharness.agents import tomllib
    from autoharness.env import Project
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'project'
    root.mkdir()
    assert cli.main(['--project', str(root), '--agent', 'codex', 'gateway', 'install']) == 0
    project = Project(root, 'codex')
    task = project.init_task('env forwarding')
    warnings = task.add_mcp('secret-server', {'command': 'python', 'env': {'TOKEN': '${TEST_TOKEN}'}},
                           source='test', trust={}, permissions={})
    cfg = tomllib.loads(project.mcp_path.read_text('utf-8'))['mcp_servers']['autoharness']
    assert '--project' in cfg['args'] and str(root) in cfg['args']
    assert {'AUTOHARNESS_HOME', 'TEST_TOKEN'} <= set(cfg['env_vars'])
    assert cfg['command'] != 'autoharness', 'bind the interpreter, not a different PATH installation'
    assert any('restart' in w.lower() for w in warnings)
    assert 'TEST_TOKEN' not in cfg.get('env', {})


@pytest.mark.parametrize('operation', ['request', 'close'])
@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_http_does_not_follow_redirect_or_forward_secrets(operation, status):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            self.redirect()

        def do_DELETE(self):
            self.redirect()

        def redirect(self):
            self.send_response(status)
            self.send_header('Location', f'http://localhost:{self.server.server_port}/target')
            self.end_headers()

        def do_GET(self):
            seen.append(dict(self.headers))
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = {'type': 'http', 'url': f'http://127.0.0.1:{server.server_port}/mcp',
               'headers': {'Authorization': 'Bearer fake-test-token', 'X-Api-Key': 'fake-key'}}
        client = HttpClient('test', cfg)
        client._cfg, client.session = cfg, 'fake-session'
        if operation == 'request':
            with pytest.raises(McpError, match='redirect'):
                client.request('ping')
        else:
            client.close()
        assert seen == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
