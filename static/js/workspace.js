(function () {
  "use strict";

  const stageSelect = document.querySelector("[data-stage-select]");
  if (stageSelect) {
    stageSelect.addEventListener("change", function () {
      if (stageSelect.value) {
        window.location.assign(stageSelect.value);
      }
    });
  }

  const ASSESSMENT_FILE_PICKER_ID = "vmware-to-oci-assessment-json";
  const ASSESSMENT_FILE_START_DIRECTORY = "downloads";

  const menu = document.querySelector("[data-assessment-menu]");
  const trigger = menu ? menu.querySelector("[data-assessment-menu-trigger]") : null;
  const panel = menu ? menu.querySelector("[data-assessment-menu-panel]") : null;

  function actionableItems() {
    if (!panel) return [];
    return Array.from(
      panel.querySelectorAll(
        'a[href]:not([aria-disabled="true"]), button:not([disabled]):not([aria-disabled="true"]), [tabindex]:not([tabindex="-1"]):not([aria-disabled="true"])'
      )
    );
  }

  function openMenu() {
    if (!trigger || !panel) return;
    panel.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    const firstItem = actionableItems()[0];
    if (firstItem) firstItem.focus();
  }

  function closeMenu(returnFocus) {
    if (!trigger || !panel) return;
    panel.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (returnFocus) trigger.focus();
  }

  if (trigger && panel && menu) {
    trigger.addEventListener("click", function () {
      if (panel.hidden) {
        openMenu();
      } else {
        closeMenu(true);
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !panel.hidden) {
        event.preventDefault();
        closeMenu(true);
      }
    });

    document.addEventListener("click", function (event) {
      if (!panel.hidden && !menu.contains(event.target)) {
        closeMenu(false);
      }
    });
  }

  function assessmentFileTypes() {
    return [
      {
        description: "VMware to OCI assessment",
        accept: {
          "application/json": [".json"],
        },
      },
    ];
  }

  document.querySelectorAll("[data-assessment-save-form]").forEach(function (saveForm) {
    const saveButton = saveForm.querySelector("[data-assessment-save]");
    if (!saveButton) return;

    saveForm.addEventListener("submit", async function (event) {
      if (!window.showSaveFilePicker || !window.fetch || !window.FormData) {
        return;
      }

      event.preventDefault();
      let handle;
      try {
        handle = await window.showSaveFilePicker({
          id: ASSESSMENT_FILE_PICKER_ID,
          startIn: ASSESSMENT_FILE_START_DIRECTORY,
          suggestedName: "vmware-to-oci-assessment.json",
          types: assessmentFileTypes(),
        });
      } catch (error) {
        if (!error || error.name !== "AbortError") {
          console.error("Assessment save picker failed", error);
        }
        return;
      }

      closeMenu(false);
      saveButton.disabled = true;
      try {
        const response = await fetch(saveForm.action || window.location.href, {
          method: "POST",
          body: new FormData(saveForm),
          credentials: "same-origin",
        });
        const contentType = response.headers.get("Content-Type") || "";
        if (!response.ok || !contentType.includes("application/json")) {
          throw new Error("Portable assessment export did not return JSON.");
        }
        const writable = await handle.createWritable();
        await writable.write(await response.blob());
        await writable.close();
      } catch (error) {
        console.error("Assessment save failed", error);
        saveForm.submit();
      } finally {
        saveButton.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-assessment-open-form]").forEach(function (openForm) {
    const openButton = openForm.querySelector("[data-assessment-open]");
    const openFile = openForm.querySelector("[data-assessment-open-file]");
    if (!openButton || !openFile) return;

    openFile.addEventListener("change", function () {
      if (openFile.files && openFile.files.length === 1) {
        openForm.submit();
      }
    });

    openButton.addEventListener("click", async function () {
      if (window.showOpenFilePicker && window.DataTransfer) {
        try {
          const handles = await window.showOpenFilePicker({
            id: ASSESSMENT_FILE_PICKER_ID,
            startIn: ASSESSMENT_FILE_START_DIRECTORY,
            multiple: false,
            types: assessmentFileTypes(),
          });
          if (handles.length !== 1) return;
          const transfer = new DataTransfer();
          transfer.items.add(await handles[0].getFile());
          openFile.files = transfer.files;
          openForm.submit();
          return;
        } catch (error) {
          if (!error || error.name !== "AbortError") {
            console.error("Assessment open picker failed", error);
          }
          return;
        }
      }
      openFile.click();
    });
  });
})();
