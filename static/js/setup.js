(() => {
  const modeRadios = Array.from(document.querySelectorAll('input[name="inventory_mode"]'));
  const modePanels = Array.from(document.querySelectorAll("[data-inventory-mode-panel]"));
  const manualFallback = document.querySelector("[data-manual-inventory-fallback]");
  const inventoryForm = document.getElementById("inventory-source-form");
  const uploadActions = new Set(["upload_rvtools_file", "select_rvtools_file"]);

  const setInventoryMode = (mode) => {
    modeRadios.forEach((radio) => {
      radio.checked = radio.value === mode;
    });
  };

  const showInventoryMode = (mode, keepFocus = false) => {
    modePanels.forEach((panel) => {
      const panelMode = panel.dataset.inventoryModePanel;
      const isUploadPanel = panelMode === "upload";
      const isActive = isUploadPanel || panelMode === mode;
      panel.hidden = !isActive;
      panel.setAttribute("aria-hidden", String(!isActive));
      panel.querySelectorAll("input, select, textarea, button").forEach((control) => {
        control.disabled = !isActive;
        if (control.dataset.modeRequired === "true") {
          control.required = isActive;
        }
      });
    });

    if (manualFallback && manualFallback.open !== (mode === "manual")) {
      manualFallback.open = mode === "manual";
    }

    const selectedRadio = modeRadios.find((radio) => radio.checked);
    if (keepFocus && selectedRadio) {
      selectedRadio.focus({ preventScroll: true });
    }
  };

  modeRadios.forEach((radio) => {
    radio.addEventListener("change", () => {
      if (radio.checked) {
        showInventoryMode(radio.value, true);
      }
    });
  });

  if (manualFallback) {
    manualFallback.addEventListener("toggle", () => {
      const nextMode = manualFallback.open ? "manual" : "upload";
      setInventoryMode(nextMode);
      showInventoryMode(nextMode);
    });
  }

  if (inventoryForm) {
    inventoryForm.addEventListener("click", (event) => {
      const submitter = event.target.closest('button[name="action"]');
      if (!submitter) return;
      if (uploadActions.has(submitter.value)) {
        setInventoryMode("upload");
        showInventoryMode("upload");
      } else if (submitter.value === "create_manual_inventory") {
        setInventoryMode("manual");
        showInventoryMode("manual");
      }
    });
  }

  const initialMode = modeRadios.find((radio) => radio.checked);
  if (initialMode) {
    showInventoryMode(initialMode.value);
  }

  const errorSummary = document.querySelector("[data-setup-error-summary]");
  if (errorSummary) {
    errorSummary.focus({ preventScroll: true });
  }

  const downloadForm = document.getElementById("download_pricing_form");
  const currencySelect = document.getElementById("currency_code");
  const currencyStatus = document.getElementById("currency_download_status");
  const downloadButton = document.getElementById("download_pricing_button");

  if (downloadForm && currencySelect) {
    currencySelect.addEventListener("change", () => {
      if (currencyStatus) {
        currencyStatus.textContent = currencySelect.value
          ? `${currencySelect.value} selected.`
          : "Select the currency for OCI list pricing.";
      }
    });

    downloadForm.addEventListener("submit", () => {
      if (currencyStatus && currencySelect.value) {
        currencyStatus.textContent = `Retrieving ${currencySelect.value} pricing...`;
      }
      if (downloadButton && currencySelect.value) {
        downloadButton.disabled = true;
        downloadButton.textContent = "Retrieving...";
      }
    });
  }
})();
