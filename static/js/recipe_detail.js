// /recipes/<id> — detail page. Read recipe, render in field-journal layout.
// Honors settings_kv.translation_mode: hover | side_by_side | italian_only.
(function () {
  const root = document.getElementById("recipeRoot");
  const rid = root?.dataset.rid;
  let recipe = null;
  let translationMode = "hover";

  async function load() {
    try {
      const [r, settings] = await Promise.all([
        king.fetchJSON(`/api/recipes/${rid}`),
        king.fetchJSON("/api/settings"),
      ]);
      recipe = r;
      translationMode = settings.kv?.translation_mode?.value || "hover";
      document.documentElement.dataset.translationMode = translationMode;
      render();
    } catch (e) {
      root.innerHTML = `<p class="result err">${king.escapeHTML(e.message)}</p>`;
    }
  }

  function render() {
    const r = recipe;
    const titleHTML = renderTitle(r);
    const macroStrip = renderMacroStrip(r);
    const nutritionTrust = renderNutritionTrust(r);
    const ingredientsHTML = renderIngredients(r);
    const stepsHTML = renderSteps(r);
    const feedbackHTML = renderFeedback(r);
    const notesHTML = r.notes ? `<div style="margin-top: var(--sp-4);"><p class="eyebrow">Notes</p><p class="dim">${king.escapeHTML(r.notes)}</p></div>` : "";
    const sourceHTML = r.source_url ? `<p class="hint" style="margin-top: var(--sp-3);">Source: <a href="${king.escapeHTML(r.source_url)}" target="_blank" rel="noreferrer">${king.escapeHTML(r.source_url)}</a></p>` : "";
    const favHTML = `<button class="btn" id="favBtn" type="button"><i data-lucide="bookmark"></i>${r.favorite ? "Favorited" : "Favorite"}</button>`;
    const preparedHTML = Number(r.prepared_portions || 0) > 0
      ? `<div class="prepared-banner"><i data-lucide="package-check"></i><span><strong>${king.formatNumber(r.prepared_portions)} portion${Number(r.prepared_portions) === 1 ? "" : "s"} ready</strong><small>Use these before cooking another batch.</small></span></div>`
      : "";

    root.innerHTML = `
      <div class="recipe-detail-head">
        <div>${titleHTML}</div>
        <div class="page-head-actions">
          ${favHTML}
          <button class="btn" id="editBtn" type="button"><i data-lucide="pencil"></i>Edit</button>
          <button class="btn btn-danger" id="deleteBtn" type="button"><i data-lucide="archive"></i>Archive</button>
        </div>
      </div>
      <hr class="hr-dotted">
      ${macroStrip}
      ${nutritionTrust}
      ${preparedHTML}
      <div class="recipe-body">
        <div>
          <p class="section-eyebrow">Ingredients</p>
          <hr class="hr-dotted">
          ${ingredientsHTML}
        </div>
        <div>
          <p class="section-eyebrow">Steps</p>
          <hr class="hr-dotted">
          ${stepsHTML}
        </div>
      </div>
      ${notesHTML}
      ${sourceHTML}
      ${feedbackHTML}

      <hr class="hr-dotted" style="margin-top: var(--sp-6);">
      <div class="recipe-cta">
        <button class="btn btn-primary" id="cookNowBtn" type="button"><i data-lucide="cooking-pot"></i>Start cooking</button>
        ${Number(r.prepared_portions || 0) > 0
          ? '<button class="btn" id="usePreparedBtn" type="button"><i data-lucide="package-check"></i>Use prepared</button>'
          : ""}
        <button class="btn" id="planSlotBtn" type="button"><i data-lucide="calendar-plus"></i>Plan a meal</button>
      </div>

      <!-- Plan-into-slot picker (small inline modal) -->
      <div class="modal" id="planSlotModal" hidden role="dialog" aria-modal="true" aria-labelledby="planSlotTitle">
        <div class="modal-card">
          <div class="modal-head">
            <h2 id="planSlotTitle">Plan into a slot</h2>
            <button class="icon-btn modal-x" id="planSlotClose" type="button" aria-label="Close"><i data-lucide="x"></i></button>
          </div>
          <p class="dim" style="font-size: var(--fs-sm);">When do you want to cook this?</p>
          <div class="field-row" style="margin-top: var(--sp-3);">
            <div class="field"><label for="planSlotDate">Day</label><input type="date" id="planSlotDate"></div>
            <div class="field">
              <label for="planSlotSlot">Slot</label>
              <select id="planSlotSlot">
                <option value="breakfast">Breakfast</option>
                <option value="lunch">Lunch</option>
                <option value="dinner" selected>Dinner</option>
                <option value="snack">Snack</option>
              </select>
            </div>
            <div class="field"><label for="planSlotServings">Portions</label><input type="number" id="planSlotServings" min="0.1" max="99" step="0.5" value="1"></div>
          </div>
          <div class="page-head-actions" style="justify-content: flex-end; margin-top: var(--sp-3);">
            <button class="btn btn-ghost" id="planSlotCancel">Cancel</button>
            <button class="btn btn-primary" id="planSlotConfirm">Add to plan</button>
          </div>
          <div class="result" id="planSlotResult"></div>
        </div>
      </div>
    `;
    document.getElementById("favBtn").addEventListener("click", toggleFav);
    document.getElementById("deleteBtn").addEventListener("click", deleteRecipe);
    document.getElementById("editBtn").addEventListener("click", () => {
      window.location.href = `/recipes?edit=${rid}`;
    });
    document.getElementById("completeNutritionBtn")?.addEventListener("click", () => {
      window.location.href = `/recipes?edit=${rid}`;
    });
    document.getElementById("cookNowBtn").addEventListener("click", startGuidedCook);
    document.getElementById("usePreparedBtn")?.addEventListener("click", usePreparedNow);
    document.getElementById("planSlotBtn").addEventListener("click", openPlanSlot);
    document.getElementById("planSlotClose").addEventListener("click", () => king.closeModal("planSlotModal"));
    document.getElementById("planSlotCancel").addEventListener("click", () => king.closeModal("planSlotModal"));
    document.getElementById("planSlotConfirm").addEventListener("click", confirmPlanSlot);
    wireFeedback();
  }

  function likelySlot() {
    const h = new Date().getHours();
    if (h >= 5 && h < 10) return "breakfast";
    if (h >= 10 && h < 15) return "lunch";
    if (h >= 15 && h < 21) return "dinner";
    return "snack";
  }

  function startGuidedCook() {
    const params = new URLSearchParams({
      date: king.isoToday(),
      slot: likelySlot(),
      servings: "1",
    });
    window.location.href = `/recipes/${rid}/cook?${params.toString()}`;
  }

  // Log a stored portion without forcing the guided fresh-cook flow.
  function usePreparedNow() {
    const slot = likelySlot();
    const date = king.isoToday();
    king.openCook({
      date,
      slot,
      recipeId: Number(rid),
      name: recipe.name,
      servings: 1,
      recipeYield: recipe.servings || 1,
      preparedPortions: recipe.prepared_portions || 0,
      onComplete: load,
    });
  }

  function renderFeedback(r) {
    const feedback = r.feedback || { rating: null, preference: "neutral" };
    const status = feedback.preference === "avoid"
      ? "Avoided in automatic plans"
      : feedback.preference === "make_again"
        ? "Favored in automatic plans"
        : feedback.rating
          ? `Rated ${feedback.rating}/5`
          : "Not rated";
    return `<section class="recipe-feedback-panel" aria-labelledby="recipeFeedbackTitle">
      <div class="recipe-feedback-head">
        <div>
          <p class="section-eyebrow">Your take</p>
          <h2 id="recipeFeedbackTitle">Plan feedback</h2>
        </div>
        <span class="dim" id="recipeFeedbackStatus">${king.escapeHTML(status)}</span>
      </div>
      <div class="recipe-feedback-controls">
        <div class="rating-control" id="detailFeedbackStars" aria-label="Recipe rating"></div>
        <button class="icon-btn" id="detailClearRating" type="button"
                aria-label="Clear rating" title="Clear rating"
                ${feedback.rating ? "" : "disabled"}>
          <i data-lucide="eraser"></i>
        </button>
        <div class="segmented feedback-preference" role="group" aria-label="Planning preference">
          ${[
            ["make_again", "repeat-2", "Make again"],
            ["neutral", "minus", "Neutral"],
            ["avoid", "ban", "Avoid"],
          ].map(([value, icon, label]) => `
            <button type="button" data-detail-preference="${value}"
                    class="${feedback.preference === value ? "active" : ""}"
                    aria-pressed="${feedback.preference === value}">
              <i data-lucide="${icon}"></i>${label}
            </button>`).join("")}
        </div>
      </div>
    </section>`;
  }

  function wireFeedback() {
    const feedback = recipe.feedback || { rating: null, preference: "neutral" };
    king.mountRatingControl(
      document.getElementById("detailFeedbackStars"),
      feedback.rating,
      (rating) => saveFeedback({ rating })
    );
    document.getElementById("detailClearRating")?.addEventListener(
      "click",
      () => saveFeedback({ rating: null })
    );
    document.querySelectorAll("[data-detail-preference]").forEach((button) => {
      button.addEventListener("click", () => saveFeedback({
        preference: button.dataset.detailPreference,
      }));
    });
  }

  async function saveFeedback(changes) {
    const current = recipe.feedback || { rating: null, preference: "neutral" };
    document.querySelectorAll(
      "#recipeRoot .rating-star, #recipeRoot [data-detail-preference], " +
      "#detailClearRating"
    ).forEach((button) => { button.disabled = true; });
    try {
      recipe.feedback = await king.saveRecipeFeedback(rid, {
        ...current,
        ...changes,
      });
      render();
      king.toast("Planning feedback updated.", "success");
    } catch (error) {
      king.toast(error.message, "error");
      render();
    }
  }

  // ---- "Plan into slot" — set recipe_id for date+slot, status stays planned ----
  function openPlanSlot() {
    document.getElementById("planSlotDate").value = king.isoToday();
    king.openModal("planSlotModal");
  }
  async function confirmPlanSlot() {
    const date = document.getElementById("planSlotDate").value;
    const slot = document.getElementById("planSlotSlot").value;
    const servings = Number(document.getElementById("planSlotServings").value);
    const out  = document.getElementById("planSlotResult");
    if (!date) { out.className = "result err"; out.textContent = "Pick a day."; return; }
    try {
      await king.fetchJSON(`/api/plan/${date}/${slot}`, {
        method: "PATCH", body: JSON.stringify({ recipe_id: rid, servings }),
      });
      king.closeModal("planSlotModal");
      king.toast(`Added to ${date} ${slot}.`, "success");
    } catch (e) {
      out.className = "result err";
      out.textContent = e.message;
    }
  }

  function renderTitle(r) {
    if (translationMode === "italian_only" && r.name_it) {
      return `<h1 class="recipe-title">${king.escapeHTML(r.name_it)}</h1>` +
             `<p class="recipe-title-it">${king.escapeHTML(r.name)}</p>`;
    }
    if (translationMode === "side_by_side" && r.name_it) {
      return `<h1 class="recipe-title">${king.escapeHTML(r.name)}</h1>` +
             `<p class="recipe-title-it">${king.escapeHTML(r.name_it)}</p>`;
    }
    // hover (default)
    return `<h1 class="recipe-title">${king.escapeHTML(r.name)}</h1>` +
           (r.name_it ? `<p class="recipe-title-it" title="${king.escapeHTML(r.name_it)}">${king.escapeHTML(r.name_it)}</p>` : "");
  }

  function renderMacroStrip(r) {
    const cell = (label, num, unit = "") =>
      `<div class="macro-cell"><span class="num">${num != null ? num : "—"}</span><span class="dim">${label}${unit ? " · " + unit : ""}</span></div>`;
    return `<div class="macro-strip">
      ${cell("kcal", r.kcal != null ? Math.round(r.kcal) : null)}
      ${cell("protein", r.protein_g != null ? Math.round(r.protein_g) : null, "g")}
      ${cell("carbs", r.carbs_g != null ? Math.round(r.carbs_g) : null, "g")}
      ${cell("fat", r.fat_g != null ? Math.round(r.fat_g) : null, "g")}
      ${cell("time", r.total_time_min != null ? r.total_time_min : null, "min")}
      ${cell("servings", r.servings)}
    </div>`;
  }

  function nutritionBadge(item, compact = false) {
    const status = item.nutrition_status;
    if (status && status !== "counted") {
      const statusMeta = {
        missing_amount: ["Amount needed", "Add an amount to count this ingredient"],
        unknown_unit: ["Unit not supported", "Use grams or another supported unit"],
        no_match: ["Match needed", "Choose the correct food match"],
        no_nutrition: ["No nutrient data", "The selected source has no nutrient values"],
      }[status] || ["Review needed", "Nutrition is incomplete"];
      return `<span class="confidence-badge confidence-low" ` +
        `title="${king.escapeHTML(statusMeta[1])}">${king.escapeHTML(statusMeta[0])}</span>`;
    }
    const confidence = ["high", "medium", "low", "unknown"].includes(
      item.nutrition_confidence
    ) ? item.nutrition_confidence : "unknown";
    const source = {
      usda: "USDA",
      off: "Open Food Facts",
      user: "Reviewed",
      manual: "Manual",
      summary: "Aggregate",
      unknown: "No source",
    }[item.nutrition_source] || "No source";
    const basis = (item.nutrition_basis || "unknown").replaceAll("_", " ");
    const label = compact ? `${source} · ${confidence}` : `${confidence} confidence`;
    return `<span class="confidence-badge confidence-${confidence}" ` +
      `title="${king.escapeHTML(`${source}; ${confidence} confidence; ${basis}`)}">` +
      `${king.escapeHTML(label)}</span>`;
  }

  function renderNutritionTrust(r) {
    const summary = r.nutrition_summary || {
      confidence: "unknown",
      sourced: 0,
      total: r.ingredients?.length || 0,
    };
    if (summary.complete) {
      return `<div class="nutrition-trust-summary">
        <span class="label-tag">Nutrition per serving</span>
        ${nutritionBadge({
          nutrition_source: "summary",
          nutrition_confidence: summary.confidence,
          nutrition_basis: "lowest ingredient confidence",
          nutrition_status: "counted",
        })}
        <span class="dim">${summary.counted}/${summary.total} ingredients counted</span>
      </div>`;
    }
    const issueParts = [
      summary.missing_amount ? `${summary.missing_amount} need amounts` : null,
      summary.unknown_unit ? `${summary.unknown_unit} need units` : null,
      summary.no_match ? `${summary.no_match} need matches` : null,
      summary.no_nutrition ? `${summary.no_nutrition} lack data` : null,
    ].filter(Boolean).join(" · ");
    return `<div class="nutrition-completion-banner">
      <i data-lucide="triangle-alert"></i>
      <span>
        <strong>${summary.empty ? "Nutrition not calculated" : `${summary.counted}/${summary.total} ingredients counted`}</strong>
        <small>${king.escapeHTML(issueParts || "No ingredients have been added")}</small>
      </span>
      <button class="btn" id="completeNutritionBtn" type="button">
        <i data-lucide="list-checks"></i>Complete nutrition
      </button>
    </div>`;
  }

  function renderIngredients(r) {
    if (!r.ingredients?.length) return '<p class="dim">No ingredients yet.</p>';
    return r.ingredients.map((i) => renderIngredientLine(i)).join("");
  }

  function renderIngredientLine(i) {
    const qty = i.quantity != null ? formatQty(i.quantity) : "";
    const unit = i.unit || "";
    const en = i.display_name || "";
    const it = i.display_name_it || "";
    let label;
    if (translationMode === "italian_only" && it) {
      label = `<span title="${king.escapeHTML(en)}">${king.escapeHTML(it)}</span>`;
    } else if (translationMode === "side_by_side" && it && it.toLowerCase() !== en.toLowerCase()) {
      label = `<span>${king.escapeHTML(en)}</span><span class="dim" style="font-style: italic;"> / ${king.escapeHTML(it)}</span>`;
    } else if (it && it.toLowerCase() !== en.toLowerCase()) {
      label = `<span title="${king.escapeHTML(it)}" class="has-it">${king.escapeHTML(en)}</span>`;
    } else {
      label = king.escapeHTML(en);
    }
    return `<div class="ingredient-line${i.optional ? " optional" : ""}">
      <span class="ing-qty num">${king.escapeHTML(qty)}</span>
      <span class="ing-unit num dim">${king.escapeHTML(unit)}</span>
      <span class="ing-name">${label}</span>
      <span class="ing-trust">${nutritionBadge(i, true)}</span>
    </div>`;
  }

  function formatQty(q) {
    // Show clean fractions for halves/quarters
    if (Number.isInteger(q)) return String(q);
    const fracs = [[0.25, "¼"], [0.5, "½"], [0.75, "¾"], [0.33, "⅓"], [0.67, "⅔"]];
    for (const [v, sym] of fracs) {
      if (Math.abs(q - v) < 0.01) return sym;
      const w = Math.floor(q);
      if (Math.abs(q - w - v) < 0.01) return `${w} ${sym}`;
    }
    return q.toFixed(1).replace(/\.0$/, "");
  }

  function renderSteps(r) {
    if (!r.steps?.length) return `<p class="dim">No steps yet.</p>`;
    return `<ol class="recipe-steps num">${r.steps.map((s) =>
      `<li>${king.escapeHTML(s)}</li>`).join("")}</ol>`;
  }

  async function toggleFav() {
    try {
      const r = await king.fetchJSON(`/api/recipes/${rid}/favorite`, { method: "POST", body: "{}" });
      recipe.favorite = r.favorite;
      render();
      king.toast(r.favorite ? "Favorited." : "Removed from favorites.", "success");
    } catch (e) { king.toast(e.message, "error"); }
  }

  async function deleteRecipe() {
    if (!confirm(`Archive "${recipe.name}"? Existing meal history will be kept.`)) return;
    try {
      await king.fetchJSON(`/api/recipes/${rid}`, { method: "DELETE" });
      window.location.href = "/recipes";
    } catch (e) { king.toast(e.message, "error"); }
  }

  document.addEventListener("DOMContentLoaded", load);
})();
