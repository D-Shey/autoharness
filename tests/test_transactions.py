import concurrent.futures
from pathlib import Path

import pytest

from autoharness.env import Project
from autoharness import transactions


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'project'
    root.mkdir()
    return Project(root, 'codex')


def test_mcp_install_recovers_after_config_written(project, monkeypatch):
    task = project.init_task('recovery')
    real = transactions.atomic_write
    def fail(path, content):
        if Path(path).name == 'mcp.lock.json':
            raise OSError('simulated disk failure')
        real(path, content)
    with monkeypatch.context() as patch:
        patch.setattr(transactions, 'atomic_write', fail)
        with pytest.raises(OSError, match='disk failure'):
            task.add_mcp('demo', {'command': 'python'}, source='test', trust={}, permissions={})
    assert project.mcp_path.exists()
    with transactions.locked(project.root):
        transactions.recover(project.root)
    assert task.mcp_lock()['demo']['config'] == {'command': 'python'}
    task.remove_mcp('demo')
    assert not project.agent.mcp_store(project.root).has('demo')


def test_recovery_preserves_user_edit(project, monkeypatch):
    path = project.root / 'a.txt'
    other = project.root / 'b.txt'
    path.write_text('before', encoding='utf-8')
    real = transactions.atomic_write
    def fail(p, content):
        if Path(p) == other:
            raise OSError('simulated failure')
        real(p, content)
    with monkeypatch.context() as patch:
        patch.setattr(transactions, 'atomic_write', fail)
        with pytest.raises(OSError):
            transactions.commit(project.root, {path: 'after', other: 'second'})
    path.write_text('user edit', encoding='utf-8')
    with pytest.raises(OSError, match='changed'):
        transactions.recover(project.root)
    assert path.read_text('utf-8') == 'user edit'
    assert not other.exists()


def test_parallel_mcp_adds_preserve_every_entry(project):
    task = project.init_task('parallel')
    def add(n):
        task.add_mcp(f'server{n}', {'command': 'python'}, source='test', trust={}, permissions={})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(add, range(8)))
    assert len(task.mcp_lock()) == 8
    assert all(project.agent.mcp_store(project.root).has(f'server{n}') for n in range(8))
