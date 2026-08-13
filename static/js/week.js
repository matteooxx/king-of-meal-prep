// /week - responsive week view, slot actions, recipe picker, proposal/commit.
(function () {
  const $ = (id) => document.getElementById(id);
  const SLOTS = ["breakfast", "lunch", "dinner", "snack"];
  const SLOT_LABELS = {
    breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner", snack: "Snack",
  };
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  let currentStart = null;
  let currentData = null;
  let selected = null;
  let proposal = null;
  let recipes = null;

  async function load(start) {
    currentStart = start;
    try {
      currentData = await king.fetchJSON(`/api/plan/week?start=${start}`);
      render(currentData);
    } catch (error) {
      $("weekRoot").innerHTML = `<p class="result err">${king.escapeHTML(error.message)}</p>`;
    }
  }

  function render(data) {
    const today = king.isoToday();
    const days = Array.from({ length: 7 }, (_, index) => king.addDays(data.start, index));
    $("weekSub").textContent = `Week of ${data.start} to ${king.addDays(data.start, 6)}`;

    let grid = `<div class="week-grid desktop-only"><div class="week-slot-head"></div>`;
    days.forEach((day, index) => {
      const current = day === today ? " today" : "";
      grid += `<div class="week-day-head${current}">${DAYS[index]} ${day.slice(8)}</div>`;
    });
    SLOTS.forEach((slot) => {
      grid += `<div class="week-slot-head">${SLOT_LABELS[slot]}</div>`;
      days.forEach((day) => {
        grid += cellHTML(day, slot, data.plan[day]?.[slot] || null);
      });
    });
    grid += "</div>";

    const mobile = `<div class="week-day-list mobile-only">${days.map((day, index) => `
      <section class="week-day-section${day === today ? " today" : ""}">
        <h2>${DAYS[index]} <span class="num">${day.slice(8)}</span></h2>
        ${SLOTS.map((slot) => mobileCellHTML(
          day, slot, data.plan[day]?.[slot] || null
        )).join("")}
      </section>`).join("")}</div>`;

    $("weekRoot").innerHTML = grid + mobile;
    $("weekRoot").querySelectorAll("[data-week-cell]").forEach((button) => {
      button.addEventListener("click", () => openActions(button));
    });
  }

  function statusText(cell) {
    if (!cell) return "Choose recipe";
    const flags = [];
    if (cell.status && cell.status !== "planned") flags.push(cell.status);
    if (cell.locked) flags.push("locked");
    if (Number(cell.prepared_portions || 0) > 0) {
      flags.push(`${king.formatNumber(cell.prepared_portions)} ready`);
    }
    return flags.join(" · ") ||
      `${cell.kcal || 0} kcal · ${king.formatNumber(cell.servings || 1)} portion${Number(cell.servings || 1) === 1 ? "" : "s"}`;
  }

  function cellAttrs(date, slot, cell) {
    return `data-week-cell data-date="${date}" data-slot="${slot}" ` +
      `data-rid="${cell?.recipe_id || ""}" data-version="${cell?.version || ""}"`;
  }

  function cellHTML(date, slot, cell) {
    const classes = [
      "week-cell",
      !cell ? "empty" : "",
      cell?.status === "cooked" ? "cooked" : "",
      cell?.locked ? "locked" : "",
    ].filter(Boolean).join(" ");
    return `<button type="button" class="${classes}" ${cellAttrs(date, slot, cell)}
      aria-label="${king.escapeHTML(`${date} ${SLOT_LABELS[slot]}: ${cell?.name || "empty"}`)}">
      <span class="recipe-name">${king.escapeHTML(cell?.name || "Choose")}</span>
      <span class="kcal">${king.escapeHTML(statusText(cell))}</span>
    </button>`;
  }

  function mobileCellHTML(date, slot, cell) {
    return `<button type="button" class="week-mobile-cell" ${cellAttrs(date, slot, cell)}>
      <span class="slot-label">${SLOT_LABELS[slot]}</span>
      <span class="week-mobile-meal">${king.escapeHTML(cell?.name || "Choose recipe")}</span>
      <span class="dim">${king.escapeHTML(statusText(cell))}</span>
    </button>`;
  }

  function selectedCell() {
    return currentData?.plan?.[selected.date]?.[selected.slot] || null;
  }

  function openActions(button) {
    selected = {
      date: button.dataset.date,
      slot: button.dataset.slot,
    };
    const cell = selectedCell();
    $("cellActionTitle").textContent = `${selected.date} · ${SLOT_LABELS[selected.slot]}`;
    $("cellActionName").textContent = cell?.name || "Empty slot";
    $("cellChooseBtn").textContent = cell ? "Swap recipe" : "Choose recipe";
    $("cellOpenBtn").hidden = !cell?.recipe_id;
    $("cellCookBtn").hidden = !cell?.recipe_id || cell.status === "cooked";
    $("cellSkipBtn").hidden = !cell?.recipe_id || cell.status === "skipped";
    $("cellResetBtn").hidden = !cell?.recipe_id || cell.status === "planned";
    $("cellLockBtn").hidden = !cell?.recipe_id;
    $("cellLockBtn").textContent = cell?.locked ? "Unlock" : "Lock";
    $("cellChooseBtn").disabled = cell?.status === "cooked";
    $("cellServingsField").hidden = !cell?.recipe_id;
    $("cellServings").value = cell?.servings || 1;
    if (cell?.recipe_id && cell.status !== "cooked") {
      const ready = Number(cell.prepared_portions || 0) >= Number(cell.servings || 1);
      $("cellCookBtn").innerHTML = ready
        ? '<i data-lucide="package-check"></i>Use prepared'
        : '<i data-lucide="cooking-pot"></i>Cook meal';
      king.icons();
    }
    king.openModal("cellActionModal");
  }

  async function patchSelected(payload) {
    const cell = selectedCell();
    if (cell?.version) payload.expected_version = cell.version;
    const result = await king.fetchJSON(
      `/api/plan/${selected.date}/${selected.slot}`,
      { method: "PATCH", body: JSON.stringify(payload) }
    );
    king.closeModal("cellActionModal");
    await load(currentStart);
    return result;
  }

  async function statusAction(status) {
    try {
      const payload = { status };
      if (status === "cooked") payload.idempotency_key = king.idempotencyKey();
      const result = await patchSelected(payload);
      const missing = result.pantry?.missing?.length || 0;
      king.toast(
        status === "cooked"
          ? (missing ? `Cooked; ${missing} pantry item(s) short.` : "Cooked. Pantry deducted.")
          : status === "skipped" ? "Skipped." : "Reset to planned.",
        "success"
      );
    } catch (error) {
      king.toast(error.message, "error");
    }
  }

  function openCook() {
    const cell = selectedCell();
    if (!cell?.recipe_id) return;
    king.closeModal("cellActionModal");
    const servings = Number(cell.servings || 1);
    const preparedPortions = Number(cell.prepared_portions || 0);
    if (preparedPortions + 0.000001 < servings) {
      const params = new URLSearchParams({
        date: selected.date,
        slot: selected.slot,
        servings: String(servings),
        version: String(cell.version),
      });
      window.location.href =
        `/recipes/${cell.recipe_id}/cook?${params.toString()}`;
      return;
    }
    king.openCook({
      date: selected.date,
      slot: selected.slot,
      recipeId: cell.recipe_id,
      name: cell.name,
      servings,
      recipeYield: cell.recipe_servings || 1,
      preparedPortions,
      expectedVersion: cell.version,
      onComplete: () => load(currentStart),
    });
  }

  async function openPicker() {
    king.closeModal("cellActionModal");
    king.openModal("recipePickerModal");
    $("recipePickerSearch").value = "";
    $("recipePickerList").innerHTML = '<p class="dim">Loading...</p>';
    try {
      if (!recipes) {
        recipes = (await king.fetchJSON("/api/recipes?limit=500")).items || [];
      }
      renderRecipePicker();
    } catch (error) {
      $("recipePickerList").innerHTML =
        `<p class="result err">${king.escapeHTML(error.message)}</p>`;
    }
  }

  function renderRecipePicker() {
    const query = $("recipePickerSearch").value.trim().toLowerCase();
    const matches = recipes.filter((recipe) => (
      !query ||
      (recipe.name || "").toLowerCase().includes(query) ||
      (recipe.cuisine || "").toLowerCase().includes(query)
    ));
    $("recipePickerList").innerHTML = matches.length
      ? matches.map((recipe) => `<button type="button" class="recipe-pick" data-rid="${recipe.id}">
          <span>${king.escapeHTML(recipe.name)}</span>
          <span class="dim">${recipe.total_time_min ? `${recipe.total_time_min} min` : ""}</span>
        </button>`).join("")
      : '<p class="dim">No matching recipes.</p>';
    $("recipePickerList").querySelectorAll("[data-rid]").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await patchSelected({ recipe_id: Number(button.dataset.rid) });
          king.closeModal("recipePickerModal");
          king.toast("Meal assigned.", "success");
        } catch (error) {
          button.disabled = false;
          king.toast(error.message, "error");
        }
      });
    });
  }

  async function reviewProposal() {
    const button = $("planWeekBtn");
    button.disabled = true;
    try {
      proposal = await king.fetchJSON("/api/plan/week/proposal", {
        method: "POST",
        body: JSON.stringify({ start: currentStart }),
      });
      let generated = 0;
      let preserved = 0;
      Object.values(proposal.plan || {}).forEach((slots) => {
        Object.values(slots).forEach((item) => {
          if (!item) return;
          if (item.preserved) preserved++;
          else generated++;
        });
      });
      $("weekProposalSummary").textContent =
        `${generated} new picks · ${preserved} preserved · ` +
        `${(proposal.skipped || []).length} empty`;
      $("weekProposalConflicts").textContent = (proposal.conflicts || []).length
        ? `Preserved: ${proposal.conflicts.map((item) =>
            `${item.date} ${item.slot} (${item.reason})`
          ).join(" · ")}`
        : "No existing slots need preservation.";
      $("weekProposalPicks").innerHTML = king.proposalPicksHTML(proposal);
      king.openModal("weekProposalModal");
    } catch (error) {
      king.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function commitProposal() {
    if (!proposal) return;
    $("weekProposalConfirm").disabled = true;
    try {
      const result = await king.fetchJSON("/api/plan/week/commit", {
        method: "POST",
        body: JSON.stringify({
          proposal_id: proposal.proposal_id,
          expected_version: proposal.expected_version,
        }),
      });
      proposal = null;
      king.closeModal("weekProposalModal");
      king.toast(`Saved ${result.saved} planned slots.`, "success");
      await load(currentStart);
    } catch (error) {
      king.toast(error.message, "error");
    } finally {
      $("weekProposalConfirm").disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("prevWeekBtn").addEventListener("click", () => load(king.addDays(currentStart, -7)));
    $("nextWeekBtn").addEventListener("click", () => load(king.addDays(currentStart, 7)));
    $("todayBtn").addEventListener("click", () => load(king.mondayOf(king.isoToday())));
    $("planWeekBtn").addEventListener("click", reviewProposal);

    $("cellActionClose").addEventListener("click", () => king.closeModal("cellActionModal"));
    $("cellChooseBtn").addEventListener("click", openPicker);
    $("cellOpenBtn").addEventListener("click", () => {
      const cell = selectedCell();
      if (cell?.recipe_id) window.location.href = `/recipes/${cell.recipe_id}`;
    });
    $("cellCookBtn").addEventListener("click", openCook);
    $("cellSkipBtn").addEventListener("click", () => statusAction("skipped"));
    $("cellResetBtn").addEventListener("click", () => statusAction("planned"));
    $("cellLockBtn").addEventListener("click", async () => {
      try {
        await patchSelected({ locked: !selectedCell()?.locked });
        king.toast("Lock updated.", "success");
      } catch (error) {
        king.toast(error.message, "error");
      }
    });
    $("cellSaveServingsBtn").addEventListener("click", async () => {
      const servings = Number($("cellServings").value);
      if (!Number.isFinite(servings) || servings < 0.1) {
        king.toast("Enter a valid portion count.", "error");
        return;
      }
      try {
        await patchSelected({ servings });
        king.toast("Portions updated.", "success");
      } catch (error) {
        king.toast(error.message, "error");
      }
    });

    $("recipePickerClose").addEventListener("click", () => king.closeModal("recipePickerModal"));
    $("recipePickerSearch").addEventListener("input", renderRecipePicker);
    $("weekProposalClose").addEventListener("click", () => king.closeModal("weekProposalModal"));
    $("weekProposalCancel").addEventListener("click", () => king.closeModal("weekProposalModal"));
    $("weekProposalConfirm").addEventListener("click", commitProposal);

    load(king.mondayOf(king.isoToday()));
  });
})();
