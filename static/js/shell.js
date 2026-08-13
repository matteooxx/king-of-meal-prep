// Profile drawer + sign-out. Lives at the shell level so every page gets it.
(function () {
  const $ = (id) => document.getElementById(id);
  let returnFocus = null;

  function focusableDrawerItems() {
    return [...$("profileDrawer").querySelectorAll(
      "button:not([disabled]), a[href], input:not([disabled]), " +
      "select:not([disabled]), textarea:not([disabled]), " +
      "[tabindex]:not([tabindex='-1'])"
    )].filter((element) => element.offsetParent !== null);
  }

  function openDrawer() {
    returnFocus = document.activeElement;
    $("profileDrawer").hidden = false;
    $("drawerOverlay").hidden = false;
    $("profileTrigger").setAttribute("aria-expanded", "true");
    document.body.style.overflow = "hidden";
    requestAnimationFrame(() => {
      $("profileDrawer").classList.add("open");
      $("drawerClose").focus();
    });
    refreshMacroStrip();
  }
  function closeDrawer() {
    $("profileDrawer").classList.remove("open");
    setTimeout(() => {
      $("profileDrawer").hidden = true;
      $("drawerOverlay").hidden = true;
      $("profileTrigger").setAttribute("aria-expanded", "false");
      document.body.style.overflow = "";
      if (returnFocus?.focus) returnFocus.focus();
      returnFocus = null;
    }, 200);
  }

  // Glanceable macro mini-strip in the drawer head.
  // Pulls today's totals from /api/log; renders 4 stacked thin bars.
  async function refreshMacroStrip() {
    try {
      const today = king.isoToday();
      const d = await king.fetchJSON(`/api/log/${today}`);
      const labels = { kcal: "kcal", protein_g: "P", carbs_g: "C", fat_g: "F" };
      const colors = {
        kcal:      "var(--macro-kcal)",
        protein_g: "var(--macro-protein)",
        carbs_g:   "var(--macro-carbs)",
        fat_g:     "var(--macro-fat)",
      };
      $("macroMini").innerHTML = ["kcal","protein_g","carbs_g","fat_g"].map(k => {
        const cur = Math.round(d.totals[k] || 0);
        const tgt = Math.round(d.target[k] || 1);
        const pct = Math.min(150, (cur / tgt) * 100);
        return `<div class="macro-mini-row">
          <span class="dim">${labels[k]}</span>
          <span class="macro-mini-bar"><span class="macro-mini-fill" style="width:${Math.min(100, pct)}%; background:${colors[k]};"></span></span>
          <span class="num">${cur}<span class="dim">/${tgt}</span></span>
        </div>`;
      }).join("");
    } catch {
      $("macroMini").innerHTML = `<p class="dim" style="font-size: var(--fs-xxs);">—</p>`;
    }
  }

  async function signout() {
    try {
      await king.fetchJSON("/api/logout", { method: "POST", body: "{}" });
    } catch {}
    Object.keys(localStorage).forEach((key) => {
      if (
        key.startsWith("king-shopping") ||
        key.startsWith("king-cook-progress-")
      ) {
        localStorage.removeItem(key);
      }
    });
    if ("caches" in window) {
      caches.keys().then((keys) => keys.forEach((key) => caches.delete(key)));
    }
    window.location.href = "/login";
  }

  function applyTheme(choice) {
    localStorage.setItem("king-theme", choice);
    const dark = choice === "system"
      ? matchMedia("(prefers-color-scheme: dark)").matches
      : choice === "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.classList.toggle("active", button.dataset.themeChoice === choice);
    });
  }

  function refreshConnection() {
    const online = navigator.onLine;
    const dot = $("connectionDot");
    const label = $("connectionLabel");
    if (dot) dot.classList.toggle("offline", !online);
    if (label) label.textContent = online ? "Online" : "Offline";
  }

  document.addEventListener("DOMContentLoaded", () => {
    const t = $("profileTrigger");
    if (t) t.addEventListener("click", openDrawer);
    const c = $("drawerClose");
    if (c) c.addEventListener("click", closeDrawer);
    const o = $("drawerOverlay");
    if (o) o.addEventListener("click", closeDrawer);
    const s = $("signoutBtn");
    if (s) s.addEventListener("click", signout);
    const theme = localStorage.getItem("king-theme") || "dark";
    applyTheme(theme);
    document.querySelectorAll("[data-theme-choice]").forEach((button) => {
      button.addEventListener("click", () => applyTheme(button.dataset.themeChoice));
    });
    refreshConnection();
    window.addEventListener("online", refreshConnection);
    window.addEventListener("offline", refreshConnection);
    document.addEventListener("keydown", (e) => {
      if ($("profileDrawer").hidden) return;
      if (e.key === "Escape") {
        e.preventDefault();
        closeDrawer();
        return;
      }
      if (e.key !== "Tab") return;
      const focusable = focusableDrawerItems();
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    });
  });
})();
