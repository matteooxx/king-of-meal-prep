// /pantry — expiry-bucketed list + add/edit modal.
(function () {
  const $ = (id) => document.getElementById(id);
  let editingId = null;
  let pantryItems = [];
  let preparedItems = [];
  let editingPreparedId = null;

  async function load() {
    try {
      const [pantry, prepared] = await Promise.all([
        king.fetchJSON("/api/pantry"),
        king.fetchJSON("/api/prepared"),
      ]);
      preparedItems = prepared.items || [];
      renderPantry(pantry, prepared);
      renderPrepared(prepared);
    } catch (e) {
      $("pantryRoot").innerHTML = `<p class="result err">${king.escapeHTML(e.message)}</p>`;
    }
  }

  const BUCKETS = [
    { key: "urgent",     label: "Use today/tomorrow", icon: "⚠" },
    { key: "this_week",  label: "This week",          icon: "" },
    { key: "stocked",    label: "Stocked",            icon: "" },
    { key: "frozen_dry", label: "Frozen · Dry",       icon: "" },
  ];

  function renderPantry(d, prepared) {
    pantryItems = Object.values(d.buckets || {}).flat();
    $("pantryCount").textContent =
      `${d.total} ingredient${d.total === 1 ? "" : "s"} · ` +
      `${king.formatNumber(prepared.total_portions || 0)} prepared`;
    if (!d.total) {
      $("pantryRoot").innerHTML = `<p class="dim" style="text-align:center; padding: var(--sp-7);">Nothing in your pantry yet. Click <strong>+ Add</strong>.</p>`;
      return;
    }
    let html = "";
    for (const b of BUCKETS) {
      const items = d.buckets[b.key] || [];
      if (!items.length) continue;
      html += `<div class="pantry-section pantry-${b.key}">
        <p class="eyebrow">${king.escapeHTML(b.label)}${b.icon ? ' <span style="color: var(--warn);">' + b.icon + '</span>' : ''} · ${items.length}</p>
        <hr class="hr-dotted">
        ${items.map(rowHTML).join("")}
      </div>`;
    }
    $("pantryRoot").innerHTML = html;
    $("pantryRoot").querySelectorAll(".pantry-row").forEach(el => {
      el.querySelector(".p-edit").addEventListener("click", () => openEdit(el.dataset.id));
      el.querySelector(".p-del").addEventListener("click", () => removeItem(el.dataset.id));
      const useButton = el.querySelector(".p-use-portion");
      if (useButton) {
        useButton.addEventListener("click", () => consumePortion(el.dataset.id));
      }
      const logButton = el.querySelector(".p-log-food");
      if (logButton) {
        logButton.addEventListener("click", () => {
          window.location.href = `/log?pantry=${encodeURIComponent(el.dataset.id)}`;
        });
      }
    });
  }

  function renderPrepared(data) {
    const total = Number(data.total_portions || 0);
    const expired = Number(data.expired_portions || 0);
    $("preparedCount").textContent =
      `${king.formatNumber(total)} usable portion${total === 1 ? "" : "s"}` +
      (expired > 0
        ? ` · ${king.formatNumber(expired)} expired`
        : ` across ${data.total_batches || 0} batch${data.total_batches === 1 ? "" : "es"}`);
    if (!preparedItems.length) {
      $("preparedRoot").innerHTML =
        '<p class="empty-inline">Cook a recipe with surplus portions and they will appear here.</p>';
      return;
    }
    $("preparedRoot").innerHTML = preparedItems.map((item) => {
      const storage = item.frozen ? "Frozen" : "Fridge";
      const detail = item.expired
        ? `Expired ${king.escapeHTML(item.expires_on)} · update or discard`
        : `${storage} · use by ${king.escapeHTML(item.expires_on || "not set")}`;
      return `<button class="prepared-row${item.expired ? " expired" : ""}" type="button" data-prepared-id="${item.id}">
        <span class="prepared-row-icon"><i data-lucide="${item.expired ? "triangle-alert" : item.frozen ? "snowflake" : "package-check"}"></i></span>
        <span class="prepared-row-main">
          <strong>${king.escapeHTML(item.recipe_name)}</strong>
          <small>${detail}</small>
        </span>
        <span class="prepared-row-count num">${king.formatNumber(item.portions_remaining)}<small>portions</small></span>
        <i data-lucide="chevron-right" class="prepared-row-chevron"></i>
      </button>`;
    }).join("");
    $("preparedRoot").querySelectorAll("[data-prepared-id]").forEach((button) => {
      button.addEventListener("click", () => openPrepared(button.dataset.preparedId));
    });
  }

  function rowHTML(it) {
    const exp = it.expires_on ? `<span class="num dim">${it.expires_on}</span>` : `<span class="dim">—</span>`;
    const portioned = it.portion_quantity != null;
    const portionSummary = portioned
      ? `<small>${formatQty(it.portions_remaining)} portion${Number(it.portions_remaining) === 1 ? "" : "s"} · ` +
        `${formatQty(it.portion_quantity)} ${king.escapeHTML(it.unit)} each</small>`
      : "";
    const nutritionSummary = it.nutrition_available
      ? `<small class="p-nutrition">${[
          it.kcal_100g != null ? `${Math.round(it.kcal_100g)} kcal` : null,
          it.protein_100g != null ? `${formatQty(it.protein_100g)}g P` : null,
        ].filter(Boolean).join(" · ")} / 100 g</small>`
      : "";
    const usePortion = portioned
      ? `<button class="icon-btn p-use-portion" type="button" title="Use one portion" ` +
        `aria-label="Use one portion of ${king.escapeHTML(it.display_name)}"><i data-lucide="circle-minus"></i></button>`
      : "";
    const logFood = it.nutrition_available
      ? `<button class="icon-btn p-log-food" type="button" title="Log food" ` +
        `aria-label="Log ${king.escapeHTML(it.display_name)}"><i data-lucide="utensils"></i></button>`
      : "";
    return `<div class="pantry-row" data-id="${it.id}">
      <span class="p-name">${king.escapeHTML(it.display_name)}</span>
      <span class="p-qty num"><span>${formatQty(it.quantity)} <span class="dim">${king.escapeHTML(it.unit)}</span></span>${portionSummary}${nutritionSummary}</span>
      <span class="p-exp">${exp}</span>
      <span class="p-actions">
        ${logFood}
        ${usePortion}
        <button class="icon-btn p-edit" type="button" title="Edit" aria-label="Edit ${king.escapeHTML(it.display_name)}"><i data-lucide="pencil"></i></button>
        <button class="icon-btn p-del" type="button" title="Remove" aria-label="Remove ${king.escapeHTML(it.display_name)}"><i data-lucide="trash-2"></i></button>
      </span>
    </div>`;
  }

  function formatQty(q) {
    if (q == null) return "—";
    const value = Number(q);
    if (!Number.isFinite(value)) return "—";
    return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  function updatePortionHint() {
    const quantity = Number.parseFloat($("pantryQty").value);
    const portions = Number.parseFloat($("pantryPortions").value);
    $("pantryPortionHint").textContent =
      Number.isFinite(quantity) && quantity > 0 &&
      Number.isFinite(portions) && portions > 0
        ? `${formatQty(quantity / portions)} ${$("pantryUnit").value} each`
        : "";
  }

  function openModal() { king.openModal("pantryModal"); }
  function closeModal() {
    king.closeModal("pantryModal");
    resetModal();
  }
  function resetModal() {
    editingId = null;
    $("pantryRaw").value = "";
    $("pantryName").value = "";
    $("pantryQty").value = "";
    $("pantryUnit").value = "g";
    $("pantryPortions").value = "";
    $("pantryExp").value = "";
    $("pantryAutoExp").textContent = "";
    updatePortionHint();
    $("pantryResult").textContent = "";
    $("pantryModalTitle").textContent = "Add to pantry";
  }

  function openEdit(id) {
    const row = pantryItems.find(item => String(item.id) === String(id));
    if (!row) return;
    editingId = id;
    $("pantryModalTitle").textContent = "Edit pantry item";
    $("pantryName").value = row.display_name;
    $("pantryQty").value = row.quantity;
    $("pantryUnit").value = row.unit;
    $("pantryPortions").value =
      row.portions_remaining == null
        ? ""
        : String(Math.round(Number(row.portions_remaining) * 1000) / 1000);
    $("pantryExp").value = row.expires_on || "";
    updatePortionHint();
    openModal();
  }

  async function save() {
    const raw = $("pantryRaw").value.trim();
    const name = $("pantryName").value.trim();
    const qty = parseFloat($("pantryQty").value);
    const unit = $("pantryUnit").value;
    const portionInput = $("pantryPortions").value.trim();
    const portions = portionInput ? Number.parseFloat(portionInput) : null;
    const exp  = $("pantryExp").value || null;
    let body;
    if (editingId) {
      body = { display_name: name, quantity: qty, unit, portions, expires_on: exp };
    } else {
      body = raw && !name
        ? { raw, portions }
        : { display_name: name, quantity: qty, unit, portions, expires_on: exp };
    }
    const url = editingId ? `/api/pantry/${editingId}` : `/api/pantry`;
    const method = editingId ? "PATCH" : "POST";
    $("pantrySave").disabled = true;
    $("pantryResult").className = "result";
    $("pantryResult").textContent = "Saving…";
    try {
      await king.fetchJSON(url, { method, body: JSON.stringify(body) });
      closeModal();
      load();
    } catch (e) {
      $("pantryResult").className = "result err";
      $("pantryResult").textContent = e.data?.error || e.message;
    } finally {
      $("pantrySave").disabled = false;
    }
  }

  async function removeItem(id) {
    if (!confirm("Remove this item from your pantry?")) return;
    try {
      await king.fetchJSON(`/api/pantry/${id}`, { method: "DELETE" });
      load();
    } catch (e) { king.toast(e.message, "error"); }
  }

  async function consumePortion(id) {
    const item = pantryItems.find(row => String(row.id) === String(id));
    if (!item) return;
    const amount = Math.min(
      Number(item.quantity),
      Number(item.portion_quantity)
    );
    if (!confirm(
      `Use ${formatQty(amount)} ${item.unit} of ${item.display_name}?`
    )) return;
    try {
      const result = await king.fetchJSON(
        `/api/pantry/${id}/consume-portion`,
        { method: "POST", body: "{}" }
      );
      king.toast(
        `Used ${formatQty(result.item.consumed_quantity)} ${result.item.unit}.`,
        "success"
      );
      await load();
    } catch (e) {
      king.toast(e.message, "error");
    }
  }

  function openPrepared(id) {
    const item = preparedItems.find((row) => String(row.id) === String(id));
    if (!item) return;
    editingPreparedId = item.id;
    $("preparedModalTitle").textContent = item.recipe_name;
    $("preparedQty").value = item.portions_remaining;
    $("preparedExpiry").value = item.expires_on || "";
    $("preparedFrozen").checked = !!item.frozen;
    $("preparedResult").textContent = "";
    king.openModal("preparedModal");
  }

  function closePrepared() {
    king.closeModal("preparedModal");
    editingPreparedId = null;
  }

  async function savePrepared() {
    if (!editingPreparedId) return;
    const body = {
      portions_remaining: Number($("preparedQty").value),
      expires_on: $("preparedExpiry").value,
      frozen: $("preparedFrozen").checked,
    };
    $("preparedSave").disabled = true;
    try {
      await king.fetchJSON(`/api/prepared/${editingPreparedId}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
      closePrepared();
      await load();
    } catch (error) {
      $("preparedResult").className = "result err";
      $("preparedResult").textContent = error.message;
    } finally {
      $("preparedSave").disabled = false;
    }
  }

  async function discardPrepared() {
    if (!editingPreparedId || !confirm("Discard these prepared portions?")) return;
    try {
      await king.fetchJSON(`/api/prepared/${editingPreparedId}`, {
        method: "PATCH",
        body: JSON.stringify({ discard: true }),
      });
      closePrepared();
      await load();
    } catch (error) {
      $("preparedResult").className = "result err";
      $("preparedResult").textContent = error.message;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("addPantryBtn").addEventListener("click", () => { resetModal(); openModal(); });
    const fab = $("addPantryFab");
    if (fab) fab.addEventListener("click", () => { resetModal(); openModal(); });
    $("pantryCancel").addEventListener("click", closeModal);
    $("pantryCloseBtn").addEventListener("click", closeModal);
    $("pantrySave").addEventListener("click", save);
    ["pantryQty", "pantryPortions"].forEach((id) => {
      $(id).addEventListener("input", updatePortionHint);
    });
    $("pantryUnit").addEventListener("change", updatePortionHint);
    $("preparedCloseBtn").addEventListener("click", closePrepared);
    $("preparedCancel").addEventListener("click", closePrepared);
    $("preparedSave").addEventListener("click", savePrepared);
    $("preparedDiscard").addEventListener("click", discardPrepared);
    $("preparedFrozen").addEventListener("change", () => {
      $("preparedExpiry").value = king.addDays(
        king.isoToday(),
        $("preparedFrozen").checked
          ? king.frozenShelfLifeDays
          : king.preparedShelfLifeDays
      );
    });
    // Auto-fill name when raw line typed; show estimated expiry
    $("pantryRaw").addEventListener("blur", async () => {
      const raw = $("pantryRaw").value.trim();
      if (!raw) return;
      // Use a tiny POST to /api/pantry/parse if we ever build one; for now,
      // just hint at expiry estimate locally.
      $("pantryAutoExp").textContent = "Expiry will be auto-estimated from category if you don't set one.";
    });
    load();
  });
})();
