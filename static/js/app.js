// Shared utilities: toast, fetchJSON, escapeHTML, date helpers, modal helpers.
window.king = window.king || {};

// ---- Date helpers (centralized; week.js + shopping.js consume these) ----
king.timezone = document.documentElement.dataset.timezone || "Europe/Dublin";
king.preparedShelfLifeDays = Math.max(
  1, Number(document.documentElement.dataset.preparedShelfLife || 4)
);
king.frozenShelfLifeDays = Math.max(
  1, Number(document.documentElement.dataset.frozenShelfLife || 90)
);
king.isoToday = () => {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: king.timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
};
king.mondayOf = (iso) => {
  const d = new Date(iso + "T00:00:00Z");
  const day = (d.getUTCDay() + 6) % 7;       // Mon = 0
  d.setUTCDate(d.getUTCDate() - day);
  return d.toISOString().slice(0, 10);
};
king.addDays = (iso, n) => {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};
king.formatNumber = (value) => {
  const n = Number(value);
  if (!Number.isFinite(n)) return "0";
  return Number.isInteger(n) ? String(n) : n.toFixed(1).replace(/\.0$/, "");
};

king.proposalPicksHTML = (proposal) => {
  const labels = {
    breakfast: "Breakfast",
    lunch: "Lunch",
    dinner: "Dinner",
    snack: "Snack",
  };
  const picks = [];
  Object.entries(proposal.plan || {}).forEach(([date, slots]) => {
    Object.entries(slots || {}).forEach(([slot, item]) => {
      if (!item || item.preserved) return;
      picks.push({ date, slot, item });
    });
  });
  if (!picks.length) {
    return '<p class="hint">No new picks in this proposal.</p>';
  }
  return `<div class="proposal-picks" aria-label="Proposed meals">
    ${picks.map(({ date, slot, item }) => {
      const reasons = (item.reasons || ["Best available fit"]).join(" · ");
      return `<div class="proposal-pick">
        <span class="proposal-pick-when num">${king.escapeHTML(date.slice(5))}<br>${king.escapeHTML(labels[slot] || slot)}</span>
        <span class="proposal-pick-meal"><strong>${king.escapeHTML(item.name || "Recipe")}</strong><small>${king.escapeHTML(reasons)}</small></span>
      </div>`;
    }).join("")}
  </div>`;
};

king.icons = () => {
  if (!window.lucide || !document.querySelector("i[data-lucide]")) return;
  window.lucide.createIcons({
    attrs: {
      "aria-hidden": "true",
      "stroke-width": 1.75,
    },
  });
};

// ---- Italian tooltip on TAP (mobile) ----
// `title=` only works on desktop hover. Bind a click handler that surfaces
// the title via a quick toast on mobile so taps reveal the translation
// instead of doing nothing. Audit L10.
document.addEventListener("click", (e) => {
  const el = e.target.closest(".has-it[title], .recipe-title-it[title]");
  if (!el) return;
  const it = el.getAttribute("title");
  if (!it) return;
  // Only fire on touch (don't compete with desktop hover behavior).
  if (matchMedia("(hover: hover)").matches) return;
  king.toast(it, "info");
});

// ---- Translation helpers ----
// Read once on first call, cached for the page. Reset by full reload.
king._translationMode = null;
king.getTranslationMode = async () => {
  if (king._translationMode) return king._translationMode;
  try {
    const d = await king.fetchJSON("/api/settings");
    king._translationMode = d.kv?.translation_mode?.value || "hover";
  } catch { king._translationMode = "hover"; }
  return king._translationMode;
};

// Render a recipe title honoring the user's translation_mode. Used by both
// /recipes list rows and /recipes/<id> detail so the modes flow through
// uniformly (audit H3: list ignored translation_mode entirely).
//
// Returns an HTML string (titleHTML) that pairs the EN + IT names per mode.
king.renderRecipeTitle = (r, mode) => {
  const en = king.escapeHTML(r.name || "");
  const it = r.name_it ? king.escapeHTML(r.name_it) : "";
  if (!it) return en;
  if (mode === "italian_only") {
    return `<span title="${en}">${it}</span>`;
  }
  if (mode === "side_by_side") {
    return `${en}<span class="dim" style="font-style: italic; margin-left: 4px;"> · ${it}</span>`;
  }
  // hover (default): tooltip on the EN, dotted underline as affordance
  return `<span class="has-it" title="${it}">${en}</span>`;
};

// ---- Modal helpers (consistent body-overflow handling across pages) ----
king.openModal = (id) => {
  const el = document.getElementById(id);
  if (!el) return;
  el._returnFocus = document.activeElement;
  el.setAttribute("role", el.getAttribute("role") || "dialog");
  el.setAttribute("aria-modal", "true");
  el.hidden = false;
  document.body.style.overflow = "hidden";
  const focusable = el.querySelector(
    "button:not([disabled]), input:not([disabled]), select:not([disabled]), " +
    "textarea:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])"
  );
  if (focusable) requestAnimationFrame(() => focusable.focus());
};
king.closeModal = (id) => {
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden = true;
  // Only release scroll lock if no other modals remain open.
  const stillOpen = document.querySelectorAll(".modal:not([hidden])").length;
  if (!stillOpen) document.body.style.overflow = "";
  if (el._returnFocus?.focus) el._returnFocus.focus();
};

