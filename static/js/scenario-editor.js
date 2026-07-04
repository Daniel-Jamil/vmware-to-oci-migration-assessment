(function () {
  "use strict";

  const tabs = Array.from(document.querySelectorAll("[data-scenario-tab]"));
  const panels = Array.from(document.querySelectorAll("[data-scenario-panel]"));
  const scenarioForm = document.querySelector("[data-scenario-form]");
  const activeScenarioInput = document.getElementById("active_scenario");
  const dirtyLiveRegions = Array.from(document.querySelectorAll("[data-scenario-dirty-live]"));
  const mainTitle = document.getElementById("step4-main-title");
  const stageSelect = document.getElementById("workspace-stage-select");
  const nativeRows = Array.from(document.querySelectorAll("[data-native-editor-row]"));
  const nativeMobileNav = document.querySelector("[data-native-mobile-nav]");
  const nativeMobilePrevious = document.querySelector("[data-native-mobile-previous]");
  const nativeMobileNext = document.querySelector("[data-native-mobile-next]");
  const nativeMobileStatus = document.querySelector("[data-native-mobile-status]");
  const nativeMobileMedia = window.matchMedia("(max-width: 767px)");
  const hybridEditor = document.querySelector("[data-hybrid-editor]");
  const dialogBackdrop = document.getElementById("shape-strategy-modal");
  const shapeStrategyDialog = dialogBackdrop ? dialogBackdrop.querySelector('[role="dialog"]') : null;
  const openShapeStrategy = document.getElementById("open-shape-strategy");
  const closeShapeStrategyControls = dialogBackdrop
    ? Array.from(dialogBackdrop.querySelectorAll("#close-shape-strategy, #cancel-shape-strategy, #apply-shape-strategy"))
    : [];
  const currentStageValue = stageSelect ? stageSelect.value : "";
  const titleByScenario = {
    native: "Migration to OCI Native",
    ocvs: "Migration to OCVS",
    hybrid: "Migration to Hybrid Platform",
  };
  let dirty = false;
  let submitting = false;
  let navigationConfirmed = false;
  let dialogReturnFocus = null;
  let activeNativeRowIndex = Math.max(
    0,
    nativeRows.findIndex((row) => row.getAttribute("data-native-mobile-active") === "true"),
  );

  function announce(message) {
    dirtyLiveRegions.forEach((region) => {
      region.textContent = message;
    });
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

  function dialogFocusableElements() {
    if (!shapeStrategyDialog) return [];
    return Array.from(
      shapeStrategyDialog.querySelectorAll(
        'button:not([disabled]), select:not([disabled]), input:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((element) => !element.hidden);
  }

  function focusDialog() {
    if (!shapeStrategyDialog) return;
    const firstControl = dialogFocusableElements()[0];
    (firstControl || shapeStrategyDialog).focus({ preventScroll: true });
  }

  function openAccessibleDialog() {
    if (!dialogBackdrop || !shapeStrategyDialog) return;
    dialogReturnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : openShapeStrategy;
    dialogBackdrop.style.display = "flex";
    dialogBackdrop.setAttribute("aria-hidden", "false");
    window.requestAnimationFrame(focusDialog);
  }

  function closeAccessibleDialog() {
    if (!dialogBackdrop) return;
    dialogBackdrop.style.display = "none";
    dialogBackdrop.setAttribute("aria-hidden", "true");
    const returnTarget = dialogReturnFocus && document.contains(dialogReturnFocus)
      ? dialogReturnFocus
      : openShapeStrategy;
    dialogReturnFocus = null;
    if (returnTarget) returnTarget.focus({ preventScroll: true });
  }

  if (openShapeStrategy) openShapeStrategy.addEventListener("click", openAccessibleDialog);
  closeShapeStrategyControls.forEach((control) => {
    control.addEventListener("click", closeAccessibleDialog);
  });
  if (dialogBackdrop) {
    dialogBackdrop.addEventListener("click", function (event) {
      if (event.target === dialogBackdrop) closeAccessibleDialog();
    });
  }

  document.addEventListener("keydown", function (event) {
    if (!dialogBackdrop || dialogBackdrop.getAttribute("aria-hidden") !== "false") return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeAccessibleDialog();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = dialogFocusableElements();
    if (!focusable.length) {
      event.preventDefault();
      focusDialog();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === shapeStrategyDialog)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });

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

  function initHybridEditor() {
    if (!hybridEditor || hybridEditor.dataset.hybridInitialized === "true") return;
    hybridEditor.dataset.hybridInitialized = "true";

    const rows = Array.from(hybridEditor.querySelectorAll("[data-hybrid-row]"));
    const search = hybridEditor.querySelector("[data-hybrid-search]");
    const supportFilter = hybridEditor.querySelector("[data-hybrid-support-filter]");
    const placementFilter = hybridEditor.querySelector("[data-hybrid-placement-filter]");
    const bulkScope = hybridEditor.querySelector("[data-hybrid-bulk-scope]");
    const bulkPlacement = hybridEditor.querySelector("[data-hybrid-bulk-placement]");
    const bulkApply = hybridEditor.querySelector("[data-hybrid-bulk-apply]");
    const bulkUndo = hybridEditor.querySelector("[data-hybrid-bulk-undo]");
    const bulkStatus = hybridEditor.querySelector("[data-hybrid-bulk-status]");
    const previousPage = hybridEditor.querySelector("[data-hybrid-page-previous]");
    const nextPage = hybridEditor.querySelector("[data-hybrid-page-next]");
    const pageStatus = hybridEditor.querySelector("[data-hybrid-page-status]");
    const visibleSummary = hybridEditor.querySelector("[data-hybrid-visible-summary]");
    const pageSize = Math.max(1, Number(hybridEditor.dataset.hybridPageSize || 25));
    let currentPage = 1;
    let filteredRows = rows.slice();
    let pageRows = rows.slice(0, pageSize);
    let hybridBulkSnapshot = null;

    function placementSelect(row) {
      return row.querySelector("[data-hybrid-placement-select]");
    }

    function updateHybridRow(row) {
      const select = placementSelect(row);
      if (!select) return;
      const placement = select.value;
      const recommended = row.dataset.hybridRecommended || "ocvs";
      const pricedAs = row.querySelector("[data-hybrid-priced-as]");
      const reason = row.querySelector("[data-hybrid-reason]");
      row.dataset.hybridPlacement = placement;
      if (pricedAs) pricedAs.textContent = placement === "native" ? "OCI Native" : "OCVS";
      if (reason) {
        if (placement === "review") {
          reason.textContent = "Pending placement review; conservatively priced as OCVS";
        } else if (placement === recommended) {
          reason.textContent = "Matches the support-based recommendation";
        } else {
          reason.textContent = "Manual override from the support-based recommendation";
        }
      }
    }

    function renderHybridCounts() {
      const counts = { native: 0, ocvs: 0, review: 0, manual: 0 };
      rows.forEach((row) => {
        const placement = row.dataset.hybridPlacement || "ocvs";
        if (Object.prototype.hasOwnProperty.call(counts, placement)) counts[placement] += 1;
        if (placement !== (row.dataset.hybridRecommended || "ocvs")) counts.manual += 1;
      });
      Object.entries(counts).forEach(([name, value]) => {
        const output = hybridEditor.querySelector(`[data-hybrid-count="${name}"]`);
        if (output) output.textContent = String(value);
      });
    }

    function rowMatchesFilters(row) {
      const searchValue = search ? search.value.trim().toLowerCase() : "";
      const supportValue = supportFilter ? supportFilter.value : "all";
      const placementValue = placementFilter ? placementFilter.value : "all";
      return (
        (!searchValue || (row.dataset.hybridSearchText || "").toLowerCase().includes(searchValue))
        && (supportValue === "all" || row.dataset.hybridSupport === supportValue)
        && (placementValue === "all" || row.dataset.hybridPlacement === placementValue)
      );
    }

    function renderHybridEditor(resetPage) {
      filteredRows = rows.filter(rowMatchesFilters);
      const pageCount = Math.max(1, Math.ceil(filteredRows.length / pageSize));
      if (resetPage) currentPage = 1;
      currentPage = Math.min(Math.max(currentPage, 1), pageCount);
      const start = (currentPage - 1) * pageSize;
      pageRows = filteredRows.slice(start, start + pageSize);
      const visibleSet = new Set(pageRows);
      rows.forEach((row) => {
        row.hidden = !visibleSet.has(row);
      });
      if (previousPage) previousPage.disabled = currentPage <= 1;
      if (nextPage) nextPage.disabled = currentPage >= pageCount;
      if (pageStatus) pageStatus.textContent = `Page ${currentPage} of ${pageCount}`;
      if (visibleSummary) {
        const first = filteredRows.length ? start + 1 : 0;
        const last = Math.min(start + pageSize, filteredRows.length);
        visibleSummary.textContent = `${first}-${last} of ${filteredRows.length} matching VM(s)`;
      }
      renderHybridCounts();
    }

    function rowsForBulkScope() {
      const scope = bulkScope ? bulkScope.value : "page";
      if (scope === "all") return rows;
      if (scope === "filtered") return filteredRows;
      return pageRows;
    }

    rows.forEach((row) => {
      const select = placementSelect(row);
      if (!select) return;
      select.addEventListener("change", function () {
        updateHybridRow(row);
        renderHybridEditor(false);
      });
      updateHybridRow(row);
    });

    [search, supportFilter, placementFilter].forEach((control) => {
      if (!control) return;
      control.addEventListener(control === search ? "input" : "change", function () {
        renderHybridEditor(true);
      });
    });

    if (previousPage) {
      previousPage.addEventListener("click", function () {
        currentPage -= 1;
        renderHybridEditor(false);
      });
    }
    if (nextPage) {
      nextPage.addEventListener("click", function () {
        currentPage += 1;
        renderHybridEditor(false);
      });
    }

    if (bulkApply) {
      bulkApply.addEventListener("click", function () {
        const applied = bulkPlacement ? bulkPlacement.value : "";
        if (!applied) {
          if (bulkStatus) bulkStatus.textContent = "Choose a placement before applying.";
          return;
        }
        const affectedRows = rowsForBulkScope()
          .map((row) => ({ row, select: placementSelect(row) }))
          .filter((item) => item.select && item.select.value !== applied);
        hybridBulkSnapshot = affectedRows.map((item) => ({
          row: item.row,
          select: item.select,
          before: item.select.value,
          applied,
        }));
        hybridBulkSnapshot.forEach((item) => {
          item.select.value = applied;
          updateHybridRow(item.row);
        });
        if (bulkUndo) bulkUndo.disabled = hybridBulkSnapshot.length === 0;
        if (bulkStatus) bulkStatus.textContent = `Applied to ${hybridBulkSnapshot.length} VM(s).`;
        if (hybridBulkSnapshot.length) markDirty();
        renderHybridEditor(false);
      });
    }

    if (bulkUndo) {
      bulkUndo.addEventListener("click", function () {
        if (!hybridBulkSnapshot) return;
        let restored = 0;
        hybridBulkSnapshot.forEach((item) => {
          if (item.select.value !== item.applied) return;
          item.select.value = item.before;
          updateHybridRow(item.row);
          restored += 1;
        });
        if (bulkStatus) bulkStatus.textContent = `Restored ${restored} VM(s); later row edits were kept.`;
        hybridBulkSnapshot = null;
        bulkUndo.disabled = true;
        if (restored) markDirty();
        renderHybridEditor(false);
      });
    }

    renderHybridEditor(true);
  }

  initHybridEditor();

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
