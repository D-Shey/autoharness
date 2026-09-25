"""Opt-in integration with an installed Codex CLI; no model calls or account needed."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

import pytest

from autoharness import cli
from autoharness.env import Project

pytestmark = pytest.mark.skipif(
    os.environ.get('AUTOHARNESS_TEST_CODEX') != '1' or not shutil.which('codex'),
    reason='set AUTOHARNESS_TEST_CODEX=1 and install Codex CLI to run',
)


class Rpc:
    def __init__(self, command, cwd, env):
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding='utf-8')
        self.inbox = queue.Queue()
        self.rid = 0
        def read():
            for line in self.proc.stdout:
                self.inbox.put(json.loads(line))
        threading.Thread(target=read, daemon=True).start()

    def send(self, method, params, rid=None):
        self.proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params,
                                         **({'id': rid} if rid is not None else {})}) + '\n')
        self.proc.stdin.flush()

    def call(self, method, params):
        self.rid += 1
        self.send(method, params, self.rid)
        end = time.monotonic() + 40
        while time.monotonic() < end:
            try:
                msg = self.inbox.get(timeout=0.2)
            except queue.Empty:
                assert self.proc.poll() is None, 'Codex exited without replying'
                continue
            if msg.get('id') == self.rid and 'method' not in msg:
                assert 'error' not in msg, msg
                return msg['result']
        pytest.fail(f'no response to {method}')

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def test_codex_reads_skill_and_calls_gateway(tmp_path, monkeypatch, capsys):
    root = tmp_path / 'project with spaces'
    root.mkdir()
    home = tmp_path / 'codex-home'
    home.mkdir()
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'harness-home'))
    monkeypatch.setenv('AUTOHARNESS_OFFLINE', '1')
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (home / 'config.toml').write_text(
        f'[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"\n', encoding='utf-8')
    base = ['--project', str(root), '--agent', 'codex']
    assert cli.main(base + ['install-skill']) == 0
    assert cli.main(base + ['gateway', 'install']) == 0
    capsys.readouterr()
    project = Project(root, 'codex')
    task = project.init_task('Codex integration')
    task.add_mcp('fake', {'command': sys.executable,
                         'args': [str(Path(__file__).with_name('fake_mcp_server.py'))],
                         'env': {'FAKE_TOKEN': '${CODEX_TEST_TOKEN}'}},
                 source='test', trust={}, permissions={})
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'PATHEXT',
                            'LOCALAPPDATA', 'APPDATA', 'COMSPEC', 'AUTOHARNESS_HOME'}}
    env.update(CODEX_HOME=str(home), HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               AUTOHARNESS_OFFLINE='1', CODEX_TEST_TOKEN='dummy-token')
    codex = shutil.which('codex')
    loaded = subprocess.run([codex, 'mcp', 'get', 'autoharness', '--json'], cwd=root,
                            env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert loaded.returncode == 0, loaded.stderr
    assert json.loads(loaded.stdout)['name'] == 'autoharness'
    rpc = Rpc([codex, 'app-server', '--stdio'], root, env)
    try:
        rpc.call('initialize', {'clientInfo': {'name': 'autoharness-tests', 'version': '1'},
                                'capabilities': {'experimentalApi': True}})
        rpc.send('initialized', {})
        skills = rpc.call('skills/list', {'cwds': [str(root)], 'forceReload': True})
        found = [s for row in skills['data'] for s in row['skills']
                 if s['name'] == 'autoharness' and Path(s['path']).is_relative_to(root)]
        assert found, skills
        thread = rpc.call('thread/start', {'cwd': str(root), 'approvalPolicy': 'never',
                                          'sandbox': 'read-only'})
        thread_id = thread['thread']['id']
        end = time.monotonic() + 30
        while True:
            result = rpc.call('mcpServer/tool/call', {'threadId': thread_id,
                'server': 'autoharness', 'tool': 'harness_tools', 'arguments': {}})
            text = result.get('content', result.get('result', {}).get('content', []))
            if text and 'fake' in text[0].get('text', '') and 'running' in text[0]['text']:
                break
            assert time.monotonic() < end, result
            time.sleep(0.2)
        result = rpc.call('mcpServer/tool/call', {'threadId': thread_id,
            'server': 'autoharness', 'tool': 'harness_call',
            'arguments': {'server': 'fake', 'tool': 'echo', 'arguments': {'text': 'codex-ok'}}})
        assert 'codex-ok' in json.dumps(result), result
        # Hot-add another server to the same running Codex thread.
        task.add_mcp('later', {'command': sys.executable,
                     'args': [str(Path(__file__).with_name('fake_mcp_server.py'))]},
                     source='test', trust={}, permissions={})
        end = time.monotonic() + 15
        while True:
            result = rpc.call('mcpServer/tool/call', {'threadId': thread_id,
                'server': 'autoharness', 'tool': 'harness_call',
                'arguments': {'server': 'later', 'tool': 'echo', 'arguments': {'text': 'hot-add-ok'}}})
            if 'hot-add-ok' in json.dumps(result):
                break
            assert time.monotonic() < end, result
            time.sleep(0.2)
        task.remove_mcp('later')
        end = time.monotonic() + 15
        while True:
            result = rpc.call('mcpServer/tool/call', {'threadId': thread_id,
                'server': 'autoharness', 'tool': 'harness_tools', 'arguments': {}})
            servers = json.loads(result['content'][0]['text'])['servers']
            if 'later' not in servers:
                break
            assert time.monotonic() < end, result
            time.sleep(0.2)
    finally:
        rpc.close()
