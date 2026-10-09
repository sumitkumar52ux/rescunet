/* RescuNet service worker - receives Web Push even when the website is closed. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data.json(); } catch (_) { d = { title: 'RescuNet', body: e.data ? e.data.text() : '' }; }
  const urgent = !!d.urgent;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (wins.some(w => w.visibilityState === 'visible')) {      // site is open on screen: it shows the alert itself
      wins.forEach(w => w.postMessage({ type: 'push', data: d }));
      return;
    }
    await self.registration.showNotification(d.title || 'RescuNet', {
      body: d.body || '',
      icon: '/static/icon-192.png', badge: '/static/icon-192.png',
      tag: d.tag || 'rescunet', renotify: true, silent: false, timestamp: Date.now(),
      requireInteraction: urgent,                                // stays on screen until the person reacts
      vibrate: urgent ? [1000, 500, 1000, 500, 1000, 500, 1000] : [200, 100, 200],
      data: { url: d.url || '/', kind: d.kind }
    });
  })());
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      if ('focus' in w) { await w.focus(); w.postMessage({ type: 'open', url }); return; }
    }
    await self.clients.openWindow(url);                         // '/#siren' -> the page starts the full-screen siren
  })());
});
