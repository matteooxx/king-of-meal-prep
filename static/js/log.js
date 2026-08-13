// /log — daily macro rings + planned/ad-hoc lists.
(function () {
  const $ = (id) => document.getElementById(id);
  let currentDate = king.isoToday();
  let currentLog = null;
  let pantryFoods = [];
  let logMode = "pantry";
  let previewSequence = 0;

  function addDays(iso, n) {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }

  async function load() {
    $("logTitle").textContent = currentDate === king.isoToday() ? "Today" : currentDate;
    try {
      const d = await king.fetchJSON(`/api/log/${currentDate}`);
      render(d);
    } catch (e) {
      king.toast(e.message, "error");
    }
  }

  function render(d) {
    currentLog = d;
    $("logSub").textContent = d.date + (d.is_training_day ? " · training day" : " · rest day");
    document.querySelector('input[name="train"][value="0"]').checked = !d.is_training_day;
    document.querySelector('input[name="train"][value="1"]').checked = !!d.is_training_day;

    // Macro rings
    const macros = [
      { key: "kcal",      label: "kcal",    color: "var(--macro-kcal)" },
      { key: "protein_g", label: "protein", color: "var(--macro-protein)" },
      { key: "carbs_g",   label: "carbs",   color: "var(--macro-carbs)" },
      { key: "fat_g",     label: "fat",     color: "var(--macro-fat)" },
    ];
    // Compute "all 4 on target" so we can flag the day with a tiny celebration.
    const allOn = macros.every(m => {
      const cur = d.totals[m.key] || 0, tgt = d.target[m.key] || 1;
      const pct = (cur / tgt) * 100;
      return pct >= 90 && pct <= 110;
    });
    $("macroRings").innerHTML = macros.map(m => ringHTML(m, d.totals[m.key], d.target[m.key])).join("")
      + (allOn ? `<div class="day-on-target" role="status">✓ Day on target</div>` : "");

    // Planned
    if (!d.planned.length) {
      $("plannedList").innerHTML = `<p class="dim" style="padding: var(--sp-3);">No planned meals for ${d.date}.</p>`;
    } else {
      $("plannedList").innerHTML = d.planned.map(plannedRow).join("");
      $("plannedList").querySelectorAll("[data-cook]").forEach(b =>
        b.addEventListener("click", () => cookSlot(b.dataset.cook))
      );
    }

    // Ad-hoc
    if (!d.ad_hoc.length) {
      $("adHocList").innerHTML = `<p class="dim" style="padding: var(--sp-3);">No foods logged.</p>`;
    } else {
      $("adHocList").innerHTML = d.ad_hoc.map(adHocRow).join("");
      $("adHocList").querySelectorAll("[data-del]").forEach(b =>
        b.addEventListener("click", () => removeAdHoc(b.dataset.del))
      );
    }
  }

  function ringHTML(m, current, target) {
    // Four states (was binary): under / on-target ±10% / warn 10-30% over /
    // urgent 30%+ over. The on-target state shows a small ✓ inside the ring
    // so hitting a goal feels distinguishable from "almost there".
    current = current || 0; target = target || 1;
    const pct = (current / target) * 100;
    const r = 36, c = 2 * Math.PI * r;
    const dash = (Math.min(100, pct) / 100) * c;
    let cls = "macro-ring";
    let stroke = m.color;
    if (pct >= 90 && pct <= 110)        cls += " on-target";
    else if (pct > 110 && pct <= 130) { cls += " warn";    stroke = "var(--warn)"; }
    else if (pct > 130)               { cls += " over";    stroke = "var(--urgent)"; }
    else                                cls += " under";
    const checkmark = (pct >= 90 && pct <= 110)
      ? `<text x="40" y="60" text-anchor="middle" font-size="11" fill="var(--accent)">✓</text>`
      : "";
    return `<div class="${cls}">
      <svg viewBox="0 0 80 80">
        <circle class="ring-track" cx="40" cy="40" r="${r}"></circle>
        <circle class="ring-fill" cx="40" cy="40" r="${r}" style="stroke:${stroke}; stroke-dasharray:${c}; stroke-dashoffset:${c-dash}; transform: rotate(-90deg); transform-origin: center;"></circle>
        <text x="40" y="44" text-anchor="middle" class="num" font-size="14" fill="currentColor">${Math.round(current)}</text>
        ${checkmark}
      </svg>
      <span class="ring-label">${m.label}</span>
      <span class="num dim" style="font-size: var(--fs-xxs);">${Math.round(current)}/${Math.round(target)}</span>
    </div>`;
  }

  function plannedRow(p) {
    const action = p.status === "cooked"
      ? `<span class="dim" style="font-size: var(--fs-xxs);">cooked</span>`
      : `<button class="btn" data-cook="${p.slot}" type="button"><i data-lucide="${Number(p.prepared_portions || 0) >= Number(p.servings || 1) ? "package-check" : "cooking-pot"}"></i>Complete</button>`;
    const name = p.name || `<span class="dim">— no recipe —</span>`;
    return `<div class="recipe-row">
      <div>
        <div class="recipe-row-title"><span class="eyebrow" style="margin-right: 8px;">${p.slot}</span> ${king.escapeHTML(p.name||"—")}</div>
        <div class="recipe-row-stats num"><span>${p.kcal ? Math.round(p.kcal * Number(p.servings || 1)) : "—"} kcal · ${p.protein_g ? Math.round(p.protein_g * Number(p.servings || 1)) : "—"}g P · ${king.formatNumber(p.servings || 1)} portion${Number(p.servings || 1) === 1 ? "" : "s"}</span><span>${action}</span></div>
      </div>
    </div>`;
  }

  function adHocRow(a) {
    const m = [
      a.est_kcal ? `${Math.round(a.est_kcal)} kcal` : null,
      a.est_protein_g ? `${Math.round(a.est_protein_g)}g P` : null,
    ].filter(Boolean).join(" · ");
    const amount = a.food_quantity != null
      ? `${king.formatNumber(a.food_quantity)} ${king.escapeHTML(a.food_unit || "")}`
      : null;
    const source = {
      usda: "USDA",
      off: "Open Food Facts",
      user: "Manual",
    }[a.nutrition_source] || null;
    const ts = (a.logged_at || "").slice(11, 16);
    return `<div class="recipe-row">
      <div>
        <div class="recipe-row-title"><span class="eyebrow" style="margin-right: 8px;">${ts}</span>${king.escapeHTML(a.free_text || "—")}</div>
        <div class="recipe-row-stats num"><span>${[amount, m || "no macros", source].filter(Boolean).join(" · ")}</span><span><button class="icon-btn" data-del="${a.id}" type="button" aria-label="Delete entry" title="Delete entry"><i data-lucide="trash-2"></i></button></span></div>
      </div>
    </div>`;
  }

  async function cookSlot(slot) {
    const meal = currentLog?.planned?.find((item) => item.slot === slot);
    if (!meal?.recipe_id) return;
    king.openCook({
      date: currentDate,
      slot,
      recipeId: meal.recipe_id,
      name: meal.name,
      servings: meal.servings || 1,
      recipeYield: meal.recipe_servings || 1,
      preparedPortions: meal.prepared_portions || 0,
      onComplete: load,
    });
  }

  async function removeAdHoc(id) {
    try {
      await king.fetchJSON(`/api/log/ad-hoc/${id}`, { method: "DELETE" });
      load();
    } catch (e) { king.toast(e.message, "error"); }
  }

  function inferSlot() {
    // Heuristic: 5-10 = breakfast, 10-15 = lunch, 15-21 = dinner, else snack.
    // Matches what the user is most likely doing when they open the app.
    const h = new Date().getHours();
    if (h >= 5 && h < 10) return "breakfast";
    if (h >= 10 && h < 15) return "lunch";
    if (h >= 15 && h < 21) return "dinner";
    return "snack";
  }

  function openAdHoc() {
    $("ahSlot").value = inferSlot();
    king.openModal("adHocModal");
    if (logMode === "pantry" && pantryFoods.length) {
      updatePantryAmount();
      setTimeout(() => $("ahPantryItem").focus(), 50);
    } else {
      setLogMode("quick");
      setTimeout(() => $("ahFreeText").focus(), 50);
    }
  }
  function closeAdHoc() { king.closeModal("adHocModal"); }

  function setLogMode(next) {
    logMode = next === "pantry" && pantryFoods.length ? "pantry" : "quick";
    const pantry = logMode === "pantry";
    $("ahPantryMode").classList.toggle("active", pantry);
    $("ahPantryMode").setAttribute("aria-pressed", String(pantry));
    $("ahQuickMode").classList.toggle("active", !pantry);
    $("ahQuickMode").setAttribute("aria-pressed", String(!pantry));
    $("ahPantryFields").hidden = !pantry;
    $("ahQuickFields").hidden = pantry;
    $("ahManualMacros").hidden = pantry;
    $("ahServingsField").hidden = pantry;
    if (pantry) updateFoodPreview();
  }

  function selectedPantryFood() {
    return pantryFoods.find(
      (item) => String(item.id) === $("ahPantryItem").value
    );
  }

  function defaultFoodAmount(item) {
    if (!item) return { quantity: "", unit: "g" };
    if (Number(item.portion_quantity) > 0) {
      return {
        quantity: Math.round(Number(item.portion_quantity) * 1000) / 1000,
        unit: item.unit,
      };
    }
    if (item.unit === "kg") return { quantity: 100, unit: "g" };
    if (item.unit === "l") return { quantity: 250, unit: "ml" };
    if (item.unit === "g") {
      return { quantity: Math.min(100, Number(item.quantity)), unit: "g" };
    }
    if (item.unit === "ml") {
      return { quantity: Math.min(250, Number(item.quantity)), unit: "ml" };
    }
    if (item.nutrition_available && !item.nutrition_amount_available) {
      return { quantity: 100, unit: "g" };
    }
    return { quantity: 1, unit: item.unit };
  }

  function updatePantryAmount() {
    const amount = defaultFoodAmount(selectedPantryFood());
    $("ahFoodQuantity").value = amount.quantity;
    $("ahFoodUnit").value = amount.unit;
    updateFoodPreview();
  }

  async function updateFoodPreview() {
    if (logMode !== "pantry") return;
    const item = selectedPantryFood();
    const quantity = Number.parseFloat($("ahFoodQuantity").value);
    const unit = $("ahFoodUnit").value.trim();
    if (!item || !Number.isFinite(quantity) || quantity <= 0 || !unit) {
      $("ahFoodPreview").innerHTML =
        '<span class="nutrition-state nutrition-warning">Enter an amount</span>';
      return;
    }
    const sequence = ++previewSequence;
    $("ahFoodPreview").innerHTML = '<span class="dim">Calculating...</span>';
    try {
      const result = await king.fetchJSON("/api/log/pantry-preview", {
        method: "POST",
        body: JSON.stringify({
          pantry_item_id: item.id,
          quantity,
          unit,
        }),
      });
      if (sequence !== previewSequence) return;
      const n = result.nutrition;
      if (n.nutrition_status !== "counted") {
        $("ahFoodPreview").innerHTML =
          '<span class="nutrition-state nutrition-missing">Nutrition unavailable for this amount</span>';
        return;
      }
      const values = [
        n.kcal != null ? `${Math.round(n.kcal)} kcal` : null,
        n.protein_g != null ? `${king.formatNumber(n.protein_g)}g protein` : null,
        n.carbs_g != null ? `${king.formatNumber(n.carbs_g)}g carbs` : null,
        n.fat_g != null ? `${king.formatNumber(n.fat_g)}g fat` : null,
      ].filter(Boolean);
      const source = n.nutrition_source === "off"
        ? "Open Food Facts"
        : n.nutrition_source === "usda" ? "USDA" : "Saved profile";
      $("ahFoodPreview").innerHTML = `
        <span class="food-log-macros num">${king.escapeHTML(values.join(" · "))}</span>
        <small>${king.escapeHTML(source)}</small>`;
    } catch (error) {
      if (sequence !== previewSequence) return;
      $("ahFoodPreview").innerHTML =
        `<span class="nutrition-state nutrition-missing">${king.escapeHTML(error.message)}</span>`;
    }
  }

  async function loadPantryFoods() {
    try {
      const data = await king.fetchJSON("/api/pantry");
      pantryFoods = Object.values(data.buckets || {})
        .flat()
        .filter((item) => item.nutrition_available);
      const requested = new URLSearchParams(window.location.search).get("pantry");
      $("ahPantryItem").innerHTML = pantryFoods.length
        ? pantryFoods.map((item) => {
            const profile = [
              item.kcal_100g != null ? `${Math.round(item.kcal_100g)} kcal` : null,
              item.protein_100g != null ? `${king.formatNumber(item.protein_100g)}g P` : null,
            ].filter(Boolean).join(" · ");
            return `<option value="${item.id}">${king.escapeHTML(item.display_name)}${profile ? ` · ${king.escapeHTML(profile)}/100g` : ""}</option>`;
          }).join("")
        : '<option value="">No foods with nutrition</option>';
      $("ahPantryMode").disabled = !pantryFoods.length;
      if (requested && pantryFoods.some((item) => String(item.id) === requested)) {
        $("ahPantryItem").value = requested;
        setLogMode("pantry");
        openAdHoc();
        history.replaceState({}, "", "/log");
      } else if (!pantryFoods.length) {
        setLogMode("quick");
      } else {
        updatePantryAmount();
      }
    } catch (error) {
      pantryFoods = [];
      $("ahPantryMode").disabled = true;
      setLogMode("quick");
    }
  }

  async function saveAdHoc() {
    const body = {
      date: currentDate,
      slot: $("ahSlot").value,
    };
    if (logMode === "pantry") {
      const item = selectedPantryFood();
      body.pantry_item_id = item?.id;
      body.quantity = Number.parseFloat($("ahFoodQuantity").value);
      body.unit = $("ahFoodUnit").value.trim();
      if (!item || !Number.isFinite(body.quantity) || body.quantity <= 0 || !body.unit) {
        $("ahResult").className = "result err";
        $("ahResult").textContent = "Choose a food and enter an amount.";
        return;
      }
    } else {
      body.free_text = $("ahFreeText").value.trim();
      body.servings = parseFloat($("ahServings").value) || 1;
      for (const f of ["kcal", "protein", "carbs", "fat"]) {
        const el = $("ah" + f.charAt(0).toUpperCase() + f.slice(1));
        const v = parseFloat(el.value);
        if (!isNaN(v)) body[f === "protein" ? "protein_g" : f === "carbs" ? "carbs_g" : f === "fat" ? "fat_g" : f] = v;
      }
      if (!body.free_text) {
        $("ahResult").className = "result err";
        $("ahResult").textContent = "Type what you ate.";
        return;
      }
    }
    try {
      await king.fetchJSON("/api/log/ad-hoc", { method: "POST", body: JSON.stringify(body) });
      $("ahFreeText").value = "";
      $("ahKcal").value = $("ahProtein").value = $("ahCarbs").value = $("ahFat").value = "";
      $("ahResult").textContent = "";
      closeAdHoc(); load();
    } catch (e) {
      $("ahResult").className = "result err";
      $("ahResult").textContent = e.message;
    }
  }

  async function setTraining(value) {
    // Stamp today's existing meal_plan rows. If none, create a sentinel row.
    try {
      const log = await king.fetchJSON(`/api/log/${currentDate}`);
      if (log.planned.length) {
        for (const p of log.planned) {
          await king.fetchJSON(`/api/plan/${currentDate}/${p.slot}`, {
            method: "PATCH", body: JSON.stringify({ is_training_day: !!value }),
          });
        }
      } else {
        // No planned meals: create an empty placeholder so the toggle persists.
        await king.fetchJSON(`/api/plan/${currentDate}/snack`, {
          method: "PATCH", body: JSON.stringify({ is_training_day: !!value }),
        });
      }
      load();
    } catch (e) { king.toast(e.message, "error"); }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("prevDayBtn").addEventListener("click", () => { currentDate = addDays(currentDate, -1); load(); });
    $("nextDayBtn").addEventListener("click", () => { currentDate = addDays(currentDate,  1); load(); });
    $("todayBtn").addEventListener("click", () => { currentDate = king.isoToday(); load(); });
    $("adHocBtn").addEventListener("click", openAdHoc);
    const fab = $("adHocFab");
    if (fab) fab.addEventListener("click", openAdHoc);
    $("ahCancel").addEventListener("click", closeAdHoc);
    const xBtn = $("ahCloseBtn");
    if (xBtn) xBtn.addEventListener("click", closeAdHoc);
    $("ahSave").addEventListener("click", saveAdHoc);
    $("ahPantryMode").addEventListener("click", () => setLogMode("pantry"));
    $("ahQuickMode").addEventListener("click", () => setLogMode("quick"));
    $("ahPantryItem").addEventListener("change", updatePantryAmount);
    let previewTimer;
    ["ahFoodQuantity", "ahFoodUnit"].forEach((id) => {
      $(id).addEventListener("input", () => {
        previewSequence += 1;
        clearTimeout(previewTimer);
        $("ahFoodPreview").innerHTML = '<span class="dim">Calculating...</span>';
        previewTimer = setTimeout(updateFoodPreview, 250);
      });
    });
    document.querySelectorAll('input[name="train"]').forEach(r =>
      r.addEventListener("change", () => setTraining(r.value === "1"))
    );
    load();
    loadPantryFoods();
  });
})();
