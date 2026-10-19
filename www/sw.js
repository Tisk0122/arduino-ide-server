'use strict';

const CACHE_PREFIX = 'arduino-web-ide-';
const CACHE_NAME = `${CACHE_PREFIX}shell-v1`;
const PRECACHE_URLS = [
  '/',
  '/docs',
  '/manifest.webmanifest?v=1',
  '/favicon.png?v=2',
  '/icons/icon-192.png?v=1',
  '/icons/icon-512.png?v=1'
];
const APP_ROUTES = new Set(['/', '/index.html', '/account', '/account/']);
const DOC_ROUTES = new Set(['/docs', '/docs.html']);

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    await cache.addAll(PRECACHE_URLS);
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter(key => key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME)
      .map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === 'navigate') {
    const appRoute = APP_ROUTES.has(url.pathname);
    const docRoute = DOC_ROUTES.has(url.pathname);
    if (!appRoute && !docRoute) return;

    event.respondWith((async () => {
      const cache = await caches.open(CACHE_NAME);
      const fallbackUrl = new URL(docRoute ? '/docs' : '/', self.location.origin);
      try {
        const response = await fetch(request);
        if (response.ok) await cache.put(fallbackUrl, response.clone());
        return response;
      } catch (error) {
        const cached = await cache.match(fallbackUrl);
        if (cached) return cached;
        throw error;
      }
    })());
    return;
  }

  if (!['/manifest.webmanifest', '/favicon.png', '/icons/icon-192.png', '/icons/icon-512.png'].includes(url.pathname)) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok) await cache.put(request, response.clone());
    return response;
  })());
});
