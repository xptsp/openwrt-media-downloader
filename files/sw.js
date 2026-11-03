const CACHE_NAME = 'media-downloader-v1';
const STATIC_ASSETS = [
  '/',
  '/index.html',
  '/style.css',
  '/manifest.json',
  '/app-icon.png',
  '/favicon.ico',
  '/wallpaper.webp'
];

// 📥 Install Event: Cache layout sheets instantly into client browser memory
self.addEventListener('install', (e) => {
  e.waitUntil(
	caches.open(CACHE_NAME).then((cache) => {
	  console.log('[Service Worker] Pre-caching structural layout shell assets...');
	  return cache.addAll(STATIC_ASSETS);
	})
  );
  self.skipWaiting();
});

// ⚡ Activate Event: Instantly claim client pages and purge old legacy caches
self.addEventListener('activate', (e) => {
  e.waitUntil(
	caches.keys().then((keys) => {
	  return Promise.all(
		keys.map((key) => {
		  if (key !== CACHE_NAME) {
			console.log('[Service Worker] Removing old legacy app shell cache:', key);
			return caches.delete(key);
		  }
		})
	  );
	})
  );
  self.clients.claim();
});

// Add this directly inside your existing self.addEventListener('fetch', (e) => { ... }) block:

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // 📡 Intercept the System Web Share Intent locally inside the browser framework
  if (e.request.method === 'POST' && url.pathname === '/api/share') {
    e.respondWith(
      (async () => {
        try {
          // Unpack the form elements right inside your local phone memory threads
          const formData = await e.request.formData();
          const sharedUrl = formData.get('url') || formData.get('text') || '';

          if (sharedUrl) {
            // Locate your open dashboard window sheets
            const windowClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
            
            let targetClient = null;
            for (const client of windowClients) {
              await client.focus();
              targetClient = client;
              break;
            }

            // Launch a fresh layout window frame if the app wasn't running active
            if (!targetClient) {
              targetClient = await self.clients.openWindow('/');
              // Give the newly opened tab a moment to boot up before transmitting
              await new Promise(resolve => setTimeout(resolve, 1200));
            }

            // Clean up mobile share bundles (extract just the URL if text noise exists)
            let cleanUrl = sharedUrl.trim();
            if (cleanUrl.includes('http')) {
              const urlExtract = cleanUrl.match(/(https?:\/\/[^\s]+)/);
              if (urlExtract) cleanUrl = urlExtract[0];
            }

            // 🌟 FIXED: Convert to Query Parameter delivery to guarantee frontend capture
            const cleanSharedUrl = encodeURIComponent(cleanUrl);
            await targetClient.navigate('/?incoming_share_link=' + cleanSharedUrl);
          }
        } catch (err) {
          console.error('[Service Worker] Share target handling error:', err);
        }

        // 🌟 FIXED: Deliver a native HTML landing bridge instead of throwing an unhandled Response.redirect 400 crash
        const landingBridgeHtml = `
          <!DOCTYPE html>
          <html>
            <head>
              <title>Processing Share...</title>
              <script>
                // Instantly wipe form context and focus back onto your primary absolute root app panel layout
                window.location.replace(window.location.origin + '/');
              </script>
            </head>
            <body></body>
          </html>
        `;

        return new Response(landingBridgeHtml, {
          status: 200,
          headers: { 'Content-Type': 'text/html; charset=utf-8' }
        });
      })()
    );
    return;
  }

  // 📡 Network-Only Strategy for dynamic live backend API requests
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(fetch(e.request));
    return;
  }

  // 🧠 Cache-First, Network-Fallback Strategy for fast app loading
  e.respondWith(
    caches.match(e.request).then((cachedResponse) => {
      if (cachedResponse) {
        fetch(e.request).then((networkResponse) => {
          if (networkResponse.status === 200) {
            caches.open(CACHE_NAME).then((cache) => cache.put(e.request, networkResponse));
          }
        }).catch(() => {});
        return cachedResponse;
      }
      return fetch(e.request);
    })
  );
});
