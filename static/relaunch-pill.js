/* A quiet, honest "a new version is ready" notice, shared by the dashboard and
   branded demos.

   Five things it must get right (and does):
   1. It knows the build it loaded (window.__CC_LOADED_REV, stamped server side)
      and learns a newer one is deployed by comparing it to /healthz "rev".
   2. When a newer build exists it shows a quiet notice in plain words, with
      what changed, and an offer to load it. No version number, no hash.
   3. Loading it is one click, and it never restarts under someone: if a form is
      half filled, it says so and lets them finish instead of reloading.
   4. It is honest when there is nothing to say: nothing shows when the build has
      not changed or is unknown, and "Not now" hides it for a while rather than
      nagging.
   5. What changed comes from the real changelog (/api/whats-new). When that
      cannot be reached, it shows the fact that it is newer and the date, never
      an invented changelog. */
(function () {
  'use strict';
  var loadedRev = String(window.__CC_LOADED_REV || '').trim();
  var dismissKey = 'cc-relaunch-dismissed-until';
  var panel = null;

  // Never restart under someone. Any typing marks the page dirty; a submit or
  // reset (or an explicit cc:saved event) clears it. Erring toward "dirty" is
  // deliberate: at worst the owner clicks Update a second time after saving.
  var dirty = false;
  function markDirty() { dirty = true; }
  function markClean() { dirty = false; }
  document.addEventListener('input', markDirty, true);
  document.addEventListener('change', markDirty, true);
  document.addEventListener('submit', markClean, true);
  document.addEventListener('reset', markClean, true);
  document.addEventListener('cc:saved', markClean, true);

  function dismissed() {
    try { return Number(sessionStorage.getItem(dismissKey) || 0) > Date.now(); }
    catch (e) { return false; }
  }
  function dismiss() {
    try { sessionStorage.setItem(dismissKey, String(Date.now() + 30 * 60 * 1000)); }
    catch (e) { /* private mode: just hide for this view */ }
    if (panel) { panel.remove(); panel = null; }
  }

  function fmtDate(iso) {
    var d = iso ? new Date(iso) : new Date();
    if (isNaN(d.getTime())) return '';
    try { return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
    catch (e) { return d.toISOString().slice(0, 10); }
  }

  // Build the "what changed" lines from the real changelog, or an honest
  // date-only fallback when it cannot be produced.
  function changeLines(info) {
    var lines = [];
    var entries = (info && info.entries) || [];
    for (var i = 0; i < entries.length && lines.length < 3; i++) {
      var t = (entries[i] && entries[i].title || '').trim();
      if (t) lines.push(t);
    }
    if (lines.length) return { kind: 'list', items: lines };
    var when = fmtDate(info && info.checked_at);
    return { kind: 'date', text: when ? ('Ready as of ' + when + '.') : 'It is ready to load.' };
  }

  function render(info) {
    if (panel || dismissed()) return;
    panel = document.createElement('div');
    panel.className = 'cc-relaunch-panel';

    var change = changeLines(info);
    var body = '';
    if (change.kind === 'list') {
      body = '<ul class="cc-relaunch-list">';
      for (var i = 0; i < change.items.length; i++) {
        body += '<li></li>';
      }
      body += '</ul>';
    } else {
      body = '<p class="cc-relaunch-sub"></p>';
    }

    panel.innerHTML =
      '<div class="cc-relaunch-head">A new version is ready</div>' +
      body +
      '<p class="cc-relaunch-note" hidden>Finish what you are typing first, then update. Nothing you have entered will be lost.</p>' +
      '<div class="cc-relaunch-actions">' +
      '<button type="button" class="cc-relaunch-update">Update now</button>' +
      '<button type="button" class="cc-relaunch-dismiss">Not now</button>' +
      '</div>';

    // Fill text via textContent so a changelog title can never inject markup.
    if (change.kind === 'list') {
      var lis = panel.querySelectorAll('.cc-relaunch-list li');
      for (var j = 0; j < lis.length; j++) lis[j].textContent = change.items[j];
    } else {
      panel.querySelector('.cc-relaunch-sub').textContent = change.text;
    }

    var note = panel.querySelector('.cc-relaunch-note');
    panel.querySelector('.cc-relaunch-update').addEventListener('click', function () {
      if (dirty) { note.hidden = false; return; }   // never restart under someone
      var url = new URL(window.location.href);
      url.searchParams.set('_v', String(Date.now()));  // bust the cache
      window.location.replace(url.toString());
    });
    panel.querySelector('.cc-relaunch-dismiss').addEventListener('click', dismiss);
    document.body.appendChild(panel);
  }

  function announce() {
    // A newer build is live. Ask the changelog what changed, then show the
    // notice either way (a failed changelog just means a date-only notice).
    fetch('/api/whats-new?since=' + encodeURIComponent(loadedRev), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (info) { render(info || {}); })
      .catch(function () { render({}); });
  }

  function check() {
    if (!loadedRev || loadedRev === 'unknown' || panel || dismissed()) return;
    fetch('/healthz', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (health) {
        if (health && health.rev && health.rev !== loadedRev) announce();
      })
      .catch(function () { /* offline: say nothing, try again next tick */ });
  }

  var style = document.createElement('style');
  style.textContent =
    '.cc-relaunch-panel{position:fixed;top:72px;right:18px;z-index:1000;max-width:280px;' +
    'padding:12px 14px;border:1px solid rgba(var(--brand-rgb,212,175,55),.48);border-radius:14px;' +
    'background:rgba(17,16,13,.96);box-shadow:0 8px 24px rgba(0,0,0,.28);' +
    'font:400 12px/1.4 Inter,system-ui,sans-serif;color:var(--brand-light,#e6cf88)}' +
    '.cc-relaunch-head{font-weight:600;font-size:13px;margin-bottom:6px}' +
    '.cc-relaunch-sub{margin:0 0 10px;opacity:.85}' +
    '.cc-relaunch-list{margin:0 0 10px;padding-left:16px}.cc-relaunch-list li{margin:2px 0;opacity:.85}' +
    '.cc-relaunch-note{margin:0 0 10px;color:#e8c98a;opacity:.95}' +
    '.cc-relaunch-actions{display:flex;gap:8px}' +
    '.cc-relaunch-update{flex:1;height:30px;border:0;border-radius:8px;cursor:pointer;font:600 12px Inter,system-ui;' +
    'background:var(--brand-rgb,212,175,55);background:rgba(var(--brand-rgb,212,175,55),.9);color:#17100d}' +
    '.cc-relaunch-dismiss{height:30px;border:1px solid rgba(var(--brand-rgb,212,175,55),.3);border-radius:8px;' +
    'background:transparent;color:var(--brand-light,#e6cf88);cursor:pointer;padding:0 10px;font:600 12px Inter,system-ui}';
  document.head.appendChild(style);

  setTimeout(check, 1000);
  setInterval(check, 60000);
}());
