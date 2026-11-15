'use strict';

const $ = (sel, el = document) => el.querySelector(sel);

// Build an element: h('a', {href: '/', onclick: fn, text: 'Home'}, child, [more, children]).
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat(3)) {
    if (kid !== null && kid !== undefined && kid !== false) el.append(kid);
  }
  return el;
}

let S = null;              // the last state the server gave
let timer = 0;
let drawn = '';            // what was last drawn, to skip redrawing the same thing
let drawnAt = 0;
let offline = false;
let flash = '';            // one message from a request that was refused
let picked = decodeURIComponent(location.hash.slice(1));  // the project shown on the right
const tails = new Map();   // attempt id -> the end of its output, once fetched

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Pulley': '1'},
    body: JSON.stringify(body),
  };
  const reply = await fetch(path, opts);
  if (path.startsWith('/api/log') && reply.ok) return reply.text();
  const data = await reply.json().catch(() => ({}));
  if (!reply.ok) throw new Error(data.error || `The request failed (${reply.status}).`);
  return data;
}

// --- words ---------------------------------------------------------------------

const rel = new Intl.RelativeTimeFormat('en', {numeric: 'auto'});

function ago(when) {
  const t = typeof when === 'number' ? when * 1000 : Date.parse(when);
  if (!t) return '';
  const s = (t - Date.now()) / 1000, a = Math.abs(s);
  if (a < 45) return 'just now';
  if (a < 3600) return rel.format(Math.round(s / 60), 'minute');
  if (a < 86400) return rel.format(Math.round(s / 3600), 'hour');
  if (a < 86400 * 30) return rel.format(Math.round(s / 86400), 'day');
  if (a < 86400 * 365) return rel.format(Math.round(s / 86400 / 30), 'month');
  return rel.format(Math.round(s / 86400 / 365), 'year');
}

// A length of time: "40 s", "12 min", "5 h 56 min", "8 days".
function lasting(sec) {
  sec = Math.max(0, sec);
  if (sec < 90) return `${Math.round(sec)} s`;
  const min = sec / 60;
  if (min < 90) return `${Math.round(min)} min`;
  const hours = min / 60;
  if (hours < 10) {
    const whole = Math.floor(hours), rest = Math.round((hours - whole) * 60);
    return rest ? `${whole} h ${rest} min` : `${whole} h`;
  }
  if (hours < 36) return `${Math.round(hours)} h`;
  const days = hours / 24;
  return days < 60 ? `${Math.round(days)} days` : `${Math.round(days / 30)} months`;
}

