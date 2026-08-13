// /today — single-day hero, full-width tap-to-cook buttons per slot, day-ribbon.
// Solves the audit's C1+C2+C4: the daily ritual surface the design doc
// originally specced as the mobile-week view.
(function () {
  const $ = (id) => document.getElementById(id);

  const SLOTS = ["breakfast", "lunch", "dinner", "snack"];
  const SLOT_LABELS = { breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner", snack: "Snack" };
  const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  let currentDate = king.isoToday();
  let recipeCount = null;     // cached on first load to decide momentum-panel
  let weekStart   = null;

  // ---- empty-state momentum panel (D2) ----
  // When recipes < 5 the user has nothing to plan with; replace the day hero
  // with a 3-step what-to-do-next panel until they're set up.
  function renderMomentum() {
    const root = $("todayRoot");
    root.innerHTML = `
      <section class="momentum">
        <p class="section-eyebrow">Get started</p>
        <hr class="hr-dotted">
        <p class="dim" style="margin-bottom: var(--sp-4);">You're set up but the planner needs a few recipes before it can help. Here's the fastest path:</p>
        <ol class="momentum-steps">
          <li>
            <div class="momentum-num num">1</div>
            <div class="momentum-body">
              <strong>Add 5–10 recipes.</strong>
              <p class="dim">Paste a URL — recipe-scrapers handles ~500 cooking sites. Or generate one with Gemini.</p>
              <div class="page-head-actions" style="margin-top: var(--sp-2);">
                <a class="btn btn-primary" href="/recipes">+ Add recipes</a>
              </div>
            </div>
          </li>
          <li>
            <div class="momentum-num num">2</div>
            <div class="momentum-body">
              <strong>Plan your week.</strong>
              <p class="dim">Once you have a few recipes, the solver fills 7 days × 4 slots based on your goals.</p>
            </div>
          </li>
          <li>
            <div class="momentum-num num">3</div>
            <div class="momentum-body">
              <strong>Cook one tonight.</strong>
              <p class="dim">Tap-to-cook deducts the pantry and counts toward your daily macros.</p>
            </div>
          </li>
        </ol>
      </section>`;
  }

  // ---- day ribbon ----
  function renderDayRibbon() {
    const today = king.isoToday();
    const monday = king.mondayOf(today);
    weekStart = monday;
    const items = [];
    for (let i = 0; i < 7; i++) {
      const d = king.addDays(monday, i);
      const isToday = d === today;
      const isCurrent = d === currentDate;
      const cls = ["day-pill"];
      if (isToday) cls.push("today");
      if (isCurrent) cls.push("active");
      items.push(`<button type="button" class="${cls.join(' ')}" data-day="${d}">
        <span class="day-name">${DAY_NAMES[i]}</span>
        <span class="day-num num">${d.slice(8)}</span>
      </button>`);
    }
    $("dayRibbon").innerHTML = items.join("");
    $("dayRibbon").querySelectorAll(".day-pill").forEach(b =>
      b.addEventListener("click", () => { currentDate = b.dataset.day; load(); })
    );
  }

  // ---- main load ----
  async function load() {
    renderDayRibbon();
    try {
      const [logData, recipesData] = await Promise.all([
        king.fetchJSON(`/api/log/${currentDate}`),
        recipeCount === null ? king.fetchJSON("/api/recipes?limit=5") : Promise.resolve(null),
      ]);
      if (recipeCount === null && recipesData) {
        recipeCount = (recipesData.items || []).length;
      }

      // Show momentum panel only when the user has truly nothing AND today has no plan.
      // (If they planned manually with 2 recipes, don't condescend.)
      const planned = logData.planned || [];
      if (recipeCount < 5 && planned.every(p => !p.recipe_id)) {
        renderMomentum();
        $("todayTitle").textContent = "The King of Meal Prep";
        $("todaySub").textContent = "Welcome — let's set up your first week.";
        return;
      }

      renderHero(logData);
    } catch (e) {
      $("todayRoot").innerHTML = `<p class="result err">${king.escapeHTML(e.message)}</p>`;
    }
  }

  function renderHero(d) {
    // Title: "Tonight" / "Today" / weekday name depending on which day we're viewing.
    const today = king.isoToday();
    let label;
    if (d.date === today) {
      const h = new Date().getHours();
      label = h >= 16 ? "Tonight" : h < 11 ? "Today" : "Today";
    } else {
      const dt = new Date(d.date + "T00:00:00Z");
      label = dt.toLocaleDateString("en-IE", { weekday: "long", day: "numeric", month: "short" });
    }
    $("todayTitle").textContent = label;
    $("todaySub").textContent =
      `${d.date}` +
      (d.is_training_day ? " · training day" : " · rest day");

    // Build the four slot cards, full-width, tap-to-cook.
    const slotsHTML = SLOTS.map(slot => {
      const p = (d.planned || []).find(x => x.slot === slot);
      return slotCardHTML(slot, p, d.date);
    }).join("");

    // Daily macro mini-strip below the slots
    const macros = ["kcal", "protein_g", "carbs_g", "fat_g"];
    const macroLabels = { kcal: "kcal", protein_g: "P", carbs_g: "C", fat_g: "F" };
    const macroStrip = `
      <div class="day-macro-strip">
        ${macros.map(k => {
          const cur = Math.round(d.totals[k] || 0);
          const tgt = Math.round(d.target[k] || 0);
          const pct = tgt ? Math.round((cur / tgt) * 100) : 0;
          const cls = pct >= 90 && pct <= 110 ? "on" : pct > 130 ? "over" : pct > 110 ? "warn" : "under";
          return `<div class="day-macro ${cls}">
            <span class="num">${cur}<span class="dim">/${tgt}</span></span>
            <span class="dim">${macroLabels[k]}</span>
          </div>`;
        }).join("")}
      </div>`;

    $("todayRoot").innerHTML = `
      <section class="day-slots">${slotsHTML}</section>
      ${macroStrip}
      <div class="page-head-actions" style="justify-content: space-between; margin-top: var(--sp-5);">
        <a class="btn btn-ghost" href="/log">See full log</a>
        <button class="btn" id="trainingToggleBtn" type="button">${d.is_training_day ? "✓ Training day" : "Mark training day"}</button>
      </div>`;
    wireSlotActions();
    $("trainingToggleBtn").addEventListener("click", () => toggleTraining(!d.is_training_day));
  }

  function slotCardHTML(slot, p, date) {
    if (!p || !p.recipe_id) {
      // Empty slot — link to /week to plan it
      return `<article class="day-slot empty">
        <header><span class="slot-label">${SLOT_LABELS[slot]}</span></header>
        <p class="dim" style="font-size: var(--fs-sm);">Nothing planned.</p>
        <a class="btn btn-ghost" href="/week" style="align-self: flex-start;">Plan a meal</a>
      </article>`;
    }
    const cooked = p.status === "cooked";
    const skipped = p.status === "skipped";
    const stats = [
      p.kcal ? `${Math.round(p.kcal * Number(p.servings || 1))} kcal` : null,
      p.protein_g ? `${Math.round(p.protein_g * Number(p.servings || 1))}g P` : null,
      p.servings ? `${king.formatNumber(p.servings)} portion${Number(p.servings) === 1 ? "" : "s"}` : null,
    ].filter(Boolean).join(" · ");
    const prepared = Number(p.prepared_portions || 0);
    const preparedBadge = prepared > 0
      ? `<span class="prepared-chip"><i data-lucide="package-check"></i>${king.formatNumber(prepared)} ready</span>`
      : "";
    const action = cooked
      ? `<button class="btn btn-ghost slot-undo" data-slot="${slot}" data-date="${date}" type="button"><i data-lucide="undo-2"></i>Undo</button>`
      : skipped
      ? `<button class="btn btn-ghost slot-reset" data-slot="${slot}" data-date="${date}" type="button"><i data-lucide="undo-2"></i>Reset</button>`
      : `<button class="btn btn-primary slot-cook"
          data-slot="${slot}" data-date="${date}" data-rid="${p.recipe_id}"
          data-name="${king.escapeHTML(p.name || "")}"
          data-servings="${p.servings || 1}"
          data-yield="${p.recipe_servings || 1}"
          data-prepared="${prepared}" data-version="${p.version || ""}" type="button">
          <i data-lucide="${prepared >= Number(p.servings || 1) ? "package-check" : "cooking-pot"}"></i>
          ${prepared >= Number(p.servings || 1) ? "Use prepared" : "Cook meal"}
        </button>`;
    const cls = "day-slot" + (cooked ? " cooked" : "") + (skipped ? " skipped" : "");
    return `<article class="${cls}">
      <header>
        <span class="slot-label">${SLOT_LABELS[slot]}</span>
        ${cooked ? `<span class="slot-status">${p.cook_mode === "prepared" ? "prepared" : "cooked"}</span>` : skipped ? '<span class="slot-status">skipped</span>' : preparedBadge}
      </header>
      <a href="/recipes/${p.recipe_id}" class="slot-name">${king.escapeHTML(p.name || '—')}</a>
      <p class="slot-stats num dim">${king.escapeHTML(stats)}</p>
      <div class="slot-actions">
        ${action}
        ${!cooked && !skipped ? `<button class="btn btn-ghost slot-skip" data-slot="${slot}" data-date="${date}" type="button"><i data-lucide="forward"></i>Skip</button>` : ''}
      </div>
    </article>`;
  }

  function wireSlotActions() {
    document.querySelectorAll(".slot-cook").forEach((button) => {
      button.addEventListener("click", () => {
        const servings = Number(button.dataset.servings || 1);
        const preparedPortions = Number(button.dataset.prepared || 0);
        if (preparedPortions + 0.000001 >= servings) {
          king.openCook({
            date: button.dataset.date,
            slot: button.dataset.slot,
            recipeId: Number(button.dataset.rid),
            name: button.dataset.name,
            servings,
            recipeYield: Number(button.dataset.yield || 1),
            preparedPortions,
            expectedVersion: Number(button.dataset.version || 0) || undefined,
            onComplete: load,
          });
          return;
        }
        const params = new URLSearchParams({
          date: button.dataset.date,
          slot: button.dataset.slot,
          servings: String(servings),
        });
        if (button.dataset.version) {
          params.set("version", button.dataset.version);
        }
        window.location.href =
          `/recipes/${button.dataset.rid}/cook?${params.toString()}`;
      });
    });
    document.querySelectorAll(".slot-skip").forEach(b =>
      b.addEventListener("click", () => updateStatus(b.dataset.date, b.dataset.slot, "skipped")));
    document.querySelectorAll(".slot-undo, .slot-reset").forEach(b =>
      b.addEventListener("click", () => updateStatus(b.dataset.date, b.dataset.slot, "planned")));
  }

  async function updateStatus(date, slot, status) {
    try {
      const payload = { status };
      if (status === "cooked") payload.idempotency_key = king.idempotencyKey();
      const result = await king.fetchJSON(`/api/plan/${date}/${slot}`, {
        method: "PATCH", body: JSON.stringify(payload),
      });
      const missing = result.pantry?.missing?.length || 0;
      const verb = status === "cooked"
        ? (missing ? `Cooked. ${missing} pantry item(s) were short.` : "Cooked. Pantry deducted.")
        : status === "skipped" ? "Skipped." : "Reset to planned.";
      king.toast(verb, "success");
      load();
    } catch (e) { king.toast(e.message, "error"); }
  }

  async function toggleTraining(on) {
    try {
      // Stamp every slot for this date so the training-day flag survives even
      // if the user reschedules later.
      const log = await king.fetchJSON(`/api/log/${currentDate}`);
      const slots = (log.planned || []).filter(p => p.slot).map(p => p.slot);
      if (!slots.length) {
        await king.fetchJSON(`/api/plan/${currentDate}/snack`, {
          method: "PATCH", body: JSON.stringify({ is_training_day: on }),
        });
      } else {
        for (const s of slots) {
          await king.fetchJSON(`/api/plan/${currentDate}/${s}`, {
            method: "PATCH", body: JSON.stringify({ is_training_day: on }),
          });
        }
      }
      load();
    } catch (e) { king.toast(e.message, "error"); }
  }

  // ---- plan-week preview (D5) ----
  let planProposal = null;

  async function openPlanPreview() {
    try {
      planProposal = await king.fetchJSON("/api/plan/week/proposal", {
        method: "POST",
        body: JSON.stringify({ start: weekStart }),
      });
      let generated = 0;
      let preserved = 0;
      for (const slots of Object.values(planProposal.plan || {})) {
        for (const item of Object.values(slots)) {
          if (!item) continue;
          if (item.preserved) preserved++;
          else generated++;
        }
      }
      $("planPreviewBody").innerHTML = `
        <span class="num">${weekStart}</span> to <span class="num">${king.addDays(weekStart, 6)}</span><br>
        <strong>${generated}</strong> new picks · <strong>${preserved}</strong> preserved ·
        <strong>${(planProposal.skipped || []).length}</strong> empty
      `;
      $("planPreviewSkipped").innerHTML = (planProposal.conflicts || []).length
        ? `<p class="hint">Preserved: ${(planProposal.conflicts || []).map((item) =>
            `${king.escapeHTML(item.date)} ${king.escapeHTML(item.slot)} (${king.escapeHTML(item.reason)})`
          ).join(" · ")}</p>`
        : "";
      $("planPreviewPicks").innerHTML = king.proposalPicksHTML(planProposal);
      king.openModal("planPreviewModal");
    } catch (e) { king.toast(e.message, "error"); }
  }

  async function confirmPlan() {
    if (!planProposal) return;
    $("planPreviewConfirm").disabled = true;
    try {
      const r = await king.fetchJSON("/api/plan/week/commit", {
        method: "POST",
        body: JSON.stringify({
          proposal_id: planProposal.proposal_id,
          expected_version: planProposal.expected_version,
        }),
      });
      king.closeModal("planPreviewModal");
      planProposal = null;
      const skippedCount = (r.skipped || []).length;
      king.toast(
        skippedCount
          ? `Planned ${r.saved} slots · ${skippedCount} skipped (no eligible recipe).`
          : `Planned ${r.saved} slots.`,
        "success"
      );
      // Reset cached recipe count so momentum panel re-evaluates
      recipeCount = null;
      load();
    } catch (e) {
      king.toast(e.message, "error");
    } finally {
      $("planPreviewConfirm").disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("planWeekBtn").addEventListener("click", openPlanPreview);
    $("planPreviewClose").addEventListener("click", () => king.closeModal("planPreviewModal"));
    $("planPreviewCancel").addEventListener("click", () => king.closeModal("planPreviewModal"));
    $("planPreviewConfirm").addEventListener("click", confirmPlan);
    load();
  });
})();
