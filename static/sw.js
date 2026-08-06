// The Dreamerie Command Center — service worker.
//
// Two jobs:
//   1. Web Push (lock-screen alerts) — receives push events while the app isn't
//      in the foreground.
//   2. App shell caching — so launching the installed PWA with no signal shows
//      the real UI plus an offline notice instead of a blank white screen.
//
// Caching rules are deliberately conservative because this app is session-gated:
//   - Only same-origin GET requests are ever touched.
//   - /api/, /auth/, /run/ are never intercepted at all (live data + OAuth only).
//   - A response is only cached when res.ok — the access gate returns 401 for
//     /static/* without a session, and caching a 401 would poison the shell.
//   - Navigations are network-FIRST, so a fresh deploy is never masked by a
//     stale cached page, and a 401 lock screen is shown normally rather than
//     being replaced by the cached app.

const CACHE = 'dreamerie-shell-v1';

// Small, public, rarely-changing assets. Kept short on purpose: a failure in
// any one of these would abort the whole install with cache.addAll.
const PRECACHE = [
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
];

const OFFLINE_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline — The Dreamerie Command Center</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0a0a0c;color:#e6e2d6;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       text-align:center;padding:24px}
  img{width:96px;height:96px;margin-bottom:20px}
  h1{font-size:20px;margin-bottom:8px;color:#c9a84c}
  p{color:#9aa0aa;max-width:320px}
</style></head><body><div>
<img src="/static/icon-192.png" alt="Stinger Industries">
<h1>No connection</h1>
<p>Your assistant needs a network connection to answer. Reconnect and this page will reload itself.</p>
</div>
<script>
  // Reload ONLY when the network is provably back -- never on a timer.
  //
  // This used to be setInterval(() => { if (navigator.onLine) location.reload() }, 5000).
  // navigator.onLine reports true whenever ANY interface exists, even with no
  // real connectivity, so on flaky mobile data (driving between job sites) it
  // reloaded the page every 5 seconds -- which on the demo device looks exactly
  // like "the platform keeps reloading mid-sentence while she's talking".
  //
  // So: prove the server actually answers before reloading, and back off.
  let _delay = 3000;
  async function _recheck() {
    try {
      const r = await fetch('/healthz', { cache: 'no-store' });
      if (r && r.ok) { location.reload(); return; }
    } catch (e) { /* still down */ }
    _delay = Math.min(_delay * 2, 30000);   // 3s → 6s → 12s → 24s → 30s cap
    setTimeout(_recheck, _delay);
  }
  addEventListener('online', () => { _delay = 3000; _recheck(); });
  setTimeout(_recheck, _delay);
</script>
</body></html>`;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(PRECACHE))
      // Never let a precache miss block activation — push must keep working
      // even if an icon 404s or the gate 401s us.
      .catch(() => undefined)
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function cachePut(request, response) {
  // Only cache complete, successful, same-origin responses.
  if (!response || !response.ok || response.type === 'opaque') return;
  const copy = response.clone();
  caches.open(CACHE).then((c) => c.put(request, copy)).catch(() => undefined);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Live data, auth callbacks and cron triggers: hands off entirely.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/auth/') ||
    url.pathname.startsWith('/run/')
  ) {
    return;
  }

  // Navigations (and any HTML): network-first so deploys land immediately and
  // the 401 lock page still works. Cache is a pure offline fallback.
  if (req.mode === 'navigate' || url.pathname.endsWith('.html')) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          cachePut(req, res);
          return res;
        })
        .catch(async () => {
          const hit =
            (await caches.match(req, { ignoreSearch: true })) ||
            (await caches.match('/static/basic.html', { ignoreSearch: true }));
          return (
            hit ||
            new Response(OFFLINE_HTML, {
              status: 200,
              headers: { 'Content-Type': 'text/html; charset=utf-8' },
            })
          );
        })
    );
    return;
  }

  // Everything else under /static/ (icons, manifest): stale-while-revalidate.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((hit) => {
        const network = fetch(req)
          .then((res) => {
            cachePut(req, res);
            return res;
          })
          .catch(() => hit);
        return hit || network;
      })
    );
  }
});

self.addEventListener('push', (event) => {
  let data = { title: 'Your Assistant', body: 'New alert', url: '/static/basic.html' };
  try { if (event.data) data = { ...data, ...event.data.json() }; } catch (e) {}
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      data: { url: data.url || '/static/basic.html' },
      tag: 'dreamerie-alert',
      renotify: true,
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/static/basic.html';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const c of clients) {
        if (c.url.includes('basic.html') && 'focus' in c) return c.focus();
      }
      return self.clients.openWindow(url);
    })
  );
});
