"""CI result contracts, with no real nested test run or network requests."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def ci(monkeypatch):
    monkeypatch.setenv('PYTHONPATH', os.environ.get('PYTHONPATH', ''))
    monkeypatch.setenv('UV_OFFLINE', os.environ.get('UV_OFFLINE', ''))
    path = Path(__file__).resolve().parents[1] / 'ci/run.py'
    spec = importlib.util.spec_from_file_location('fusion_ci', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/bin/' + name)
    monkeypatch.setattr(module, 'pytest_command', lambda: ['fake-pytest'])
    return module


def fake_commands(ci, monkeypatch, failure=False, skip=False, malformed=False):
    def command(args):
        if args[0] == 'git':
            return SimpleNamespace(returncode=0, stdout='abc123\n')
        if args[0] == 'fake-pytest':
            target = next(a.split('=', 1)[1] for a in args if a.startswith('--junitxml='))
            body = '<failure message="broken"/>' if failure else '<skipped/>' if skip else ''
            Path(target).write_text('<testsuites><testsuite><testcase name="example">'
                                    + body + '</testcase></testsuite></testsuites>')
            return SimpleNamespace(returncode=int(failure), stdout='test output')
        assert args == ['bash', 'fusion/verify_all.sh']
        return SimpleNamespace(returncode=0, stdout='no summary' if malformed else
                               '  PASS example\n  PASS 36 / FAIL 0 / SKIP 0\n')
    monkeypatch.setattr(ci, 'command', command)


def test_ci_status_json(ci, monkeypatch, tmp_path):
    fake_commands(ci, monkeypatch)
    assert ci.run(tmp_path) == 0
    status = json.loads((tmp_path / 'ci-status.json').read_text())
    assert {'time', 'commit', 'tests_passed', 'tests_failed', 'acceptance_pass',
            'acceptance_fail', 'ok'} <= status.keys()
    assert status['ok'] is True
    assert status['tests_passed'] == 1
    assert status['acceptance_pass'] == 36
    assert not (tmp_path / 'ci-failures.log').exists()


def test_ci_marks_skips(ci, monkeypatch, tmp_path):
    fake_commands(ci, monkeypatch)
    monkeypatch.setattr(ci, 'pytest_command', lambda: None)
    assert ci.run(tmp_path) == 2
    status = json.loads((tmp_path / 'ci-status.json').read_text())
    assert status['ok'] is False
    assert status['tests_status'] == status['acceptance_status'] == 'SKIP'
    assert status['tests_passed'] == status['acceptance_pass'] == 0
    assert 'pytest' in status['reasons'][0]
    assert 'SKIP' in (tmp_path / 'ci-failures.log').read_text()


def test_ci_appends_failures(ci, monkeypatch, tmp_path):
    fake_commands(ci, monkeypatch, failure=True)
    assert ci.run(tmp_path) == 1
    assert ci.run(tmp_path) == 1
    entries = [json.loads(line) for line in (tmp_path / 'ci-failures.log').read_text().splitlines()]
    assert len(entries) == 2
    assert entries[0]['tests_failed'] == 1
    assert entries[0]['failure_items'] == ['::example']
    assert entries[0]['commit'] == 'abc123'
    assert entries[0]['time']


def test_ci_pytest_skip_is_not_pass(ci, monkeypatch, tmp_path):
    fake_commands(ci, monkeypatch, skip=True)
    assert ci.run(tmp_path) == 2
    status = json.loads((tmp_path / 'ci-status.json').read_text())
    assert status['tests_status'] == 'SKIP'
    assert status['tests_skipped'] == 1
    assert status['ok'] is False


def test_ci_rejects_missing_acceptance_summary(ci, monkeypatch, tmp_path):
    fake_commands(ci, monkeypatch, malformed=True)
    assert ci.run(tmp_path) == 1
    status = json.loads((tmp_path / 'ci-status.json').read_text())
    assert status['acceptance_status'] == 'FAIL'
    assert status['ok'] is False


def test_ci_shell_missing_python(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'ci.sh'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('dirname', 'mkdir', 'flock', 'date', 'git', 'mv'):
        (bin_dir / name).symlink_to(shutil.which(name))
    output = tmp_path / 'output'
    result = subprocess.run(['/bin/bash', str(script), '--output-dir', str(output)],
                            env={'PATH': str(bin_dir), 'HOME': str(tmp_path)},
                            capture_output=True, text=True)
    assert result.returncode == 2, result.stderr
    status = json.loads((output / 'ci-status.json').read_text())
    assert status['ok'] is False
    assert status['tests_status'] == 'SKIP'
    assert status['reasons'] == ['SKIP: python3 missing']
    assert 'python3 missing' in (output / 'ci-failures.log').read_text()


def test_ci_lock_preserves_status(ci, tmp_path):
    status = tmp_path / 'ci-status.json'
    status.write_text('previous')
    with (tmp_path / 'ci.lock').open('a') as lock:
        ci.fcntl.flock(lock, ci.fcntl.LOCK_EX | ci.fcntl.LOCK_NB)
        assert ci.run(tmp_path) == 2
    assert status.read_text() == 'previous'
