window.toast = (message, bad = false) => {
  const node = document.querySelector("#toast");
  if (!node) return;
  node.textContent = message;
  node.className = bad ? "show bad" : "show";
  setTimeout(() => { node.className = ""; }, 4500);
};

window.responseJson = async (response) => {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const nested = data.detail && typeof data.detail === "object" ? data.detail : null;
    const detail = Array.isArray(data.detail) ? data.detail.join(" · ")
      : nested?.delivery_error || nested?.detail || data.detail;
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return data;
};

document.querySelector("[data-login-start]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const phone = new FormData(event.currentTarget).get("phone");
    const data = await responseJson(await fetch("/api/v1/auth/sms/start", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({phone})
    }));
    const verify = document.querySelector("[data-login-verify]");
    verify.hidden = false;
    verify.querySelector("[name=challenge_id]").value = data.challenge_id;
    if (data.demo_code) verify.querySelector("[data-demo-code]").textContent = `Demo code: ${data.demo_code}`;
    toast("One-time code sent.");
  } catch (error) { toast(error.message, true); }
});

document.querySelector("[data-login-verify]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  try {
    await responseJson(await fetch("/api/v1/auth/sms/verify", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(values)
    }));
    const next = new URLSearchParams(location.search).get("next");
    location.assign(next && next.startsWith("/") && !next.startsWith("//") ? next : "/dashboard");
  } catch (error) { toast(error.message, true); }
});

document.querySelector("[data-logout]")?.addEventListener("click", async (event) => {
  try {
    await responseJson(await fetch("/api/v1/auth/logout", {
      method: "POST", headers: {"X-CSRF-Token": event.currentTarget.dataset.csrf}
    }));
    location.assign("/");
  } catch (error) { toast(error.message, true); }
});

const numeric = new Set([
  "amount_minor", "budget_cap_minor", "total_cap_minor", "qfv_price_min_minor",
  "qfv_price_max_minor", "max_qfvs_per_play", "require_face_count",
  "weather_weight", "sports_weight", "market_weight"
]);

document.querySelectorAll("[data-sports-league]").forEach((leagueSelect) => {
  const form = leagueSelect.closest("form");
  const teamSelect = form?.querySelector("[data-sports-team]");
  if (!teamSelect) return;
  const syncTeams = () => {
    const matching = [...teamSelect.options].filter(
      (option) => option.dataset.league === leagueSelect.value
    );
    for (const option of teamSelect.options) {
      const available = option.dataset.league === leagueSelect.value;
      option.disabled = !available;
      option.hidden = !available;
    }
    if (!matching.some((option) => option.selected) && matching[0]) {
      matching[0].selected = true;
    }
  };
  leagueSelect.addEventListener("change", syncTeams);
  syncTeams();
});

document.querySelectorAll("form[data-api]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector("button[type=submit], button:not([type])");
    if (submit) submit.disabled = true;
    try {
      const marketSymbol = form.querySelector("[name=market_symbol]");
      if (marketSymbol) marketSymbol.value = marketSymbol.value.trim().toUpperCase();
      const body = new FormData(form);
      let payload = body;
      const headers = {"X-CSRF-Token": form.dataset.csrf};
      if (form.hasAttribute("data-idempotent")) {
        headers["Idempotency-Key"] = crypto.randomUUID();
      }
      if (form.hasAttribute("data-json")) {
        const value = {};
        for (const [key, raw] of body.entries()) {
          if (key === "media_ids") {
            (value.media_ids ||= []).push(raw);
          } else if (form.querySelector(`[name="${key}"]`)?.hasAttribute("data-json-field")) {
            if (String(raw).trim()) value[key] = JSON.parse(raw);
          } else if (numeric.has(key)) {
            value[key] = Number(raw);
          } else if (key.endsWith("_at") || key === "valid_from" || key === "valid_to") {
            value[key] = new Date(raw).toISOString();
          } else {
            value[key] = raw;
          }
        }
        payload = JSON.stringify(value);
        headers["Content-Type"] = "application/json";
      }
      const data = await responseJson(await fetch(form.dataset.api, {
        method: form.dataset.method || "POST", headers, body: payload
      }));
      if (form.hasAttribute("data-checkout")) {
        if (!data.checkout_url) throw new Error("Checkout URL was not returned.");
        location.assign(data.checkout_url);
        return;
      }
      if (form.hasAttribute("data-show-token") && data.acceptance_token) {
        await navigator.clipboard?.writeText(data.acceptance_token);
        toast(`Invitation token copied: ${data.acceptance_token}`);
        if (submit) submit.disabled = false;
        return;
      }
      if (form.hasAttribute("data-preview") && data.current_qfv_price_minor !== undefined) {
        const output = form.querySelector("[data-preview-output]");
        if (output) output.textContent = `${data.soft_score.toFixed(4)} score → ${data.current_qfv_price_minor.toFixed(3)}¢/QFV`;
        if (submit) submit.disabled = false;
        return;
      }
      toast("Saved successfully.");
      let target = form.dataset.redirect;
      if (form.dataset.resultRedirect) {
        target = form.dataset.resultRedirect.replace("{id}", data.id);
      }
      if (target) location.assign(target);
    } catch (error) {
      toast(error.message, true);
      if (submit) submit.disabled = false;
    }
  });
});

document.querySelector("[data-invitation-accept]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const token = new FormData(form).get("token");
  try {
    await responseJson(await fetch(`/api/v1/invitations/${encodeURIComponent(token)}/accept`, {
      method: "POST", headers: {"X-CSRF-Token": form.dataset.csrf}
    }));
    location.assign("/organizations");
  } catch (error) { toast(error.message, true); }
});
