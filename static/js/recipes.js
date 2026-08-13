// /recipes — list + filter + add modal + manual/URL form.
(function () {
  const $ = (id) => document.getElementById(id);
  let allItems = [];
  let activeFilter = "all";
  let editingRid = null;          // null = creating new, otherwise id being edited
  let formMode = "manual";
  let translationMode = "hover";  // refreshed on load via /api/settings
  let difficultyLabels = ["trivial", "easy", "medium", "hard", "project"];
  let ingredientDrafts = [];
  let ingredientSummary = null;
  let ingredientReviewActive = false;
  let allowIncompleteNutrition = false;

  // ---- list ----

  async function load() {
    try {
      // Fetch list + translation mode in parallel; mode flows through to the
      // shared renderRecipeTitle util.
      const [d, settings] = await Promise.all([
        king.fetchJSON("/api/recipes?limit=500"),
        king.fetchJSON("/api/settings"),
      ]);
      allItems = d.items || [];
      translationMode = settings.kv?.translation_mode?.value || "hover";
      difficultyLabels = settings.kv?.difficulty_labels?.value || difficultyLabels;
      configureDifficulty();
      render();
    } catch (e) {
      $("recipeList").innerHTML = `<p class="result err">${king.escapeHTML(e.message)}</p>`;
    }
  }

  function render() {
    const search = $("searchInput").value.trim().toLowerCase();
    let items = allItems;
    if (search) {
      items = items.filter((r) =>
        (r.name || "").toLowerCase().includes(search) ||
        (r.name_it || "").toLowerCase().includes(search) ||
        (r.cuisine || "").toLowerCase().includes(search)
      );
    }
    if (activeFilter === "favorites") items = items.filter((r) => r.favorite);
    else if (activeFilter === "quick") items = items.filter((r) => (r.total_time_min || 999) <= 30);
    else if (["breakfast","lunch","dinner","snack"].includes(activeFilter)) {
      items = items.filter((r) => (r.meal_slot || "").includes(activeFilter));
    }
    $("recipeCount").textContent = `${items.length} recipe${items.length === 1 ? "" : "s"}${activeFilter !== "all" ? ` · ${activeFilter}` : ""}`;
    if (!items.length) {
      $("recipeList").innerHTML = `<p class="dim" style="text-align:center; padding: var(--sp-7);">No recipes yet. Click <strong>+ Recipe</strong> to add one.</p>`;
      return;
    }
    $("recipeList").innerHTML = items.map(rowHTML).join("<hr class='hr-dotted'>");
  }

  function rowHTML(r) {
    // Use the shared title renderer so translation_mode (hover / side-by-
    // side / italian_only) flows through to the list, not just the detail
    // page. Audit H3: list previously hardcoded EN — IT regardless of mode.
    const titleHTML = king.renderRecipeTitle(r, translationMode);
    const fav = r.favorite ? '<span aria-hidden="true" class="fav-mark">⚑</span>' : "";
    const stats = [
      r.total_time_min ? `${r.total_time_min} min` : null,
      r.kcal ? `${Math.round(r.kcal)} kcal` : null,
      r.protein_g ? `${Math.round(r.protein_g)}g P` : null,
      r.cuisine || null,
      r.meal_slot || null,
      r.preference === "avoid"
        ? "avoid"
        : r.rating
          ? `rated ${r.rating}/5`
          : null,
      Number(r.ingredient_count || 0) === 0 ||
      Number(r.nutrition_count || 0) < Number(r.ingredient_count || 0)
        ? "nutrition incomplete"
        : null,
    ].filter(Boolean).join(" · ");
    const last = r.last_cooked_at ? `last ${r.last_cooked_at.slice(0, 10)}` : "never cooked";
    const prepared = Number(r.prepared_portions || 0) > 0
      ? `<span class="prepared-chip"><i data-lucide="package-check"></i>${king.formatNumber(r.prepared_portions)} ready</span>`
      : "";
    return `<a class="recipe-row" href="/recipes/${r.id}">
      <div class="recipe-row-title">${titleHTML} ${fav} ${prepared}</div>
      <div class="recipe-row-stats num"><span>${king.escapeHTML(stats)}</span><span class="dim">${last}</span></div>
    </a>`;
  }

  // ---- modal helpers ----

  function openModal(id) { king.openModal(id); }
  function closeModal(id) { king.closeModal(id); }

  // ---- add modal ----

  function openAdd() { openModal("addModal"); }
  function pickMode(mode) {
    closeModal("addModal");
    editingRid = null;
    formMode = mode;
    resetForm();
    $("formTitle").textContent = mode === "url" ? "Import from URL"
                              : mode === "generate" ? "Generate with Gemini"
                              : mode === "paste" ? "Import pasted recipe"
                              : "New recipe";
    $("urlBlock").hidden = mode !== "url";
    $("generateBlock").hidden = mode !== "generate";
    $("pasteBlock").hidden = mode !== "paste";
    openModal("formModal");
  }

  async function generateRecipe() {
    const p = document.getElementById("genPromptInput").value.trim();
    const out = document.getElementById("genResult");
    if (!p) { out.className = "result err"; out.textContent = "Type a prompt."; return; }
    out.className = "result"; out.textContent = "Generating…";
    document.getElementById("genGoBtn").disabled = true;
    try {
      const r = await king.fetchJSON("/api/recipes/generate", {
        method: "POST", body: JSON.stringify({ prompt: p }),
      });
      fillFromProposal(r.proposal);
      out.className = "result ok";
      out.textContent = "Generated. Review + Save.";
    } catch (e) {
      out.className = "result err";
      out.textContent = e.data?.error || e.message;
    } finally { document.getElementById("genGoBtn").disabled = false; }
  }

  async function importText() {
    const raw = $("pasteRecipeInput").value.trim();
    const out = $("pasteResult");
    if (!raw) {
      out.className = "result err";
      out.textContent = "Paste a recipe first.";
      return;
    }
    $("pasteImportBtn").disabled = true;
    out.className = "result";
    out.textContent = "Parsing...";
    try {
      const result = await king.fetchJSON("/api/recipes/from-text", {
        method: "POST",
        body: JSON.stringify({ text: raw }),
      });
      fillFromProposal(result.proposal);
      out.className = "result ok";
      out.textContent = "Parsed. Review the fields before saving.";
    } catch (error) {
      out.className = "result err";
      out.textContent = error.message;
    } finally {
      $("pasteImportBtn").disabled = false;
    }
  }

  function configureDifficulty() {
    const select = $("recipeDifficulty");
    const current = select.value;
    select.innerHTML = '<option value="">Not set</option>' +
      difficultyLabels.slice(0, 5).map((label, index) =>
        `<option value="${index + 1}">${index + 1} - ${king.escapeHTML(label)}</option>`
      ).join("");
    select.value = current;
  }

  // ---- form ----

  function resetForm() {
    $("urlInput").value = "";
    $("recipeName").value = "";
    $("recipeServings").value = 1;
    $("recipeTotalTime").value = "";
    $("recipeActiveTime").value = "";
    $("recipeCuisine").value = "";
    $("recipeMealSlot").value = "";
    $("recipeDifficulty").value = "";
    $("recipeEquipment").value = "";
    $("recipeIngredients").value = "";
    $("recipeIngredients").hidden = false;
    $("ingredientHint").hidden = false;
    $("ingredientReview").hidden = true;
    ingredientDrafts = [];
    ingredientSummary = null;
    ingredientReviewActive = false;
    allowIncompleteNutrition = false;
    $("formSaveBtn").textContent = "Save";
    $("recipeSteps").value = "";
    $("recipeNotes").value = "";
    $("urlResult").textContent = "";
    $("urlResult").className = "result";
    $("genPromptInput").value = "";
    $("genResult").textContent = "";
    $("genResult").className = "result";
    $("pasteRecipeInput").value = "";
    $("pasteResult").textContent = "";
    $("pasteResult").className = "result";
    $("formResult").textContent = "";
    $("formResult").className = "result";
  }

  function fillFromProposal(p) {
    $("recipeName").value = p.name || "";
    $("recipeServings").value = p.servings || 1;
    $("recipeTotalTime").value = p.total_time_min || "";
    $("recipeActiveTime").value = p.active_time_min || "";
    $("recipeCuisine").value = p.cuisine || "";
    $("recipeMealSlot").value = p.meal_slot || "";
    $("recipeDifficulty").value = p.difficulty || "";
    $("recipeEquipment").value = (p.equipment || []).join(", ");
    const ingredients = p.ingredients || [];
    $("recipeIngredients").value = ingredients.map(ingredientLine).join("\n");
    if (ingredients.some((item) => typeof item === "object")) {
      reviewIngredients(ingredients, true);
    } else {
      showRawIngredients(false);
    }
    $("recipeSteps").value = (p.steps || []).join("\n");
    $("recipeNotes").value = p.notes || "";
  }

  function ingredientLine(item) {
    if (typeof item === "string") return item;
    return [
      item.quantity != null ? item.quantity : "",
      item.unit || "",
      item.display_name || "",
    ].filter((part) => part !== "").join(" ");
  }

  function rawIngredientLines() {
    return $("recipeIngredients").value
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function collectIngredientRows() {
    return [...$("ingredientReviewList").querySelectorAll("[data-ingredient-row]")]
      .map((row) => {
        const quantityValue = row.querySelector("[data-ingredient-quantity]").value;
        return {
          display_name: row.querySelector("[data-ingredient-name]").value.trim(),
          quantity: quantityValue === "" ? null : Number.parseFloat(quantityValue),
          unit: row.querySelector("[data-ingredient-unit]").value.trim() || null,
          ingredient_key: row.dataset.ingredientKey || null,
          optional: row.querySelector("[data-ingredient-optional]").checked,
        };
      })
      .filter((item) => item.display_name);
  }

  function nutritionStatusMeta(status) {
    return {
      counted: ["Counted", "circle-check", "complete"],
      missing_amount: ["Amount needed", "circle-alert", "warning"],
      unknown_unit: ["Unit not supported", "scale", "warning"],
      no_match: ["Food match needed", "search-x", "missing"],
      no_nutrition: ["No nutrient data", "database-zap", "missing"],
    }[status] || ["Review needed", "circle-help", "missing"];
  }

  function ingredientSummaryHTML(summary) {
    if (!summary || summary.empty) {
      return '<span class="nutrition-review-total">No ingredients</span>';
    }
    const issues = [
      ["missing_amount", "amount"],
      ["unknown_unit", "unit"],
      ["no_match", "match"],
      ["no_nutrition", "data"],
    ].filter(([key]) => summary[key]).map(([key, label]) =>
      `<span>${summary[key]} ${label}</span>`
    ).join("");
    return `<span class="nutrition-review-total ${summary.complete ? "is-complete" : ""}">
      ${summary.counted}/${summary.total} counted
    </span>${issues ? `<span class="nutrition-review-issues">${issues}</span>` : ""}`;
  }

  function renderIngredientReview() {
    ingredientReviewActive = true;
    $("recipeIngredients").hidden = true;
    $("ingredientHint").hidden = true;
    $("ingredientReview").hidden = false;
    $("ingredientReviewSummary").innerHTML = ingredientSummaryHTML(ingredientSummary);
    $("ingredientReviewList").innerHTML = ingredientDrafts.map((item, index) => {
      const [statusLabel, statusIcon, statusClass] = nutritionStatusMeta(
        item.nutrition_status
      );
      const macros = item.nutrition_status === "counted"
        ? [
            item.kcal != null ? `${Math.round(item.kcal)} kcal` : null,
            item.protein_g != null ? `${king.formatNumber(item.protein_g)}g P` : null,
            item.carbs_g != null ? `${king.formatNumber(item.carbs_g)}g C` : null,
            item.fat_g != null ? `${king.formatNumber(item.fat_g)}g F` : null,
          ].filter(Boolean).join(" · ")
        : "";
      const source = {
        usda: "USDA",
        off: "Open Food Facts",
        user: "Manual",
        manual: "Unmatched",
        unknown: "Unmatched",
      }[item.nutrition_source] || "Unmatched";
      return `<article class="ingredient-review-row nutrition-${statusClass}"
                       data-ingredient-row="${index}"
                       data-ingredient-key="${king.escapeHTML(item.ingredient_key || "")}">
        <div class="ingredient-review-fields">
          <input type="number" step="any" min="0.000001" data-ingredient-quantity
                 aria-label="Ingredient amount" placeholder="Amount"
                 value="${item.quantity == null ? "" : king.escapeHTML(item.quantity)}">
          <input type="text" maxlength="30" data-ingredient-unit
                 aria-label="Ingredient unit" placeholder="g"
                 value="${king.escapeHTML(item.unit || "")}">
          <input type="text" maxlength="200" data-ingredient-name
                 aria-label="Ingredient name" placeholder="Ingredient"
                 value="${king.escapeHTML(item.display_name || "")}">
          <button class="icon-btn ingredient-match-btn" type="button"
                  aria-label="Choose food match" title="Choose food match">
            <i data-lucide="search"></i>
          </button>
          <button class="icon-btn ingredient-remove-btn" type="button"
                  aria-label="Remove ingredient" title="Remove ingredient">
            <i data-lucide="trash-2"></i>
          </button>
        </div>
        <div class="ingredient-review-meta">
          <span class="nutrition-state nutrition-${statusClass}">
            <i data-lucide="${statusIcon}"></i>${statusLabel}
          </span>
          <span class="num">${king.escapeHTML(macros)}</span>
          <span class="dim">${king.escapeHTML(source)}</span>
          <label class="ingredient-optional">
            <input type="checkbox" data-ingredient-optional ${item.optional ? "checked" : ""}>
            <span>Optional</span>
          </label>
        </div>
        <div class="ingredient-match-results" hidden></div>
      </article>`;
    }).join("");
    bindIngredientReview();
  }

  function markNutritionChanged(row) {
    row.classList.add("nutrition-stale");
    allowIncompleteNutrition = false;
    $("formSaveBtn").textContent = "Save";
  }

  function bindIngredientReview() {
    $("ingredientReviewList").querySelectorAll("[data-ingredient-row]").forEach((row) => {
      row.querySelectorAll("input").forEach((input) => {
        input.addEventListener("input", () => markNutritionChanged(row));
      });
      row.querySelector(".ingredient-match-btn").addEventListener(
        "click", () => loadIngredientMatches(row)
      );
      row.querySelector(".ingredient-remove-btn").addEventListener("click", async () => {
        const rows = collectIngredientRows();
        rows.splice(Number(row.dataset.ingredientRow), 1);
        allowIncompleteNutrition = false;
        await reviewIngredients(rows);
      });
    });
  }

  async function loadIngredientMatches(row) {
    const query = row.querySelector("[data-ingredient-name]").value.trim();
    if (!query) return;
    const target = row.querySelector(".ingredient-match-results");
    const button = row.querySelector(".ingredient-match-btn");
    button.disabled = true;
    target.hidden = false;
    target.innerHTML = '<span class="dim">Searching...</span>';
    try {
      const result = await king.fetchJSON(
        `/api/nutrition/search?q=${encodeURIComponent(query)}`
      );
      if (!result.items.length) {
        target.innerHTML = '<span class="dim">No matches found.</span>';
        return;
      }
      target.innerHTML = result.items.map((match) => {
        const macros = [
          match.kcal_100g != null ? `${Math.round(match.kcal_100g)} kcal` : null,
          match.protein_100g != null ? `${king.formatNumber(match.protein_100g)}g P` : null,
        ].filter(Boolean).join(" · ");
        return `<button type="button" class="ingredient-match-option"
                        data-match-key="${king.escapeHTML(match.ingredient_key)}">
          <span>${king.escapeHTML(match.display_name)}</span>
          <small>${king.escapeHTML(macros)} per 100 g · ${king.escapeHTML((match.nutrition_source || "").toUpperCase())}</small>
        </button>`;
      }).join("");
      target.querySelectorAll("[data-match-key]").forEach((option) => {
        option.addEventListener("click", async () => {
          allowIncompleteNutrition = false;
          $("formSaveBtn").textContent = "Save";
          row.dataset.ingredientKey = option.dataset.matchKey;
          await reviewIngredients(collectIngredientRows());
        });
      });
    } catch (error) {
      target.innerHTML = `<span class="result err">${king.escapeHTML(error.message)}</span>`;
    } finally {
      button.disabled = false;
    }
  }

  async function reviewIngredients(input = null, quiet = false) {
    const ingredients = input || (
      ingredientReviewActive ? collectIngredientRows() : rawIngredientLines()
    );
    const button = $("ingredientReviewBtn");
    button.disabled = true;
    try {
      const result = await king.fetchJSON("/api/nutrition/ingredients/preview", {
        method: "POST",
        body: JSON.stringify({ ingredients }),
      });
      ingredientDrafts = result.items || [];
      ingredientSummary = result.summary;
      renderIngredientReview();
      return result;
    } catch (error) {
      if (!quiet) {
        $("formResult").className = "result err";
        $("formResult").textContent = error.message;
      }
      return null;
    } finally {
      button.disabled = false;
    }
  }

  function showRawIngredients(preserveReview = true) {
    if (preserveReview && ingredientReviewActive) {
      $("recipeIngredients").value = collectIngredientRows()
        .map(ingredientLine)
        .join("\n");
    }
    ingredientReviewActive = false;
    ingredientDrafts = [];
    ingredientSummary = null;
    allowIncompleteNutrition = false;
    $("recipeIngredients").hidden = false;
    $("ingredientHint").hidden = false;
    $("ingredientReview").hidden = true;
    $("formSaveBtn").textContent = "Save";
  }

  function addIngredientRow() {
    const current = collectIngredientRows();
    ingredientDrafts = current.concat([{
      display_name: "",
      quantity: null,
      unit: "g",
      ingredient_key: "",
      optional: false,
      nutrition_status: "missing_amount",
    }]);
    ingredientSummary = null;
    allowIncompleteNutrition = false;
    renderIngredientReview();
    const name = $("ingredientReviewList")
      .lastElementChild?.querySelector("[data-ingredient-name]");
    name?.focus();
  }

  async function importUrl() {
    const url = $("urlInput").value.trim();
    if (!url) return;
    const btn = $("urlImportBtn");
    btn.disabled = true;
    $("urlResult").className = "result";
    $("urlResult").textContent = "Importing…";
    try {
      const r = await king.fetchJSON("/api/recipes/from-url", {
        method: "POST", body: JSON.stringify({ url }),
      });
      fillFromProposal(r.proposal);
      $("urlResult").className = "result ok";
      $("urlResult").textContent = r.used_llm
        ? "Parsed via Gemini (recipe-scrapers couldn't). Review + Save."
        : "Parsed. Review + Save.";
    } catch (e) {
      $("urlResult").className = "result err";
      $("urlResult").textContent = e.data?.error || e.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function save() {
    const preview = await reviewIngredients();
    if (!preview) return;
    const nutritionIncomplete = !preview.summary.complete;
    if (nutritionIncomplete && !allowIncompleteNutrition) {
      allowIncompleteNutrition = true;
      $("formSaveBtn").textContent = "Save with gaps";
      $("formResult").className = "result warn";
      $("formResult").textContent = preview.summary.empty
        ? "This recipe has no ingredients. Save again to confirm."
        : `${preview.summary.incomplete} ingredient${preview.summary.incomplete === 1 ? "" : "s"} will not count toward nutrients. Fix the highlighted rows or save again.`;
      return;
    }
    const stepLines = $("recipeSteps").value.split("\n").map((s) => s.trim()).filter(Boolean);
    const body = {
      name: $("recipeName").value.trim(),
      servings: parseInt($("recipeServings").value, 10) || 1,
      total_time_min: parseInt($("recipeTotalTime").value, 10) || null,
      active_time_min: parseInt($("recipeActiveTime").value, 10) || null,
      cuisine: $("recipeCuisine").value.trim() || null,
      meal_slot: $("recipeMealSlot").value || null,
      difficulty: parseInt($("recipeDifficulty").value, 10) || null,
      equipment: $("recipeEquipment").value.split(",").map((s) => s.trim()).filter(Boolean),
      notes: $("recipeNotes").value.trim() || null,
      ingredients: preview.items,
      accept_incomplete_nutrition: nutritionIncomplete,
      steps: stepLines,
    };
    if (!editingRid) {
      body.source = formMode === "generate" ? "llm" : formMode === "url" ? "url" : "manual";
      body.source_url = formMode === "url" ? ($("urlInput").value.trim() || null) : null;
    }
    if (!body.name) { $("formResult").className = "result err"; $("formResult").textContent = "Title required."; return; }
    const btn = $("formSaveBtn");
    btn.disabled = true;
    $("formResult").className = "result"; $("formResult").textContent = "Saving…";
    try {
      let id;
      if (editingRid) {
        await king.fetchJSON(`/api/recipes/${editingRid}`, { method: "PATCH", body: JSON.stringify(body) });
        id = editingRid;
      } else {
        const d = await king.fetchJSON("/api/recipes", { method: "POST", body: JSON.stringify(body) });
        id = d.id;
      }
      closeModal("formModal");
      window.location.href = `/recipes/${id}`;
    } catch (e) {
      $("formResult").className = "result err";
      $("formResult").textContent = e.data?.error || e.message;
    } finally {
      btn.disabled = false;
    }
  }

  // ---- wiring ----

  // ?edit=<rid> opens the form modal pre-populated with that recipe.
  // Triggered by the "Edit" button on /recipes/<id>.
  async function maybeOpenEdit() {
    const params = new URLSearchParams(window.location.search);
    const rid = params.get("edit");
    if (!rid) return;
    try {
      const r = await king.fetchJSON(`/api/recipes/${rid}`);
      editingRid = +rid;
      formMode = "edit";
      resetForm();
      $("formTitle").textContent = `Edit: ${r.name}`;
      $("urlBlock").hidden = true;
      $("generateBlock").hidden = true;
      $("pasteBlock").hidden = true;
      // Pre-fill from the loaded recipe (same shape as a URL import proposal)
      fillFromProposal({
        name: r.name,
        servings: r.servings,
        total_time_min: r.total_time_min,
        active_time_min: r.active_time_min,
        cuisine: r.cuisine,
        meal_slot: r.meal_slot,
        difficulty: r.difficulty,
        equipment: r.equipment || [],
        notes: r.notes,
        ingredients: (r.ingredients || []).map(i => ({
          quantity: i.quantity,
          unit: i.unit,
          display_name: i.display_name,
          ingredient_key: i.ingredient_key,
          optional: !!i.optional,
          kcal: i.kcal,
          protein_g: i.protein_g,
          carbs_g: i.carbs_g,
          fat_g: i.fat_g,
          fiber_g: i.fiber_g,
          nutrition_source: i.nutrition_source,
          nutrition_confidence: i.nutrition_confidence,
          nutrition_basis: i.nutrition_basis,
        })),
        steps: r.steps || [],
      });
      // meal_slot select needs explicit set
      if (r.meal_slot) $("recipeMealSlot").value = r.meal_slot;
      openModal("formModal");
    } catch (e) {
      king.toast(`Couldn't load recipe ${rid}: ${e.message}`, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("addRecipeBtn").addEventListener("click", openAdd);
    $("addModalClose").addEventListener("click", () => closeModal("addModal"));
    document.querySelectorAll(".add-option").forEach((b) => {
      if (b.disabled) return;
      b.addEventListener("click", () => pickMode(b.dataset.mode));
    });
    $("urlImportBtn").addEventListener("click", importUrl);
    $("genGoBtn").addEventListener("click", generateRecipe);
    $("pasteImportBtn").addEventListener("click", importText);
    $("ingredientReviewBtn").addEventListener("click", () => {
      allowIncompleteNutrition = false;
      reviewIngredients();
    });
    $("ingredientRecalculateBtn").addEventListener("click", () => {
      allowIncompleteNutrition = false;
      reviewIngredients();
    });
    $("ingredientRawBtn").addEventListener("click", () => showRawIngredients());
    $("ingredientAddBtn").addEventListener("click", addIngredientRow);
    $("recipeIngredients").addEventListener("input", () => {
      allowIncompleteNutrition = false;
      $("formSaveBtn").textContent = "Save";
    });
    $("formSaveBtn").addEventListener("click", save);
    $("formCancelBtn").addEventListener("click", () => closeModal("formModal"));
    $("searchInput").addEventListener("input", render);
    document.querySelectorAll(".filter-chip").forEach((c) => {
      c.addEventListener("click", () => {
        document.querySelectorAll(".filter-chip").forEach((x) => x.classList.remove("active"));
        c.classList.add("active");
        activeFilter = c.dataset.filter;
        render();
      });
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (!$("addModal").hidden) closeModal("addModal");
        if (!$("formModal").hidden) closeModal("formModal");
      }
    });
    load();
    maybeOpenEdit();
  });
})();
