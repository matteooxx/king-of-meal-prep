// Shared batch-cook dialog used by Today, Week, Log, and recipe detail.
(function () {
  const $ = (id) => document.getElementById(id);
  let state = null;
  let mode = "fresh";

  function number(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  }

  function setMode(next) {
    mode = next;
    document.querySelectorAll("[data-cook-mode]").forEach((button) => {
      const selected = button.dataset.cookMode === mode;
      button.classList.toggle("active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    const fresh = mode === "fresh";
    $("cookBatchField").hidden = !fresh;
    $("cookStorageFields").hidden = !fresh;
    updateAvailability();
  }

  function expiryFor(frozen) {
    return king.addDays(
      king.isoToday(),
      frozen ? king.frozenShelfLifeDays : king.preparedShelfLifeDays
    );
  }

  function updateAvailability() {
    if (!state) return;
    const eat = number($("cookEatServings").value, state.servings);
    const available = Number(state.preparedPortions || 0);
    const preparedButton = document.querySelector("[data-cook-mode='prepared']");
    const enough = available + 0.000001 >= eat;
    preparedButton.disabled = !enough;
    if (mode === "prepared" && !enough) setMode("fresh");
    $("cookAvailability").textContent = enough
      ? `${king.formatNumber(available)} prepared portion${available === 1 ? "" : "s"} available.`
      : available > 0
        ? `${king.formatNumber(available)} prepared; cook fresh for ${king.formatNumber(eat)}.`
        : "No prepared portions. Surplus from this batch will be stored.";
  }

  king.openCook = (options) => {
    state = {
      servings: 1,
      recipeYield: 1,
      preparedPortions: 0,
      ...options,
    };
    $("cookRecipeName").textContent = state.name || "Planned meal";
    $("cookEatServings").value = number(state.servings, 1);
    $("cookBatchServings").value = Math.max(
      number(state.batchServings, number(state.recipeYield, 1)),
      number(state.servings, 1)
    );
    $("cookFrozen").checked = false;
    $("cookExpires").value = expiryFor(false);
    $("cookResult").textContent = "";
    const canUsePrepared = (
      Number(state.preparedPortions || 0) + 0.000001
      >= number(state.servings, 1)
    );
    const initialMode = state.initialMode === "fresh"
      ? "fresh"
      : canUsePrepared
        ? "prepared"
        : "fresh";
    setMode(initialMode);
    king.openModal("cookModal");
  };

  async function confirmCook() {
    if (!state) return;
    const eat = number($("cookEatServings").value, 0);
    if (!eat) {
      $("cookResult").className = "result err";
      $("cookResult").textContent = "Enter portions eaten.";
      return;
    }
    const payload = {
      recipe_id: Number(state.recipeId),
      servings: eat,
      status: "cooked",
      idempotency_key: king.idempotencyKey(),
      cook_mode: mode,
    };
    if (state.expectedVersion) {
      payload.expected_version = Number(state.expectedVersion);
    }
    if (mode === "fresh") {
      const batch = number($("cookBatchServings").value, 0);
      if (!batch || batch < eat) {
        $("cookResult").className = "result err";
        $("cookResult").textContent =
          "Portions prepared must be at least portions eaten.";
        return;
      }
      payload.prepared_servings = batch;
      payload.frozen = $("cookFrozen").checked;
      if (batch > eat && $("cookExpires").value) {
        payload.expires_on = $("cookExpires").value;
      }
    }
    $("cookConfirm").disabled = true;
    $("cookResult").className = "result";
    $("cookResult").textContent = "Updating pantry and meal log...";
    try {
      const result = await king.fetchJSON(
        `/api/plan/${state.date}/${state.slot}`,
        { method: "PATCH", body: JSON.stringify(payload) }
      );
      const stored = result.prepared?.created?.portions_remaining || 0;
      const message = mode === "prepared"
        ? "Prepared portion logged."
        : stored > 0
          ? `Meal logged; ${king.formatNumber(stored)} portion${stored === 1 ? "" : "s"} stored.`
          : "Meal logged and pantry updated.";
      const completed = state;
      const callback = state.onComplete;
      state = null;
      king.closeModal("cookModal");
      king.toast(message, "success");
      if (callback) callback(result);
      if (king.openFeedback) {
        king.openFeedback({
          recipeId: completed.recipeId,
          name: completed.name,
        });
      }
    } catch (error) {
      $("cookResult").className = "result err";
      $("cookResult").textContent = error.message;
    } finally {
      $("cookConfirm").disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-cook-mode]").forEach((button) => {
      button.addEventListener("click", () => setMode(button.dataset.cookMode));
    });
    $("cookEatServings").addEventListener("input", updateAvailability);
    $("cookFrozen").addEventListener("change", () => {
      $("cookExpires").value = expiryFor($("cookFrozen").checked);
    });
    $("cookModalClose").addEventListener("click", () => king.closeModal("cookModal"));
    $("cookCancel").addEventListener("click", () => king.closeModal("cookModal"));
    $("cookConfirm").addEventListener("click", confirmCook);
  });
})();
