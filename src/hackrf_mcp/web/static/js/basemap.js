// The operator's base map, if there is one: OpenStreetMap vector tiles that
// the browser reads from hackrf-web in byte ranges and draws itself
// (protomaps-leaflet, the global protomapsL). The archive covers its whole
// area down to full_zoom and only a core below that, so a layer of the whole
// area, stretched past full_zoom, lies under a layer of the core's detail
// that asks for nothing outside the core. protomaps-leaflet draws each zoom
// from the data one zoom up, hence the + 2. Nothing is loaded from other
// sites: with no base map on the server (info null) nothing is drawn.
export function addBasemap(group, info) {
  if (!info || !window.protomapsL) return false;
  const opts = { url: '/api/basemap.pmtiles', flavor: 'dark', lang: 'en', noWrap: true };
  window.protomapsL.leafletLayer({ ...opts, maxDataZoom: info.full_zoom, zIndex: 1 }).addTo(group);
  if (info.core && info.maxzoom > info.full_zoom) {
    const [w, s, e, n] = info.core;
    window.protomapsL
      .leafletLayer({ ...opts, maxDataZoom: info.maxzoom, minZoom: info.full_zoom + 2, bounds: L.latLngBounds([s, w], [n, e]), zIndex: 2 })
      .addTo(group);
  }
  return true;
}
