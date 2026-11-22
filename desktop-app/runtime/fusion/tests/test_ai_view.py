"""Offline acceptance of the AI timeline and its read-only HTTP boundary."""
import copy
import json
import threading
import urllib.error
import urllib.request

import pytest

from fusion.ai import DecisionClient
from fusion.ai_pipeline import Pipeline
from fusion.ai_view import read_runs, run_view
from fusion.core import Store
from fusion.dashboard import make_server
from fusion.tests.test_ai import BAD, DISPATCH, GOOD, PLAN, ROUTE, WORK, FakeTransport


def populate(store):
    client = DecisionClient(store.home / 'ai' / 'AI-test',
                            FakeTransport([ROUTE, PLAN, BAD, PLAN, GOOD, DISPATCH, WORK]))
    Pipeline(store, client, 'AI-test').run('write report.md', allow_unpriced=True)
    return client


def test_pure_projection_rejection_revision_approval(tmp_path):
    store = Store(tmp_path)
    client = populate(store)
    snapshot, records = store.snapshot(), client.summary()['steps']
    before = copy.deepcopy((snapshot, records))
    view = run_view(snapshot, records, 'AI-test')
    assert (snapshot, records) == before
    assert [s['actor'] for s in view['steps']] == [
        'taizi', 'zhongshu', 'menxia', 'zhongshu', 'menxia', 'shangshu', 'gongbu']
    assert [s['plan_revision'] for s in view['steps'][1:5]] == [1, 1, 2, 2]
    assert [r['verdict'] for r in view['reviews']] == ['reject', 'approve']
    assert [r['attempt'] for r in view['reviews']] == [1, 2]
    assert view['reviews'][0]['required_changes'] == BAD['required_changes']
    assert view['reviews'][0]['reasons'] == BAD['reasons']
    assert all(s['model'] and s['timestamp'] and s['tokens']['total_tokens'] == 15
               for s in view['steps'])
    assert view['status'] == 'awaiting_board' and view['state'] == 'Review'
    assert view['artifacts'][0]['path'].endswith('/manifest.json')
    assert read_runs(store) == [view]
    view['steps'][0]['decision']['reason'] = 'changed'
    assert records[0]['decision']['reason'] != 'changed'


@pytest.mark.parametrize('state,expected', [('Cancelled', 'rejected'), ('Done', 'done')])
def test_final_state_without_audit(state, expected):
    snapshot = {'tasks': [{'id': 'AI-x', 'state': state}], 'runs': []}
    assert run_view(snapshot, [], 'AI-x')['status'] == expected


def test_namespace_partial_record_and_symlinks(tmp_path):
    alpha = Store(tmp_path, company='alpha')
    beta = Store(tmp_path, company='beta')
    populate(alpha)
    audit = alpha.home / 'ai' / 'AI-test' / 'decisions.jsonl'
    with audit.open('a') as stream:
        stream.write('{"partial":')
    (beta.home / 'ai').symlink_to(alpha.home / 'ai', target_is_directory=True)
    assert read_runs(beta) == []
    assert len(read_runs(alpha)[0]['steps']) == 7
    assert read_runs(Store(tmp_path)) == []


def test_ledger_fallback_preserves_reviews_without_inventing_usage(tmp_path):
    store = Store(tmp_path)
    populate(store)
    view = run_view(store.snapshot(), [], 'AI-test')
    assert view['source'] == 'ledger'
    assert [r['verdict'] for r in view['reviews']] == ['reject', 'approve']
    assert view['reviews'][0]['reasons'] == ['unsafe']
    assert all(s['model'] is None and s['tokens'] is None for s in view['steps'])


@pytest.mark.parametrize('with_data', [False, True])
def test_routes_read_only_and_panel(tmp_path, with_data, monkeypatch):
    store = Store(tmp_path)
    if with_data:
        populate(store)
    server = make_server(store, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    before = store.snapshot()
    files = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*')
             if p.is_file() and p.suffix not in {'.sqlite3', '.sqlite3-wal', '.sqlite3-shm'}
             and not p.name.startswith('fusion.sqlite3')}

    def forbidden(*args, **kwargs):
        raise AssertionError('GET must not invoke a model or ledger write')

    monkeypatch.setattr(DecisionClient, 'decide', forbidden)
    monkeypatch.setattr(store, 'transaction', forbidden)
    try:
        with urllib.request.urlopen(base + '/api/ai-runs') as response:
            assert response.status == 200
            listing = json.load(response)['runs']
        assert len(listing) == int(with_data)
        if with_data:
            with urllib.request.urlopen(base + '/api/ai-runs/AI-test') as response:
                assert response.status == 200
                view = json.load(response)
                assert [r['verdict'] for r in view['reviews']] == ['reject', 'approve']
        for path in ['/api/ai-runs/missing', '/api/ai-runs/../outside']:
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path)
            assert error.value.code == 404
        for path in ['/api/ai-runs', '/api/ai-runs/AI-test', '/api/advance-state']:
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base + path, data=b'{}'))
            assert error.value.code == 405
        with urllib.request.urlopen(base + '/') as response:
            page = response.read().decode()
            assert '三省决策' in page and '暂无 AI 决策记录' in page
            assert 'textContent = value' in page
        assert store.snapshot() == before
        assert all((tmp_path / path).read_bytes() == body for path, body in files.items())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
