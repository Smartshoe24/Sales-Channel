/* Stepwhere Sales Desk: keeps the app's data on the server instead of in this browser.
   hosted.py adds this script to the top of index.html, so index.html itself is unchanged.
   The app keeps calling localStorage as before; reads and writes of its one data key
   are sent to /api/state. */
(function () {
  'use strict';
  var KEY = 'stepwhere_sales_v2';
  var cache = null, version = 0, dirty = false, saving = false, timer = null, lastBy = '';
  var redirecting = false, conflict = false;

  function request(method, url) {
    var x = new XMLHttpRequest();
    x.open(method, url, false); // synchronous, so the app sees its data on its first read
    x.send();
    return x;
  }

  try {
    var first = request('GET', '/api/state');
    if (first.status === 401) {
      redirecting = true;
      location.replace('/login');
    } else if (first.status === 200) {
      var s = JSON.parse(first.responseText);
      cache = typeof s.data === 'string' ? s.data : null;
      version = s.version || 0;
      lastBy = s.by || '';
    }
  } catch (e) {
    console.error('Could not load data from the server', e);
  }

  var rawGet = Storage.prototype.getItem;
  var rawSet = Storage.prototype.setItem;
  var rawRemove = Storage.prototype.removeItem;

  Storage.prototype.getItem = function (k) {
    if (this === window.localStorage && k === KEY) return cache;
    return rawGet.apply(this, arguments);
  };
  Storage.prototype.setItem = function (k, v) {
    if (this === window.localStorage && k === KEY) {
      cache = String(v);
      schedule();
      return;
    }
    return rawSet.apply(this, arguments);
  };
  Storage.prototype.removeItem = function (k) {
    if (this === window.localStorage && k === KEY) {
      // Never wipe the shared data by accident; the app saves an emptied copy instead.
      console.warn('Ignored removal of shared data key');
      return;
    }
    return rawRemove.apply(this, arguments);
  };

  /* ---------- saving ---------- */
  function schedule() {
    dirty = true;
    clearTimeout(timer);
    timer = setTimeout(flush, 400);
  }

  function flush() {
    if (saving || !dirty || conflict || redirecting) return;
    saving = true;
    dirty = false;
    fetch('/api/state', {
      method: 'PUT',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_version: version, data: cache })
    }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; });
    }).then(function (res) {
      saving = false;
      if (res.status === 200) {
        version = res.body.version;
        if (dirty) flush(); else banner(null);
      } else if (res.status === 409) {
        conflict = true;
        banner('Someone else saved changes first, so your last change was not saved.', 'Reload', true);
      } else if (res.status === 401) {
        location.href = '/login';
      } else {
        dirty = true;
        banner('Could not save (error ' + res.status + '). Retrying…');
        setTimeout(flush, 5000);
      }
    }).catch(function () {
      saving = false;
      dirty = true;
      banner('No connection. Your change will be saved when it returns.');
      setTimeout(flush, 5000);
    });
  }

  window.addEventListener('beforeunload', function (e) {
    if (dirty || saving) { e.preventDefault(); e.returnValue = ''; }
  });

  /* ---------- picking up other people's changes ---------- */
  function editing() {
    var a = document.activeElement;
    return !!a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName);
  }

  setInterval(function () {
    if (saving || dirty || conflict || redirecting) return;
    fetch('/api/version', { credentials: 'same-origin' }).then(function (r) {
      if (r.status === 401) { location.href = '/login'; return null; }
      return r.json();
    }).then(function (j) {
      if (!j || j.version <= version) return;
      if (!editing()) { location.reload(); return; }
      banner((j.by || 'Someone') + ' saved changes.', 'Refresh', true);
    }).catch(function () { /* offline; try again next time */ });
  }, 10000);

  /* ---------- small UI: banner and account pill ---------- */
  var bar = null;
  function ensureUi() {
    if (bar || !document.body) return;
    bar = document.createElement('div');
    bar.setAttribute('role', 'status');
    bar.style.cssText = 'position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:100000;display:none;' +
      'max-width:92vw;padding:10px 14px;border-radius:10px;background:#2a1f06;color:#ffd58a;border:1px solid #6b4d10;' +
      'font:14px/1.4 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.4);align-items:center;gap:12px';
    document.body.appendChild(bar);
  }

  function banner(text, actionLabel, reload) {
    ensureUi();
    if (!bar) return;
    if (!text) { bar.style.display = 'none'; return; }
    bar.textContent = '';
    var span = document.createElement('span');
    span.textContent = text;
    bar.appendChild(span);
    if (actionLabel) {
      var b = document.createElement('button');
      b.textContent = actionLabel;
      b.style.cssText = 'background:#f5a524;color:#1a1200;border:0;border-radius:7px;padding:6px 12px;font:inherit;font-weight:700;cursor:pointer';
      b.onclick = function () { if (reload) { dirty = false; location.reload(); } };
      bar.appendChild(b);
    }
    bar.style.display = 'flex';
  }

  function accountPill() {
    ensureUi();
    fetch('/api/me', { credentials: 'same-origin' }).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      if (!j) return;
      var pill = document.createElement('div');
      pill.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:99999;display:flex;gap:10px;align-items:center;' +
        'padding:6px 6px 6px 12px;border-radius:999px;background:rgba(21,26,35,.92);color:#aab3c2;border:1px solid #262d3a;' +
        'font:13px system-ui,sans-serif';
      var name = document.createElement('span');
      name.textContent = j.username;
      var out = document.createElement('button');
      out.textContent = 'Sign out';
      out.style.cssText = 'background:#262d3a;color:#e8ebf1;border:0;border-radius:999px;padding:5px 11px;font:inherit;cursor:pointer';
      out.onclick = function () {
        fetch('/logout', { method: 'POST', credentials: 'same-origin' }).then(function () { location.href = '/login'; });
      };
      pill.appendChild(name);
      pill.appendChild(out);
      document.body.appendChild(pill);
    });
  }

  document.addEventListener('DOMContentLoaded', accountPill);
})();