document.addEventListener("keydown", (event) => {
  const modal = [...document.querySelectorAll(".modal:not([hidden])")].pop();
  if (!modal) return;
  if (event.key === "Escape") {
    event.preventDefault();
    king.closeModal(modal.id);
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...modal.querySelectorAll(
    "button:not([disabled]), input:not([disabled]), select:not([disabled]), " +
    "textarea:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])"
  )].filter((el) => el.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

document.addEventListener("click", (event) => {
  if (event.target.classList?.contains("modal") && event.target.id) {
    king.closeModal(event.target.id);
  }
});

king.idempotencyKey = () => (
  window.crypto?.randomUUID?.() ||
  `${Date.now()}-${Math.random().toString(36).slice(2)}`
);

king.escapeHTML = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

king.toast = (msg, kind = "info") => {
  const region = document.getElementById("toastRegion");
  if (!region) { console.log(msg); return; }
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  region.appendChild(t);
  if (kind !== "error") setTimeout(() => t.remove(), 3500);
  else t.addEventListener("click", () => t.remove());
};

// CSRF token: fetched once via /api/me on first state-changing call, cached
// in-memory. Server rotates per session at login; we re-fetch on 403.
king._csrf = null;
async function _ensureCsrf() {
  if (king._csrf) return king._csrf;
  try {
    const r = await fetch("/api/me", { headers: { "X-Requested-With": "XMLHttpRequest" } });
    if (r.ok) {
      const d = await r.json();
      if (d && d.csrf_token) king._csrf = d.csrf_token;
    }
  } catch {}
  return king._csrf;
}

king.fetchJSON = async (url, opts = {}) => {
  const headers = {
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    ...(opts.headers || {}),
  };
  // Add CSRF for every state-changing request. GET/HEAD don't need it but
  // setting it doesn't hurt and keeps the path uniform.
  const method = (opts.method || "GET").toUpperCase();
  if (method !== "GET" && method !== "HEAD") {
    const tok = await _ensureCsrf();
    if (tok) headers["X-CSRF-Token"] = tok;
  }
  let res = await fetch(url, { ...opts, headers });
  // Token rotation: server may issue a new one (e.g. after login); on 403
  // refresh the cache and retry once.
  if (res.status === 403 && method !== "GET") {
    king._csrf = null;
    const tok = await _ensureCsrf();
    if (tok) {
      headers["X-CSRF-Token"] = tok;
      res = await fetch(url, { ...opts, headers });
    }
  }
  let data = null;
  try { data = await res.json(); } catch {}
  if (res.status === 401) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.assign(`/login?expired=1&next=${next}`);
    const err = new Error("Session expired");
    err.status = 401;
    err.data = data;
    throw err;
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
};

king.fetchMultipart = async (url, formData, opts = {}) => {
  const method = (opts.method || "POST").toUpperCase();
  const headers = {
    "X-Requested-With": "XMLHttpRequest",
    ...(opts.headers || {}),
  };
  if (method !== "GET" && method !== "HEAD") {
    const token = await _ensureCsrf();
    if (token) headers["X-CSRF-Token"] = token;
  }
  let response = await fetch(url, {
    ...opts,
    method,
    body: formData,
    headers,
  });
  if (response.status === 403 && method !== "GET") {
    king._csrf = null;
    const token = await _ensureCsrf();
    if (token) {
      headers["X-CSRF-Token"] = token;
      response = await fetch(url, {
        ...opts,
        method,
        body: formData,
        headers,
      });
    }
  }
  let data = null;
  try { data = await response.json(); } catch {}
  if (response.status === 401) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.assign(`/login?expired=1&next=${next}`);
  }
  if (!response.ok) {
    const error = new Error((data && data.error) || `HTTP ${response.status}`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
};

king.fetchBlob = async (url, opts = {}) => {
  const method = (opts.method || "GET").toUpperCase();
  const headers = {
    "X-Requested-With": "XMLHttpRequest",
    ...(opts.headers || {}),
  };
  if (typeof opts.body === "string" && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  if (method !== "GET" && method !== "HEAD") {
    const token = await _ensureCsrf();
    if (token) headers["X-CSRF-Token"] = token;
  }
  let response = await fetch(url, { ...opts, method, headers });
  if (response.status === 403 && method !== "GET") {
    king._csrf = null;
    const token = await _ensureCsrf();
    if (token) {
      headers["X-CSRF-Token"] = token;
      response = await fetch(url, { ...opts, method, headers });
    }
  }
  if (response.status === 401) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    window.location.assign(`/login?expired=1&next=${next}`);
  }
  if (!response.ok) {
    let data = null;
    try { data = await response.json(); } catch {}
    const error = new Error((data && data.error) || `HTTP ${response.status}`);
    error.status = response.status;
    error.data = data;
    throw error;
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  const plainMatch = disposition.match(/filename="?([^";]+)"?/i);
  const filename = utf8Match
    ? decodeURIComponent(utf8Match[1])
    : plainMatch?.[1] || "download";
  return { blob: await response.blob(), filename };
};

king.downloadBlob = ({ blob, filename }) => {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.hidden = true;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

document.addEventListener("DOMContentLoaded", () => {
  king.icons();
  if ("serviceWorker" in navigator && (
    location.protocol === "https:" || location.hostname === "localhost"
  )) {
    navigator.serviceWorker.register("/static/sw.js").catch(() => {});
  }
});

let iconRefreshPending = false;
new MutationObserver(() => {
  if (iconRefreshPending || !document.querySelector("i[data-lucide]")) return;
  iconRefreshPending = true;
  requestAnimationFrame(() => {
    iconRefreshPending = false;
    king.icons();
  });
}).observe(document.documentElement, { childList: true, subtree: true });
