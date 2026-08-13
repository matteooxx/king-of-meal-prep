// Collapsible settings page. Each section renders its own form on first
// expansion and saves via PATCH; KV knobs have a "reset to default" button.
(function () {
  let data = null;

  async function load() {
    data = await king.fetchJSON("/api/settings");
    decorateSectionHeaders();
  }

  function decorateSectionHeaders() {
    // Show "(default)" chip on sections whose KV knobs are all still defaults.
    const knobsBySec = {
      timing: ["slot_kcal_split", "cook_time_budget_min"],
      rotation: ["rotation_window_days", "favorites_bypass_mode", "planner_preserve_manual"],
      shopping: ["default_servings", "shopping_include_optional"],
      scanning: ["barcode_online_lookup"],
      translations: ["translation_mode"],
    };
    document.querySelectorAll(".settings-section").forEach((sec) => {
      const id = sec.dataset.section;
      const chip = sec.querySelector(".default-chip");
      if (!chip) return;
      const keys = knobsBySec[id];
      if (!keys) return;
      const allDefault = keys.every((k) => data.kv[k]?.is_default !== false);
      chip.hidden = !allDefault;
    });
  }

  // Renderers per section. Lazy — called on first expansion.
  const renderers = {
    profile: (body) => {
      const p = data.profile;
      const t = (k, label, type = "number") =>
        `<div class="kv-row"><span class="key">${label}</span>` +
        `<input type="${type}" id="prof_${k}" value="${king.escapeHTML(p[k] ?? "")}"></div>`;
      body.innerHTML = `
        ${t("weight_kg", "weight (kg)")}
        ${t("height_cm", "height (cm)")}
        ${t("age_years", "age")}
        <div class="kv-row"><span class="key">sex</span>
          <select id="prof_sex">
            <option value="">—</option>
            <option value="m"${p.sex==="m"?" selected":""}>male</option>
            <option value="f"${p.sex==="f"?" selected":""}>female</option>
          </select></div>
        <div class="kv-row"><span class="key">activity</span>
          <select id="prof_activity_level">
            ${["sedentary","light","moderate","active","very_active"].map(v =>
              `<option value="${v}"${p.activity_level===v?" selected":""}>${v}</option>`).join("")}
          </select></div>
        <div class="kv-row"><span class="key">goal</span>
          <select id="prof_goal">
            ${["cut","maintain","bulk"].map(v =>
              `<option value="${v}"${p.goal===v?" selected":""}>${v}</option>`).join("")}
          </select></div>
        ${t("training_kcal_delta", "training Δ kcal")}
        ${t("training_protein_delta", "training Δ protein g")}
        <div class="kv-row"><span class="key dim">computed targets</span>
          <span class="num">${p.rest_kcal_target||"?"} kcal · ${p.rest_protein_g||"?"}g P · ${p.rest_carbs_g||"?"}g C · ${p.rest_fat_g||"?"}g F</span></div>
        <div style="margin-top: var(--sp-3); display:flex; gap:var(--sp-2);">
          <button class="btn btn-primary" data-act="profile-save">Save</button>
          <button class="btn" data-act="recompute">Recompute targets</button>
        </div>
        <div class="result" id="profileResult"></div>`;
      body.querySelector('[data-act="profile-save"]').addEventListener("click", async () => {
        const payload = {};
        ["weight_kg","height_cm","age_years","training_kcal_delta","training_protein_delta"].forEach((k) => {
          const v = body.querySelector(`#prof_${k}`).value;
          if (v !== "") payload[k] = parseFloat(v);
        });
        ["sex","activity_level","goal"].forEach((k) => {
          const v = body.querySelector(`#prof_${k}`).value;
          if (v) payload[k] = v;
        });
        try {
          const r = await king.fetchJSON("/api/settings/profile", { method:"PATCH", body: JSON.stringify(payload) });
          data.profile = r.profile;
          king.toast("Saved.", "success");
          renderers.profile(body);  // re-render to show new targets
        } catch (e) { king.toast(e.message, "error"); }
      });
      body.querySelector('[data-act="recompute"]').addEventListener("click", async () => {
        try {
          await king.fetchJSON("/api/settings/recompute-targets", { method: "POST", body: "{}" });
          data = await king.fetchJSON("/api/settings");
          renderers.profile(body);
          king.toast("Targets recomputed.", "success");
        } catch (e) { king.toast(e.message, "error"); }
      });
    },

    equipment: (body) => renderTagSection(body, "equipment"),
    avoid:     (body) => {
      body.innerHTML = "";
      const warning = document.createElement("p");
      warning.className = "hint";
      warning.textContent = "Planner avoidance is a convenience filter, not an allergy-safety guarantee.";
      body.appendChild(warning);
      const a = document.createElement("div"); body.appendChild(a);
      renderTagSection(a, "allergies");
      const d = document.createElement("div"); body.appendChild(d);
      d.innerHTML = "<hr class=hr-dotted>";
      renderTagSection(d, "dislikes");
    },
    favorites: (body) => {
      const count = (data.preferences.favorites || []).length;
      body.innerHTML = `<p>${count} favorite recipe${count === 1 ? "" : "s"}.</p>
        <a class="btn" href="/recipes">Manage in Recipes</a>`;
    },

    timing: (body) => {
      renderKVNumberMap(body, "slot_kcal_split", "Slot kcal split (%)",
        ["breakfast","lunch","dinner","snack"], (v)=>Math.round((v||0)*100), (n)=>n/100);
      const sep = document.createElement("hr"); sep.className = "hr-dotted"; body.appendChild(sep);
      renderKVNumberMap(body, "cook_time_budget_min", "Cook-time budget (minutes)",
        ["mon","tue","wed","thu","fri","sat","sun"], (v)=>v ?? 30, (n)=>Math.round(n));
      renderKVText(body, "timezone", "timezone");
    },
    rotation: (body) => {
      renderKVNumber(body, "rotation_window_days", "no-repeat days");
      renderKVSelect(body, "favorites_bypass_mode", "favorites bypass",
        [["always","always"],["max_once_per_week","max 1×/week"],["off","off"]]);
      renderKVBoolean(body, "planner_preserve_manual", "preserve manual assignments");
    },
    shopping: (body) => {
      renderKVNumber(body, "default_servings", "default servings");
      renderKVBoolean(body, "shopping_include_optional", "include optional ingredients");
    },
    scanning: (body) => {
      renderKVBoolean(
        body,
        "barcode_online_lookup",
        "look up unknown barcodes on Open Food Facts"
      );
    },
    translations: (body) => {
      renderKVSelect(body, "translation_mode", "italian display",
        [["hover","hover/tap"],["side_by_side","side-by-side"],["italian_only","italian only"]]);
    },
    data: (body) => {
      body.innerHTML = `
        <div class="data-tools">
          <section class="data-tool">
            <div>
              <p class="section-eyebrow">Portable export</p>
              <p class="section-summary">Account secrets and stored images are excluded.</p>
            </div>
            <div class="page-head-actions">
              <button class="btn" data-export="json" type="button"><i data-lucide="braces"></i>JSON</button>
              <button class="btn" data-export="csv" type="button"><i data-lucide="table-2"></i>CSV bundle</button>
            </div>
          </section>
          <section class="data-tool">
            <div>
              <p class="section-eyebrow">Encrypted full backup</p>
              <p class="section-summary">Includes the database, stored photos, and app.env secrets.</p>
            </div>
            <div class="field-row">
              <div class="field">
                <label for="backupPassphrase">Passphrase</label>
                <input id="backupPassphrase" type="password" minlength="12" maxlength="256" autocomplete="new-password">
              </div>
              <div class="field">
                <label for="backupPassphraseConfirm">Confirm passphrase</label>
                <input id="backupPassphraseConfirm" type="password" minlength="12" maxlength="256" autocomplete="new-password">
              </div>
            </div>
            <button class="btn btn-primary" data-act="backup-download" type="button">
              <i data-lucide="shield-check"></i>Create backup
            </button>
          </section>
          <section class="data-tool">
            <div>
              <p class="section-eyebrow">Validate backup</p>
              <p class="section-summary">Decrypts and checks integrity without changing live data.</p>
            </div>
            <div class="field-row">
              <div class="field">
                <label for="backupValidationFile">Backup file</label>
                <input id="backupValidationFile" type="file" accept=".kingbackup,application/octet-stream">
              </div>
              <div class="field">
                <label for="backupValidationPassphrase">Passphrase</label>
                <input id="backupValidationPassphrase" type="password" maxlength="256" autocomplete="current-password">
              </div>
            </div>
            <button class="btn" data-act="backup-validate" type="button">
              <i data-lucide="file-check-2"></i>Validate
            </button>
            <div class="result" data-result="backup"></div>
          </section>
        </div>`;

      body.querySelectorAll("[data-export]").forEach((button) => {
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            const path = button.dataset.export === "json"
              ? "/api/data/export.json"
              : "/api/data/export.csv.zip";
            king.downloadBlob(await king.fetchBlob(path));
          } catch (error) {
            king.toast(error.message, "error");
          } finally {
            button.disabled = false;
          }
        });
      });

      body.querySelector('[data-act="backup-download"]').addEventListener("click", async (event) => {
        const passphrase = body.querySelector("#backupPassphrase").value;
        const confirmation = body.querySelector("#backupPassphraseConfirm").value;
        if (passphrase.length < 12) {
          king.toast("Use a passphrase of at least 12 characters.", "error");
          return;
        }
        if (passphrase !== confirmation) {
          king.toast("The backup passphrases do not match.", "error");
          return;
        }
        event.currentTarget.disabled = true;
        try {
          king.downloadBlob(await king.fetchBlob("/api/data/backup", {
            method: "POST",
            body: JSON.stringify({ passphrase }),
          }));
          body.querySelector("#backupPassphrase").value = "";
          body.querySelector("#backupPassphraseConfirm").value = "";
          king.toast("Encrypted backup created.", "success");
        } catch (error) {
          king.toast(error.message, "error");
        } finally {
          event.currentTarget.disabled = false;
        }
      });

      body.querySelector('[data-act="backup-validate"]').addEventListener("click", async (event) => {
        const file = body.querySelector("#backupValidationFile").files[0];
        const passphrase = body.querySelector("#backupValidationPassphrase").value;
        const result = body.querySelector('[data-result="backup"]');
        if (!file || !passphrase) {
          king.toast("Choose a backup and enter its passphrase.", "error");
          return;
        }
        event.currentTarget.disabled = true;
        result.className = "result";
        result.textContent = "Validating...";
        const form = new FormData();
        form.append("file", file);
        form.append("passphrase", passphrase);
        try {
          const report = await king.fetchMultipart("/api/data/backup/validate", form);
          result.className = "result ok";
          result.textContent =
            `Valid backup · schema v${report.schema_version} · ` +
            `${new Intl.NumberFormat().format(report.database_bytes)} database bytes`;
          body.querySelector("#backupValidationPassphrase").value = "";
        } catch (error) {
          result.className = "result err";
          result.textContent = error.message;
        } finally {
          event.currentTarget.disabled = false;
        }
      });
    },
    security: (body) => {
      const base = data.kv.public_base_url?.value || "";
      body.innerHTML = `
        <div class="field">
          <label for="publicBaseUrl">Canonical HTTPS URL</label>
          <input id="publicBaseUrl" type="url" value="${king.escapeHTML(base)}" placeholder="https://mealprep.example.com">
        </div>
        <button class="btn btn-primary" data-act="base-save" type="button">Save URL</button>
        <hr class="hr-dotted">
        <div class="field"><label for="currentPassword">Current password</label><input id="currentPassword" type="password" autocomplete="current-password"></div>
        <div class="field"><label for="newPassword">New password</label><input id="newPassword" type="password" autocomplete="new-password" minlength="8"></div>
        <button class="btn btn-primary" data-act="password-save" type="button">Change password</button>
        <button class="btn" data-act="logout-all" type="button">Sign out everywhere</button>`;
      body.querySelector('[data-act="base-save"]').addEventListener("click", async () => {
        try {
          const value = body.querySelector("#publicBaseUrl").value.trim();
          const result = await king.fetchJSON("/api/settings/kv/public_base_url", {
            method: "PATCH", body: JSON.stringify({ value }),
          });
          data.kv.public_base_url = { value: result.value, is_default: false };
          king.toast("Canonical URL saved.", "success");
        } catch (error) { king.toast(error.message, "error"); }
      });
      body.querySelector('[data-act="password-save"]').addEventListener("click", async () => {
        try {
          await king.fetchJSON("/api/change-password", {
            method: "POST",
            body: JSON.stringify({
              current: body.querySelector("#currentPassword").value,
              new: body.querySelector("#newPassword").value,
            }),
          });
          body.querySelector("#currentPassword").value = "";
          body.querySelector("#newPassword").value = "";
          king.toast("Password changed.", "success");
        } catch (error) { king.toast(error.message, "error"); }
      });
      body.querySelector('[data-act="logout-all"]').addEventListener("click", async () => {
        try {
          await king.fetchJSON("/api/logout-all", { method: "POST", body: "{}" });
        } finally {
          window.location.href = "/login";
        }
      });
    },
    secrets: (body) => {
      const env = data.env;
      const row = (k, secret = false) => {
        const meta = secret ? (env[k]?.set ? `set · ${env[k].length} chars` : "not set")
                            : (env[k]?.value ? "" : "not set");
        return `<div class="kv-row"><span class="key">${k}</span>` +
               `<input type="${secret?"password":"text"}" id="sec_${k}" value="${secret?"":king.escapeHTML(env[k]?.value||"")}" placeholder="${meta}"></div>`;
      };
      body.innerHTML = `
        ${row("GEMINI_API_KEY", true)}
        <div style="display:flex; gap:.5rem; align-items:center; margin: var(--sp-2) 0;">
          <button class="btn" data-act="test-gemini">Test Gemini key</button>
          <span class="result" data-result="test-gemini"></span>
        </div>
        <hr class="hr-dotted">
        ${row("SMTP_HOST")}
        ${row("SMTP_PORT")}
        ${row("SMTP_USER")}
        ${row("SMTP_PASS", true)}
        ${row("SMTP_FROM")}
        ${row("OWNER_EMAIL")}
        <div style="margin-top: var(--sp-3);">
          <button class="btn btn-primary" data-act="secrets-save">Save</button>
        </div>
        <div class="result" id="secretsResult"></div>`;
      // Test button: hits the saved key OR the value typed in the input
      // (untyped → "" → backend falls back to saved env var).
      body.querySelector('[data-act="test-gemini"]').addEventListener("click", async (e) => {
        const out = body.querySelector('[data-result="test-gemini"]');
        const typed = body.querySelector("#sec_GEMINI_API_KEY")?.value || "";
        e.target.disabled = true;
        out.className = "result"; out.textContent = "Testing…";
        try {
          const r = await king.fetchJSON("/api/settings/test/gemini", {
            method: "POST",
            body: JSON.stringify(typed ? { key: typed } : {}),
          });
          out.textContent = r.message;
          out.className = "result " + (r.ok ? "ok" : "err");
        } catch (err) {
          out.textContent = `Network error: ${err.message}`;
          out.className = "result err";
        } finally {
          e.target.disabled = false;
        }
      });
      body.querySelector('[data-act="secrets-save"]').addEventListener("click", async () => {
        const payload = {};
        ["GEMINI_API_KEY","SMTP_HOST","SMTP_PORT","SMTP_USER","SMTP_PASS","SMTP_FROM","OWNER_EMAIL"].forEach((k) => {
          const el = body.querySelector(`#sec_${k}`);
          if (el && el.value !== "") payload[k] = el.value;
        });
        try {
          const r = await king.fetchJSON("/api/settings/secrets", { method:"PATCH", body: JSON.stringify(payload) });
          data.env = r.env || data.env;
          king.toast(r.persisted ? "Saved." : "Saved (in-memory only — disk write failed).", r.persisted ? "success" : "error");
          renderers.secrets(body);
        } catch (e) { king.toast(e.message, "error"); }
      });
    },
    llm: (body) => {
      body.innerHTML = `<p class="dim">Loading…</p>`;
      king.fetchJSON("/api/llm/budget").then(d => {
        const fmt = (rows) => {
          if (!rows.length) return `<p class="dim">No calls.</p>`;
          return `<table style="width:100%; border-collapse: collapse; font-size: var(--fs-sm);">
            <thead><tr><th style="text-align:left; padding: 4px;">model</th><th style="text-align:left; padding: 4px;">status</th><th style="text-align:right; padding: 4px;">calls</th><th style="text-align:right; padding: 4px;">in tok</th><th style="text-align:right; padding: 4px;">out tok</th></tr></thead>
            <tbody>${rows.map(r => `<tr style="border-top:1px dotted var(--hairline);"><td style="padding:4px;">${king.escapeHTML(r.model)}</td><td style="padding:4px;">${king.escapeHTML(r.status)}</td><td class="num" style="text-align:right; padding:4px;">${r.n}</td><td class="num" style="text-align:right; padding:4px;">${r.in_t}</td><td class="num" style="text-align:right; padding:4px;">${r.out_t}</td></tr>`).join("")}</tbody>
          </table>`;
        };
        body.innerHTML = `
          <p class="eyebrow">Today</p>${fmt(d.today)}
          <p class="eyebrow" style="margin-top: var(--sp-3);">Last 7 days</p>${fmt(d.week)}
          <p class="eyebrow" style="margin-top: var(--sp-3);">Last 30 days</p>${fmt(d.month)}
          <p class="hint" style="margin-top: var(--sp-3);">Recipe generation is capped at 5/day. Translations, scrape-fallback, and the daily total are uncapped (free-tier limit applies).</p>`;
      }).catch(e => { body.innerHTML = `<p class="result err">${king.escapeHTML(e.message)}</p>`; });
    },
  };

  // ---- helpers used by multiple renderers ----

  function renderTagSection(host, key) {
    const wrap = document.createElement("div");
    wrap.innerHTML = `
      <div class="kv-row"><span class="key">${key}</span>
        <div class="tag-input" data-tags="${key}"></div>
      </div>
      <div style="margin-top: var(--sp-2);">
        <button class="btn btn-primary" data-act="prefs-save">Save</button>
      </div>`;
    host.appendChild(wrap);
    const c = wrap.querySelector(`[data-tags="${key}"]`);
    const input = document.createElement("input");
    input.type = "text"; input.placeholder = "type and press Enter…";
    c.appendChild(input);
    function render() {
      [...c.querySelectorAll(".tag")].forEach((n) => n.remove());
      (data.preferences[key] || []).forEach((val) => {
        const tag = document.createElement("span");
        tag.className = "tag";
        tag.innerHTML = `${king.escapeHTML(val)}<button type="button" aria-label="remove">×</button>`;
        tag.querySelector("button").addEventListener("click", () => {
          data.preferences[key] = data.preferences[key].filter((x) => x !== val);
          render();
        });
        c.insertBefore(tag, input);
      });
    }
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        const v = input.value.trim().replace(/,/g, "");
        if (!v) return;
        const list = data.preferences[key] || (data.preferences[key] = []);
        if (!list.includes(v)) list.push(v);
        input.value = ""; render();
      }
    });
    render();
    wrap.querySelector('[data-act="prefs-save"]').addEventListener("click", async () => {
      try {
        await king.fetchJSON("/api/settings/preferences", {
          method: "PATCH",
          body: JSON.stringify({ [key]: data.preferences[key] }),
        });
        king.toast("Saved.", "success");
      } catch (e) { king.toast(e.message, "error"); }
    });
  }

  function appendKVHeader(host, kvKey, label) {
    const r = document.createElement("div");
    r.className = "kv-row";
    r.innerHTML = `
      <span class="key">${label}</span>
      <span data-kv-host="${kvKey}"></span>
      <button class="reset" data-reset="${kvKey}">reset</button>`;
    host.appendChild(r);
    return r;
  }

  function renderKVNumber(host, kvKey, label) {
    const r = appendKVHeader(host, kvKey, label);
    const slot = r.querySelector(`[data-kv-host="${kvKey}"]`);
    slot.innerHTML = `<input type="number" value="${king.escapeHTML(data.kv[kvKey]?.value ?? "")}">`;
    const input = slot.querySelector("input");
    input.addEventListener("change", async () => {
      const value = parseFloat(input.value);
      try {
        await king.fetchJSON(`/api/settings/kv/${kvKey}`, { method: "PATCH", body: JSON.stringify({ value }) });
        data.kv[kvKey] = { value, is_default: false };
        king.toast(`${label} saved.`, "success");
        decorateSectionHeaders();
      } catch (e) { king.toast(e.message, "error"); }
    });
    r.querySelector(".reset").addEventListener("click", async () => {
      try {
        const r2 = await king.fetchJSON(`/api/settings/kv/${kvKey}/reset`, { method: "POST", body: "{}" });
        data.kv[kvKey] = { value: r2.value, is_default: true };
        input.value = r2.value;
        king.toast("Reset to default.", "success");
        decorateSectionHeaders();
      } catch (e) { king.toast(e.message, "error"); }
    });
  }

  function renderKVSelect(host, kvKey, label, options) {
    const r = appendKVHeader(host, kvKey, label);
    const slot = r.querySelector(`[data-kv-host="${kvKey}"]`);
    const cur = data.kv[kvKey]?.value;
    slot.innerHTML = `<select>${options.map(([v,l]) =>
      `<option value="${v}"${cur===v?" selected":""}>${king.escapeHTML(l)}</option>`).join("")}</select>`;
    const sel = slot.querySelector("select");
    sel.addEventListener("change", async () => {
      const value = sel.value;
      try {
        await king.fetchJSON(`/api/settings/kv/${kvKey}`, { method: "PATCH", body: JSON.stringify({ value }) });
        data.kv[kvKey] = { value, is_default: false };
        king.toast(`${label} saved.`, "success");
        decorateSectionHeaders();
      } catch (e) { king.toast(e.message, "error"); }
    });
    r.querySelector(".reset").addEventListener("click", async () => {
      try {
        const r2 = await king.fetchJSON(`/api/settings/kv/${kvKey}/reset`, { method: "POST", body: "{}" });
        data.kv[kvKey] = { value: r2.value, is_default: true };
        sel.value = r2.value;
        king.toast("Reset to default.", "success");
        decorateSectionHeaders();
      } catch (e) { king.toast(e.message, "error"); }
    });
  }

  function renderKVBoolean(host, kvKey, label) {
    const row = appendKVHeader(host, kvKey, label);
    const slot = row.querySelector(`[data-kv-host="${kvKey}"]`);
    slot.innerHTML = `<input type="checkbox"${data.kv[kvKey]?.value ? " checked" : ""}>`;
    const input = slot.querySelector("input");
    input.addEventListener("change", async () => {
      try {
        await king.fetchJSON(`/api/settings/kv/${kvKey}`, {
          method: "PATCH", body: JSON.stringify({ value: input.checked }),
        });
        data.kv[kvKey] = { value: input.checked, is_default: false };
        decorateSectionHeaders();
      } catch (error) {
        input.checked = !input.checked;
        king.toast(error.message, "error");
      }
    });
    row.querySelector(".reset").addEventListener("click", async () => {
      const result = await king.fetchJSON(`/api/settings/kv/${kvKey}/reset`, {
        method: "POST", body: "{}",
      });
      data.kv[kvKey] = { value: result.value, is_default: true };
      input.checked = Boolean(result.value);
      decorateSectionHeaders();
    });
  }

  function renderKVText(host, kvKey, label) {
    const row = appendKVHeader(host, kvKey, label);
    const slot = row.querySelector(`[data-kv-host="${kvKey}"]`);
    slot.innerHTML = `<input type="text" value="${king.escapeHTML(data.kv[kvKey]?.value || "")}">`;
    const input = slot.querySelector("input");
    input.addEventListener("change", async () => {
      try {
        const result = await king.fetchJSON(`/api/settings/kv/${kvKey}`, {
          method: "PATCH", body: JSON.stringify({ value: input.value.trim() }),
        });
        data.kv[kvKey] = { value: result.value, is_default: false };
        king.toast(`${label} saved.`, "success");
      } catch (error) { king.toast(error.message, "error"); }
    });
    row.querySelector(".reset").addEventListener("click", async () => {
      const result = await king.fetchJSON(`/api/settings/kv/${kvKey}/reset`, {
        method: "POST", body: "{}",
      });
      data.kv[kvKey] = { value: result.value, is_default: true };
      input.value = result.value;
    });
  }

  function renderKVNumberMap(host, kvKey, label, keys, toDisplay, fromDisplay) {
    const cur = data.kv[kvKey]?.value || {};
    const rows = keys.map((k) =>
      `<div class="kv-row"><span class="key">${k}</span>` +
      `<input type="number" data-mk="${k}" value="${toDisplay(cur[k])}">` +
      `</div>`).join("");
    const wrap = document.createElement("div");
    wrap.innerHTML = `<p class="eyebrow">${label}</p>${rows}` +
      `<div style="margin-top: var(--sp-2);"><button class="btn btn-primary" data-act="kvmap-save">Save</button>` +
      `<button class="btn" data-act="kvmap-reset">Reset</button></div>`;
    host.appendChild(wrap);
    wrap.querySelector('[data-act="kvmap-save"]').addEventListener("click", async () => {
      const out = {};
      wrap.querySelectorAll("input[data-mk]").forEach((el) => { out[el.dataset.mk] = fromDisplay(parseFloat(el.value) || 0); });
      try {
        await king.fetchJSON(`/api/settings/kv/${kvKey}`, { method: "PATCH", body: JSON.stringify({ value: out }) });
        data.kv[kvKey] = { value: out, is_default: false };
        king.toast(`${label} saved.`, "success");
        decorateSectionHeaders();
      } catch (e) { king.toast(e.message, "error"); }
    });
    wrap.querySelector('[data-act="kvmap-reset"]').addEventListener("click", async () => {
      try {
        const r = await king.fetchJSON(`/api/settings/kv/${kvKey}/reset`, { method: "POST", body: "{}" });
        data.kv[kvKey] = { value: r.value, is_default: true };
        wrap.remove();
        renderKVNumberMap(host, kvKey, label, keys, toDisplay, fromDisplay);
        king.toast("Reset to default.", "success");
        decorateSectionHeaders();
      } catch (e) { king.toast(e.message, "error"); }
    });
  }

  // ---- collapse / expand ----

  document.addEventListener("DOMContentLoaded", async () => {
    try { await load(); } catch (e) { king.toast(`Load failed: ${e.message}`, "error"); return; }

    document.querySelectorAll(".settings-section-head").forEach((head) => {
      head.addEventListener("click", () => {
        const sec = head.closest(".settings-section");
        const id = sec.dataset.section;
        const open = sec.classList.toggle("open");
        head.setAttribute("aria-expanded", String(open));
        if (open) {
          const body = sec.querySelector("[data-section-body]");
          if (!body.dataset.rendered) {
            body.innerHTML = "";
            (renderers[id] || ((b) => { b.textContent = "(empty)"; }))(body);
            body.dataset.rendered = "1";
          }
        }
      });
    });

    document.getElementById("openAllBtn").addEventListener("click", () => {
      document.querySelectorAll(".settings-section").forEach((sec) => {
        if (!sec.classList.contains("open")) sec.querySelector(".settings-section-head").click();
      });
    });
  });
})();
