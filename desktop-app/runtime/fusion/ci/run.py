"""Offline regression coordinator. Exit 0=PASS, 1=FAIL, 2=SKIP/environment."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[2]


def command(args):
    return subprocess.run(args, cwd=REPO, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=False)


def pytest_command():
    # Match verify_all.sh's runner selection, including its offline uv path.
    if shutil.which('uv'):
        cached = ['uv', 'tool', 'run', '--offline', '--from', 'pytest', 'pytest']
        if command(cached + ['--version']).returncode == 0:
            return cached
        return None
    direct = [sys.executable, '-m', 'pytest']
    if command(direct + ['--version']).returncode == 0:
        return direct
    return None


def junit(path):
    cases = list(ET.parse(path).getroot().iter('testcase'))
    failed = [c for c in cases if c.find('failure') is not None or c.find('error') is not None]
    skipped = [c for c in cases if c.find('skipped') is not None]
    names = [f"{c.get('classname', '')}::{c.get('name', '')}" for c in failed]
    return len(cases) - len(failed) - len(skipped), len(failed), len(skipped), names


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    # A manual run and timer must not clobber each other's status/logs.
    with (output / 'ci.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('SKIP: another CI run holds ci.lock', file=sys.stderr)
            return 2
        return run_locked(output)


def run_locked(output):
    os.environ['UV_OFFLINE'] = '1'
    os.environ['PYTHONPATH'] = str(REPO) + os.pathsep + str(REPO / 'scripts')
    git = command(['git', 'rev-parse', 'HEAD']) if shutil.which('git') else None
    status = dict(time=datetime.now(timezone.utc).isoformat(),
                  commit=git.stdout.strip() if git and git.returncode == 0 else 'unknown',
                  tests_passed=0, tests_failed=0, acceptance_pass=0, acceptance_fail=0,
                  tests_skipped=0, acceptance_skip=0, tests_status='SKIP',
                  acceptance_status='SKIP', reasons=[], failure_items=[], ok=False)
    test_cmd = pytest_command()
    missing = [name for name in ('bash', 'openssl', 'node', 'which') if not shutil.which(name)]
    if test_cmd is None:
        missing.append('pytest (python module or uv offline cache)')
    if missing:
        status['reasons'].append('SKIP: missing dependencies: ' + ', '.join(missing))
    else:
        with tempfile.TemporaryDirectory(prefix='fusion-ci-') as temp:
            report = Path(temp) / 'pytest.xml'
            result = command(test_cmd + ['tests/', 'fusion/tests/', '-q', '-ra',
                                         '--junitxml=' + str(report)])
            (output / 'ci-tests.log').write_text(result.stdout, encoding='utf-8')
            try:
                passed, failed, skipped, names = junit(report)
                status.update(tests_passed=passed, tests_failed=failed, tests_skipped=skipped)
                status['failure_items'].extend(names)
            except (OSError, ET.ParseError) as exc:
                status['reasons'].append(f'pytest report unavailable: {exc}')
            status['tests_status'] = ('PASS' if result.returncode == 0 and
                                      status['tests_passed'] > 0 and not
                                      (status['tests_failed'] or status['tests_skipped'] or status['reasons'])
                                      else 'SKIP' if result.returncode == 0 and status['tests_skipped']
                                      else 'FAIL')
            if result.returncode:
                status['reasons'].append(f'pytest exit {result.returncode}; see ci-tests.log')
            if status['tests_skipped']:
                status['reasons'].append('SKIP: pytest skipped cases; see ci-tests.log for reasons')
            # Run the full acceptance command even if pytest failed.
            result = command(['bash', 'fusion/verify_all.sh'])
            (output / 'ci-acceptance.log').write_text(result.stdout, encoding='utf-8')
            summary = re.search(r'PASS (\d+) / FAIL (\d+) / SKIP (\d+)\s*$', result.stdout)
            if summary:
                p, f, s = map(int, summary.groups())
                status.update(acceptance_pass=p, acceptance_fail=f, acceptance_skip=s)
                status['acceptance_status'] = ('FAIL' if result.returncode or f or not p
                                                else 'SKIP' if s else 'PASS')
                if s:
                    status['reasons'].extend(re.findall(r'^\s*SKIP (.+)$', result.stdout, re.M))
            else:
                status['acceptance_status'] = 'FAIL'
                status['reasons'].append('acceptance summary missing; see ci-acceptance.log')
            status['failure_items'].extend(re.findall(r'^\s*FAIL (.+)$', result.stdout, re.M))
            if result.returncode:
                status['reasons'].append(f'acceptance exit {result.returncode}; see ci-acceptance.log')
    status['ok'] = status['tests_status'] == status['acceptance_status'] == 'PASS'
    payload = json.dumps(status, ensure_ascii=False, indent=2) + '\n'
    # Atomic replacement: readers never see partially written JSON.
    temporary = output / 'ci-status.json.tmp'
    temporary.write_text(payload, encoding='utf-8')
    temporary.replace(output / 'ci-status.json')
    if not status['ok']:
        with (output / 'ci-failures.log').open('a', encoding='utf-8') as log:
            log.write(json.dumps(status, ensure_ascii=False) + '\n')
    print(payload, end='')
    if status['ok']:
        return 0
    return 1 if 'FAIL' in (status['tests_status'], status['acceptance_status']) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('/home/ljw/boss-lab'))
    args = parser.parse_args()
    return run(args.output_dir.resolve())


if __name__ == '__main__':
    sys.exit(main())
