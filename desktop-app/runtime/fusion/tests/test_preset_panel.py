"""Exercise the actual browser controller offline with a small DOM double."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_preset_install_poll_apply_and_logout():
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable")
    root = Path(__file__).resolve().parents[2]
    script = r"""
const assert = require('node:assert/strict');
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.handlers = {}; this.value = '';
    this.textContent = ''; this.dataset = {}; this.classList = {toggle(){}};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(event, handler) { this.handlers[event] = handler; }
  setAttribute(key, value) { this[key] = value; }
  querySelectorAll(tag) {
    return this.children.flatMap(c => [...(c.tag === tag ? [c] : []), ...c.querySelectorAll(tag)]);
  }
}
const elements = new Map();
global.document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); },
  createElement(tag) { return new Element(tag); },
  documentElement: new Element('html')
};
const $ = id => document.getElementById(id);
let installed = false, forbidden = false, installs = 0, polls = 0, binds = 0;
let timers = new Map(), timerId = 0;
global.setTimeout = (callback, ms) => { const id = ++timerId; timers.set(id, {callback, ms}); return id; };
global.clearTimeout = id => timers.delete(id);
global.fetch = async (url, options) => {
  let data;
  if (url.startsWith('/api/agents') && forbidden) return {ok: false, status: 403, json: async () => ({error: 'admin only'})};
  if (url === '/api/agents') data = {
    preset: [
      {role: 'taizi', agent: 'hermes', model: 'gpt-6.1-terra', found: false},
      {role: 'zhongshu', agent: 'codex', model: 'gpt-6.1-sol', found: installed, path: '<img onerror=evil>'},
      {role: 'menxia', agent: 'claude', model: '', found: true, path: '/fixture/claude'}
    ],
    installability: {hermes: {installable: false, docs: 'https://hermes-agent.nousresearch.com/docs/'}, codex: {installable: true}},
    binding: {bindings: installed ? {zhongshu: {agent: 'codex'}} : {}}
  };
  else if (url === '/api/agents/install') {
    assert.deepEqual(JSON.parse(options.body), {agent: 'codex'});
    installs++; data = {job: 'a'.repeat(32)};
  } else if (url.startsWith('/api/agents/install/')) {
    polls++; installed = polls > 1;
    data = {status: installed ? 'succeeded' : 'running', tail: '<script>unsafe()</script>\ndone', exit_code: installed ? 0 : null};
  } else if (url === '/api/agents/bind') {
    binds++; data = {missing: ['taizi']};
  } else if (url === '/api/tasks') data = {tasks: []};
  else if (url === '/api/ai-runs') data = {runs: []};
  else if (url === '/api/logout') data = {ok: true};
  else throw new Error('unexpected endpoint ' + url);
  return {ok: true, json: async () => data};
};
require('./fusion/web/app.js');
const flush = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  $('token').value = 'fixture-session';
  await $('refresh').handlers.click(); await flush();
  let rows = $('bindings-rows').children;
  assert.equal(rows[0].querySelectorAll('button').length, 0);
  assert.equal(rows[0].querySelectorAll('a').length, 1);
  assert.match(rows[2].children[1].textContent, /agent 默认模型/);
  const button = rows[1].querySelectorAll('button')[0];
  button.handlers.click(); await flush(); await flush();
  assert.equal(installs, 1); assert.equal(polls, 1);
  assert.equal($('apply-preset').disabled, true);
  assert.match($('install-progress').textContent, /running/);
  const pending = [...timers].find(([, value]) => value.ms === 1000);
  assert.ok(pending); timers.delete(pending[0]);
  await pending[1].callback(); await flush();
  assert.equal(polls, 2);
  assert.match($('install-progress').textContent, /succeeded/);
  assert.equal($('install-progress').children.length, 0);
  rows = $('bindings-rows').children;
  assert.equal(rows[1].querySelectorAll('button').length, 0);
  assert.match(rows[1].children[2].textContent, /已就绪/);
  assert.match(rows[1].children[2].title, /<img/);
  await $('apply-preset').handlers.click();
  assert.equal(binds, 1);
  assert.match($('bindings-status').textContent, /预设已应用.*taizi/);
  assert.match($('bindings-current').textContent, /zhongshu/);
  forbidden = true;
  await $('refresh').handlers.click(); await flush();
  assert.match($('bindings-status').textContent, /权限/);
  assert.equal($('apply-preset').disabled, true);
  for (const expected of ['wujin', 'qingci', 'paper']) {
    $('theme').handlers.click(); assert.equal(document.documentElement.dataset.theme, expected);
  }
  await $('logout').handlers.click();
  assert.equal($('bindings-rows').children.length, 0);
  assert.equal($('install-progress').hidden, true);
  assert.equal([...timers.values()].filter(t => t.ms === 1000).length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script], cwd=root, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
