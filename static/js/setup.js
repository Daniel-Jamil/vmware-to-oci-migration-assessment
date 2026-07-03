(() => {
  const modeRadios = Array.from(document.querySelectorAll('input[name="inventory_mode"]'));
  const modePanels = Array.from(document.querySelectorAll("[data-inventory-mode-panel]"));

  const showInventoryMode = (mode, keepFocus = false) => {
    modePanels.forEach((panel) => {
      const isActive = panel.dataset.inventoryModePanel === mode;
      panel.hidden = !isActive;
      panel.setAttribute("aria-hidden", String(!isActive));
      panel.querySelectorAll("input, select, textarea, button").forEach((control) => {
        control.disabled = !isActive;
        if (control.dataset.modeRequired === "true") {
          control.required = isActive;
        }
      });
    });

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
