(function () {
  const VERDICT_COLOR = {
    'MATCH': '#2ca02c', 'MATCH_FAR': '#e0a81c',
    'MISSING': '#e36c1d', 'SKIPPED': '#888'
  };
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
    ));
  }
  function candIcon(verdict) {
    const color = VERDICT_COLOR[verdict] || '#9aa0a6';
    return L.divIcon({
      className: '',
      html: '<div style="width:10px;height:10px;background:' + color + ';border:1.5px solid #fff;border-radius:50%;box-shadow:0 0 2px #0006"></div>',
      iconSize: [10, 10], iconAnchor: [5, 5]
    });
  }
  const osmAddrIcon = L.divIcon({
    className: '',
    html: '<div style="width:8px;height:8px;background:#fff;border:2px solid #7a3fc2;border-radius:50%"></div>',
    iconSize: [12, 12], iconAnchor: [6, 6]
  });
  const osmPoiIcon = L.divIcon({
    className: '',
    html: '<div style="width:8px;height:8px;background:#fff;border:2px solid #6b7280;border-radius:50%"></div>',
    iconSize: [12, 12], iconAnchor: [6, 6]
  });
  function makeCandPopup(runId, view) {
    return function (c) {
      const addr = c.address || [c.housenumber, c.street].filter(Boolean).join(' ') || ('#' + c.candidate_id);
      const parts = ['<strong>' + esc(addr) + '</strong>'];
      const meta = [];
      if (c.verdict) meta.push('<span class="pill">verdict: ' + esc(c.verdict) + '</span>');
      if (c.review_status) meta.push('<span class="pill">' + esc(c.review_status) + '</span>');
      else if (c.stage) meta.push('<span class="pill">' + esc(c.stage) + '</span>');
      if (meta.length) parts.push('<div style="margin-top:.25rem">' + meta.join(' ') + '</div>');
      if (c.address_class && c.address_class !== 'Land') {
        parts.push('<div class="muted" style="margin-top:.15rem">class: ' + esc(c.address_class) + '</div>');
      }
      parts.push('<div style="margin-top:.35rem"><a href="/runs/' + runId + '/' + view + '/' + c.candidate_id + '">Open candidate &rarr;</a></div>');
      return parts.join('');
    };
  }
  function osmPopup(o) {
    const head = [o.housenumber, o.street].filter(Boolean).join(' ') || (o.type + ' #' + o.id);
    const parts = ['<strong>' + esc(head) + '</strong>'];
    const kindPill = o.kind === 'poi'
      ? '<span class="pill" style="background:#d1f5ec">POI' + (o.poi_tag ? ' &middot; ' + esc(o.poi_tag) : '') + '</span>'
      : '<span class="pill" style="background:#ede3f7">address</span>';
    parts.push('<div style="margin-top:.25rem">' + kindPill + '</div>');
    const extra = [];
    if (o.unit) extra.push('unit ' + esc(o.unit));
    if (o.floor) extra.push('floor ' + esc(o.floor));
    if (o.postcode) extra.push(esc(o.postcode));
    if (extra.length) parts.push('<div class="muted" style="margin-top:.15rem">' + extra.join(' &middot; ') + '</div>');
    if (o.name) parts.push('<div style="margin-top:.15rem">' + esc(o.name) + '</div>');
    parts.push('<div class="muted" style="margin-top:.25rem">' + esc(o.type) + ' #' + esc(o.id) + '</div>');
    parts.push('<div style="margin-top:.25rem"><a href="https://www.openstreetmap.org/' + esc(o.type) + '/' + esc(o.id) + '" target="_blank" rel="noopener">Open in OSM &uarr;</a></div>');
    return parts.join('');
  }
  window.t2Detail = {
    esc: esc,
    candIcon: candIcon,
    osmAddrIcon: osmAddrIcon,
    osmPoiIcon: osmPoiIcon,
    osmPopup: osmPopup,
    makeCandPopup: makeCandPopup
  };
})();
