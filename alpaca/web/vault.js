/* Alpaca sign-in page (login.html) and workspace hub (workspaces.html). The only security decision
   is the server's: this page posts the code to /auth/login and the server sets the session cookie.
   The hub lists /workspaces.json and reads each tile's live summary. */
(function () {
  'use strict';

  var root = document.documentElement;
  var THEME_KEY = root.getAttribute('data-theme-key') || 'alpaca.theme';
  var storage = null;
  try { storage = window.localStorage; } catch (_) { storage = null; }

  /* ---------- theme ---------- */
  function storedTheme() {
    try { return storage && storage.getItem(THEME_KEY); } catch (_) { return null; }
  }
  (function initTheme() {
    var t = storedTheme();
    if (t === 'light' || t === 'dark') root.setAttribute('data-theme', t);
    document.addEventListener('click', function (event) {
      if (!event.target.closest || !event.target.closest('[data-vault-theme]')) return;
      var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      try { storage && storage.setItem(THEME_KEY, next); } catch (_) { /* the page setting still applies */ }
      window.dispatchEvent(new CustomEvent('alpaca:themechange', {detail: {theme: next}}));
    });
  })();

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  /* ---------- sign-in ---------- */
  function destination() {
    if (location.pathname.indexOf('/login') === 0) {
      var next = new URLSearchParams(location.search).get('next') || '/';
      return next.charAt(0) === '/' && next.charAt(1) !== '/' ? next : '/';
    }
    return location.pathname + location.search + location.hash;   // the locked page itself
  }

  function initLogin() {
    var form = document.getElementById('login-form'), code = document.getElementById('code');
    var user = document.getElementById('user'), userField = document.getElementById('user-field');
    var card = document.getElementById('card'), error = document.getElementById('error');
    var submit = document.getElementById('submit');

    function fail(message) {
      error.textContent = message;
      error.hidden = false;
      card.classList.add('denied');
    }

    fetch('/login/info.json', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) { return r.json(); }).then(function (info) {
      if (info.signed_in) { location.replace(destination()); return; }
      if (info.user) userField.hidden = false;
      if (!info.configured) fail('No web login is configured on this server. Set .alpaca/files-auth first.');
    }).catch(function () { /* the form still works; the server answers on submit */ });

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      error.hidden = true;
      card.classList.remove('denied');
      submit.disabled = true;
      fetch('/auth/login', {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({user: user.value.trim(), code: code.value})})
        .then(function (r) { return r.json().catch(function () { return {}; }).then(function (b) { return {status: r.status, body: b}; }); })
        .then(function (res) {
          submit.disabled = false;
          if (res.status === 200 && res.body.ok) { location.replace(destination()); return; }
          code.value = '';
          fail(res.body.error || 'Sign-in failed (' + res.status + ').');
          code.focus();
        })
        .catch(function () {
          submit.disabled = false;
          fail('The server did not answer. Check the connection and try again.');
        });
    });
    code.focus();
  }

  /* ---------- workspace hub ---------- */
  function ago(iso) {
    var t = Date.parse(iso);
    if (!t) return '';
    var s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 90) return Math.round(s) + ' s ago';
    if (s < 5400) return Math.round(s / 60) + ' min ago';
    if (s < 172800) return Math.round(s / 3600) + ' h ago';
    return Math.round(s / 86400) + ' d ago';
  }

  function summarize(data) {
    var pulse = data.pulse || {}, now = pulse.now || {}, tasks = pulse.tasks || {};
    var op = (now.ops || []).filter(function (o) { return o.status === 'open'; })[0] || null;
    var active = ((now.sessions || {}).work || []).filter(function (s) { return s.active; }).length;
    return {level: now.level || '', op: op, opsOpen: pulse.ops_open || 0, open: tasks.open || 0,
      doing: tasks.doing || 0, done: tasks.done || 0, active: active, next: now.next_action || '',
      asOf: now.as_of || data.generated || ''};
  }

  function fillTile(tile, s) {
    var live = tile.querySelector('.tile-live');
    live.classList.toggle('on', s.active > 0);
    live.textContent = s.active ? s.active + ' live' : 'idle';
    var op = tile.querySelector('.tile-op');
    op.textContent = '';
    var note = tile.querySelector('.bar-note');
    if (s.op) {
      op.appendChild(el('b', null, s.op.id));
      op.appendChild(document.createTextNode(s.op.title || s.op.done_when || ''));
      var total = s.op.rows_total || s.op.tasks_total || 0;
      var done = (s.op.rows_total ? s.op.rows_done : s.op.tasks_done) || 0;
      tile.querySelector('.bar span').style.width = (total ? Math.round((100 * done) / total) : 0) + '%';
      note.textContent = total ? done + ' of ' + total + (s.op.rows_total ? ' rows discharged' : ' tasks done') : 'no rows or tasks yet';
    } else {
      op.textContent = 'No open operation.';
      note.textContent = '';
    }
    var stats = tile.querySelectorAll('.stat b');
    stats[0].textContent = s.opsOpen; stats[1].textContent = s.doing; stats[2].textContent = s.open; stats[3].textContent = s.done;
    tile.querySelector('.tile-next').textContent = 'next: ' + (s.next || 'nothing queued');
    tile.querySelector('.tile-open small').textContent = (s.level ? 'level ' + s.level + ', ' : '') + (s.asOf ? 'updated ' + ago(s.asOf) : '');
  }

  function buildTile(ws) {
    var a = el('a', 'card tile');
    a.href = ws.href;
    var kind = ws.kind || 'Operations workspace';
    a.setAttribute('aria-label', 'Open the ' + ws.name + ' ' + kind.toLowerCase());
    var top = el('div', 'tile-top');
    top.appendChild(el('span', 'tile-mark', ws.name.charAt(0)));
    var names = el('div');
    names.appendChild(el('div', 'tile-name', ws.name));
    names.appendChild(el('div', 'tile-kind', kind + ', ' + (ws.project || ws.id || '')));
    top.appendChild(names);
    top.appendChild(el('span', 'tile-live', 'checking'));
    a.appendChild(top);
    a.appendChild(el('p', 'tile-op', 'Reading the record'));
    var bar = el('div', 'bar'); bar.appendChild(el('span')); a.appendChild(bar);
    a.appendChild(el('div', 'bar-note'));
    var stats = el('div', 'stats');
    ['Open ops', 'Doing', 'Queued', 'Done'].forEach(function (label) {
      var s = el('div', 'stat'); s.appendChild(el('b', null, '-')); s.appendChild(el('span', null, label)); stats.appendChild(s);
    });
    a.appendChild(stats);
    a.appendChild(el('div', 'tile-next'));
    var open = el('div', 'tile-open'); open.appendChild(el('span', null, 'Open workspace')); open.appendChild(el('small'));
    a.appendChild(open);
    return a;
  }

  function refreshTile(tile, ws) {
    var op = tile.querySelector('.tile-op');
    if (!ws.summary) {
      op.textContent = 'No live summary for this workspace. Open it to see its cockpit.';
      return Promise.resolve();
    }
    return fetch(ws.summary, {credentials: 'same-origin', cache: 'no-cache'}).then(function (r) {
      // A 401 is this hub's own sign-in lapsing. Another instance's tile is reachability only.
      if (r.status === 401) { location.replace('/login?next=' + encodeURIComponent(location.pathname)); throw Error('signed out'); }
      if (!r.ok) throw Error('status ' + r.status);
      return r.json();
    }).then(function (data) {
      if (ws.self !== false) { fillTile(tile, summarize(data)); return; }
      var live = tile.querySelector('.tile-live'), up = !!(data && data.reachable);
      live.classList.toggle('on', up);
      live.textContent = up ? 'up' : 'down';
      op.textContent = up ? 'Workspace is up. Open it and sign in there to see its cockpit.'
                          : 'Workspace not answering. It may be stopped.';
    }).catch(function (err) {
      if (err.message === 'signed out') return;
      op.textContent = 'Live summary unavailable (' + err.message + '). The workspace may still open.';
    });
  }

  function initHub() {
    var grid = document.getElementById('grid');
    document.getElementById('signout').addEventListener('click', function () {
      fetch('/auth/logout', {method: 'POST', credentials: 'same-origin'}).finally(function () { location.replace('/login'); });
    });
    fetch('/workspaces.json', {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
      if (r.status === 401) { location.replace('/login'); throw Error('signed out'); }
      return r.json();
    }).then(function (body) {
      grid.textContent = '';
      var list = body.workspaces || [];
      document.getElementById('count').textContent = list.length + (list.length === 1 ? ' workspace' : ' workspaces');
      var tiles = list.map(function (ws) { var t = buildTile(ws); grid.appendChild(t); return {tile: t, ws: ws}; });
      if (list.length < 2) {
        var ghost = el('div', 'tile-ghost');
        ghost.appendChild(el('b', null, 'More workspaces'));
        ghost.appendChild(el('p', null, 'Each instance registered on this host gets its own tile here. Register another with ' +
          '`alpaca workspace add` (docs/workspaces.md).'));
        grid.appendChild(ghost);
      }
      function all() { tiles.forEach(function (x) { refreshTile(x.tile, x.ws); }); }
      all();
      setInterval(function () { if (!document.hidden) all(); }, 15000);
    }).catch(function (err) {
      if (err.message !== 'signed out') grid.textContent = 'Could not load the workspace list: ' + err.message;
    });
  }

  function boot() {
    var page = document.body.getAttribute('data-page');
    if (page === 'login') initLogin();
    else if (page === 'hub') initHub();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once: true});
  else boot();
})();
