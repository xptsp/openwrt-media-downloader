self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('fetch', (e) => {
  // Pass-through strategy to let your local API endpoints respond normally
  e.respondWith(fetch(e.request));
});
