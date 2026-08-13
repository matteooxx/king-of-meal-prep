// Full-screen, resumable cooking mode with scaled ingredients and local timers.
(function () {
  const $ = (id) => document.getElementById(id);
  const root = $("guidedCookRoot");
  const recipeId = Number(root?.dataset.rid);
  const params = new URLSearchParams(window.location.search);
  const slots = new Set(["breakfast", "lunch", "dinner", "snack"]);
  const slotLabels = {
    breakfast: "Breakfast",
    lunch: "Lunch",
    dinner: "Dinner",
    snack: "Snack",
  };

  let recipe = null;
  let progressKey = "";
  let timerTick = null;
  let wakeLock = null;
  let wakeFailed = false;
  let audioContext = null;
  let pointerStart = null;

  const date = /^\d{4}-\d{2}-\d{2}$/.test(params.get("date") || "")
    ? params.get("date")
    : king.isoToday();
  const slot = slots.has(params.get("slot"))
    ? params.get("slot")
    : likelySlot();
  const mealServings = boundedNumber(params.get("servings"), 1, 0.1, 99);
  const expectedVersion = boundedNumber(
    params.get("version"),
    null,
    1,
    Number.MAX_SAFE_INTEGER
  );

  const state = {
    stepIndex: 0,
    batchPortions: 1,
    checked: new Set(),
    timers: [],
    wakeEnabled: true,
  };

  function likelySlot() {
    const hour = new Date().getHours();
    if (hour >= 5 && hour < 10) return "breakfast";
    if (hour >= 10 && hour < 15) return "lunch";
    if (hour >= 15 && hour < 21) return "dinner";
    return "snack";
  }

  function boundedNumber(value, fallback, minimum, maximum) {
    if (value === null || value === "") return fallback;
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(maximum, Math.max(minimum, parsed));
  }

  function steps() {
    return recipe?.steps?.length
      ? recipe.steps
      : ["Review the ingredients, then prepare the meal."];
  }

  function formatAmount(value) {
    if (!Number.isFinite(value)) return "";
    if (Number.isInteger(value)) return String(value);
    return value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
  }

  function scaledIngredient(item) {
    if (item.quantity === null || item.quantity === undefined) return "";
    const recipeYield = Math.max(Number(recipe.servings || 1), 0.1);
    return formatAmount(
      Number(item.quantity) * Number(state.batchPortions) / recipeYield
    );
  }

  function saveProgress() {
    if (!progressKey) return;
    try {
      localStorage.setItem(progressKey, JSON.stringify({
        version: 1,
        stepIndex: state.stepIndex,
        batchPortions: state.batchPortions,
        checked: [...state.checked],
        timers: state.timers,
        wakeEnabled: state.wakeEnabled,
      }));
    } catch {}
  }

  function restoreProgress() {
    try {
      const saved = JSON.parse(localStorage.getItem(progressKey) || "null");
      if (!saved || saved.version !== 1) return;
      state.stepIndex = Math.min(
        Math.max(Number(saved.stepIndex) || 0, 0),
        steps().length - 1
      );
      state.batchPortions = boundedNumber(
        saved.batchPortions,
        state.batchPortions,
        mealServings,
        200
      );
      state.checked = new Set(
        Array.isArray(saved.checked) ? saved.checked.map(String) : []
      );
      state.timers = Array.isArray(saved.timers)
        ? saved.timers.filter((timer) => (
          timer &&
          Number.isFinite(Number(timer.endAt)) &&
          Number(timer.endAt) > Date.now() - 24 * 60 * 60 * 1000
        )).map((timer) => ({
          id: String(timer.id),
          endAt: Number(timer.endAt),
          label: String(timer.label || "Timer"),
          notified: Boolean(timer.notified),
        }))
        : [];
      state.wakeEnabled = saved.wakeEnabled !== false;
    } catch {
      try {
        localStorage.removeItem(progressKey);
      } catch {}
    }
  }

  function renderIngredients() {
    const ingredients = recipe.ingredients || [];
    $("batchPortions").textContent = king.formatNumber(state.batchPortions);
    $("batchMinus").disabled = state.batchPortions <= mealServings + 0.000001;
    $("guidedIngredientList").innerHTML = ingredients.length
      ? ingredients.map((item, index) => {
        const key = String(item.id ?? index);
        const amount = scaledIngredient(item);
        const detail = [amount, item.unit || ""].filter(Boolean).join(" ");
        return `<label class="guided-ingredient${item.optional ? " optional" : ""}">
          <input type="checkbox" data-ingredient-key="${king.escapeHTML(key)}"
                 ${state.checked.has(key) ? "checked" : ""}>
          <span class="guided-ingredient-check"><i data-lucide="check"></i></span>
          <span><strong>${king.escapeHTML(item.display_name || "Ingredient")}</strong>
          <small class="num">${king.escapeHTML(detail || "as needed")}${item.optional ? " - optional" : ""}</small></span>
        </label>`;
      }).join("")
      : '<p class="dim">No ingredients listed.</p>';
    $("guidedIngredientList").querySelectorAll("[data-ingredient-key]").forEach(
      (input) => {
        input.addEventListener("change", () => {
          if (input.checked) state.checked.add(input.dataset.ingredientKey);
          else state.checked.delete(input.dataset.ingredientKey);
          saveProgress();
          renderIngredientProgress();
        });
      }
    );
    renderIngredientProgress();
  }

  function renderIngredientProgress() {
    const total = (recipe.ingredients || []).length;
    const completed = [...state.checked].filter((key) => (
      (recipe.ingredients || []).some(
        (item, index) => String(item.id ?? index) === key
      )
    )).length;
    $("ingredientProgress").textContent = total
      ? `${completed}/${total}`
      : "";
  }

  function normalized(value) {
    return String(value || "")
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  function stepIngredients(stepText) {
    const haystack = ` ${normalized(stepText)} `;
    const ignored = new Set([
      "fresh", "dried", "ground", "chopped", "sliced", "optional",
      "large", "small", "medium", "finely", "roughly",
    ]);
    return (recipe.ingredients || []).filter((item) => {
      const name = normalized(item.display_name);
      if (!name) return false;
      if (haystack.includes(` ${name} `)) return true;
      const tokens = name.split(" ").filter(
        (word) => word.length >= 4 && !ignored.has(word)
      );
      return tokens.some((word) => haystack.includes(` ${word} `));
    });
  }

  function extractDurations(text) {
    const pattern = /(\d+(?:\.\d+)?)\s*(?:(?:-|\u2013|to)\s*(\d+(?:\.\d+)?)\s*)?(seconds?|secs?|minutes?|mins?|hours?|hrs?|secondi|minuti?|ore?|ora)\b/gi;
    const durations = [];
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const amount = Number(match[2] || match[1]);
      const unit = match[3].toLowerCase();
      const seconds = unit.startsWith("h") || unit === "ora" || unit.startsWith("ore")
        ? amount * 3600
        : unit.startsWith("s") || unit.startsWith("second")
          ? amount
          : amount * 60;
      if (seconds >= 5 && seconds <= 24 * 60 * 60) {
        durations.push(Math.round(seconds));
      }
    }
    return [...new Set(durations)];
  }

  function durationLabel(seconds) {
    if (seconds % 3600 === 0) return `${seconds / 3600} hr`;
    if (seconds >= 3600) {
      const hours = Math.floor(seconds / 3600);
      const minutes = Math.round((seconds % 3600) / 60);
      return `${hours} hr ${minutes} min`;
    }
    if (seconds % 60 === 0) return `${seconds / 60} min`;
    return `${seconds} sec`;
  }

  function renderStep() {
    const allSteps = steps();
    state.stepIndex = Math.min(Math.max(state.stepIndex, 0), allSteps.length - 1);
    const text = allSteps[state.stepIndex] || "";
    const count = state.stepIndex + 1;
    $("guidedStepCount").textContent = `Step ${count} of ${allSteps.length}`;
    $("guidedStepText").textContent = text;
    $("guidedMealPortions").textContent =
      `${king.formatNumber(mealServings)} eating`;
    $("guidedProgressBar").style.width =
      `${Math.round((count / allSteps.length) * 100)}%`;
    $("guidedPrev").disabled = state.stepIndex === 0;
    $("guidedNext").hidden = state.stepIndex === allSteps.length - 1;
    $("guidedFinish").hidden = state.stepIndex !== allSteps.length - 1;

    const related = stepIngredients(text);
    $("guidedStepIngredients").innerHTML = related.length
      ? related.map((item) => {
        const amount = [scaledIngredient(item), item.unit || ""]
          .filter(Boolean).join(" ");
        return `<span><strong class="num">${king.escapeHTML(amount || "As needed")}</strong>${king.escapeHTML(item.display_name)}</span>`;
      }).join("")
      : "";

    const durations = extractDurations(text);
    $("guidedTimerActions").innerHTML = durations.map((seconds) => (
      `<button class="btn" type="button" data-timer-seconds="${seconds}">
        <i data-lucide="timer"></i>Start ${durationLabel(seconds)}
      </button>`
    )).join("");
    $("guidedTimerActions").querySelectorAll("[data-timer-seconds]").forEach(
      (button) => {
        button.addEventListener("click", () => {
          startTimer(Number(button.dataset.timerSeconds));
        });
      }
    );
    saveProgress();
  }

  function ensureAudio() {
    if (audioContext) return;
    const AudioCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtor) return;
    audioContext = new AudioCtor();
    audioContext.resume().catch(() => {});
  }

  function startTimer(seconds) {
    ensureAudio();
    state.timers.push({
      id: `${Date.now()}-${state.stepIndex}-${seconds}`,
      endAt: Date.now() + seconds * 1000,
      label: `Step ${state.stepIndex + 1} - ${durationLabel(seconds)}`,
      notified: false,
    });
    saveProgress();
    renderTimers();
  }

  function soundTimer() {
    if (navigator.vibrate) navigator.vibrate([180, 100, 180]);
    if (!audioContext) return;
    try {
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.frequency.value = 880;
      gain.gain.setValueAtTime(0.12, audioContext.currentTime);
      gain.gain.exponentialRampToValueAtTime(
        0.001,
        audioContext.currentTime + 0.7
      );
      oscillator.connect(gain);
      gain.connect(audioContext.destination);
      oscillator.start();
      oscillator.stop(audioContext.currentTime + 0.7);
    } catch {}
  }

  function renderTimers() {
    const tray = $("guidedTimerTray");
    if (!state.timers.length) {
      tray.hidden = true;
      tray.innerHTML = "";
      return;
    }
    let changed = false;
    tray.hidden = false;
    tray.innerHTML = state.timers.map((timer) => {
      const remaining = Math.max(0, Math.ceil((timer.endAt - Date.now()) / 1000));
      const done = remaining === 0;
      if (done && !timer.notified) {
        timer.notified = true;
        changed = true;
        soundTimer();
      }
      const clock = done
        ? "Done"
        : `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(remaining % 60).padStart(2, "0")}`;
      return `<div class="guided-timer${done ? " done" : ""}">
        <i data-lucide="${done ? "alarm-clock-check" : "timer"}"></i>
        <span><strong class="num">${clock}</strong><small>${king.escapeHTML(timer.label)}</small></span>
        <button class="icon-btn" type="button" data-cancel-timer="${king.escapeHTML(timer.id)}"
                aria-label="${done ? "Dismiss" : "Cancel"} timer"
                title="${done ? "Dismiss" : "Cancel"} timer">
          <i data-lucide="x"></i>
        </button>
      </div>`;
    }).join("");
    tray.querySelectorAll("[data-cancel-timer]").forEach((button) => {
      button.addEventListener("click", () => {
        state.timers = state.timers.filter(
          (timer) => timer.id !== button.dataset.cancelTimer
        );
        saveProgress();
        renderTimers();
      });
    });
    if (changed) saveProgress();
  }

  function updateWakeButton() {
    const button = $("wakeLockBtn");
    if (!("wakeLock" in navigator)) {
      button.disabled = true;
      button.querySelector("span").textContent = "Wake Lock unavailable";
      button.setAttribute("aria-pressed", "false");
      return;
    }
    button.disabled = false;
    button.classList.toggle("active", state.wakeEnabled && Boolean(wakeLock));
    button.setAttribute("aria-pressed", String(state.wakeEnabled));
    button.querySelector("span").textContent = !state.wakeEnabled
      ? "Allow sleep"
      : wakeFailed && !wakeLock
        ? "Keep screen awake"
        : "Screen awake";
  }

  async function requestWakeLock() {
    if (!state.wakeEnabled || document.visibilityState !== "visible") return;
    if (!("wakeLock" in navigator) || wakeLock) {
      updateWakeButton();
      return;
    }
    try {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeFailed = false;
      wakeLock.addEventListener("release", () => {
        wakeLock = null;
        updateWakeButton();
      }, { once: true });
    } catch {
      wakeFailed = true;
    }
    updateWakeButton();
  }

  async function releaseWakeLock() {
    const current = wakeLock;
    wakeLock = null;
    if (current) {
      try {
        await current.release();
      } catch {}
    }
    updateWakeButton();
  }

  async function toggleWakeLock() {
    state.wakeEnabled = !state.wakeEnabled;
    wakeFailed = false;
    saveProgress();
    if (state.wakeEnabled) await requestWakeLock();
    else await releaseWakeLock();
  }

  function moveStep(delta) {
    const next = Math.min(
      Math.max(state.stepIndex + delta, 0),
      steps().length - 1
    );
    if (next === state.stepIndex) return;
    state.stepIndex = next;
    renderStep();
    $("guidedStepStage").focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function changeBatch(delta) {
    const next = Math.round((state.batchPortions + delta) * 10) / 10;
    state.batchPortions = Math.min(200, Math.max(mealServings, next));
    renderIngredients();
    renderStep();
  }

  function finishCooking() {
    king.openCook({
      date,
      slot,
      recipeId,
      name: recipe.name,
      servings: mealServings,
      recipeYield: Number(recipe.servings || 1),
      batchServings: state.batchPortions,
      preparedPortions: Number(recipe.prepared_portions || 0),
      expectedVersion: expectedVersion || undefined,
      initialMode: "fresh",
      onComplete: () => {
        try {
          localStorage.removeItem(progressKey);
        } catch {}
        state.timers = [];
        renderTimers();
        releaseWakeLock();
        $("guidedCookLayout").hidden = true;
        $("guidedComplete").hidden = false;
        $("guidedCompleteTitle").textContent = recipe.name;
        $("guidedProgressBar").style.width = "100%";
      },
    });
  }

  function wireEvents() {
    $("batchMinus").addEventListener("click", () => changeBatch(-0.5));
    $("batchPlus").addEventListener("click", () => changeBatch(0.5));
    $("guidedPrev").addEventListener("click", () => moveStep(-1));
    $("guidedNext").addEventListener("click", () => moveStep(1));
    $("guidedFinish").addEventListener("click", finishCooking);
    $("wakeLockBtn").addEventListener("click", toggleWakeLock);

    document.addEventListener("keydown", (event) => {
      if (document.querySelector(".modal:not([hidden])")) return;
      if (["INPUT", "BUTTON", "SELECT", "TEXTAREA"].includes(
        document.activeElement?.tagName
      )) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        moveStep(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        moveStep(1);
      }
    });

    const stage = $("guidedStepStage");
    stage.addEventListener("pointerdown", (event) => {
      if (event.target.closest("button, input, a")) return;
      pointerStart = { x: event.clientX, y: event.clientY };
    });
    stage.addEventListener("pointerup", (event) => {
      if (!pointerStart) return;
      const x = event.clientX - pointerStart.x;
      const y = event.clientY - pointerStart.y;
      pointerStart = null;
      if (Math.abs(x) < 70 || Math.abs(x) < Math.abs(y) * 1.5) return;
      moveStep(x < 0 ? 1 : -1);
    });
    stage.addEventListener("pointercancel", () => { pointerStart = null; });

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") requestWakeLock();
    });
    document.addEventListener("pointerdown", requestWakeLock, { once: true });
    window.addEventListener("pagehide", releaseWakeLock);
  }

  async function load() {
    try {
      recipe = await king.fetchJSON(`/api/recipes/${recipeId}`);
      progressKey = `king-cook-progress-v1:${recipeId}:${date}:${slot}`;
      state.batchPortions = Math.max(
        mealServings,
        Number(recipe.servings || 1)
      );
      restoreProgress();
      $("guidedRecipeName").textContent = recipe.name;
      $("guidedContext").textContent =
        `${date} - ${slotLabels[slot]} - Guided cooking`;
      $("guidedExit").href = date === king.isoToday() ? "/today" : "/week";
      $("guidedIngredients").open = !window.matchMedia(
        "(max-width: 820px)"
      ).matches;
      $("guidedCookLayout").hidden = false;
      renderIngredients();
      renderStep();
      renderTimers();
      wireEvents();
      updateWakeButton();
      requestWakeLock();
      timerTick = window.setInterval(renderTimers, 1000);
    } catch (error) {
      $("guidedError").hidden = false;
      $("guidedError").textContent = error.message;
    }
  }

  window.addEventListener("pagehide", () => {
    if (timerTick) window.clearInterval(timerTick);
  });
  document.addEventListener("DOMContentLoaded", load);
})();
