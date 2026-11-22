// Exercise the shipped controller with an offline DOM/fetch double.
const assert = require('node:assert/strict');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.value = ''; this.dataset = {}; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  focus() { this.focused = true; }
  all(tag) { return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.all(tag)]); }
}
const elements = new Map();
global.document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); },
  createElement(tag) { return new Element(tag); }, documentElement: new Element('html')
};
const events = {};
global.window = {addEventListener(name, handler) { events[name] = handler; }};
let timers = [];
global.setTimeout = callback => { timers.push(callback); return timers.length; };
global.clearTimeout = () => { timers = []; };
const $ = id => document.getElementById(id);
let detected = ['hermes', 'pi'], denied = false, polls = 0, posted = [], calls = [];
const names = ['claude', 'codex', 'hermes', 'pi'];
const preset = [
  {role: 'taizi', agent: 'hermes', model: 'gpt-6.1-terra'},
  {role: 'zhongshu', agent: 'codex', model: 'gpt-6.1-sol'},
  {role: 'menxia', agent: 'claude', model: ''},
  {role: 'shangshu', agent: 'pi', model: 'deepseek-flash'},
  {role: 'gongbu', agent: 'codex', model: 'gpt-6.1-sol'}
];
global.fetch = async (url, options) => {
  calls.push(url);
  if (denied && url !== '/api/login') return {ok: false, status: 403, json: async () => ({})};
  let result;
  if (url === '/api/onboarding') result = {
    agents: names.map(agent => ({agent, found: detected.includes(agent), version: '<img onerror=evil()>1.0'})),
    suggested: names.find(name => detected.includes(name)),
    preset: preset.map(row => ({...row, found: detected.includes(row.agent)})),
    installable: Object.fromEntries(names.map(name => [name, {installable: name !== 'hermes',
      reason: '需手动安装', docs: 'https://hermes-agent.nousresearch.com/docs/getting-started/installation/'}])),
    catalog: names.map(name => ({id: name, title: name, vendor: 'fixture', description: 'fixture agent',
      roles: [], integrated: true, found: detected.includes(name), version: '<img onerror=evil()>1.0',
      installable: name !== 'hermes', docs: 'https://hermes-agent.nousresearch.com/docs/getting-started/installation/'}))
  };
  else if (url === '/api/login') { denied = false; result = {role: 'admin', session: 'fixture-session'}; }
  else if (url === '/api/agents/install') {
    // 会话在 httpOnly cookie 里（前端拿不到 token）；状态变更必须带 CSRF 头。
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['X-Fusion-Request'], '1');
    assert.equal(options.headers.Authorization, undefined);
    assert.deepEqual(JSON.parse(options.body), {agent: 'codex'});
    result = {job: 'a'.repeat(32)};
  } else if (url.startsWith('/api/agents/install/')) {
    if (++polls === 2) detected.push('codex');
    result = {status: polls === 2 ? 'succeeded' : 'running', tail: '<script>evil()</script>'};
  } else if (url === '/api/onboarding/single') {
    posted.push(JSON.parse(options.body));
    result = {bindings: Object.fromEntries(preset.map(row => [row.role, {agent: 'pi', model: 'deepseek-flash'}])), missing: []};
  } else if (url === '/api/onboarding/multi') {
    result = {bindings: {zhongshu: {agent: 'codex', model: 'gpt-6.1-sol'}}, missing: ['taizi', 'menxia', 'shangshu']};
  } else if (url === '/api/onboarding/complete') result = {mode: 'single', completed_at: 'fixture'};
  else throw Error('unexpected URL ' + url);
  return {ok: true, json: async () => result};
};
const flush = () => new Promise(resolve => setImmediate(resolve));
const controller = require('../web/onboarding.js');
(async () => {
  assert.equal(calls.length, 0); // Page one only chooses a mode.
  $('ob-single').onclick(); await flush();
  let radios = $('ob-config').all('input');
  assert.equal(radios.length, 2);
  assert.equal(radios.find(radio => radio.checked).value, 'hermes');
  assert.equal($('ob-start').disabled, false);
  radios.find(radio => radio.value === 'pi').onchange();
  await $('ob-start').onclick();
  assert.deepEqual(posted, [{agent: 'pi'}]); // User overrides the recommendation.
  assert.equal($('ob-step3').hidden, false);
  assert.equal($('ob-summary').all('p').length, 6);
  assert.equal($('ob-config').all('img').length, 0); // Version is plain text.
  for (const expected of ['wujin', 'qingci', 'paper']) {
    $('ob-theme').onclick(); assert.equal(document.documentElement.dataset.theme, expected);
  }
  controller.bootOnboarding(); detected = []; denied = true;
  $('ob-multi').onclick(); await flush();
  assert.equal($('ob-login').hidden, false);
  assert.equal($('ob-start').disabled, true);
  $('ob-username').value = 'admin'; $('ob-password').value = 'fixture-password';
  await $('ob-login').onsubmit({preventDefault(){}});
  assert.equal($('ob-password').value, '');
  assert.equal($('ob-login').hidden, true);
  assert.match($('ob-config').all('p')[0].textContent, /未探测到/);
  assert.equal($('ob-config').all('tbody')[0].children.length, 5);
  assert.equal($('ob-catalog').all('a').length, 1); // Hermes manual-only.
  const codex = $('ob-catalog').all('article').find(card => card.all('strong')[0]?.textContent === 'codex');
  await codex.all('button')[0].onclick();
  assert.equal(polls, 1); assert.equal($('ob-start').disabled, true);
  assert.equal(timers.length, 1);
  await timers.shift()();
  assert.equal(polls, 2); assert.equal($('ob-start').disabled, false);
  assert.equal($('ob-install-log').textContent, '<script>evil()</script>');
  assert.equal($('ob-install-log').children.length, 0);
  await $('ob-start').onclick();
  assert.match($('ob-summary').all('p').at(-1).textContent, /尚未绑定：太子、门下、尚书/);
  assert.equal($('ob-step3').hidden, false);
  events.pagehide(); assert.equal(timers.length, 0);
})().catch(error => {console.error(error); process.exitCode = 1;});
