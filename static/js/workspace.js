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

  const menu = document.querySelector("[data-assessment-menu]");
  if (!menu) return;

  const trigger = menu.querySelector("[data-assessment-menu-trigger]");
  const panel = menu.querySelector("[data-assessment-menu-panel]");
  if (!trigger || !panel) return;

  function actionableItems() {
    return Array.from(
      panel.querySelectorAll(
        'a[href]:not([aria-disabled="true"]), button:not([disabled]):not([aria-disabled="true"]), [tabindex]:not([tabindex="-1"]):not([aria-disabled="true"])'
      )
    );
  }

  function openMenu() {
    panel.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    const firstItem = actionableItems()[0];
    if (firstItem) firstItem.focus();
  }

  function closeMenu(returnFocus) {
    panel.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (returnFocus) trigger.focus();
  }

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
})();
