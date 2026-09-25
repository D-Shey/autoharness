import io
import json
import math
from pathlib import Path

import pytest

from autoharness import cli
from autoharness.env import Project


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('AUTOHARNESS_OFFLINE', '1')
    root = tmp_path / 'project'
    root.mkdir()
    return Project(root, 'claude')


def run(project, capsys, *args):
    code = cli.main(['--project', str(project.root), '--agent', 'claude', *args])
    return code, json.loads(capsys.readouterr().out)


def test_light_init_skips_discovery_and_brief_and_can_upgrade(project, capsys, monkeypatch):
    import autoharness.cli as module
    monkeypatch.setattr(module, 'discover', lambda *a, **k: pytest.fail('light mode searched registries'))
    code, created = run(project, capsys, 'init', '--mode', 'light', 'fix a typo')
    assert code == 0 and created['mode'] == 'light'
    assert 'discovered' not in created and 'all_capabilities' not in created
    assert project.task().info()['mode'] == 'light'
    code, denied = run(project, capsys, 'add', 'mcp', 'playwright')
    assert code == 1 and 'mode full' in denied['error']
    monkeypatch.setattr(module, 'discover', lambda *a, **k: [{'skipped': 'test'}])
    code, upgraded = run(project, capsys, 'mode', 'full')
    assert code == 0 and upgraded['mode'] == 'full'
    assert project.task().info()['discovery'] == [{'skipped': 'test'}]


def test_light_mode_rejects_skill_new(project, capsys):
    assert run(project, capsys, 'init', '--mode', 'light', 'fix a typo')[0] == 0
    code, denied = run(project, capsys, 'skill', 'new', 'new-tool', '--description', 'Use for testing')
    assert code == 1 and 'mode full' in denied['error']
    assert not (project.root / '.claude' / 'skills' / 'new-tool').exists()


def test_auto_mode_selects_light_or_full_without_changing_default(project, capsys):
    assert run(project, capsys, 'init', '--mode', 'auto', 'fix typo in text')[1]['mode'] == 'light'
    assert run(project, capsys, 'teardown')[0] == 0
    assert run(project, capsys, 'init', '--mode', 'auto', 'fix browser form')[1]['mode'] == 'full'
    assert run(project, capsys, 'teardown', '--force')[0] == 0
    assert run(project, capsys, 'init', 'fix typo in text')[1]['mode'] == 'full'


def test_light_mode_opens_hook_gate_and_tears_down_without_review(project, capsys, monkeypatch):
    assert run(project, capsys, 'init', '--mode', 'light', 'fix browser form')[0] == 0
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({
        'hook_event_name': 'PreToolUse', 'session_id': 'light-session',
        'tool_name': 'Write', 'tool_input': {'file_path': 'x'}})))
    assert cli.main(['--project', str(project.root), 'hook']) == 0
    assert capsys.readouterr().out == ''
    assert run(project, capsys, 'teardown')[0] == 0


def test_light_mode_honors_explicit_critic_requirement(project, capsys):
    assert run(project, capsys, 'init', '--mode', 'light', 'fix a typo')[0] == 0
    assert run(project, capsys, 'brief', '--goal', 'correct spelling', '--inputs', 'the existing file',
               '--constraints', '', '--verify', 'read the file', '--candidates', 'none',
               '--gaps', 'none: the editor is enough', '--review', 'critic')[0] == 0
    code, result = run(project, capsys, 'teardown')
    assert code == 1 and 'critic' in str(result).lower()


