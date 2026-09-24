const card = document.querySelector("[data-plays-card]");
if (card) {
  const api = card.dataset.api;
  const chart = card.querySelector("[data-plays-chart]");
  const summary = card.querySelector("[data-plays-summary]");
  const campaignList = card.querySelector("[data-plays-campaigns]");
  const allCampaigns = card.querySelector("[data-plays-all]");
  const campaignToggle = card.querySelector("[data-plays-toggle=campaigns]");
  const rangeToggle = card.querySelector("[data-plays-toggle=range]");
  const customBox = card.querySelector("[data-plays-custom]");
  const fromInput = card.querySelector("[data-plays-from]");
  const toInput = card.querySelector("[data-plays-to]");
  const state = {range: "last_30d", campaignIds: [], all: true};

  const closeMenus = () => {
    card.querySelectorAll("[data-plays-menu]").forEach((menu) => {
      menu.querySelector(".plays-menu-panel").hidden = true;
      menu.querySelector("[data-plays-toggle]").setAttribute("aria-expanded", "false");
    });
  };

  const openMenu = (name) => {
    card.querySelectorAll("[data-plays-menu]").forEach((menu) => {
      const panel = menu.querySelector(".plays-menu-panel");
      const toggle = menu.querySelector("[data-plays-toggle]");
      const isTarget = menu.dataset.playsMenu === name;
      const shouldOpen = isTarget && panel.hidden;
      panel.hidden = !shouldOpen;
      toggle.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
    });
  };

  const selectedCampaigns = () => {
    if (allCampaigns.checked) return [];
    return [...campaignList.querySelectorAll("input:checked")].map((input) => input.value);
  };

  const campaignLabel = (payload) => {
    if (payload.all_campaigns) return "All campaigns";
    if (!payload.campaign_ids.length) return "No campaigns";
    if (payload.campaign_ids.length === 1) {
      const match = payload.campaigns.find((item) => item.id === payload.campaign_ids[0]);
      return match ? match.name : "1 campaign";
    }
    return `${payload.campaign_ids.length} campaigns`;
  };

  const renderCampaigns = (campaigns) => {
    campaignList.replaceChildren();
    for (const campaign of campaigns) {
      const label = document.createElement("label");
      label.className = "check";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = campaign.id;
      input.checked = state.all || state.campaignIds.includes(campaign.id);
      input.disabled = allCampaigns.checked;
      const name = document.createElement("span");
      name.textContent = campaign.name;
      label.append(input, name);
      campaignList.append(label);
    }
  };

  const draw = (payload) => {
    const series = payload.series || [];
    const width = 920;
    const height = 280;
    const pad = {top: 18, right: 16, bottom: 36, left: 44};
    const innerW = width - pad.left - pad.right;
    const innerH = height - pad.top - pad.bottom;
    const max = Math.max(1, ...series.map((item) => item.plays));
    const x = (index) => pad.left + (series.length <= 1 ? innerW / 2 : (index / (series.length - 1)) * innerW);
    const y = (plays) => pad.top + innerH - (plays / max) * innerH;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "presentation");
    const axis = document.createElementNS("http://www.w3.org/2000/svg", "line");
    axis.setAttribute("x1", String(pad.left));
    axis.setAttribute("x2", String(width - pad.right));
    axis.setAttribute("y1", String(height - pad.bottom));
    axis.setAttribute("y2", String(height - pad.bottom));
    axis.setAttribute("class", "plays-axis");
    svg.append(axis);
    for (const tick of [0, Math.round(max / 2), max]) {
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", String(pad.left));
      line.setAttribute("x2", String(width - pad.right));
      line.setAttribute("y1", String(y(tick));
      line.setAttribute("y2", String(y(tick));
      line.setAttribute("class", "plays-grid");
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", String(pad.left - 8));
      label.setAttribute("y", String(y(tick) + 4));
      label.setAttribute("class", "plays-tick");
      label.textContent = String(tick);
      svg.append(line, label);
    }
    if (series.length) {
      const coords = series.map((item, index) => `${x(index)},${y(item.plays)}`);
      const area = document.createElementNS("http://www.w3.org/2000/svg", "path");
      area.setAttribute(
        "d",
        `M${x(0)},${y(0)} L${coords.join(" L")} L${x(series.length - 1)},${y(0)} Z`
      );
      area.setAttribute("class", "plays-area");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", `M${coords.join(" L")}`);
      path.setAttribute("class", "plays-line");
      svg.append(area, path);
      const first = series[0].bucket.slice(0, 10);
      const last = series[series.length - 1].bucket.slice(0, 10);
      for (const [stamp, index] of [[first, 0], [last, series.length - 1]]) {
        const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
        label.setAttribute("x", String(x(index)));
        label.setAttribute("y", String(height - 12));
        label.setAttribute("class", "plays-tick plays-x");
        label.textContent = stamp;
        svg.append(label);
      }
    }
    chart.replaceChildren(svg);
    if (!payload.total_plays) {
      const empty = document.createElement("p");
      empty.className = "empty plays-empty";
      empty.textContent = "No plays in this range yet.";
      chart.append(empty);
    }
  };

  const load = async () => {
    const query = new URLSearchParams({range: state.range});
    if (state.range === "custom" && fromInput.value && toInput.value) {
      query.set("period_start", `${fromInput.value}T00:00:00Z`);
      const end = new Date(`${toInput.value}T00:00:00Z`);
      end.setUTCDate(end.getUTCDate() + 1);
      query.set("period_end", end.toISOString());
    }
    if (!state.all) {
      for (const id of state.campaignIds) query.append("campaign_id", id);
    }
    const payload = await responseJson(await fetch(`${api}?${query}`, {
      headers: {Accept: "application/json"}
    }));
    campaignToggle.textContent = campaignLabel(payload);
    rangeToggle.textContent = payload.range_label;
    summary.textContent = `${payload.total_plays} play${payload.total_plays === 1 ? "" : "s"} · ${payload.range_label}`;
    if (!campaignList.children.length) renderCampaigns(payload.campaigns || []);
    draw(payload);
  };

  card.querySelectorAll("[data-plays-toggle]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openMenu(button.dataset.playsToggle);
    });
  });
  card.querySelectorAll("[data-plays-range]").forEach((button) => {
    button.addEventListener("click", () => {
      state.range = button.dataset.playsRange;
      customBox.hidden = state.range !== "custom";
      if (state.range !== "custom") {
        closeMenus();
        load().catch((error) => toast(error.message, true));
      }
    });
  });
  allCampaigns.addEventListener("change", () => {
    campaignList.querySelectorAll("input").forEach((input) => {
      input.disabled = allCampaigns.checked;
      if (allCampaigns.checked) input.checked = true;
    });
  });
  card.querySelector("[data-plays-apply-campaigns]").addEventListener("click", () => {
    state.all = allCampaigns.checked;
    state.campaignIds = selectedCampaigns();
    closeMenus();
    load().catch((error) => toast(error.message, true));
  });
  card.querySelector("[data-plays-apply-custom]").addEventListener("click", () => {
    if (!fromInput.value || !toInput.value) {
      toast("Choose a start and end date.", true);
      return;
    }
    state.range = "custom";
    closeMenus();
    load().catch((error) => toast(error.message, true));
  });
  document.addEventListener("click", (event) => {
    if (!card.contains(event.target)) closeMenus();
  });
  load().catch((error) => {
    chart.textContent = "";
    const empty = document.createElement("p");
    empty.className = "empty plays-empty";
    empty.textContent = error.message;
    chart.append(empty);
  });
}
