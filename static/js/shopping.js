// Durable, aisle-grouped shopping checklist with an offline local cache.
(function () {
  const $ = (id) => document.getElementById(id);
  let currentStart = null;
  let lastData = null;
  const checked = new Set();
  const QUEUE_KEY = "king-shopping-sync";

  function cacheKey(start) {
    return `king-shopping:${start}`;
  }

  function saveCache(data) {
    try {
      localStorage.setItem(cacheKey(data.start), JSON.stringify(data));
    } catch {}
  }

  function cached(start) {
    try {
      return JSON.parse(localStorage.getItem(cacheKey(start)) || "null");
    } catch {
      return null;
    }
  }

  function queueCheck(itemKey, value) {
    let queue = [];
    try { queue = JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]"); } catch {}
    queue = queue.filter((item) => !(
      item.start === currentStart && item.item_key === itemKey
    ));
    queue.push({ start: currentStart, item_key: itemKey, checked: value });
    localStorage.setItem(QUEUE_KEY, JSON.stringify(queue.slice(-500)));
  }

  async function flushQueue() {
    if (!navigator.onLine) return;
    let queue = [];
    try { queue = JSON.parse(localStorage.getItem(QUEUE_KEY) || "[]"); } catch {}
    if (!queue.length) return;
    const remaining = [];
    for (const item of queue) {
      try {
        await king.fetchJSON("/api/shopping/check", {
          method: "PATCH",
          body: JSON.stringify(item),
        });
      } catch {
        remaining.push(item);
      }
    }
    localStorage.setItem(QUEUE_KEY, JSON.stringify(remaining));
  }

  async function load(start) {
    currentStart = start;
    try {
      const data = await king.fetchJSON(`/api/shopping?start=${start}`);
      lastData = data;
      saveCache(data);
      render(data, false);
      flushQueue();
    } catch (error) {
      const data = cached(start);
      if (!data) {
        $("shopRoot").innerHTML =
          `<p class="result err">${king.escapeHTML(error.message)}</p>`;
        return;
      }
      lastData = data;
      render(data, true);
    }
  }

  function render(data, offline) {
    checked.clear();
    let total = 0;
    for (const aisle of data.aisle_order) {
      for (const item of data.aisles[aisle] || []) {
        total++;
        if (item.checked) checked.add(item.item_key);
      }
    }
    $("shopSub").textContent =
      `Week of ${data.start} · ${total} item${total === 1 ? "" : "s"}` +
      (offline ? " · offline copy" : "");
    if (!total) {
      $("shopRoot").innerHTML =
        '<div class="empty-state"><i data-lucide="shopping-basket"></i><strong>Your list is clear</strong><p>Plan the week to calculate whole-batch ingredient needs.</p></div>';
      return;
    }
    const supermarkets = (data.supermarkets || []).length
      ? `<p class="shop-markets"><i data-lucide="store"></i>${data.supermarkets.map(king.escapeHTML).join(" · ")}</p>`
      : "";
    let html = supermarkets;
    for (const aisle of data.aisle_order) {
      const items = data.aisles[aisle] || [];
      if (!items.length) continue;
      html += `<section class="aisle-section">
        <div class="aisle-head"><p class="eyebrow">${king.escapeHTML(aisle)}</p><span class="num">${items.length}</span></div>
        ${items.map(rowHTML).join("")}
      </section>`;
    }
    $("shopRoot").innerHTML = html;
    $("shopRoot").querySelectorAll(".shop-check").forEach((input) => {
      input.addEventListener("change", () => toggle(input));
    });
  }

  function rowHTML(item) {
    const itemKey = king.escapeHTML(item.item_key);
    const ingredientKey = king.escapeHTML(item.ingredient_key);
    const name = king.escapeHTML(item.display_name);
    const unit = king.escapeHTML(item.unit);
    const isChecked = checked.has(item.item_key);
    return `<label class="shop-row${isChecked ? " checked" : ""}">
      <input class="shop-check" type="checkbox" ${isChecked ? "checked" : ""}
        data-item-key="${itemKey}" data-name="${name}"
        data-qty="${item.missing}" data-unit="${unit}"
        data-key-ing="${ingredientKey}">
      <span class="check" aria-hidden="true"><i data-lucide="check"></i></span>
      <span>${name}</span>
      <span class="num dim">${formatQty(item.missing)} ${unit}</span>
    </label>`;
  }

  function formatQty(quantity) {
    if (quantity == null) return "-";
    return king.formatNumber(quantity);
  }

  async function toggle(input) {
    const itemKey = input.dataset.itemKey;
    const value = input.checked;
    input.closest(".shop-row").classList.toggle("checked", value);
    if (value) checked.add(itemKey);
    else checked.delete(itemKey);

    for (const items of Object.values(lastData.aisles || {})) {
      const item = items.find((candidate) => candidate.item_key === itemKey);
      if (item) item.checked = value;
    }
    saveCache(lastData);
    try {
      await king.fetchJSON("/api/shopping/check", {
        method: "PATCH",
        body: JSON.stringify({
          start: currentStart,
          item_key: itemKey,
          checked: value,
        }),
      });
    } catch {
      queueCheck(itemKey, value);
      king.toast("Saved offline; this check will sync later.", "info");
    }
  }

  async function done() {
    if (!checked.size) {
      king.toast("Check purchased items first.", "error");
      return;
    }
    const items = [];
    document.querySelectorAll(".shop-check:checked").forEach((input) => {
      const quantity = Number(input.dataset.qty);
      if (quantity > 0) {
        items.push({
          item_key: input.dataset.itemKey,
          ingredient_key: input.dataset.keyIng,
          display_name: input.dataset.name,
          quantity,
          unit: input.dataset.unit,
        });
      }
    });
    if (!items.length) return;
    try {
      const result = await king.fetchJSON("/api/shopping/done", {
        method: "POST",
        body: JSON.stringify({ start: currentStart, items }),
      });
      localStorage.removeItem(cacheKey(currentStart));
      king.toast(`Added ${result.added} item${result.added === 1 ? "" : "s"} to pantry.`, "success");
      load(currentStart);
    } catch (error) {
      king.toast(error.message, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("prevWeekBtn").addEventListener("click", () => load(king.addDays(currentStart, -7)));
    $("nextWeekBtn").addEventListener("click", () => load(king.addDays(currentStart, 7)));
    $("thisWeekBtn").addEventListener("click", () => load(king.mondayOf(king.isoToday())));
    $("doneBtn").addEventListener("click", done);
    $("doneBtnMobile")?.addEventListener("click", done);
    window.addEventListener("online", () => {
      flushQueue();
      load(currentStart);
    });
    load(king.mondayOf(king.isoToday()));
  });
})();