def test_hook_closes_gate_again_when_light_task_upgrades(project, capsys, monkeypatch):
    assert run(project, capsys, 'init', '--mode', 'light', 'fix a typo')[0] == 0
    event = {'hook_event_name': 'PreToolUse', 'session_id': 'upgrade-session',
             'tool_name': 'Write', 'tool_input': {'file_path': 'x'}}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(event)))
    assert cli.main(['--project', str(project.root), 'hook']) == 0
    assert capsys.readouterr().out == ''
    assert run(project, capsys, 'mode', 'full')[0] == 0
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(event)))
    assert cli.main(['--project', str(project.root), 'hook']) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_hook_requires_new_brief_for_second_full_task_in_same_session(project, capsys, monkeypatch):
    assert run(project, capsys, 'init', 'first small task')[0] == 0
    assert run(project, capsys, 'brief', '--goal', 'first done', '--inputs', 'file',
               '--constraints', '', '--verify', 'read file', '--candidates', 'none',
               '--gaps', 'none: tools available')[0] == 0
    event = {'hook_event_name': 'PreToolUse', 'session_id': 'reused-session',
             'tool_name': 'Write', 'tool_input': {'file_path': 'x'}}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(event)))
    assert cli.main(['--project', str(project.root), 'hook']) == 0
    assert capsys.readouterr().out == ''
    assert run(project, capsys, 'teardown')[0] == 0
    assert run(project, capsys, 'init', 'second small task')[0] == 0
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(event)))
    assert cli.main(['--project', str(project.root), 'hook']) == 0
    response = json.loads(capsys.readouterr().out)
    assert response['hookSpecificOutput']['permissionDecision'] == 'deny'


def test_cli_events_omit_argument_values_and_track_duration(project, capsys):
    secret = 'private-task-text-unique'
    code, created = run(project, capsys, 'init', '--mode', 'light', secret)
    assert code == 0
    run(project, capsys, 'status')
    run(project, capsys, 'teardown')
    task_dir = project.state / 'tasks' / created['task_id']
    raw = (task_dir / 'events.jsonl').read_text('utf-8')
    events = [json.loads(line) for line in raw.splitlines()]
    assert [e['command'] for e in events] == ['init', 'teardown']
    assert all(e['duration_seconds'] >= 0 and e['exit_code'] == 0 for e in events)
    assert secret not in raw
    assert project.task(created['task_id']).info()['finished_epoch'] >= project.task(created['task_id']).info()['started_epoch']


def test_eval_record_and_compare_pairs_and_ignores_unpaired(project, capsys):
    assert run(project, capsys, 'eval', 'record', '--case', 'A', '--variant', 'baseline',
               '--passed', 'no', '--seconds', '20', '--tokens', '500')[0] == 0
    assert run(project, capsys, 'eval', 'record', '--case', 'A', '--variant', 'harness',
               '--passed', 'yes', '--seconds', '10', '--tokens', '300')[0] == 0
    assert run(project, capsys, 'eval', 'record', '--case', 'B', '--variant', 'harness',
               '--passed', 'yes', '--seconds', '2')[0] == 0
    code, report = run(project, capsys, 'eval', 'compare')
    assert code == 0 and report['paired_cases'] == 1
    assert report['unpaired_cases'] == ['B']
    assert report['baseline']['pass_rate'] == 0
    assert report['harness']['pass_rate'] == 1
    assert report['delta']['seconds'] == -10
    assert report['delta']['tokens'] == -200


def test_eval_can_measure_completed_harness_task_without_explicit_seconds(project, capsys):
    code, created = run(project, capsys, 'init', '--mode', 'light', 'tiny task')
    assert code == 0
    assert run(project, capsys, 'teardown')[0] == 0
    code, entry = run(project, capsys, 'eval', 'record', '--case', 'tiny', '--variant', 'harness',
                      '--passed', 'yes', '--task-id', created['task_id'])
    assert code == 0 and entry['run']['seconds'] >= 0
    assert entry['run']['cli_calls'] >= 2
    assert entry['run']['mcp_installed'] == []
    assert entry['run']['skills_installed'] == []
    assert run(project, capsys, 'eval', 'compare')[1]['unpaired_cases'] == ['tiny']


def test_eval_counts_server_added_then_removed_during_task(project, capsys):
    code, created = run(project, capsys, 'init', 'work with a server')
    assert code == 0
    task = project.task()
    task.add_mcp('temporary', {'command': 'python'}, source='test', trust={}, permissions={})
    task.remove_mcp('temporary')
    assert run(project, capsys, 'teardown')[0] == 0
    code, entry = run(project, capsys, 'eval', 'record', '--case', 'server-case',
                      '--variant', 'harness', '--passed', 'yes', '--task-id', created['task_id'])
    assert code == 0 and entry['run']['mcp_installed'] == ['temporary']


