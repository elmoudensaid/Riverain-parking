// Network first for the app itself (always the latest version), cache as instant fallback offline.
// Leaflet from the CDN never changes: cache first.
const V="gj-v4";
self.addEventListener("install",e=>{ self.skipWaiting(); e.waitUntil(caches.open(V).then(c=>c.addAll(["./","index.html","zone.json","manifest.json"]).catch(()=>{}))); });
self.addEventListener("activate",e=>{ e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==V).map(k=>caches.delete(k)))).then(()=>self.clients.claim())); });
self.addEventListener("fetch",e=>{
  const req=e.request; if(req.method!=="GET") return;
  const url=new URL(req.url);
  if(url.origin===location.origin){
    e.respondWith((async()=>{
      const ctl=new AbortController(); const t=setTimeout(()=>ctl.abort(),4000);
      try{ const r=await fetch(req,{cache:"no-cache",signal:ctl.signal}); clearTimeout(t); if(r.ok){ const c=await caches.open(V); c.put(req,r.clone()); } return r; }
      catch(err){ clearTimeout(t); const hit=await caches.match(req,{ignoreSearch:true}); return hit || Response.error(); }
    })());
  } else if(url.hostname==="cdnjs.cloudflare.com"){
    e.respondWith(caches.match(req).then(hit=>hit||fetch(req).then(r=>{ if(r.ok){ const cl=r.clone(); caches.open(V).then(c=>c.put(req,cl)); } return r; })));
  }
});
