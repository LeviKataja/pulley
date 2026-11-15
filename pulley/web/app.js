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
const toggled = new Map(); // "project/part" -> whether the person left that part open

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

const stamp = sec => new Date(sec * 1000).toLocaleString(undefined,
  {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'});
const took = sec => (sec < 90 ? `${Math.round(sec)} s` : `${Math.round(sec / 60)} min`);

function every(sec) {
  const [n, word] = sec % 86400 === 0 ? [sec / 86400, 'day'] : sec % 3600 === 0 ? [sec / 3600, 'hour']
    : [Math.round(sec / 60), 'minute'];
  return n === 1 ? `every ${word}` : `every ${n} ${word}s`;
}
const changes = n => `${n} ${n === 1 ? 'change' : 'changes'}`;
const called = c => c.tag || c.short;

function pill(p) {
  if (p.job) return ['busy', p.job.kind === 'back' ? 'Going back…' : 'Updating…'];
  switch (p.state) {
    case 'ok': return ['ok', 'Up to date'];
    case 'waiting': return ['new', `${changes(p.behind)} waiting`];
    case 'blocked': return ['warn', `${changes(p.behind)} waiting · cannot update`];
    case 'trouble': return ['warn', 'Needs a look'];
    default: return ['idle', 'Not checked yet'];
  }
}

const RESULT = {
  'done': ['ok', 'Done'],
  'failed': ['warn', 'Not done'],
  'put back': ['warn', 'Put back'],
  'stuck': ['bad', 'Needs a look by hand'],
  'interrupted': ['warn', 'Interrupted'],
};

// --- pieces of a project ---------------------------------------------------------

function commit(c) {
  return [h('code', {class: 'sha', text: called(c)}), ' ', h('span', {text: c.subject}), ' ',
    h('span', {class: 'when', text: ago(c.date)})];
}

function facts(p) {
  const rows = [];
  if (p.current) rows.push(['Running', commit(p.current)]);
  if (p.container) {
    const c = p.container;
    const bits = [c.status || 'unknown', c.health, c.status === 'running' && c.started ? `up since ${ago(c.started)}` : ''];
    rows.push(['Container', bits.filter(Boolean).join(' · ')]);
  }
  if (p.follows) rows.push(['Follows', p.follows]);
  rows.push(['Folder', h('span', {class: 'path', text: p.path})]);
  return h('dl', {class: 'facts'}, rows.map(([k, v]) => [h('dt', {text: k}), h('dd', null, v)]));
}

// A <details> that remembers whether the person opened or closed it.
function fold(key, startsOpen, summary, ...body) {
  const open = toggled.has(key) ? toggled.get(key) : startsOpen;
  const el = h('details', {class: 'fold', open}, h('summary', {'data-key': key, text: summary}), body);
  el.addEventListener('toggle', () => toggled.set(key, el.open));
  return el;
}

function waiting(p) {
  const more = p.behind - p.changes.length;
  return fold(`${p.name}/waiting`, p.behind <= 8, `What is waiting (${p.behind})`,
    h('ul', {class: 'changes'}, p.changes.map(c => h('li', null,
      h('code', {class: 'sha', text: called(c)}), h('span', {text: c.subject}),
      h('span', {class: 'when', text: ago(c.date)})))),
    more > 0 && h('p', {class: 'quiet', text: `…and ${more} more.`}));
}

function progress(p) {
  const out = h('pre', {class: 'out', tabindex: '0', 'data-stick': '1', text: p.job.tail.join('\n')});
  return h('div', {class: 'job'},
    h('ol', {class: 'steps', 'aria-live': 'polite'}, p.job.steps.map(s => h('li', {class: s.state},
      s.label, s.state === 'now' && h('span', {class: 'sr', text: ' (now)'})))),
    fold(`${p.name}/output`, true, 'What it is printing', out));
}

function actions(p) {
  const busy = !!S.busy;
  const items = [];
  if (p.state === 'waiting' && p.target) {
    items.push(h('button', {class: 'btn primary', type: 'button', disabled: busy, 'data-key': `${p.name}/update`,
      onclick: () => update(p)}, `Update to ${called(p.target)}`));
  }
  if (p.previous) {
    items.push(h('span', {class: 'kept'}, 'Earlier version kept: ', h('code', {class: 'sha', text: called(p.previous)}),
      ' ', p.previous.subject));
    items.push(h('button', {class: 'btn', type: 'button', disabled: busy, 'data-key': `${p.name}/back`,
      onclick: () => back(p)}, 'Go back to it'));
  }
  return items.length ? h('div', {class: 'actions'}, items) : null;
}

const what = e => (e.kind === 'back' ? 'Going back' : 'The update')
  + (e.from && e.to ? ` from ${called(e.from)} to ${called(e.to)}` : '');

// The last attempt, said out loud on the card when it did not end well.
function lastTime(p) {
  const e = p.history[0];
  if (!e || e.result === 'done') return null;
  const [tone, label] = RESULT[e.result] || ['warn', e.result];
  return h('div', {class: `last ${tone}`},
    h('p', null, h('strong', {text: `${label}.`}), ` ${what(e)}, ${stamp(e.at)}. `, e.note),
    h('button', {class: 'btn small', type: 'button', 'data-key': `${p.name}/last`,
      onclick: () => showLog(e, p)}, 'Output'));
}

function history(p) {
  if (!p.history.length) return null;
  return fold(`${p.name}/history`, false, `History (${p.history.length})`,
    h('ul', {class: 'hist'}, p.history.map(e => {
      const [tone, label] = RESULT[e.result] || ['idle', e.result];
      const what = (e.kind === 'back' ? 'Went back' : 'Update')
        + (e.from && e.to ? ` ${called(e.from)} → ${called(e.to)}` : '');
      return h('li', null,
        h('div', null, h('span', {class: 'when', text: stamp(e.at)}), ' ', h('strong', {text: what}), ' ',
          h('span', {class: `pill small ${tone}`, text: label}),
          e.seconds ? h('span', {class: 'when', text: ` ${took(e.seconds)}`}) : null),
        h('button', {class: 'btn small', type: 'button', 'data-key': `${p.name}/log/${e.id}`,
          onclick: () => showLog(e, p)}, 'Output'),
        e.note && h('p', {class: 'note', text: e.note}));
    })));
}

function card(p) {
  const [tone, label] = pill(p);
  return h('article', {class: 'proj'},
    h('div', {class: 'proj-head'}, h('h2', {text: p.name}), h('span', {class: `pill ${tone}`, text: label})),
    facts(p),
    p.problems.length ? h('ul', {class: 'problems'}, p.problems.map(t => h('li', {text: t}))) : null,
    p.notes.length ? h('ul', {class: 'notes'}, p.notes.map(t => h('li', {text: t}))) : null,
    p.job ? progress(p) : lastTime(p),
    !p.job && p.behind ? waiting(p) : null,
    !p.job ? actions(p) : null,
    history(p));
}

// --- the page ----------------------------------------------------------------------

function banners() {
  const items = [];
  if (offline) items.push(['bad', 'Pulley is not answering. Trying again…']);
  if (flash) items.push(['bad', flash]);
  if (S && S.config_error) {
    items.push(['warn', `The settings file has a problem, so the settings read before it are still in use. ${S.config_error}`]);
  }
  if (S && S.docker_error) items.push(['bad', `Docker cannot be reached: ${S.docker_error}`]);
  return items.map(([tone, text]) => h('p', {class: `banner ${tone}`, role: tone === 'bad' ? 'alert' : null, text}));
}

function draw(force) {
  if (!S) {
    $('#banners').replaceChildren(...banners());
    return;
  }
  const now = JSON.stringify([S, offline, flash]);
  if (!force && now === drawn && Date.now() - drawnAt < 30000) return;
  drawn = now;
  drawnAt = Date.now();

  const focus = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.key : '';
  const scrolls = new Map();
  document.querySelectorAll('pre[data-stick]').forEach(pre => {
    const name = pre.closest('.proj').querySelector('h2').textContent;
    scrolls.set(name, pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8 ? -1 : pre.scrollTop);
  });

  $('#demo').hidden = !S.demo;
  $('#checked').textContent = S.checking ? 'Checking…' : S.checked ? `Checked ${ago(S.checked)}` : 'Not checked yet';
  $('#checkBtn').disabled = S.checking;
  $('#banners').replaceChildren(...banners());
  $('#list').replaceChildren(...S.projects.map(card));
  $('#empty').hidden = S.projects.length > 0;
  $('#empty').textContent = `No projects yet. Add one to ${S.config_file}, then press Check now.`;
  $('#foot').textContent = `Pulley ${S.version}`
    + (S.check_every ? ` · looks for updates ${every(S.check_every)}` : ' · only looks when asked');

  document.querySelectorAll('pre[data-stick]').forEach(pre => {
    const at = scrolls.get(pre.closest('.proj').querySelector('h2').textContent);
    pre.scrollTop = at === undefined || at === -1 ? pre.scrollHeight : at;
  });
  if (focus) {
    const again = document.querySelector(`[data-key="${CSS.escape(focus)}"]`);
    if (again) again.focus({preventScroll: true});
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
  return new Promise(resolve => {
    dlg.addEventListener('close', () => resolve(dlg.returnValue === 'yes'), {once: true});
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
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refresh();
});
refresh();
