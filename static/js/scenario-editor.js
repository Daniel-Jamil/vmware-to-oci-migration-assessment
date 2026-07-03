(function () {
  "use strict";

  const tabs = Array.from(document.querySelectorAll("[data-scenario-tab]"));
  const panels = Array.from(document.querySelectorAll("[data-scenario-panel]"));
  const scenarioForm = document.querySelector("[data-scenario-form]");
  const activeScenarioInput = document.getElementById("active_scenario");
  const dirtyLiveRegion = document.querySelector("[data-scenario-dirty-live]");
  const mainTitle = document.getElementById("step4-main-title");
  const stageSelect = document.getElementById("workspace-stage-select");
  const currentStageValue = stageSelect ? stageSelect.value : "";
  const titleByScenario = {
    native: "Migration to OCI Native",
    ocvs: "Migration to OCVS",
    hybrid: "Migration to Hybrid Platform",
  };
  let dirty = false;
  let submitting = false;

  function announce(message) {
    if (dirtyLiveRegion) dirtyLiveRegion.textContent = message;
  }

  function markDirty() {
    if (submitting || dirty) return;
    dirty = true;
    document.documentElement.dataset.scenarioDirty = "true";
    announce("Unsaved scenario changes. Recalculate and save before leaving this view.");
  }

  function clearDirty() {
    dirty = false;
    delete document.documentElement.dataset.scenarioDirty;
  }

  function confirmDiscard() {
    if (!dirty) return true;
    return window.confirm("You have unsaved scenario changes. Leave this view and discard them?");
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

  document.addEventListener(
    "click",
    function (event) {
      const target = event.target instanceof Element ? event.target : null;
      const tab = target ? target.closest("[data-scenario-tab]") : null;
      if (tab) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (!confirmDiscard()) return;
        if (dirty) clearDirty();
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
        clearDirty();
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
      clearDirty();
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
      if (!confirmDiscard()) return;
      if (dirty) clearDirty();
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
    if (!dirty || submitting) return;
    event.preventDefault();
    event.returnValue = "";
  });

  const initiallySelected = tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0];
  if (initiallySelected) activateTab(initiallySelected, false);
})();
