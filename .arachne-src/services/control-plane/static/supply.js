const mapNode = document.querySelector("[data-supply-map]");

document.querySelector("[data-supply-period]")?.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = new URLSearchParams();
  for (const [name, value] of new FormData(event.currentTarget)) {
    if (value) query.set(name, new Date(value).toISOString());
  }
  location.assign(`${location.pathname}?${query}`);
});

if (mapNode && window.L) {
  const map = L.map(mapNode, {scrollWheelZoom: true}).setView([43.6532, -79.3832], 10);
  const cartoKey = mapNode.dataset.cartoKey || "";
  const cartoQuery = cartoKey ? `?api_key=${encodeURIComponent(cartoKey)}` : "";
  if (cartoKey) {
    L.tileLayer(`https://{s}.basemaps.cartocdn.com/rastertiles/voyager_nolabels/{z}/{x}/{y}{r}.png${cartoQuery}`, {
      maxZoom: 20,
      subdomains: "abcd",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
    }).addTo(map);
    L.tileLayer(`https://{s}.basemaps.cartocdn.com/rastertiles/voyager_only_labels/{z}/{x}/{y}{r}.png${cartoQuery}`, {
      maxZoom: 20,
      subdomains: "abcd",
      pane: "shadowPane"
    }).addTo(map);
  } else {
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).addTo(map);
  }

  const color = (state) => ({
    online: "#28745e",
    stale: "#f1b84b",
    offline: "#ad3940",
    unknown: "#6d747d"
  })[state] || "#6d747d";

  const popup = (device) => {
    const root = document.createElement("div");
    root.className = "map-popup";
    const title = document.createElement("strong");
    title.textContent = device.label || device.device_id || "Unknown device";
    const details = document.createElement("span");
    const fix = device.current_fix || device.current_position;
    const confidence = fix && Number.isFinite(fix.confidence)
      ? ` · fix ${Math.round(fix.confidence * 100)}%`
      : "";
    details.textContent = `${device.connectivity || "unknown"} · ${device.status || "unknown"}${confidence}`;
    const revenue = document.createElement("span");
    const amounts = Object.entries(device.revenue?.currencies || {})
      .map(([currency, value]) => `${currency} ${(value.period_revenue_minor / 100).toFixed(2)}`);
    revenue.textContent = amounts.length ? `Period revenue ${amounts.join(" · ")}` : "No period revenue";
    const link = document.createElement("a");
    link.href = `/supply/devices/${encodeURIComponent(device.device_id)}`;
    link.textContent = "View device";
    root.append(title, details, revenue, link);
    return root;
  };

  const render = (payload) => {
    const devices = payload.devices || (payload.device ? [payload.device] : []);
    const bounds = [];
    for (const device of devices) {
      const fix = device.current_fix || device.current_position;
      if (!fix || !Number.isFinite(Number(fix.latitude)) || !Number.isFinite(Number(fix.longitude))) continue;
      const latlng = [Number(fix.latitude), Number(fix.longitude)];
      const marker = L.circleMarker(latlng, {
        radius: device.movement_warning ? 11 : 8,
        color: device.movement_warning ? "#ad3940" : color(device.connectivity),
        fillColor: color(device.connectivity),
        fillOpacity: 0.85,
        weight: device.movement_warning ? 4 : 2,
        className: `fleet-marker ${device.connectivity || "unknown"}`
      }).addTo(map).bindPopup(popup(device));
      marker.bindTooltip(device.label || device.device_id);
      bounds.push(latlng);
    }
    const track = (payload.positions || [])
      .filter((position) => Number.isFinite(Number(position.latitude)) && Number.isFinite(Number(position.longitude)))
      .map((position) => [Number(position.latitude), Number(position.longitude)])
      .reverse();
    if (track.length > 1) {
      L.polyline(track, {color: "#303666", weight: 4, opacity: 0.72}).addTo(map);
      bounds.push(...track);
    }
    if (bounds.length === 1) map.setView(bounds[0], 14);
    if (bounds.length > 1) map.fitBounds(bounds, {padding: [32, 32], maxZoom: 15});
  };

  const query = new URLSearchParams(location.search);
  fetch(`${mapNode.dataset.api}${query.size ? `?${query}` : ""}`, {
    headers: {"Accept": "application/json"}
  })
    .then(responseJson)
    .then(render)
    .catch((error) => {
      mapNode.classList.add("map-error");
      const message = document.createElement("p");
      message.textContent = `Map data unavailable: ${error.message}`;
      mapNode.append(message);
    });
}
