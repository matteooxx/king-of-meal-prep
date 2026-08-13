// Shared recipe rating and planner-preference controls.
(function () {
  const $ = (id) => document.getElementById(id);
  let modalState = null;
  let loadToken = 0;

  function cleanRating(value) {
    const rating = Number(value);
    return Number.isInteger(rating) && rating >= 1 && rating <= 5
      ? rating
      : null;
  }

  function cleanPreference(value) {
    return ["neutral", "make_again", "avoid"].includes(value)
      ? value
      : "neutral";
  }

  king.mountRatingControl = (container, rating, onChange) => {
    if (!container) return;
    const selected = cleanRating(rating);
    container.innerHTML = Array.from({ length: 5 }, (_, index) => {
      const value = index + 1;
      const active = selected !== null && value <= selected;
      return `<button type="button" class="rating-star${active ? " active" : ""}" ` +
        `data-rating="${value}" aria-label="${value} star${value === 1 ? "" : "s"}" ` +
        `aria-pressed="${selected === value}"><i data-lucide="star"></i></button>`;
    }).join("");
    container.querySelectorAll("[data-rating]").forEach((button) => {
      button.addEventListener("click", () => {
        onChange(Number(button.dataset.rating));
      });
    });
    king.icons();
  };

  king.saveRecipeFeedback = async (recipeId, feedback) => {
    const result = await king.fetchJSON(`/api/recipes/${recipeId}/feedback`, {
      method: "PATCH",
      body: JSON.stringify({
        rating: cleanRating(feedback.rating),
        preference: cleanPreference(feedback.preference),
      }),
    });
    return result.feedback;
  };

  function renderModal() {
    if (!modalState) return;
    $("feedbackRecipeName").textContent = modalState.name || "This recipe";
    king.mountRatingControl(
      $("feedbackStars"),
      modalState.rating,
      (rating) => {
        modalState.rating = rating;
        renderModal();
      }
    );
    document.querySelectorAll("[data-feedback-preference]").forEach((button) => {
      const active = button.dataset.feedbackPreference === modalState.preference;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $("feedbackClearRating").disabled = modalState.rating === null;
  }

  king.openFeedback = async (options) => {
    modalState = {
      recipeId: Number(options.recipeId),
      name: options.name || "",
      rating: cleanRating(options.feedback?.rating),
      preference: cleanPreference(options.feedback?.preference),
    };
    const token = ++loadToken;
    $("feedbackResult").textContent = "";
    renderModal();
    king.openModal("feedbackModal");
    try {
      const recipe = await king.fetchJSON(`/api/recipes/${modalState.recipeId}`);
      if (!modalState || token !== loadToken) return;
      modalState.rating = cleanRating(recipe.feedback?.rating);
      modalState.preference = cleanPreference(recipe.feedback?.preference);
      renderModal();
    } catch {
      // The completion succeeded even if existing feedback could not load.
    }
  };

  async function saveModalFeedback() {
    if (!modalState) return;
    $("feedbackSave").disabled = true;
    $("feedbackResult").className = "result";
    $("feedbackResult").textContent = "Saving...";
    try {
      await king.saveRecipeFeedback(modalState.recipeId, modalState);
      king.closeModal("feedbackModal");
      modalState = null;
      king.toast("Feedback saved.", "success");
    } catch (error) {
      $("feedbackResult").className = "result err";
      $("feedbackResult").textContent = error.message;
    } finally {
      $("feedbackSave").disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("feedbackModalClose")?.addEventListener("click", () => {
      modalState = null;
      loadToken++;
      king.closeModal("feedbackModal");
    });
    $("feedbackSkip")?.addEventListener("click", () => {
      modalState = null;
      loadToken++;
      king.closeModal("feedbackModal");
    });
    $("feedbackClearRating")?.addEventListener("click", () => {
      if (!modalState) return;
      modalState.rating = null;
      renderModal();
    });
    document.querySelectorAll("[data-feedback-preference]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!modalState) return;
        modalState.preference = cleanPreference(
          button.dataset.feedbackPreference
        );
        renderModal();
      });
    });
    $("feedbackSave")?.addEventListener("click", saveModalFeedback);
  });
})();
