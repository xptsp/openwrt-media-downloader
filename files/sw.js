const CACHE_NAME = 'media-downloader-v2';

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(clients.claim());
});

self.addEventListener('fetch', (event) => {
  // Always bypass cache for API calls so live queue/logs stay fresh
  if (event.request.url.includes('/api/')) {
    return;
  }
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (error) {
    payload = { body: event.data ? event.data.text() : '' };
  }

  event.waitUntil(self.registration.showNotification(payload.title || 'Media Downloader', {
    body: payload.body || 'There is an update from Media Downloader.',
    icon: payload.icon || '/app-icon.png',
    badge: '/app-icon.png',
    data: { url: payload.url || '/queue' }
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const targetUrl = new URL(event.notification.data.url || '/queue', self.location.origin);
  const safeUrl = targetUrl.origin === self.location.origin ? targetUrl.href : new URL('/queue', self.location.origin).href;

  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
    for (const client of windowClients) {
      if (new URL(client.url).origin === self.location.origin && 'focus' in client) {
        return client.navigate(safeUrl).then((navigatedClient) => (navigatedClient || client).focus());
      }
    }
    return clients.openWindow(safeUrl);
  }));
});