def test_eval_counts_skill_created_then_removed_during_task(project, capsys):
    code, created = run(project, capsys, 'init', 'work with a skill')
    assert code == 0
    task = project.task()
    task.new_skill('temporary-skill', 'Use for temporary tasks')
    task.remove_skill('temporary-skill')
    assert run(project, capsys, 'teardown')[0] == 0
    code, entry = run(project, capsys, 'eval', 'record', '--case', 'skill-case',
                      '--variant', 'harness', '--passed', 'yes', '--task-id', created['task_id'])
    assert code == 0 and entry['run']['skills_installed'] == ['temporary-skill']


@pytest.mark.parametrize('arguments', [
    ['--seconds', '-1'], ['--seconds', 'nan'], ['--seconds', 'inf'],
    ['--seconds', '1', '--tokens', '-1'], ['--seconds', '1', '--cost', '-0.1'],
])
def test_eval_rejects_invalid_measurements(project, capsys, arguments):
    code, result = run(project, capsys, 'eval', 'record', '--case', 'A', '--variant', 'baseline',
                       '--passed', 'yes', *arguments)
    assert code == 1 and result['status'] == 'error'
    assert not (project.state / 'evals.jsonl').exists()


def test_eval_compare_empty_dataset_has_no_division_by_zero(project, capsys):
    code, report = run(project, capsys, 'eval', 'compare')
    assert code == 0 and report['paired_cases'] == 0
    assert report['baseline'] is None and report['harness'] is None


def test_eval_rejects_invalid_task_id_and_incomplete_log(project, capsys):
    code, result = run(project, capsys, 'eval', 'record', '--case', 'A', '--variant', 'harness',
                       '--passed', 'yes', '--task-id', '../../other')
    assert code == 1 and result['status'] == 'error'
    project.state.mkdir(exist_ok=True)
    (project.state / 'evals.jsonl').write_text('{bad json}\n', encoding='utf-8')
    code, result = run(project, capsys, 'eval', 'compare')
    assert code == 1 and result['status'] == 'error'


def test_eval_rejects_unknown_well_formed_task_id(project, capsys):
    code, result = run(project, capsys, 'eval', 'record', '--case', 'A', '--variant', 'harness',
                       '--passed', 'yes', '--seconds', '1', '--task-id', '20260925-120000-abcdef')
    assert code == 1 and 'task' in result['error'].lower()
    assert not (project.state / 'evals.jsonl').exists()


def test_eval_rejects_valid_json_with_wrong_schema(project, capsys):
    project.state.mkdir(exist_ok=True)
    (project.state / 'evals.jsonl').write_text('{}\n', encoding='utf-8')
    code, result = run(project, capsys, 'eval', 'compare')
    assert code == 1 and result['status'] == 'error'


def test_eval_compares_cases_with_equal_weight_and_missing_metrics(project, capsys):
    for seconds in (10, 20, 30):
        assert run(project, capsys, 'eval', 'record', '--case', 'A', '--variant', 'baseline',
                   '--passed', 'yes', '--seconds', str(seconds))[0] == 0
    assert run(project, capsys, 'eval', 'record', '--case', 'A', '--variant', 'harness',
               '--passed', 'yes', '--seconds', '10')[0] == 0
    assert run(project, capsys, 'eval', 'record', '--case', 'B', '--variant', 'baseline',
               '--passed', 'no', '--seconds', '100')[0] == 0
    assert run(project, capsys, 'eval', 'record', '--case', 'B', '--variant', 'harness',
               '--passed', 'yes', '--seconds', '50')[0] == 0
    code, report = run(project, capsys, 'eval', 'compare')
    assert code == 0 and report['paired_cases'] == 2
    assert report['baseline']['seconds'] == 60
    assert report['harness']['seconds'] == 30
    assert report['delta']['tokens'] is None
