// /scan - barcode lookup, durable receipt review, and recognition inbox.
(function () {
  const $ = (id) => document.getElementById(id);
  const MODES = ["barcode", "receipt", "review"];
  const tabs = {
    barcode: $("modeBarcode"),
    receipt: $("modeReceipt"),
    review: $("modeReview"),
  };
  const panels = {
    barcode: $("barcodeMode"),
    receipt: $("receiptMode"),
    review: $("reviewMode"),
  };

  let scanner = null;
  let mode = "barcode";
  let busyDecoding = false;
  let lastScannedEan = null;
  let lastProposalKey = null;
  let lastInboxId = null;
  let currentReceipt = null;

  const sourceLabels = {
    usda: "USDA",
    off: "Open Food Facts",
    user: "Reviewed",
    manual: "Manual",
    unknown: "No source",
  };

  function confidenceBadge(confidence = "unknown", source = "unknown", basis = "") {
    const level = ["high", "medium", "low", "unknown"].includes(confidence)
      ? confidence
      : "unknown";
    const sourceLabel = sourceLabels[source] || "No source";
    const title = basis
      ? `${sourceLabel}; ${level} confidence; ${basis.replaceAll("_", " ")}`
      : `${sourceLabel}; ${level} confidence`;
    return `<span class="confidence-badge confidence-${level}" title="${king.escapeHTML(title)}">` +
      `${king.escapeHTML(sourceLabel)} · ${king.escapeHTML(level)}</span>`;
  }

  function barcodeNutritionHTML(nutrition = {}) {
    const values = [
      nutrition.kcal_100g != null ? `${Math.round(nutrition.kcal_100g)} kcal` : null,
      nutrition.protein_100g != null ? `${king.formatNumber(nutrition.protein_100g)}g P` : null,
      nutrition.carbs_100g != null ? `${king.formatNumber(nutrition.carbs_100g)}g C` : null,
      nutrition.fat_100g != null ? `${king.formatNumber(nutrition.fat_100g)}g F` : null,
    ].filter(Boolean).join(" · ");
    return `<span class="barcode-nutrition-summary">
      ${confidenceBadge(
        nutrition.confidence,
        nutrition.source,
        nutrition.basis
      )}
      ${values ? `<small class="num">${king.escapeHTML(values)} / 100 g</small>` : ""}
    </span>`;
  }

  function stateBadge(value) {
    const safe = ["review", "committed", "discarded", "open"].includes(value)
      ? value
      : "unknown";
    return `<span class="state-badge state-${safe}">${king.escapeHTML(safe)}</span>`;
  }

  function formatMoney(value, currency = "EUR") {
    const amount = Number(value);
    if (!Number.isFinite(amount)) return "-";
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency",
        currency,
      }).format(amount);
    } catch {
      return `${amount.toFixed(2)} ${currency}`;
    }
  }

  function updateBarcodePortionHint() {
    const quantity = Number.parseFloat($("bcQty").value);
    const portions = Number.parseFloat($("bcPortions").value);
    $("bcPortionHint").textContent =
      Number.isFinite(quantity) && quantity > 0 &&
      Number.isFinite(portions) && portions > 0
        ? `${king.formatNumber(quantity / portions)} ${$("bcUnit").value || "piece"} each`
        : "";
  }

  async function setMode(next) {
    if (!MODES.includes(next)) return;
    mode = next;
    MODES.forEach((name) => {
      const active = name === next;
      panels[name].hidden = !active;
      tabs[name].classList.toggle("active", active);
      tabs[name].setAttribute("aria-selected", String(active));
      tabs[name].tabIndex = active ? 0 : -1;
    });
    await stopBarcode();
    if (next === "barcode") startBarcode();
    if (next === "receipt") await loadReceipts();
    if (next === "review") await loadInbox();
  }

  function startBarcode() {
    if (scanner || typeof Html5Qrcode === "undefined" || mode !== "barcode") return;
    scanner = new Html5Qrcode("reader");
    busyDecoding = false;
    scanner.start(
      { facingMode: "environment" },
      { fps: 10, qrbox: { width: 280, height: 140 } },
      onCode,
      () => {}
    ).catch((error) => {
      $("bcHint").textContent = "Camera unavailable: " + error.message;
    });
  }

  async function stopBarcode() {
    if (!scanner) return;
    const active = scanner;
    scanner = null;
    try { await active.stop(); } catch {}
    try { active.clear(); } catch {}
  }

  async function onCode(decoded) {
    if (busyDecoding) return;
    busyDecoding = true;
    $("bcHint").textContent = `Scanned: ${decoded}. Looking up...`;
    await stopBarcode();
    await lookupBarcode(decoded);
  }

  async function lookupBarcode(value) {
    const ean = String(value || "").trim();
    if (!ean) {
      $("bcResult").className = "result err";
      $("bcResult").textContent = "Enter a barcode.";
      return;
    }
    if ($("lookupEanBtn").disabled) return;
    busyDecoding = true;
    lastScannedEan = ean;
    lastProposalKey = null;
    lastInboxId = null;
    $("lookupEanBtn").disabled = true;
    $("bcCard").hidden = true;
    $("bcReviewBtn").hidden = true;
    $("bcNutrition").innerHTML = "";
    $("bcResult").className = "result";
    $("bcResult").textContent = "Looking up barcode...";
    await stopBarcode();
    try {
      const result = await king.fetchJSON("/api/pantry/from-barcode", {
        method: "POST",
        body: JSON.stringify({ ean }),
      });
      lastProposalKey = result.proposal.ingredient_key || null;
      $("bcName").value = result.proposal.display_name || "";
      $("bcQty").value = result.proposal.quantity ?? 1;
      $("bcUnit").value = result.proposal.unit || "piece";
      $("bcPortions").value = "";
      updateBarcodePortionHint();
      const nutrition = result.details?.nutrition || {};
      $("bcNutrition").innerHTML = barcodeNutritionHTML(nutrition);
      $("bcCard").hidden = false;
      $("bcResult").className = "result ok";
      const sourceMessage = {
        local: "Found in your saved products.",
        off_index: "Found in the offline index.",
        off_cache: "Found in the local lookup cache.",
        off_online: "Found on Open Food Facts and cached locally.",
      }[result.source] || "Product found.";
      $("bcResult").textContent = `${sourceMessage} Review before saving.`;
    } catch (error) {
      if (error.status === 400 || error.status === 422) {
        lastScannedEan = null;
        $("bcCard").hidden = true;
        $("bcResult").className = "result err";
        $("bcResult").textContent = error.message;
        $("bcHint").textContent = "Try the scan again or type the digits manually.";
        setTimeout(startBarcode, 1200);
        return;
      }
      lastInboxId = error.data?.inbox_item?.id || null;
      $("bcReviewBtn").hidden = !lastInboxId;
      $("bcResult").className = "result";
      const lead = error.status === 503
        ? "Online lookup is temporarily unavailable."
        : "No product match was found.";
      $("bcResult").textContent =
        `${lead} This barcode is in Review; naming it here will resolve it.`;
      $("bcName").value = "";
      $("bcQty").value = 1;
      $("bcUnit").value = "piece";
      $("bcPortions").value = "";
      updateBarcodePortionHint();
      $("bcNutrition").innerHTML = confidenceBadge("unknown", "unknown");
      $("bcCard").hidden = false;
      $("bcName").focus();
      loadInbox();
    } finally {
      $("lookupEanBtn").disabled = false;
    }
  }

  async function saveBarcode() {
    const button = $("bcSaveBtn");
    if (button.disabled) return;
    button.disabled = true;
    try {
      const photo = $("bcPhoto").files[0];
      if (photo && lastInboxId) {
        const form = new FormData();
        form.append("file", photo);
        await king.fetchMultipart(
          `/api/recognition-inbox/${lastInboxId}/photo`,
          form
        );
      }
      await king.fetchJSON("/api/pantry", {
        method: "POST",
        body: JSON.stringify({
          display_name: $("bcName").value.trim(),
          ingredient_key: lastProposalKey,
          quantity: Number.parseFloat($("bcQty").value) || 1,
          unit: $("bcUnit").value || "piece",
          portions: $("bcPortions").value
            ? Number.parseFloat($("bcPortions").value)
            : null,
          expires_on: $("bcExp").value || null,
          source: "barcode",
          ean: lastScannedEan,
        }),
      });
      king.toast("Added to pantry.", "success");
      resetScanCard();
      loadInbox();
      startBarcode();
    } catch (error) {
      king.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  function resetScanCard() {
    $("bcCard").hidden = true;
    $("bcName").value = "";
    $("bcQty").value = 1;
    $("bcUnit").value = "piece";
    $("bcPortions").value = "";
    $("bcExp").value = "";
    $("bcPhoto").value = "";
    $("manualEan").value = "";
    $("bcResult").textContent = "";
    $("bcResult").className = "result";
    $("bcNutrition").innerHTML = "";
    updateBarcodePortionHint();
    $("bcReviewBtn").hidden = true;
    $("bcHint").textContent = "Position the barcode inside the frame. Or type it below.";
    lastScannedEan = null;
    lastProposalKey = null;
    lastInboxId = null;
    busyDecoding = false;
  }

  async function scanAgain() {
    resetScanCard();
    await stopBarcode();
    startBarcode();
  }

  // Receipt reconciliation -------------------------------------------------

  async function uploadReceipt() {
    const file = $("receiptFile").files[0];
    if (!file) {
      king.toast("Choose a receipt photo.", "error");
      return;
    }
    const button = $("receiptUploadBtn");
    button.disabled = true;
    $("receiptResult").className = "result";
    $("receiptResult").textContent = "Processing receipt...";
    const form = new FormData();
    form.append("file", file);
    form.append("merchant", $("receiptMerchant").value.trim());
    form.append("purchased_on", $("receiptDate").value);
    form.append("currency", $("receiptCurrency").value.trim().toUpperCase() || "EUR");
    try {
      const result = await king.fetchMultipart("/api/receipts", form);
      currentReceipt = result.receipt;
      $("receiptResult").className = "result ok";
      $("receiptResult").textContent =
        `${currentReceipt.item_count} candidate item${currentReceipt.item_count === 1 ? "" : "s"} ready for review.`;
      renderReceipt(currentReceipt);
      await Promise.all([loadReceipts(), loadInbox()]);
    } catch (error) {
      $("receiptResult").className = "result err";
      $("receiptResult").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  async function loadReceipts() {
    try {
      const result = await king.fetchJSON("/api/receipts?limit=30");
      renderReceiptHistory(result.receipts || []);
    } catch (error) {
      $("receiptHistoryList").innerHTML =
        `<p class="result err">${king.escapeHTML(error.message)}</p>`;
    }
  }

  function renderReceiptHistory(receiptList) {
    $("receiptHistorySummary").textContent =
      `${receiptList.length} saved session${receiptList.length === 1 ? "" : "s"}`;
    if (!receiptList.length) {
      $("receiptHistoryList").innerHTML =
        '<div class="empty-state compact"><i data-lucide="receipt"></i><strong>No receipts yet</strong></div>';
      return;
    }
    $("receiptHistoryList").innerHTML = receiptList.map((receipt) => `
      <button class="receipt-history-row" type="button" data-receipt-id="${receipt.id}">
        <span class="receipt-history-main">
          <strong>${king.escapeHTML(receipt.merchant || `Receipt #${receipt.id}`)}</strong>
          <small>${king.escapeHTML(receipt.purchased_on || receipt.created_at.slice(0, 10))} · ${receipt.item_count} items</small>
        </span>
        <span class="receipt-history-total num">${formatMoney(receipt.total, receipt.currency)}</span>
        ${stateBadge(receipt.status)}
        <i data-lucide="chevron-right"></i>
      </button>
    `).join("");
    $("receiptHistoryList").querySelectorAll("[data-receipt-id]").forEach((row) => {
      row.addEventListener("click", () => openReceipt(Number(row.dataset.receiptId)));
    });
  }

  async function openReceipt(receiptId) {
    try {
      currentReceipt = await king.fetchJSON(`/api/receipts/${receiptId}`);
      renderReceipt(currentReceipt);
      $("receiptDetail").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      king.toast(error.message, "error");
    }
  }

  function renderReceipt(receipt) {
    $("receiptDetail").hidden = false;
    $("receiptTitle").textContent = receipt.merchant || `Receipt #${receipt.id}`;
    const total = receipt.items.reduce(
      (sum, item) => sum + (Number(item.line_total) || 0),
      0
    );
    $("receiptSummary").innerHTML =
      `${stateBadge(receipt.status)} <span>${receipt.item_count} items · ` +
      `${receipt.priced_count} priced · ${formatMoney(total, receipt.currency)}</span>`;
    $("receiptImageLink").hidden = !receipt.image_url;
    if (receipt.image_url) $("receiptImageLink").href = receipt.image_url;

    if (!receipt.items.length) {
      $("receiptLines").innerHTML =
        '<div class="empty-state compact"><i data-lucide="scan-text"></i><strong>No item lines detected</strong></div>';
    } else {
      $("receiptLines").innerHTML = receipt.items.map((item) => receiptLineHTML(
        item,
        receipt.currency,
        receipt.status
      )).join("");
      bindReceiptRows(receipt);
    }
    const editable = receipt.status === "review";
    $("receiptActions").hidden = !editable;
    $("receiptCommitBtn").disabled = !receipt.items.length;
  }

  function receiptLineHTML(item, currency, receiptStatus) {
    const editable = receiptStatus === "review";
    const matched = item.matched_pantry_item_id
      ? `<span class="match-note"><i data-lucide="combine"></i>${king.escapeHTML(item.matched_pantry_name || "Matched pantry item")} · ` +
        `${king.formatNumber(item.matched_pantry_quantity)} ${king.escapeHTML(item.matched_pantry_unit || "")}</span>`
      : "";
    let priceNote = "";
    if (item.previous_price) {
      const previous = item.previous_price;
      const currentUnit = item.line_total == null
        ? null
        : Number(item.line_total) / Number(item.quantity);
      const comparable = previous.unit === item.unit;
      const difference = comparable && Number.isFinite(currentUnit)
        ? currentUnit - Number(previous.unit_price)
        : null;
      const change = Number.isFinite(difference)
        ? ` · ${difference >= 0 ? "+" : ""}${formatMoney(difference, currency)} per ${king.escapeHTML(item.unit)}`
        : "";
      priceNote =
        `<span class="price-note">Previous ${formatMoney(previous.unit_price, previous.currency)} per ${king.escapeHTML(previous.unit)}${change}</span>`;
    }
    const flags = [
      item.duplicate ? '<span class="warning-badge">Possible duplicate</span>' : "",
      confidenceBadge(
        item.nutrition_confidence,
        item.nutrition_source,
        item.nutrition_basis
      ),
      `<span class="ocr-badge ocr-${item.ocr_confidence}">OCR ${king.escapeHTML(item.ocr_confidence)}</span>`,
    ].join("");
    const mergeOption = item.matched_pantry_item_id
      ? `<option value="merge"${item.action === "merge" ? " selected" : ""}>Merge</option>`
      : "";
    return `
      <article class="receipt-line" data-receipt-item="${item.id}">
        <div class="receipt-line-head">
          <span class="raw-line">${king.escapeHTML(item.raw_line)}</span>
          <span class="trust-flags">${flags}</span>
        </div>
        <div class="receipt-line-fields">
          <div class="field receipt-name-field">
            <label for="receipt-name-${item.id}">Item</label>
            <input id="receipt-name-${item.id}" data-field="name" type="text"
                   value="${king.escapeHTML(item.display_name)}" maxlength="200"${editable ? "" : " disabled"}>
          </div>
          <div class="field">
            <label for="receipt-qty-${item.id}">Qty</label>
            <input id="receipt-qty-${item.id}" data-field="quantity" type="number"
                   value="${king.escapeHTML(item.quantity)}" min="0.000001" step="any"${editable ? "" : " disabled"}>
          </div>
          <div class="field">
            <label for="receipt-unit-${item.id}">Unit</label>
            <input id="receipt-unit-${item.id}" data-field="unit" type="text"
                   value="${king.escapeHTML(item.unit)}" maxlength="30"${editable ? "" : " disabled"}>
          </div>
          <div class="field">
            <label for="receipt-price-${item.id}">Line price</label>
            <input id="receipt-price-${item.id}" data-field="price" type="number"
                   value="${item.line_total == null ? "" : king.escapeHTML(item.line_total)}"
                   min="0" step="0.01"${editable ? "" : " disabled"}>
          </div>
          <div class="field">
            <label for="receipt-action-${item.id}">Action</label>
            <select id="receipt-action-${item.id}" data-field="action"${editable ? "" : " disabled"}>
              <option value="add"${item.action === "add" ? " selected" : ""}>Add</option>
              ${mergeOption}
              <option value="skip"${item.action === "skip" ? " selected" : ""}>Skip</option>
            </select>
          </div>
        </div>
        <div class="receipt-line-notes">${matched}${priceNote}</div>
      </article>`;
  }

  function bindReceiptRows(receipt) {
    receipt.items.forEach((item) => {
      const row = $(`receipt-name-${item.id}`)?.closest("[data-receipt-item]");
      if (!row) return;
      row._ingredientKey = item.ingredient_key;
      const nameInput = row.querySelector('[data-field="name"]');
      const actionInput = row.querySelector('[data-field="action"]');
      nameInput.addEventListener("input", () => {
        if (nameInput.value.trim() !== item.display_name) {
          row._ingredientKey = null;
          if (actionInput.value === "merge") actionInput.value = "add";
        } else {
          row._ingredientKey = item.ingredient_key;
        }
      });
      actionInput.addEventListener("change", () => {
        row.classList.toggle("is-skipped", actionInput.value === "skip");
      });
      row.classList.toggle("is-skipped", actionInput.value === "skip");
    });
  }

  function receiptCommitItems() {
    return currentReceipt.items.map((item) => {
      const row = document.querySelector(`[data-receipt-item="${item.id}"]`);
      const action = row.querySelector('[data-field="action"]').value;
      if (action === "skip") return { id: item.id, action };
      return {
        id: item.id,
        action,
        display_name: row.querySelector('[data-field="name"]').value.trim(),
        quantity: Number.parseFloat(row.querySelector('[data-field="quantity"]').value),
        unit: row.querySelector('[data-field="unit"]').value.trim() || "piece",
        line_total: row.querySelector('[data-field="price"]').value === ""
          ? null
          : Number.parseFloat(row.querySelector('[data-field="price"]').value),
        ingredient_key: row._ingredientKey,
      };
    });
  }

  async function commitReceipt() {
    if (!currentReceipt) return;
    const button = $("receiptCommitBtn");
    button.disabled = true;
    try {
      const result = await king.fetchJSON(
        `/api/receipts/${currentReceipt.id}/commit`,
        {
          method: "POST",
          body: JSON.stringify({ items: receiptCommitItems() }),
        }
      );
      currentReceipt = result.receipt;
      renderReceipt(currentReceipt);
      king.toast(
        `${result.added} added, ${result.merged} merged, ${result.skipped} skipped.`,
        "success"
      );
      await Promise.all([loadReceipts(), loadInbox()]);
    } catch (error) {
      king.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function discardReceipt() {
    if (!currentReceipt || !confirm("Discard this receipt review?")) return;
    try {
      await king.fetchJSON(`/api/receipts/${currentReceipt.id}/discard`, {
        method: "POST",
        body: "{}",
      });
      currentReceipt = await king.fetchJSON(`/api/receipts/${currentReceipt.id}`);
      renderReceipt(currentReceipt);
      king.toast("Receipt discarded.", "success");
      await Promise.all([loadReceipts(), loadInbox()]);
    } catch (error) {
      king.toast(error.message, "error");
    }
  }

  // Recognition inbox ------------------------------------------------------

  async function loadInbox() {
    try {
      const result = await king.fetchJSON("/api/recognition-inbox?status=open&limit=100");
      renderInbox(result.items || []);
    } catch (error) {
      $("reviewList").innerHTML =
        `<p class="result err">${king.escapeHTML(error.message)}</p>`;
    }
  }

  function renderInbox(items) {
    $("reviewCount").textContent = items.length;
    $("reviewCount").hidden = items.length === 0;
    $("reviewSummary").textContent =
      `${items.length} item${items.length === 1 ? "" : "s"} waiting`;
    if (!items.length) {
      $("reviewList").innerHTML =
        '<div class="empty-state"><i data-lucide="inbox"></i><strong>Review inbox clear</strong></div>';
      return;
    }
    $("reviewList").innerHTML = items.map(reviewItemHTML).join("");
    items.forEach(bindReviewItem);
  }

  function reviewItemHTML(item) {
    const kindLabels = {
      barcode: "Unknown barcode",
      product_photo: "Product photo",
      receipt_line: "Receipt line",
    };
    const heading = item.kind === "barcode"
      ? item.barcode_display || item.barcode
      : item.suggested_name || kindLabels[item.kind];
    const image = item.image_url
      ? `<img class="review-photo" src="${king.escapeHTML(item.image_url)}" alt="">`
      : "";
    const offLink = item.off_url
      ? `<a class="btn btn-ghost" href="${king.escapeHTML(item.off_url)}" target="_blank" rel="noreferrer">
           <i data-lucide="external-link"></i>Add details on Open Food Facts
         </a>`
      : "";
    const receiptLink = item.receipt_id
      ? `<button class="btn btn-ghost" type="button" data-open-receipt="${item.receipt_id}">
           <i data-lucide="receipt-text"></i>Receipt #${item.receipt_id}
         </button>`
      : "";
    const raw = item.raw_text
      ? `<p class="review-raw">${king.escapeHTML(item.raw_text)}</p>`
      : "";
    const addControl = item.kind === "receipt_line"
      ? ""
      : `<label class="toggle-row compact">
           <span><strong>Add to pantry</strong></span>
           <input type="checkbox" data-review-field="add" checked>
         </label>`;
    return `
      <article class="review-item" data-review-item="${item.id}">
        <header class="review-item-head">
          <div>
            <span class="label-tag">${king.escapeHTML(kindLabels[item.kind] || item.kind)}</span>
            <h3>${king.escapeHTML(heading || `Review item #${item.id}`)}</h3>
          </div>
          <div class="trust-flags" data-review-trust>
            ${confidenceBadge(item.nutrition_confidence, item.nutrition_source, item.nutrition_basis)}
            ${item.attempt_count > 1 ? `<span class="state-badge">${item.attempt_count} scans</span>` : ""}
          </div>
        </header>
        ${image}
        ${raw}
        <div class="review-item-fields">
          <div class="field review-name">
            <label for="review-name-${item.id}">Name</label>
            <input id="review-name-${item.id}" data-review-field="name" type="text"
                   value="${king.escapeHTML(item.suggested_name || "")}" maxlength="200">
          </div>
          <div class="field">
            <label for="review-qty-${item.id}">Qty</label>
            <input id="review-qty-${item.id}" data-review-field="quantity" type="number"
                   value="${king.escapeHTML(item.quantity || 1)}" min="0.000001" step="any">
          </div>
          <div class="field">
            <label for="review-unit-${item.id}">Unit</label>
            <input id="review-unit-${item.id}" data-review-field="unit" type="text"
                   value="${king.escapeHTML(item.unit || "piece")}" maxlength="30">
          </div>
          <div class="field">
            <label for="review-expiry-${item.id}">Expires</label>
            <input id="review-expiry-${item.id}" data-review-field="expiry" type="date">
          </div>
        </div>
        <div class="review-match-actions">
          <button class="btn" type="button" data-review-action="suggest">
            <i data-lucide="search"></i>Find nutrition match
          </button>
          ${offLink}
          ${receiptLink}
        </div>
        <div class="review-suggestions" data-review-suggestions hidden></div>
        <div class="review-photo-attach field">
          <label for="review-attach-${item.id}">${item.has_image ? "Replace photo" : "Attach photo"}</label>
          <input id="review-attach-${item.id}" data-review-field="photo" type="file"
                 accept="image/jpeg,image/png,image/webp" capture="environment">
        </div>
        <div class="review-item-actions">
          ${addControl}
          <span class="action-spacer"></span>
          <button class="btn btn-ghost" type="button" data-review-action="dismiss">
            <i data-lucide="x"></i>Dismiss
          </button>
          <button class="btn btn-primary" type="button" data-review-action="resolve">
            <i data-lucide="check"></i>Resolve
          </button>
        </div>
      </article>`;
  }

  function bindReviewItem(item) {
    const card = document.querySelector(`[data-review-item="${item.id}"]`);
    if (!card) return;
    card._selectedKey = item.suggested_key || null;
    card.querySelector('[data-review-action="suggest"]').addEventListener(
      "click",
      () => loadSuggestions(item, card)
    );
    card.querySelector('[data-review-action="resolve"]').addEventListener(
      "click",
      () => resolveReviewItem(item, card)
    );
    card.querySelector('[data-review-action="dismiss"]').addEventListener(
      "click",
      () => dismissReviewItem(item)
    );
    card.querySelector('[data-review-field="name"]').addEventListener("input", () => {
      card._selectedKey = null;
    });
    card.querySelector('[data-review-field="photo"]').addEventListener("change", (event) => {
      const file = event.target.files[0];
      if (file) attachReviewPhoto(item, file);
    });
    const receiptButton = card.querySelector("[data-open-receipt]");
    if (receiptButton) {
      receiptButton.addEventListener("click", async () => {
        await setMode("receipt");
        await openReceipt(Number(receiptButton.dataset.openReceipt));
      });
    }
  }

  async function loadSuggestions(item, card) {
    const query = card.querySelector('[data-review-field="name"]').value.trim();
    if (!query) {
      king.toast("Enter a name first.", "error");
      return;
    }
    const host = card.querySelector("[data-review-suggestions]");
    host.hidden = false;
    host.innerHTML = '<p class="dim">Searching...</p>';
    try {
      const result = await king.fetchJSON(
        `/api/recognition-inbox/${item.id}/suggestions?q=${encodeURIComponent(query)}`
      );
      const suggestions = result.suggestions || [];
      host.innerHTML = suggestions.length
        ? suggestions.map((suggestion, index) => `
            <button class="review-suggestion" type="button" data-suggestion="${index}">
              <span><strong>${king.escapeHTML(suggestion.display_name)}</strong>
              <small>${king.escapeHTML(suggestion.ingredient_key)}</small></span>
              ${confidenceBadge(
                suggestion.nutrition_confidence,
                suggestion.nutrition_source,
                suggestion.nutrition_basis
              )}
            </button>
          `).join("")
        : '<p class="dim">No nutrition match found.</p>';
      host.querySelectorAll("[data-suggestion]").forEach((button) => {
        button.addEventListener("click", () => {
          const selected = suggestions[Number(button.dataset.suggestion)];
          card._selectedKey = selected.ingredient_key;
          card.querySelector('[data-review-field="name"]').value =
            selected.display_name;
          card.querySelector("[data-review-trust]").innerHTML = confidenceBadge(
            selected.nutrition_confidence,
            selected.nutrition_source,
            "user_selected"
          );
          host.hidden = true;
        });
      });
    } catch (error) {
      host.innerHTML = `<p class="result err">${king.escapeHTML(error.message)}</p>`;
    }
  }

  async function resolveReviewItem(item, card) {
    const button = card.querySelector('[data-review-action="resolve"]');
    button.disabled = true;
    try {
      const addControl = card.querySelector('[data-review-field="add"]');
      const result = await king.fetchJSON(
        `/api/recognition-inbox/${item.id}/resolve`,
        {
          method: "POST",
          body: JSON.stringify({
            display_name: card.querySelector('[data-review-field="name"]').value.trim(),
            ingredient_key: card._selectedKey,
            quantity: Number.parseFloat(
              card.querySelector('[data-review-field="quantity"]').value
            ),
            unit: card.querySelector('[data-review-field="unit"]').value.trim() || "piece",
            expires_on: card.querySelector('[data-review-field="expiry"]').value || null,
            add_to_pantry: addControl ? addControl.checked : false,
          }),
        }
      );
      king.toast("Review item resolved.", "success");
      await loadInbox();
      if (
        result.receipt_id &&
        currentReceipt?.id === result.receipt_id
      ) {
        await openReceipt(result.receipt_id);
      }
    } catch (error) {
      king.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function dismissReviewItem(item) {
    if (!confirm("Dismiss this review item?")) return;
    try {
      await king.fetchJSON(`/api/recognition-inbox/${item.id}/dismiss`, {
        method: "POST",
        body: "{}",
      });
      await loadInbox();
    } catch (error) {
      king.toast(error.message, "error");
    }
  }

  async function attachReviewPhoto(item, file) {
    const form = new FormData();
    form.append("file", file);
    try {
      await king.fetchMultipart(
        `/api/recognition-inbox/${item.id}/photo`,
        form
      );
      king.toast("Photo attached.", "success");
      await loadInbox();
    } catch (error) {
      king.toast(error.message, "error");
    }
  }

  async function createPhotoReview() {
    const file = $("reviewPhoto").files[0];
    if (!file) {
      king.toast("Choose a product photo.", "error");
      return;
    }
    const button = $("reviewPhotoBtn");
    button.disabled = true;
    const form = new FormData();
    form.append("file", file);
    form.append("suggested_name", $("reviewPhotoName").value.trim());
    form.append("note", $("reviewPhotoNote").value.trim());
    try {
      await king.fetchMultipart("/api/recognition-inbox/photo", form);
      $("reviewPhoto").value = "";
      $("reviewPhotoName").value = "";
      $("reviewPhotoNote").value = "";
      king.toast("Added to review.", "success");
      await loadInbox();
    } catch (error) {
      king.toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  function bindTabs() {
    MODES.forEach((name, index) => {
      const tab = tabs[name];
      tab.addEventListener("click", () => setMode(name));
      tab.addEventListener("keydown", (event) => {
        let next = index;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") {
          next = (index + 1) % MODES.length;
        } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
          next = (index - 1 + MODES.length) % MODES.length;
        } else if (event.key === "Home") {
          next = 0;
        } else if (event.key === "End") {
          next = MODES.length - 1;
        } else {
          return;
        }
        event.preventDefault();
        setMode(MODES[next]).then(() => tabs[MODES[next]].focus());
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindTabs();
    $("lookupEanBtn").addEventListener(
      "click",
      () => lookupBarcode($("manualEan").value)
    );
    $("manualEan").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        lookupBarcode(event.currentTarget.value);
      }
    });
    $("bcSaveBtn").addEventListener("click", saveBarcode);
    ["bcQty", "bcPortions", "bcUnit"].forEach((id) => {
      $(id).addEventListener("input", updateBarcodePortionHint);
    });
    $("bcScanAgainBtn").addEventListener("click", scanAgain);
    $("bcReviewBtn").addEventListener("click", () => setMode("review"));
    $("receiptDate").value = king.isoToday();
    $("receiptUploadBtn").addEventListener("click", uploadReceipt);
    $("receiptCommitBtn").addEventListener("click", commitReceipt);
    $("receiptDiscardBtn").addEventListener("click", discardReceipt);
    $("receiptRefreshBtn").addEventListener("click", loadReceipts);
    $("reviewRefreshBtn").addEventListener("click", loadInbox);
    $("reviewPhotoBtn").addEventListener("click", createPhotoReview);
    setMode("barcode");
    loadInbox();
    loadReceipts();
  });
})();
