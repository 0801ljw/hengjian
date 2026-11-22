"""Read-only projections of company-local AI audits and authoritative ledger state."""
from __future__ import annotations

import copy
import json
import re


def run_view(snapshot, records, run_id):
    """Pure projection; audit attempts are distinct from plan/review rounds."""
    task = next((t for t in snapshot['tasks'] if t['id'] == run_id), None)
    runs = [r for r in snapshot['runs'] if r['task_id'] == run_id]
    events = [e for e in snapshot.get('events', []) if e['task_id'] == run_id]
    source = 'audit' if records else 'ledger'
    if not records:
        # Historic/imported stores may retain the ledger without audit files.
        # Do not invent missing model usage or required changes.
        records = []
        for event in events:
            detail = event['detail']
            decision = {'reason': detail.get('reason', ''), 'action': event['action'],
                        'to': detail.get('to')}
            if event['action'] == 'review_decision':
                decision['verdict'] = 'reject' if detail['decision'] == 'reject' else 'approve'
            records.append({'step': event['actor'], 'at': event['at'],
                            'status': 'ledger', 'decision': decision})
    steps, reviews = [], []
    revision = 0
    for record in records:
        actor = record.get('step')
        decision = record.get('decision') or {}
        valid = record.get('status') in {'valid', 'ledger'}
        if actor == 'zhongshu' and valid and (source == 'audit' or decision.get('action') == 'payload'):
            revision += 1
        step = {
            'step': len(steps) + 1, 'actor': actor,
            'decision': copy.deepcopy(decision),
            'reasons': copy.deepcopy(decision.get('reasons', [decision['reason']] if 'reason' in decision else [])),
            'required_changes': copy.deepcopy(decision.get('required_changes', [])),
            'timestamp': record.get('at'), 'model': record.get('returned_model') or record.get('model'),
            'tokens': copy.deepcopy(record.get('tokens')), 'status': record.get('status'),
            'attempt': record.get('attempt', 1), 'plan_revision': revision or None,
            'error': record.get('error'),
        }
        steps.append(step)
        if actor == 'menxia' and valid and 'verdict' in decision:
            reviews.append({'attempt': len(reviews) + 1, 'actor': actor,
                            'plan_revision': revision, 'verdict': decision.get('verdict'),
                            'reasons': step['reasons'], 'required_changes': step['required_changes'],
                            'timestamp': step['timestamp']})
    state = task.get('state') if task else None
    if state == 'Cancelled':
        status = 'rejected'
    elif state == 'Done':
        status = 'done'
    elif state == 'Review':
        status = 'awaiting_board'
    elif runs and runs[-1]['status'] in {'failed', 'timeout', 'unavailable'}:
        status = runs[-1]['status']
    elif records and records[-1].get('status') in {'invalid', 'transport_error'}:
        status = 'error'
    elif not task and steps and steps[-1]['decision'].get('kind') == 'chat':
        status = 'chat'
    else:
        status = 'incomplete'
    artifacts = [copy.deepcopy(r['artifact']) for r in runs if r.get('artifact')]
    output = task.get('payload', {}).get('output') if task else None
    if output and output not in artifacts:
        artifacts.append(copy.deepcopy(output))
    return {'id': run_id, 'title': task.get('title', task.get('payload', {}).get('title', run_id)) if task else run_id,
            'status': status, 'state': state, 'steps': steps, 'reviews': reviews,
            'source': source, 'ledger_events': copy.deepcopy(events),
            'artifacts': artifacts, 'runs': copy.deepcopy(runs),
            'timestamp': steps[0]['timestamp'] if steps else None}


def read_runs(store):
    """Read only this Store's namespace. Never instantiate a client or follow symlinks."""
    snapshot = store.snapshot()
    audits = {}
    root = store.home / 'ai'
    if root.is_dir() and not root.is_symlink():
        for directory in sorted(root.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', directory.name):
                continue
            path = directory / 'decisions.jsonl'
            records = []
            if path.is_file() and not path.is_symlink():
                with path.open() as source:
                    for line in source:
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue  # concurrent append may end in a partial line
                        if isinstance(record, dict) and isinstance(record.get('decision'), (dict, type(None))):
                            records.append(record)
            audits[directory.name] = records
    submissions = {}
    queue = store.home / 'requests'
    if queue.is_dir() and not queue.is_symlink():
        for path in sorted(queue.glob('AI-*.json')):
            if path.is_symlink() or not re.fullmatch(r'AI-[A-Za-z0-9_-]{1,125}', path.stem):
                continue
            submissions[path.stem] = json.loads(path.read_text())
    ids = set(audits) | set(submissions) | {t['id'] for t in snapshot['tasks']
                         if t.get('payload', {}).get('route')}
    views = []
    for key in sorted(ids):
        view = run_view(snapshot, audits.get(key, []), key)
        submission = submissions.get(key, {})
        if submission.get('status') in {'queued', 'running', 'error'}:
            view['status'] = submission['status']
        if submission.get('result', {}).get('error'):
            view['error'] = copy.deepcopy(submission['result']['error'])
        views.append(view)
    return views
