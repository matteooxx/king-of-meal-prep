// Setup wizard. Loads current settings on boot, mutates DOM as the
// user navigates, saves on Next (per step) and finishes via /api/setup/finish.

(function () {
  const TOTAL = 9;

  // Equipment catalog. Keys are lowercase_snake — these are the canonical
  // identifiers stored in preferences.equipment_json and matched against
  // recipe.equipment_json[]. Labels are display-only (English; we don't
  // translate equipment names). Icons are 24x24 stroke=1.5 line art so they
  // inherit currentColor and match the field-journal aesthetic.
  //
  // Adding a new item later: just add it to the right group; no DB migration
  // needed because preferences.equipment is a freeform JSON array.
  const EQUIPMENT_GROUPS = {
    surfaces: [
      { key: "hob",         label: "Hob",         icon: '<circle cx="7" cy="8" r="2.5"/><circle cx="17" cy="8" r="2.5"/><circle cx="7" cy="17" r="2.5"/><circle cx="17" cy="17" r="2.5"/>' },
      { key: "oven",        label: "Oven",        icon: '<rect x="3" y="3" width="18" height="18" rx="1"/><line x1="3" y1="9" x2="21" y2="9"/><circle cx="7" cy="6" r="0.5"/><circle cx="11" cy="6" r="0.5"/><rect x="7" y="13" width="10" height="6"/>' },
      { key: "microwave",   label: "Microwave",   icon: '<rect x="2" y="5" width="20" height="14" rx="1"/><line x1="15" y1="5" x2="15" y2="19"/><circle cx="18" cy="9" r="0.5"/><circle cx="18" cy="12" r="0.5"/><rect x="5" y="9" width="7" height="6"/>' },
      { key: "grill",       label: "Grill",       icon: '<rect x="3" y="6" width="18" height="3" rx="0.5"/><line x1="6" y1="9" x2="6" y2="20"/><line x1="18" y1="9" x2="18" y2="20"/><line x1="6" y1="15" x2="18" y2="15"/><line x1="9" y1="20" x2="9" y2="22"/><line x1="15" y1="20" x2="15" y2="22"/>' },
    ],
    appliances: [
      { key: "air_fryer",   label: "Air fryer",   icon: '<rect x="5" y="3" width="14" height="18" rx="2"/><line x1="5" y1="14" x2="19" y2="14"/><circle cx="12" cy="8" r="2"/><line x1="9" y1="18" x2="15" y2="18"/>' },
      { key: "slow_cooker", label: "Slow cooker", icon: '<path d="M4 9h16v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V9z"/><line x1="4" y1="9" x2="20" y2="9"/><line x1="2" y1="9" x2="22" y2="9"/><circle cx="18" cy="13" r="0.7"/><line x1="9" y1="6" x2="15" y2="6"/>' },
      { key: "instant_pot", label: "Instant Pot", icon: '<rect x="4" y="6" width="16" height="14" rx="1"/><line x1="2" y1="6" x2="22" y2="6"/><circle cx="12" cy="13" r="3"/><line x1="12" y1="3" x2="12" y2="6"/>' },
      { key: "rice_cooker", label: "Rice cooker", icon: '<path d="M3 10c0-1 1-2 2-2h14c1 0 2 1 2 2v8c0 1-1 2-2 2H5c-1 0-2-1-2-2v-8z"/><line x1="3" y1="14" x2="21" y2="14"/><circle cx="17" cy="11.5" r="0.5"/><circle cx="17" cy="16.5" r="0.5"/>' },
    ],
    electrics: [
      { key: "blender",        label: "Blender",        icon: '<path d="M8 3h8l-1 8H9z"/><line x1="9" y1="11" x2="15" y2="11"/><rect x="10" y="14" width="4" height="2"/><rect x="9" y="16" width="6" height="5" rx="0.5"/>' },
      { key: "food_processor", label: "Food processor", icon: '<rect x="6" y="3" width="12" height="11" rx="1"/><circle cx="12" cy="8.5" r="2"/><rect x="4" y="14" width="16" height="6" rx="1"/><circle cx="17" cy="17" r="0.5"/>' },
      { key: "toaster",        label: "Toaster",        icon: '<rect x="3" y="8" width="18" height="11" rx="1"/><line x1="8" y1="3" x2="8" y2="9"/><line x1="16" y1="3" x2="16" y2="9"/><line x1="6" y1="13" x2="9" y2="13"/><line x1="6" y1="16" x2="9" y2="16"/><circle cx="17" cy="14" r="0.6"/>' },
      { key: "kettle",         label: "Kettle",         icon: '<path d="M5 9h14l-2 11a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1L5 9z"/><path d="M19 11l3-2v3z"/><line x1="10" y1="9" x2="10" y2="6"/><line x1="14" y1="9" x2="14" y2="6"/>' },
      { key: "stand_mixer",    label: "Stand mixer",    icon: '<rect x="3" y="14" width="18" height="6" rx="1"/><path d="M5 14V6a1 1 0 0 1 1-1h6a4 4 0 0 1 4 4v5"/><circle cx="13" cy="9" r="1"/><line x1="11" y1="14" x2="11" y2="20"/>' },
    ],
    specialty: [
      { key: "pasta_machine",  label: "Pasta machine",  icon: '<rect x="4" y="6" width="16" height="9" rx="1"/><circle cx="9" cy="10.5" r="2"/><circle cx="15" cy="10.5" r="2"/><line x1="9" y1="15" x2="9" y2="20"/><line x1="15" y1="15" x2="15" y2="20"/><line x1="20" y1="9" x2="22" y2="9"/>' },
      { key: "sous_vide",      label: "Sous vide",      icon: '<rect x="9" y="3" width="6" height="13" rx="1"/><line x1="9" y1="7" x2="15" y2="7"/><line x1="3" y1="17" x2="21" y2="17"/><path d="M5 17q3 -3 7 0t7 0"/>' },
      { key: "smoker",         label: "Smoker",         icon: '<rect x="4" y="9" width="16" height="11" rx="1"/><line x1="4" y1="13" x2="20" y2="13"/><path d="M9 5q1 -2 0 -4"/><path d="M13 6q1 -2 0 -4"/><path d="M17 5q1 -2 0 -4"/>' },
      { key: "dehydrator",     label: "Dehydrator",     icon: '<rect x="4" y="3" width="16" height="18" rx="1"/><line x1="4" y1="8" x2="20" y2="8"/><line x1="4" y1="13" x2="20" y2="13"/><line x1="4" y1="18" x2="20" y2="18"/>' },
      { key: "waffle_iron",    label: "Waffle iron",    icon: '<rect x="3" y="7" width="18" height="9" rx="1"/><line x1="3" y1="11.5" x2="21" y2="11.5"/><line x1="9" y1="7" x2="9" y2="16"/><line x1="15" y1="7" x2="15" y2="16"/><circle cx="20" cy="11.5" r="1"/>' },
    ],
  };
  let currentStep = 1;
  let state = {
    profile: {},
    preferences: { equipment: [], dislikes: [], allergies: [], favorites: [] },
    kv: {},
    env: {},
  };

  // ---- DOM helpers ----
  const $ = (id) => document.getElementById(id);
  const stepEls = () => Array.from(document.querySelectorAll(".wizard-step"));

  function showStep(n) {
    n = Math.max(1, Math.min(TOTAL, n));
    currentStep = n;
    stepEls().forEach((el) => el.classList.toggle("active", +el.dataset.step === n));
    const progress = ((n - 1) / (TOTAL - 1)) * 100;
    $("wizProgress").style.width = `${progress}%`;
    $("wizStepNum").textContent = n;
    const active = document.querySelector(`.wizard-step[data-step="${n}"]`);
    $("wizStepName").textContent = active?.dataset.name || "";
    $("wizBack").disabled = n === 1;
    $("wizDefaults").hidden = n === 1;
    $("wizNext").textContent = n === TOTAL ? "Finish" : "Next";
    if (n === TOTAL) renderReview();
    window.scrollTo(0, 0);
  }

  // ---- tag inputs ----
  function bindTagInput(containerId, key) {
    const c = $(containerId);
    if (!c) return;
    const input = document.createElement("input");
    input.type = "text";
    input.id = `${containerId}Input`;
    input.setAttribute("aria-label", key);
    input.placeholder = "type and press Enter…";
    c.appendChild(input);

    function render() {
      // Remove existing chip nodes (keep the input which is the last child)
      [...c.querySelectorAll(".tag")].forEach((n) => n.remove());
      const list = state.preferences[key] || [];
      list.forEach((val) => {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.innerHTML = `${king.escapeHTML(val)}<button type="button" aria-label="remove">×</button>`;
        tag.querySelector("button").addEventListener("click", () => {
          state.preferences[key] = list.filter((x) => x !== val);
          render();
        });
        c.insertBefore(tag, input);
      });
    }
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        const v = input.value.trim().replace(/^,+|,+$/g, "");
        if (!v) return;
        const list = state.preferences[key] || (state.preferences[key] = []);
        if (!list.includes(v)) list.push(v);
        input.value = "";
        render();
      } else if (e.key === "Backspace" && !input.value) {
        const list = state.preferences[key] || [];
        if (list.length) {
          list.pop();
          render();
        }
      }
    });
    return render;
  }

  // ---- bind step values into inputs ----
  function setRadio(name, value) {
    document.querySelectorAll(`input[name="${name}"]`).forEach((el) => {
      el.checked = el.value === value;
      // Style the wrapping label
      const label = el.closest("label");
      if (label) label.classList.toggle("checked", el.checked);
    });
  }
  function getRadio(name) {
    const el = document.querySelector(`input[name="${name}"]:checked`);
    return el ? el.value : null;
  }

  function load() {
    return king.fetchJSON("/api/settings").then((d) => {
      state = {
        profile: d.profile || {},
        preferences: {
          equipment: d.preferences?.equipment || [],
          dislikes: d.preferences?.dislikes || [],
          allergies: d.preferences?.allergies || [],
          favorites: d.preferences?.favorites || [],
        },
        kv: d.kv || {},
        env: d.env || {},
      };
      hydrate();
    });
  }

  function hydrate() {
    // 2. Body
    $("weight_kg").value = state.profile.weight_kg ?? "";
    $("height_cm").value = state.profile.height_cm ?? "";
    $("age_years").value = state.profile.age_years ?? "";
    if (state.profile.sex) setRadio("sex", state.profile.sex);
    if (state.profile.activity_level) setRadio("activity_level", state.profile.activity_level);
    if (state.profile.goal) setRadio("goal", state.profile.goal);

    // 5. Meal timing
    const split = state.kv.slot_kcal_split?.value || { breakfast: 0.2, lunch: 0.3, dinner: 0.35, snack: 0.15 };
    $("kcal_breakfast").value = Math.round((split.breakfast || 0) * 100);
    $("kcal_lunch").value     = Math.round((split.lunch     || 0) * 100);
    $("kcal_dinner").value    = Math.round((split.dinner    || 0) * 100);
    $("kcal_snack").value     = Math.round((split.snack     || 0) * 100);
    const ct = state.kv.cook_time_budget_min?.value || {};
    ["mon","tue","wed","thu","fri","sat","sun"].forEach((d) => {
      $(`ct_${d}`).value = ct[d] ?? "";
    });
    $("default_servings").value = state.kv.default_servings?.value ?? 1;

    // 6. Rotation
    $("rotation_window_days").value = state.kv.rotation_window_days?.value ?? 14;
    setRadio("favorites_bypass_mode", state.kv.favorites_bypass_mode?.value || "always");

    // 7. Translations
    setRadio("translation_mode", state.kv.translation_mode?.value || "hover");

    // 8. SMTP — pre-fill non-secrets from env
    ["SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_FROM","OWNER_EMAIL"].forEach((k) => {
      if ($(k) && state.env[k]?.value) $(k).value = state.env[k].value;
    });
    if ($("SMTP_PASS") && state.env.SMTP_PASS?.set) {
      $("SMTP_PASS").placeholder = `set · ${state.env.SMTP_PASS.length} chars (leave blank to keep)`;
    }

    // 9. Gemini
    if (state.env.GEMINI_API_KEY?.set) {
      $("GEMINI_API_KEY").placeholder = `set · ${state.env.GEMINI_API_KEY.length} chars (leave blank to keep)`;
    }

    // Tag renderers
    renderEquipment();
    renderDislikes();
    renderAllergies();

    // Radio click-styling sync
    document.querySelectorAll(".radio-row input[type=radio]").forEach((r) => {
      r.addEventListener("change", () => {
        const labels = r.closest(".radio-row").querySelectorAll("label");
        labels.forEach((l) => l.classList.remove("checked"));
        r.closest("label").classList.add("checked");
      });
    });
  }

  let renderEquipment, renderDislikes, renderAllergies;
  // ---- save current step ----

  async function saveStep(n) {
    if (n === 2) {
      const profile = {
        weight_kg: parseFloat($("weight_kg").value) || null,
        height_cm: parseFloat($("height_cm").value) || null,
        age_years: parseInt($("age_years").value, 10) || null,
        sex: getRadio("sex"),
        activity_level: getRadio("activity_level"),
        goal: getRadio("goal"),
      };
      const required = ["weight_kg", "height_cm", "age_years", "sex", "activity_level", "goal"];
      const missing = required.filter((k) => !profile[k]);
      if (missing.length) {
        king.toast(`Missing: ${missing.join(", ")}`, "error");
        return false;
      }
      const r = await king.fetchJSON("/api/settings/profile", { method: "PATCH", body: JSON.stringify(profile) });
      state.profile = r.profile;
    } else if (n === 3) {
      if (!state.preferences.equipment.length) {
        king.toast("Add at least one equipment item.", "error");
        return false;
      }
      await king.fetchJSON("/api/settings/preferences", {
        method: "PATCH",
        body: JSON.stringify({ equipment: state.preferences.equipment }),
      });
    } else if (n === 4) {
      await king.fetchJSON("/api/settings/preferences", {
        method: "PATCH",
        body: JSON.stringify({
          allergies: state.preferences.allergies,
          dislikes: state.preferences.dislikes,
        }),
      });
    } else if (n === 5) {  // was step 6 (MEAL TIMING)
      const split = {
        breakfast: (parseInt($("kcal_breakfast").value, 10) || 0) / 100,
        lunch:     (parseInt($("kcal_lunch").value, 10) || 0) / 100,
        dinner:    (parseInt($("kcal_dinner").value, 10) || 0) / 100,
        snack:     (parseInt($("kcal_snack").value, 10) || 0) / 100,
      };
      const sum = split.breakfast + split.lunch + split.dinner + split.snack;
      if (Math.abs(sum - 1) > 0.001) {
        king.toast(`Slot percentages add to ${Math.round(sum * 100)}%. Must equal 100%.`, "error");
        return false;
      }
      await king.fetchJSON("/api/settings/kv/slot_kcal_split", {
        method: "PATCH", body: JSON.stringify({ value: split }),
      });
      const cook = {};
      ["mon","tue","wed","thu","fri","sat","sun"].forEach((d) => {
        cook[d] = parseInt($(`ct_${d}`).value, 10) || 30;
      });
      await king.fetchJSON("/api/settings/kv/cook_time_budget_min", {
        method: "PATCH", body: JSON.stringify({ value: cook }),
      });
      const servings = parseFloat($("default_servings").value) || 1;
      await king.fetchJSON("/api/settings/kv/default_servings", {
        method: "PATCH", body: JSON.stringify({ value: servings }),
      });
    } else if (n === 6) {  // was step 7 (ROTATION)
      const win = parseInt($("rotation_window_days").value, 10) || 14;
      await king.fetchJSON("/api/settings/kv/rotation_window_days", {
        method: "PATCH", body: JSON.stringify({ value: win }),
      });
      const mode = getRadio("favorites_bypass_mode") || "always";
      await king.fetchJSON("/api/settings/kv/favorites_bypass_mode", {
        method: "PATCH", body: JSON.stringify({ value: mode }),
      });
    } else if (n === 7) {
      const mode = getRadio("translation_mode") || "hover";
      await king.fetchJSON("/api/settings/kv/translation_mode", {
        method: "PATCH", body: JSON.stringify({ value: mode }),
      });
    } else if (n === 8) {
      const body = {};
      ["SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","SMTP_FROM","OWNER_EMAIL"].forEach((k) => {
        const el = $(k);
        if (el && el.value) body[k] = el.value;
      });
      if (Object.keys(body).length) {
        await king.fetchJSON("/api/settings/secrets", {
          method: "PATCH", body: JSON.stringify(body),
        });
      }
    } else if (n === 9) {
      const v = $("GEMINI_API_KEY").value.trim();
      if (v) {
        await king.fetchJSON("/api/settings/secrets", {
          method: "PATCH", body: JSON.stringify({ GEMINI_API_KEY: v }),
        });
      }
    }
    return true;
  }

  function renderReview() {
    const p = state.profile;
    const eq = state.preferences.equipment.join(", ") || "—";
    const dis = state.preferences.dislikes.join(", ") || "—";
    const all = state.preferences.allergies.join(", ") || "—";
    const lines = [
      `body         ${p.weight_kg||"?"}kg · ${p.height_cm||"?"}cm · ${p.age_years||"?"}y · ${p.sex||"?"}`,
      `activity     ${p.activity_level || "?"} · ${p.goal || "?"}`,
      `targets      ${p.rest_kcal_target||"?"} kcal · ${p.rest_protein_g||"?"}g P · ${p.rest_carbs_g||"?"}g C · ${p.rest_fat_g||"?"}g F`,
      `equipment    ${eq}`,
      `allergies    ${all}`,
      `dislikes     ${dis}`,
      `gemini       ${state.env.GEMINI_API_KEY?.set ? `set · ${state.env.GEMINI_API_KEY.length} chars` : "not set (LLM features disabled)"}`,
      `email        ${state.env.OWNER_EMAIL?.value || "not set"}`,
    ];
    $("reviewSummary").textContent = lines.join("\n");
  }

  // ---- buttons ----

  function applyDefaults(step) {
    if (step === 2) {
      ["weight_kg", "height_cm", "age_years"].forEach((id) => { $(id).value = ""; });
      ["sex", "activity_level", "goal"].forEach((name) => setRadio(name, ""));
    } else if (step === 3) {
      state.preferences.equipment = [];
      renderEquipment();
      syncEquipmentPressed();
    } else if (step === 4) {
      state.preferences.allergies = [];
      state.preferences.dislikes = [];
      renderAllergies();
      renderDislikes();
    } else if (step === 5) {
      const split = { breakfast: 20, lunch: 30, dinner: 35, snack: 15 };
      Object.entries(split).forEach(([slot, value]) => {
        $(`kcal_${slot}`).value = value;
      });
      ["mon","tue","wed","thu","fri"].forEach((day) => {
        $(`ct_${day}`).value = 30;
      });
      ["sat","sun"].forEach((day) => { $(`ct_${day}`).value = 90; });
      $("default_servings").value = 1;
    } else if (step === 6) {
      $("rotation_window_days").value = 14;
      setRadio("favorites_bypass_mode", "always");
    } else if (step === 7) {
      setRadio("translation_mode", "hover");
    } else if (step === 8) {
      $("SMTP_HOST").value = "smtp.gmail.com";
      $("SMTP_PORT").value = 587;
      ["SMTP_USER", "SMTP_PASS", "SMTP_FROM", "OWNER_EMAIL"].forEach((id) => {
        $(id).value = "";
      });
    } else if (step === 9) {
      $("GEMINI_API_KEY").value = "";
    }
    king.toast("Step reset to defaults.", "info");
  }

  // Render the equipment grids. Catalog buttons toggle on tap; the custom
  // tag-input appends ad-hoc items. Both routes mutate state.preferences.equipment
  // (a flat array of canonical keys), so the saveStep(3) payload doesn't change.
  function renderEquipmentGrids() {
    document.querySelectorAll("[data-equip-group]").forEach((host) => {
      const group = host.dataset.equipGroup;
      const items = EQUIPMENT_GROUPS[group] || [];
      host.innerHTML = items.map((it) =>
        `<button type="button" class="equip-card" data-equip-key="${it.key}" aria-pressed="false">
           <svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${it.icon}</svg>
           <span>${king.escapeHTML(it.label)}</span>
         </button>`
      ).join("");
    });
    // Initial pressed state from current selection
    syncEquipmentPressed();
    // Click handlers
    document.querySelectorAll(".equip-card").forEach((btn) => {
      btn.addEventListener("click", () => {
        const key = btn.dataset.equipKey;
        const list = state.preferences.equipment;
        const idx = list.indexOf(key);
        if (idx >= 0) list.splice(idx, 1);
        else list.push(key);
        syncEquipmentPressed();
        // The custom tag-input mirrors the same array; re-render its chips so
        // a user toggling a card off doesn't leave a stale chip below.
        renderEquipment && renderEquipment();
      });
    });
  }

  function syncEquipmentPressed() {
    const set = new Set(state.preferences.equipment || []);
    document.querySelectorAll(".equip-card").forEach((btn) => {
      btn.setAttribute("aria-pressed", String(set.has(btn.dataset.equipKey)));
    });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    renderEquipment   = bindTagInput("equipmentTags",   "equipment");
    renderDislikes    = bindTagInput("dislikesTags",    "dislikes");
    renderAllergies   = bindTagInput("allergiesTags",   "allergies");

    renderEquipmentGrids();
    try { await load(); } catch (e) { king.toast(`Load failed: ${e.message}`, "error"); }
    syncEquipmentPressed();
    showStep(1);

    // Wizard step 11: "Test key" button — POSTs the typed value (or, if empty,
    // hits the saved one) to /api/settings/test/gemini and shows ok/err inline.
    $("testGeminiBtn")?.addEventListener("click", async () => {
      const btn = $("testGeminiBtn");
      const out = $("testGeminiResult");
      const typed = $("GEMINI_API_KEY").value.trim();
      btn.disabled = true; out.className = "result"; out.textContent = "Testing…";
      try {
        const body = typed ? { key: typed } : {};
        const r = await king.fetchJSON("/api/settings/test/gemini", {
          method: "POST", body: JSON.stringify(body),
        });
        out.textContent = r.message;
        out.className = "result " + (r.ok ? "ok" : "err");
      } catch (e) {
        out.textContent = `Network error: ${e.message}`;
        out.className = "result err";
      } finally {
        btn.disabled = false;
      }
    });

    $("wizBack").addEventListener("click", () => showStep(currentStep - 1));
    $("wizDefaults").addEventListener("click", () => applyDefaults(currentStep));
    $("wizNext").addEventListener("click", async () => {
      $("wizNext").disabled = true;
      try {
        const ok = await saveStep(currentStep);
        if (!ok) return;
        if (currentStep < TOTAL) {
          showStep(currentStep + 1);
        } else {
          // Finish
          try {
            await king.fetchJSON("/api/setup/finish", { method: "POST", body: "{}" });
            window.location.href = "/";
          } catch (e) {
            king.toast(`Cannot finish: ${e.data?.missing?.join(", ") || e.message}`, "error");
          }
        }
      } catch (e) {
        king.toast(`Save failed: ${e.message}`, "error");
      } finally {
        $("wizNext").disabled = false;
      }
    });
  });
})();
