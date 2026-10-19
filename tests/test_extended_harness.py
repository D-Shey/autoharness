import json
import sys

from autoharness import cli
from autoharness.env import Project
from conftest import TEST_DOMAINS, TEST_METHOD


def run(root, capsys, *args):
    code = cli.main(['--project', str(root), '--agent', 'claude', *args])
    return code, json.loads(capsys.readouterr().out)


def test_profiles_set_rules_and_reviewed_requires_critic(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('AUTOHARNESS_OFFLINE', '1')
    root = tmp_path / 'repo'
    root.mkdir()
    assert run(root, capsys, 'init', 'small task', '--profile', 'quick')[1]['mode'] == 'light'
    assert run(root, capsys, 'add', 'mcp', 'playwright')[0] == 1
    assert run(root, capsys, 'teardown')[0] == 0
    result = run(root, capsys, 'init', 'review this', '--profile', 'reviewed')[1]
    assert result['mode'] == 'reviewed'
    task = Project(root, 'claude').task()
    assert task.info()['profile'] == 'reviewed' and task.info()['mode'] == 'full'
    assert run(root, capsys, 'init', 'another', '--mode', 'light', '--profile', 'quick')[0] == 1
    code, incomplete = run(root, capsys, 'teardown')
    assert code == 1 and 'critic' in str(incomplete).lower()


def test_one_mode_flag_covers_every_level_and_a_task_can_be_raised(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('AUTOHARNESS_OFFLINE', '1')
    root = tmp_path / 'repo'
    root.mkdir()
    light = run(root, capsys, 'init', 'fix a typo in the readme', '--mode', 'light')[1]
    # light mode does no discovery, so init prints none of what discovery needs
    assert light['mode'] == 'light' and not {'all_capabilities', 'recipes', 'discovered', 'suggested'} & set(light)
    assert 'dir' in light
    raised = run(root, capsys, 'mode', 'reviewed')[1]
    assert raised['mode'] == 'reviewed' and 'critic' in raised['next']
    assert run(root, capsys, 'status')[0] == 0   # offline discovery is `skipped`, which status must survive
    task = Project(root, 'claude').task()
    assert task.info()['mode'] == 'full' and task.info()['profile'] == 'reviewed'
    # asking for full afterwards does not quietly drop the critic
    assert run(root, capsys, 'mode', 'full')[1]['mode'] == 'reviewed'
    assert run(root, capsys, 'teardown')[0] == 1   # reviewed: a critic is still missing
    assert run(root, capsys, 'teardown', '--force')[0] == 0
    assert run(root, capsys, 'init', 'review this', '--mode', 'reviewed')[1]['mode'] == 'reviewed'
    assert Project(root, 'claude').task().info()['profile'] == 'reviewed'


def test_status_is_compact_unless_asked_for_everything(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('AUTOHARNESS_OFFLINE', '1')
    root = tmp_path / 'repo'
    root.mkdir()
    assert run(root, capsys, 'init', 'a task')[0] == 0
    task = Project(root, 'claude').task()
    task.record_discovery([{'kind': 'skill', 'query': 'poster', 'results': [{'name': f's{i}'} for i in range(5)]}])
    for i in range(40):
        task.log(f'step {i}')
    short = run(root, capsys, 'status')[1]
    assert short['task']['discovery'] == ['skill: poster (5 found)']
    assert 'step 39' in short['log'] and 'step 0\n' not in short['log'] and 'status --full' in short['log']
    task.record_discovery([{'skipped': 'offline'},
                           {'kind': 'mcp', 'query': 'browser', 'error': 'timeout', 'recipe': 'web'}])
    assert run(root, capsys, 'status')[1]['task']['discovery'] == [
        'skipped: offline', 'mcp: browser (failed: timeout) [recipe web]']
    task.record_discovery([{'kind': 'skill', 'query': 'poster', 'results': [{'name': f's{i}'} for i in range(5)]}])
    full = run(root, capsys, 'status', '--full')[1]
    assert full['task']['discovery'][0]['results'][4]['name'] == 's4' and 'step 0\n' in full['log']


def test_audit_records_decisions_install_and_removal(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('AUTOHARNESS_OFFLINE', '1')
    root = tmp_path / 'repo'
    root.mkdir()
    assert run(root, capsys, 'init', 'server task')[0] == 0
    assert run(root, capsys, 'decision', 'mcp', 'server', 'select', '--reason', 'Needed for API checks')[0] == 0
    task = Project(root, 'claude').task()
    task.add_mcp('server', {'command': 'python', 'version': '1.2'}, source='test', trust={}, permissions={})
    assert run(root, capsys, 'remove', 'mcp', 'server', '--reason', 'No longer needed')[0] == 0
    events = run(root, capsys, 'audit')[1]['events']
    assert [e['action'] for e in events] == ['select', 'install', 'remove']
    assert events[1]['version'] == '1.2' and len(events[1]['fingerprint']) == 64
    assert events[2]['reason'] == 'No longer needed'


def test_verification_templates_and_recorded_evidence(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('AUTOHARNESS_OFFLINE', '1')
    root = tmp_path / 'repo'
    root.mkdir()
    for kind in ('browser', 'api', 'logs', 'database'):
        code, guide = run(root, capsys, 'verify', 'guide', kind)
        assert code == 0 and guide['steps'] and guide['brief_verify']
    assert run(root, capsys, 'init', '--mode', 'light', 'check API')[0] == 0
    code, brief = run(root, capsys, 'brief', '--goal', 'healthy API', '--inputs', 'service', '--domains', TEST_DOMAINS, '--method', TEST_METHOD, '--expects', 'none: the task settles every choice',
                      '--constraints', '', '--verify-template', 'api', '--candidates', 'none',
                      '--gaps', 'none: existing tools')
    assert code == 0
    assert Project(root, 'claude').task().info()['brief']['verify_template'] == 'api'
    proof = root / 'api-proof.txt'
    proof.write_text('GET /health -> 200', encoding='utf-8')
    code, result = run(root, capsys, 'verify', 'record', 'api', '--passed', 'yes', '--evidence', str(proof))
    assert code == 0 and result['check']['passed'] is True
    assert Project(root, 'claude').task().info()['checks'][0]['kind'] == 'api'


def test_eval_batch_runs_same_cases_and_counts_tools(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'repo'
    root.mkdir()
    dataset = tmp_path / 'cases.jsonl'
    dataset.write_text('{"case":"A","input":"hello"}\n{"case":"B","input":"world"}\n', encoding='utf-8')
    runner = tmp_path / 'runner.py'
    runner.write_text('import json,sys\ncase=json.load(sys.stdin)\n'
                      'print(json.dumps({"passed":True,"tokens":len(case["input"]),"tool_calls":2}))\n',
                      encoding='utf-8')
    for variant in ('baseline', 'harness'):
        code, result = run(root, capsys, 'eval', 'batch', '--dataset', str(dataset),
                           '--variant', variant, '--', sys.executable, str(runner))
        assert code == 0 and result['recorded'] == 2
    report = run(root, capsys, 'eval', 'compare')[1]
    assert report['paired_cases'] == 2
    assert report['baseline']['tool_calls'] == 2
    assert report['harness']['tool_calls'] == 2


def test_eval_batch_does_not_record_partial_failed_set(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'repo'
    root.mkdir()
    dataset = tmp_path / 'cases.jsonl'
    dataset.write_text('{"case":"A","input":1}\n{"case":"B","input":2}\n', encoding='utf-8')
    runner = tmp_path / 'runner.py'
    runner.write_text('import json,sys\nx=json.load(sys.stdin)\n'
                      'print(json.dumps({"passed":True}) if x["input"] == 1 else "not json")\n',
                      encoding='utf-8')
    code, error = run(root, capsys, 'eval', 'batch', '--dataset', str(dataset), '--variant', 'harness',
                      '--', sys.executable, str(runner))
    assert code == 1 and 'invalid JSON' in error['error']
    assert not (root / '.autoharness' / 'evals.jsonl').exists()


def test_eval_batch_runner_can_call_autoharness_without_locking_itself(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'repo'
    root.mkdir()
    dataset = tmp_path / 'cases.jsonl'
    dataset.write_text(json.dumps({'case': 'A', 'input': str(root)}) + '\n', encoding='utf-8')
    runner = tmp_path / 'runner.py'
    runner.write_text('import contextlib,io,json,sys\nfrom autoharness import cli\n'
                      'case=json.load(sys.stdin)\nwith contextlib.redirect_stdout(io.StringIO()):\n'
                      '    code=cli.main(["--project",case["input"],"status"])\n'
                      'print(json.dumps({"passed":code==0}))\n', encoding='utf-8')
    code, result = run(root, capsys, 'eval', 'batch', '--dataset', str(dataset), '--variant', 'baseline',
                       '--timeout', '3', '--', sys.executable, str(runner))
    assert code == 0 and result['recorded'] == 1


def test_eval_batch_rejects_bad_task_id_before_writing_rows(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv('AUTOHARNESS_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'repo'
    root.mkdir()
    dataset = tmp_path / 'cases.jsonl'
    dataset.write_text('{"case":"A","input":1}\n{"case":"B","input":2}\n', encoding='utf-8')
    runner = tmp_path / 'runner.py'
    runner.write_text('import json,sys\nx=json.load(sys.stdin)\n'
                      'print(json.dumps({"passed":True,"task_id":[]}) if x["input"] == 2 '
                      'else json.dumps({"passed":True}))\n', encoding='utf-8')
    code, error = run(root, capsys, 'eval', 'batch', '--dataset', str(dataset), '--variant', 'harness',
                      '--', sys.executable, str(runner))
    assert code == 1 and 'task ID' in error['error']
    assert not (root / '.autoharness' / 'evals.jsonl').exists()
