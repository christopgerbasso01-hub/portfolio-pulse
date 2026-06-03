/**
 * Portfolio Pulse — Service Worker
 * Strategy:
 *   App shell (HTML/fonts/libraries) → Cache-first, update in background
 *   API calls (/api/*) → Network-first, fall back to last cached response
 *   Icons / manifest → Cache-first
 */

const CACHE     = 'portfolio-pulse-v46';
const API_CACHE = 'portfolio-pulse-api-v26';

const APP_SHELL = [
  '/',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
];

// ── Install: cache the app shell ─────────────────────────────────────────────
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c =>
      c.addAll(APP_SHELL.map(url => new Request(url, { cache: 'reload' })))
    )
  );
  self.skipWaiting();
});

// ── Activate: remove old caches ───────────────────────────────────────────────
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(k => k !== CACHE && k !== API_CACHE)
          .map(k => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// ── Fetch: route requests ─────────────────────────────────────────────────────
self.addEventListener('fetch', e => {
  const { request } = e;
  const url = new URL(request.url);

  // Don't intercept non-GET or cross-origin non-API requests
  if (request.method !== 'GET') return;

  // API calls: network-first, stale fallback (shows last known data offline)
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(
      fetch(request)
        .then(resp => {
          // Cache successful API responses for offline fallback
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(API_CACHE).then(c => c.put(request, clone));
          }
          return resp;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // CDN resources (fonts, Chart.js, etc.): network-first with cache fallback
  if (!url.hostname.includes('portfolio-pulse')) {
    e.respondWith(
      fetch(request).catch(() => caches.match(request))
    );
    return;
  }

  // App shell: cache-first, revalidate in background (stale-while-revalidate)
  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(request).then(cached => {
        const networkFetch = fetch(request).then(resp => {
          if (resp.ok) cache.put(request, resp.clone());
          return resp;
        });
        return cached || networkFetch;
      })
    )
  );
});

// ── Push notifications ────────────────────────────────────────────────────────
self.addEventListener('push', e => {
  if (!e.data) return;
  let payload;
  try {
    payload = e.data.json();
  } catch {
    payload = { title: 'Portfolio Pulse', body: e.data.text() };
  }

  e.waitUntil(
    // Show the notification
    self.registration.showNotification(payload.title || 'Portfolio Pulse', {
      body:    payload.body  || '',
      icon:    '/icon-192.png',
      badge:   '/icon-192.png',
      tag:     payload.tag   || 'portfolio-pulse',
      data:    payload.data  || {},
      vibrate: [100, 50, 100],
    }).then(() => {
      // Set app icon badge — only if the app isn't currently in the foreground
      return clients.matchAll({ type: 'window', includeUncontrolled: true })
        .then(wins => {
          const appOpen = wins.some(w => w.visibilityState === 'visible');
          if (!appOpen && 'setAppBadge' in self.registration) {
            return self.registration.setAppBadge(1);
          }
        });
    })
  );
});

// ── Notification click: open/focus the app + clear badge ────────────────────
self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(wins => {
      // Clear badge when user taps the notification
      if ('clearAppBadge' in self.registration) {
        self.registration.clearAppBadge().catch(() => {});
      }
      const existing = wins.find(w => w.url.includes(self.location.origin));
      if (existing) return existing.focus();
      return clients.openWindow('/');
    })
  );
});