const stamp = sec => new Date(sec * 1000).toLocaleString(undefined,
  {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
const changes = n => `${n} ${n === 1 ? 'change' : 'changes'}`;
const called = c => c.tag || c.short;

function every(sec) {
  const [n, word] = sec % 86400 === 0 ? [sec / 86400, 'day'] : sec % 3600 === 0 ? [sec / 3600, 'hour']
    : [Math.round(sec / 60), 'minute'];
  return n === 1 ? `every ${word}` : `every ${n} ${word}s`;
}

const STEP = {code: 'code', build: 'building', swap: 'starting', health: 'waiting', restore: 'putting back'};
const RESULT = {
  'done': ['ok', 'done'],
  'failed': ['warn', 'not done'],
  'put back': ['warn', 'put back'],
  'stuck': ['stop', 'needs a look'],
  'interrupted': ['warn', 'interrupted'],
};
const LAST = {'done': 'Updated', 'failed': 'Not done', 'put back': 'Put back', 'stuck': 'Needs a look',
  'interrupted': 'Interrupted'};

// Whether the last attempt still deserves saying out loud.
function unsettled(p) {
  const e = p.history[0];
  if (!e || p.job) return null;
  if (e.result === 'put back' || e.result === 'interrupted' || e.result === 'stuck') return e;
  return e.result === 'failed' && p.behind && !p.problems.length ? e : null;
}

// How a project shows on the board: its colour, the big figure, the word under it.
function face(p) {
  if (p.job) {
    const steps = p.job.steps, at = steps.findIndex(s => s.state === 'now');
    return {key: 'busy', tone: 'new', big: `${at < 0 ? steps.length : at + 1}/${steps.length}`,
      word: at < 0 ? 'finishing' : STEP[steps[at].key] || 'working'};
  }
  const e = unsettled(p);
  if (p.state === 'unknown') return {key: 'unknown', tone: 'idle', big: '–', word: 'not checked'};
  if (e && e.result === 'stuck') return {key: 'trouble', tone: 'stop', big: '!', word: 'needs a look'};
  if (p.state === 'blocked') return {key: 'blocked', tone: 'stop', big: String(p.behind), word: 'blocked'};
  if (p.state === 'trouble') return {key: 'trouble', tone: 'stop', big: '!', word: 'needs a look'};
  if (e) return {key: 'unsettled', tone: 'warn', big: p.behind ? String(p.behind) : '!', word: RESULT[e.result][1]};
  if (p.state === 'waiting') return {key: 'waiting', tone: 'new', big: String(p.behind), word: 'waiting'};
  return {key: 'ok', tone: 'ok', big: 'OK', word: 'up to date'};
}

const TALLY = [['waiting', 'new', 'waiting'], ['busy', 'new', 'updating'], ['unsettled', 'warn', 'put back'],
  ['blocked', 'stop', 'blocked'], ['trouble', 'stop', 'need a look'], ['ok', 'ok', 'up to date'],
  ['unknown', 'idle', 'not checked']];

// --- the board ---------------------------------------------------------------------

function cell(label, main, sub, mono) {
  return h('span', {'data-l': label}, h('span', {class: mono ? 'mono' : null, text: main}), ' ',
    h('span', {class: 'sub', text: sub}));
}

function row(p) {
  const f = face(p), last = p.history[0], c = p.container;
  const up = c && c.status === 'running' && Date.parse(c.started)
    ? `up ${lasting((Date.now() - Date.parse(c.started)) / 1000)}` : '';
  return h('button', {class: 'row', type: 'button', 'aria-pressed': p.name === picked ? 'true' : 'false',
    'data-key': `row/${p.name}`, onclick: () => pick(p.name)},
  h('span', {class: `st ${f.tone}`}, h('b', {text: f.big}), h('i', {text: f.word})),
  h('span', {class: 'nm', text: p.name}),
  cell('Running', p.current ? called(p.current) : '–', p.current ? ago(p.current.date) : '', !!p.current),
  cell('Available', p.target ? called(p.target) : p.state === 'unknown' ? '–' : 'nothing newer', p.follows, !!p.target),
  cell('Container', c ? [c.status || 'unknown', c.health].filter(Boolean).join(' · ') : '–', up),
  cell('Last update', last ? (last.kind === 'back' && last.result === 'done' ? 'Went back' : LAST[last.result] || last.result)
    : 'Never here', last ? stamp(last.at) : ''),
  h('span', {class: 'open', 'aria-hidden': 'true', text: '\u203a'}));
}

function pick(name) {
  picked = name;
  history.replaceState(null, '', `#${encodeURIComponent(name)}`);
  draw(true);
}

// --- the selected project -----------------------------------------------------------

const sec = (title, ...body) => h('div', {class: 'sec'}, h('span', {class: 'label', text: title}), body);

// Output as it was printed, with Pulley's own "== step" lines set back a little.
function output(text, extra) {
  const pre = h('pre', {class: 'out', tabindex: '0', ...extra});
  text.split('\n').forEach((line, i, all) => {
    pre.append(line.startsWith('== ') ? h('i', {text: line}) : line, i < all.length - 1 ? '\n' : '');
  });
  return pre;
}

// The part of a finished attempt's output worth showing without being asked.
function tailOf(text) {
  const lines = text.split('\n');
  const at = lines.lastIndexOf('-- the last lines it printed:');
  if (at >= 0) {
    const said = [];
    for (const line of lines.slice(at + 1)) {
      if (!line.startsWith('   ')) break;
      said.push(line.slice(3));
    }
    if (said.length) return {title: 'The last lines it printed', text: said.join('\n')};
  }
  const rest = lines.filter(line => line.trim() && !line.startsWith('== '));
  return rest.length ? {title: 'The end of its output', text: rest.slice(-12).join('\n')} : null;
}

function tailFor(entry) {
  if (!tails.has(entry.id)) {
    tails.set(entry.id, null);
    api(`/api/log?id=${encodeURIComponent(entry.id)}`).then(text => {
      tails.set(entry.id, tailOf(text));
      draw(true);
    }).catch(() => {});
  }
  return tails.get(entry.id);
}

const move = e => (e.from && e.to ? `${called(e.from)} → ${called(e.to)}` : '');

function detail(p) {
  if (!p) return [];
  const f = face(p), busy = !!S.busy, c = p.container;
  const chip = f.key === 'ok' || f.big === '!' || f.big === '–' ? f.word : `${f.big} ${f.word}`;
  const parts = [h('div', {class: 'd-head'}, h('h2', {text: p.name}),
    p.job ? h('span', {class: 'chip new'}, p.job.kind === 'back' ? 'going back' : ['updating',
      p.target && [' to ', h('span', {class: 'keep', text: called(p.target)})]])
      : h('span', {class: `chip ${f.tone}`, text: chip}))];

  if (p.job) {
    parts.push(h('ol', {class: 'track', 'aria-live': 'polite'}, p.job.steps.map((s, i) =>
      h('li', {class: s.state}, h('b', {text: String(i + 1)}), s.label))));
    parts.push(sec('What it is printing', output(p.job.tail.join('\n'), {'data-stick': '1'})));
    parts.push(h('p', {class: 'nothing', text: p.job.kind === 'back' ? 'The other buttons wait until this is finished.'
      : 'The old version keeps running until the new image is built. The other buttons wait until this is finished.'}));
    return parts;
  }

  if (p.problems.length) {
    parts.push(h('div', {class: 'alerts'}, p.problems.map(t => h('p', {class: 'alert stop', text: t}))));
  }
  const e = unsettled(p);
  if (e) {
    const what = `${e.kind === 'back' ? 'Going back' : 'The update'}${move(e) ? ` ${move(e)}` : ''}, ${stamp(e.at)}.`;
    parts.push(h('p', {class: `alert ${e.result === 'stuck' ? 'stop' : 'warn'}`, text: `${what} ${e.note}`}));
    const tail = tailFor(e);
    if (tail) parts.push(sec(tail.title, output(tail.text)));
  }

  const facts = [];
  if (p.current) facts.push(['Running', [h('span', {class: 'mono', text: called(p.current)}), ' ', p.current.subject]]);
  if (p.follows) facts.push(['Follows', p.follows]);
  facts.push(['Folder', h('span', {class: 'mono', text: p.path})]);
  if (c) {
    facts.push(['Container', [c.status || 'unknown', c.health,
      c.status === 'running' && Date.parse(c.started) ? `up since ${ago(c.started)}` : ''].filter(Boolean).join(', ')]);
  }
  parts.push(h('dl', {class: 'pairs'}, facts.map(([k, v]) => [h('dt', {class: 'label', text: k}), h('dd', null, v)])));
  if (p.notes.length) parts.push(h('ul', {class: 'notes'}, p.notes.map(t => h('li', {text: t}))));

  if (p.behind) {
    const more = p.behind - p.changes.length;
    parts.push(sec(`What is waiting (${p.behind})`, h('ul', {class: 'changes'}, p.changes.map(ch => h('li', null,
      h('span', {class: 'mono', text: called(ch)}), h('span', {text: ch.subject}),
      h('span', {class: 'when', text: ago(ch.date)})))), more > 0 && h('p', {class: 'nothing', text: `…and ${more} more.`})));
  }

  const buttons = [];
  if (p.state === 'waiting' && p.target) {
    buttons.push(h('button', {class: 'btn go', type: 'button', disabled: busy, 'data-key': `${p.name}/update`,
      onclick: () => update(p)}, e ? 'Try ' : 'Update to ', h('span', {class: 'keep', text: called(p.target)}), e ? ' again' : ''));
  }
  if (e) {
    buttons.push(h('button', {class: 'btn', type: 'button', 'data-key': `${p.name}/last`, onclick: () => showLog(e, p)},
      'Full output'));
  }
  if (p.previous) {
    buttons.push(h('button', {class: 'btn', type: 'button', disabled: busy, 'data-key': `${p.name}/back`,
      onclick: () => back(p)}, 'Go back to ', h('span', {class: 'keep', text: called(p.previous)})));
  }
  if (buttons.length) {
    parts.push(h('div', {class: 'act'}, buttons, p.state === 'waiting' && !e
      && h('small', {text: 'Rebuilt and restarted. Put back if it does not come up.'})));
  }

  parts.push(sec('History', p.history.length ? h('ul', {class: 'hist'}, p.history.map(entry => {
    const [tone, word] = RESULT[entry.result] || ['idle', entry.result];
    return h('li', null,
      h('span', {class: 'when', text: stamp(entry.at)}),
      h('span', {class: 'mono', text: move(entry) || (entry.kind === 'back' ? 'going back' : 'update')}),
      h('span', {class: `res ${tone}`, text: entry.kind === 'back' && entry.result === 'done' ? 'went back' : word}),
      h('span', {class: 'when took', text: entry.seconds ? lasting(entry.seconds) : ''}),
      h('button', {class: 'btn small', type: 'button', 'data-key': `${p.name}/log/${entry.id}`,
        onclick: () => showLog(entry, p)}, 'Output'),
      entry.note && entry !== e && h('span', {class: 'why', text: entry.note}));
  })) : h('p', {class: 'nothing', text: 'Nothing has been updated from here yet.'})));
  return parts;
}

// --- the page ----------------------------------------------------------------------

function banners() {
  const items = [];
  if (offline) items.push(['stop', 'Pulley is not answering. Trying again…']);
  if (flash) items.push(['stop', flash]);
  if (S && S.config_error) {
    items.push(['warn', `The settings file has a problem, so the settings read before it are still in use. ${S.config_error}`]);
  }
  if (S && S.docker_error) items.push(['stop', `Docker cannot be reached: ${S.docker_error}`]);
  return items.map(([tone, text]) => h('p', {class: `banner ${tone}`, role: tone === 'stop' ? 'alert' : null, text}));
}

function draw(force) {
  if (!S) {
    $('#banners').replaceChildren(...banners());
    return;
  }
  const projects = S.projects;
  if (!projects.some(p => p.name === picked)) {
    const first = projects.find(p => p.job) || projects.find(p => face(p).key !== 'ok') || projects[0];
    picked = first ? first.name : '';
  }
  const now = JSON.stringify([S, offline, flash, picked, [...tails]]);
  if (!force && now === drawn && Date.now() - drawnAt < 30000) return;
  drawn = now;
  drawnAt = Date.now();

  const focus = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : '';
  const stick = $('pre[data-stick]');
  const scroll = stick && stick.scrollTop + stick.clientHeight < stick.scrollHeight - 8 ? stick.scrollTop : -1;

  const counts = {};
  projects.forEach(p => { counts[face(p).key] = (counts[face(p).key] || 0) + 1; });
  $('#tally').replaceChildren(...TALLY.filter(([key]) => counts[key]).map(([key, tone, word]) =>
    h('li', null, h('b', {class: tone, text: String(counts[key])}), word)));
  $('#demo').hidden = !S.demo;
  const next = S.check_every && S.checked ? S.checked + S.check_every - Date.now() / 1000 : 0;
  $('#facts').replaceChildren(
    h('span', null, 'Docker ', h('b', {class: S.docker_error ? 'bad' : null, text: S.docker_error ? 'not answering' : 'answering'})),
    h('span', null, S.checking ? h('b', {text: 'Checking…'}) : ['Checked ', h('b', {text: !S.checked ? 'not yet'
      : Date.now() / 1000 - S.checked < 45 ? 'just now' : `${lasting(Date.now() / 1000 - S.checked)} ago`})]),
    next > 0 && h('span', null, 'Next look ', h('b', {text: `in ${lasting(next)}`})));
  $('#checkBtn').disabled = S.checking;
  $('#banners').replaceChildren(...banners());
  $('#rows').replaceChildren(...projects.map(row));
  $('#empty').hidden = projects.length > 0;
  $('#empty').textContent = `No projects yet. Add one to ${S.config_file}, then press Check now.`;
  $('#detail').replaceChildren(...detail(projects.find(p => p.name === picked)));
  $('#detail').hidden = !projects.length;
  $('#foot').replaceChildren(
    h('span', {text: `Pulley ${S.version}`}),
    h('span', {text: S.check_every ? `Looks ${every(S.check_every)}` : 'Only looks when asked'}),
    h('span', null, 'Settings ', h('span', {class: 'keep', text: S.config_file})));

  const again = $('pre[data-stick]');
  if (again) again.scrollTop = scroll === -1 ? again.scrollHeight : scroll;
  if (focus) {
    const back = document.querySelector(`[data-key="${CSS.escape(focus)}"]`);
    if (back) back.focus({preventScroll: true});
  }
}

async function refresh() {
  clearTimeout(timer);
  try {
    S = await api('/api/state');
    offline = false;
  } catch (e) {
    offline = true;
  }
  draw();
  timer = setTimeout(refresh, S && (S.busy || S.checking) ? 1500 : 15000);
}

function ask(title, text, yes) {
  const dlg = $('#askDlg');
  $('#askTitle').textContent = title;
  $('#askText').textContent = text;
  $('#askYes').textContent = yes;
  dlg.returnValue = '';
  dlg.showModal();
  // The answer is taken from the button press itself. The dialog's own "close" event arrives only
  // once the browser next draws the page, which a tab in the background may not do for a long time.
  const form = $('form', dlg);
  return new Promise(resolve => {
    const done = answer => {
      form.removeEventListener('submit', pressed);
      dlg.removeEventListener('cancel', escaped);
      dlg.removeEventListener('close', closed);
      resolve(answer);
    };
    const pressed = e => done(!!e.submitter && e.submitter.value === 'yes');
    const escaped = () => done(false);
    const closed = () => done(dlg.returnValue === 'yes');
    form.addEventListener('submit', pressed);
    dlg.addEventListener('cancel', escaped);
    dlg.addEventListener('close', closed);
  });
}

async function send(path, body) {
  flash = '';
  try {
    await api(path, body);
  } catch (e) {
    flash = e.message;
  }
  await refresh();
  draw(true);
}

async function update(p) {
  const yes = await ask(`Update ${p.name}?`,
    `It is rebuilt with ${changes(p.behind)} and restarted, so it is away for a moment. `
    + 'If the new version does not come up, the one running now is put back.', 'Update');
  if (yes) await send('/api/update', {name: p.name, expect: p.target.sha});
}

async function back(p) {
  const yes = await ask(`Take ${p.name} back?`,
    `It is restarted on the earlier version, ${called(p.previous)}, so it is away for a moment. `
    + 'Its data is not taken back: only the program is.', 'Go back');
  if (yes) await send('/api/back', {name: p.name});
}

async function showLog(entry, p) {
  $('#logTitle').textContent = `${p.name}, ${stamp(entry.at)}`;
  $('#logText').textContent = 'Loading…';
  $('#logDlg').showModal();
  try {
    $('#logText').textContent = (await api(`/api/log?id=${encodeURIComponent(entry.id)}`)) || 'Nothing was printed.';
  } catch (e) {
    $('#logText').textContent = e.message;
  }
}

$('#checkBtn').addEventListener('click', () => send('/api/check', {}));
window.addEventListener('hashchange', () => {
  picked = decodeURIComponent(location.hash.slice(1));
  draw(true);
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});
refresh();
