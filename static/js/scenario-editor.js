(function () {
  "use strict";

  const tabs = Array.from(document.querySelectorAll("[data-scenario-tab]"));
  const panels = Array.from(document.querySelectorAll("[data-scenario-panel]"));
  const scenarioForm = document.querySelector("[data-scenario-form]");
  const activeScenarioInput = document.getElementById("active_scenario");
  const dirtyLiveRegion = document.querySelector("[data-scenario-dirty-live]");
  const mainTitle = document.getElementById("step4-main-title");
  const stageSelect = document.getElementById("workspace-stage-select");
  const nativeRows = Array.from(document.querySelectorAll("[data-native-editor-row]"));
  const nativeMobileNav = document.querySelector("[data-native-mobile-nav]");
  const nativeMobilePrevious = document.querySelector("[data-native-mobile-previous]");
  const nativeMobileNext = document.querySelector("[data-native-mobile-next]");
  const nativeMobileStatus = document.querySelector("[data-native-mobile-status]");
  const nativeMobileMedia = window.matchMedia("(max-width: 767px)");
  const currentStageValue = stageSelect ? stageSelect.value : "";
  const titleByScenario = {
    native: "Migration to OCI Native",
    ocvs: "Migration to OCVS",
    hybrid: "Migration to Hybrid Platform",
  };
  let dirty = false;
  let submitting = false;
  let navigationConfirmed = false;
  let activeNativeRowIndex = Math.max(
    0,
    nativeRows.findIndex((row) => row.getAttribute("data-native-mobile-active") === "true"),
  );

  function announce(message) {
    if (dirtyLiveRegion) dirtyLiveRegion.textContent = message;
  }

  function markDirty() {
    if (submitting || dirty) return;
    dirty = true;
    document.documentElement.dataset.scenarioDirty = "true";
    announce("Unsaved scenario changes. Recalculate and save before leaving this view.");
  }

  function confirmDiscard() {
    if (!dirty) return true;
    return window.confirm("You have unsaved scenario changes. Leave this view and discard them?");
  }

  function confirmScenarioSwitch() {
    if (!dirty) return true;
    return window.confirm(
      "You have unsaved scenario changes. Switch scenarios while keeping those changes pending?",
    );
  }

  function activateTab(tab, updateUrl) {
    if (!tab) return;
    const scenarioId = tab.dataset.scenarioTab || "native";
    tabs.forEach((candidate) => {
      const selected = candidate === tab;
      candidate.classList.toggle("is-active", selected);
      candidate.setAttribute("aria-selected", selected ? "true" : "false");
      candidate.setAttribute("tabindex", selected ? "0" : "-1");
    });
    panels.forEach((panel) => {
      const selected = panel.dataset.scenarioPanel === scenarioId;
      panel.classList.toggle("is-active", selected);
      panel.hidden = !selected;
    });
    if (activeScenarioInput) activeScenarioInput.value = scenarioId;
    if (mainTitle) mainTitle.textContent = titleByScenario[scenarioId] || "Scenario Configuration";
    document.title = `VMware to OCI Migration Assessment - ${titleByScenario[scenarioId] || "Scenario Configuration"}`;
    if (updateUrl) {
      const url = new URL(window.location.href);
      url.search = "";
      url.searchParams.set("tab", scenarioId);
      url.hash = "";
      window.history.replaceState(null, "", url);
    }
    document.dispatchEvent(
      new CustomEvent("migration-assessment:scenario-tab-changed", {
        detail: { id: scenarioId },
      }),
    );
  }

  function updateNativeMobileButton(button, targetIndex, direction) {
    if (!button) return;
    const targetRow = nativeRows[targetIndex];
    button.disabled = !targetRow;
    if (!targetRow) {
      button.removeAttribute("aria-controls");
      button.setAttribute("aria-label", `No ${direction} VM`);
      return;
    }
    const vmName = targetRow.dataset.nativeVmName || `VM ${targetIndex + 1}`;
    button.setAttribute("aria-controls", targetRow.id);
    button.setAttribute("aria-label", `Show ${direction} VM, ${vmName}`);
  }

  function renderNativeMobileRow() {
    const isMobile = nativeMobileMedia.matches;
    activeNativeRowIndex = Math.min(
      Math.max(activeNativeRowIndex, 0),
      Math.max(nativeRows.length - 1, 0),
    );
    nativeRows.forEach((row, index) => {
      const isActive = index === activeNativeRowIndex;
      row.setAttribute("data-native-mobile-active", isActive ? "true" : "false");
      row.hidden = isMobile && !isActive;
      if (row.hidden) row.setAttribute("aria-hidden", "true");
      else row.removeAttribute("aria-hidden");
    });
    if (nativeMobileNav) nativeMobileNav.hidden = !isMobile || nativeRows.length === 0;
    if (nativeMobileStatus) {
      if (nativeRows.length) {
        const activeRow = nativeRows[activeNativeRowIndex];
        const vmName = activeRow.dataset.nativeVmName || `VM ${activeNativeRowIndex + 1}`;
        nativeMobileStatus.textContent = `VM ${activeNativeRowIndex + 1} of ${nativeRows.length}: ${vmName}`;
      } else {
        nativeMobileStatus.textContent = "No matching VMs";
      }
    }
    updateNativeMobileButton(nativeMobilePrevious, activeNativeRowIndex - 1, "previous");
    updateNativeMobileButton(nativeMobileNext, activeNativeRowIndex + 1, "next");
  }

  if (nativeMobilePrevious) {
    nativeMobilePrevious.addEventListener("click", function () {
      activeNativeRowIndex -= 1;
      renderNativeMobileRow();
    });
  }
  if (nativeMobileNext) {
    nativeMobileNext.addEventListener("click", function () {
      activeNativeRowIndex += 1;
      renderNativeMobileRow();
    });
  }
  nativeMobileMedia.addEventListener("change", renderNativeMobileRow);
  renderNativeMobileRow();

  document.addEventListener(
    "click",
    function (event) {
      const target = event.target instanceof Element ? event.target : null;
      const tab = target ? target.closest("[data-scenario-tab]") : null;
      if (tab) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!confirmScenarioSwitch()) return;
        activateTab(tab, true);
        tab.focus();
        return;
      }

      const navigation = target
        ? target.closest("a[data-dirty-navigation], .stage-nav__link, .workspace-action[href]")
        : null;
      if (navigation && dirty) {
        if (!confirmDiscard()) {
          event.preventDefault();
          event.stopImmediatePropagation();
          return;
        }
        navigationConfirmed = true;
      }
    },
    true,
  );

  document.addEventListener(
    "change",
    function (event) {
      if (event.target !== stageSelect || !dirty) return;
      if (!confirmDiscard()) {
        event.preventDefault();
        event.stopImmediatePropagation();
        stageSelect.value = currentStageValue;
        return;
      }
      navigationConfirmed = true;
    },
    true,
  );

  tabs.forEach((tab, index) => {
    tab.addEventListener("keydown", function (event) {
      let nextIndex = index;
      if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = tabs.length - 1;
      else return;

      event.preventDefault();
      if (!confirmScenarioSwitch()) return;
      const nextTab = tabs[nextIndex];
      activateTab(nextTab, true);
      nextTab.focus();
    });
  });

  function handleScenarioControlChange(event) {
    const control = event.target;
    if (
      !(control instanceof HTMLInputElement)
      && !(control instanceof HTMLSelectElement)
      && !(control instanceof HTMLTextAreaElement)
    ) {
      return;
    }
    if (control.form === scenarioForm) markDirty();
  }

  document.addEventListener("input", handleScenarioControlChange);
  document.addEventListener("change", handleScenarioControlChange);

  if (scenarioForm) {
    scenarioForm.addEventListener("submit", function (event) {
      const submitter = event.submitter;
      if (submitter && submitter.value === "export_excel") return;
      submitting = true;
      announce("Saving scenario changes.");
    });
  }

  document.addEventListener("click", function (event) {
    const target = event.target instanceof Element ? event.target : null;
    if (target && target.closest("#apply-shape-strategy")) markDirty();
  });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty || submitting || navigationConfirmed) return;
    event.preventDefault();
    event.returnValue = "";
  });

  const initiallySelected = tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0];
  if (initiallySelected) activateTab(initiallySelected, false);
})();
