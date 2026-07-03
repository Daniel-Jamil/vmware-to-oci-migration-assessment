(function () {
  "use strict";

  const table = document.getElementById("inventory-table");
  if (!table) return;

  const tbody = table.querySelector("tbody");
  const rows = Array.from(table.querySelectorAll("[data-inventory-row]"));
  const searchInput = document.getElementById("inventory-search");
  const supportFilter = document.getElementById("inventory-support-filter");
  const powerFilter = document.getElementById("inventory-power-filter");
  const placementFilter = document.getElementById("inventory-placement-filter");
  const bulkPlacement = document.getElementById("inventory-bulk-placement");
  const scopeOutput = document.getElementById("inventory-bulk-scope");
  const visibleCount = document.querySelector("[data-visible-count]");
  const selectionStatus = document.querySelector("[data-selection-status]");
  const emptyFilter = document.querySelector("[data-empty-filter]");
  const undoRegion = document.getElementById("inventory-undo");
  const undoMessage = undoRegion ? undoRegion.querySelector("[data-undo-message]") : null;
  const undoButton = undoRegion ? undoRegion.querySelector("[data-undo]") : null;
  const warningButtons = Array.from(document.querySelectorAll("[data-warning-filter]"));
  let activeWarning = "all";
  let undoSnapshot = null;

  function detailRow(row) {
    return table.querySelector(`[data-inventory-detail="${row.dataset.inventoryRow}"]`);
  }

  function placementControl(row) {
    return table.querySelector(`[data-row-placement="${row.dataset.inventoryRow}"]`);
  }

  function inclusionControl(row) {
    return row.querySelector('input[name="included_vm_names"]');
  }

  function isFilterActive() {
    return Boolean(
      (searchInput && searchInput.value.trim()) ||
      (supportFilter && supportFilter.value !== "all") ||
      (powerFilter && powerFilter.value !== "all") ||
      (placementFilter && placementFilter.value !== "all") ||
      activeWarning !== "all"
    );
  }

  function rowMatches(row) {
    const query = searchInput ? searchInput.value.trim().toLowerCase() : "";
    const support = supportFilter ? supportFilter.value : "all";
    const power = powerFilter ? powerFilter.value : "all";
    const placement = placementFilter ? placementFilter.value : "all";
    const warnings = (row.dataset.warningIds || "").split(/\s+/).filter(Boolean);
    const control = placementControl(row);

    return (!query || (row.dataset.search || "").toLowerCase().includes(query)) &&
      (support === "all" || row.dataset.support === support) &&
      (power === "all" || row.dataset.power === power) &&
      (placement === "all" || (control && control.value === placement)) &&
      (activeWarning === "all" || warnings.includes(activeWarning));
  }

  function visibleRows() {
    return rows.filter((row) => !row.hidden);
  }

  function updateSelectionStatus() {
    if (!selectionStatus) return;
    const selectedCount = rows.filter((row) => {
      const control = inclusionControl(row);
      return control && control.checked;
    }).length;
    selectionStatus.textContent = `${selectedCount} of ${rows.length} included`;
  }

  function updateFilters() {
    let count = 0;
    rows.forEach((row) => {
      const visible = rowMatches(row);
      row.hidden = !visible;
      const pairedDetail = detailRow(row);
      if (pairedDetail) pairedDetail.hidden = !visible;
      if (visible) count += 1;
    });

    if (visibleCount) visibleCount.textContent = `${count} visible`;
    if (emptyFilter) emptyFilter.hidden = count !== 0;
    if (scopeOutput) {
      scopeOutput.textContent = isFilterActive()
        ? `Bulk scope: ${count} filtered of ${rows.length} loaded VMs.`
        : `Bulk scope: all ${rows.length} loaded VMs.`;
    }
  }

  function snapshotState() {
    return rows.map((row) => {
      const inclusion = inclusionControl(row);
      const placement = placementControl(row);
      return {
        rowIndex: row.dataset.inventoryRow,
        included: Boolean(inclusion && inclusion.checked),
        placement: placement ? placement.value : "review",
      };
    });
  }

  function syncPlacement(row, value) {
    const placement = placementControl(row);
    if (!placement) return;
    placement.value = value;
    row.dataset.placement = value;
    row.dataset.sortPlacement = placement.options[placement.selectedIndex].text;
    const label = row.querySelector("[data-placement-label]");
    if (label) label.textContent = placement.options[placement.selectedIndex].text;
  }

  function showUndo(message, snapshot) {
    undoSnapshot = snapshot;
    if (!undoRegion || !undoMessage) return;
    undoMessage.textContent = message;
    undoRegion.hidden = false;
  }

  function runBulk(message, rowScope, change) {
    const snapshot = snapshotState();
    rowScope.forEach(change);
    updateSelectionStatus();
    updateFilters();
    showUndo(message, snapshot);
  }

  [searchInput, supportFilter, powerFilter, placementFilter].forEach((control) => {
    if (!control) return;
    control.addEventListener(control === searchInput ? "input" : "change", updateFilters);
  });

  warningButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeWarning = button.dataset.warningFilter || "all";
      warningButtons.forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      document.querySelectorAll("[data-warning-item]").forEach((item) => {
        item.hidden = activeWarning !== "all" && item.dataset.warningItem !== activeWarning;
      });
      updateFilters();
    });
  });

  const selectAll = document.querySelector("[data-select-all]");
  if (selectAll) {
    selectAll.addEventListener("click", () => {
      runBulk(`Included all ${rows.length} loaded VMs.`, rows, (row) => {
        const control = inclusionControl(row);
        if (control) control.checked = true;
      });
    });
  }

  const includeFiltered = document.querySelector("[data-include-filtered]");
  if (includeFiltered) {
    includeFiltered.addEventListener("click", () => {
      const scopedRows = visibleRows();
      runBulk(`Included ${scopedRows.length} VMs in the current filter.`, scopedRows, (row) => {
        const control = inclusionControl(row);
        if (control) control.checked = true;
      });
    });
  }

  const excludeFiltered = document.querySelector("[data-exclude-filtered]");
  if (excludeFiltered) {
    excludeFiltered.addEventListener("click", () => {
      const scopedRows = visibleRows();
      runBulk(`Excluded ${scopedRows.length} VMs in the current filter.`, scopedRows, (row) => {
        const control = inclusionControl(row);
        if (control) control.checked = false;
      });
    });
  }

  const applyPlacement = document.querySelector("[data-apply-placement]");
  if (applyPlacement && bulkPlacement) {
    applyPlacement.addEventListener("click", () => {
      if (!bulkPlacement.value) {
        bulkPlacement.focus();
        return;
      }
      const scopedRows = visibleRows();
      const selectedLabel = bulkPlacement.options[bulkPlacement.selectedIndex].text;
      runBulk(`Applied ${selectedLabel} to ${scopedRows.length} VMs in the current filter.`, scopedRows, (row) => {
        syncPlacement(row, bulkPlacement.value);
      });
    });
  }

  if (undoButton) {
    undoButton.addEventListener("click", () => {
      if (!undoSnapshot) return;
      undoSnapshot.forEach((saved) => {
        const row = table.querySelector(`[data-inventory-row="${saved.rowIndex}"]`);
        if (!row) return;
        const inclusion = inclusionControl(row);
        if (inclusion) inclusion.checked = saved.included;
        syncPlacement(row, saved.placement);
      });
      undoSnapshot = null;
      if (undoRegion) undoRegion.hidden = true;
      updateSelectionStatus();
      updateFilters();
    });
  }

  rows.forEach((row) => {
    const inclusion = inclusionControl(row);
    const placement = placementControl(row);
    if (inclusion) inclusion.addEventListener("change", updateSelectionStatus);
    if (placement) {
      placement.addEventListener("change", () => {
        syncPlacement(row, placement.value);
        updateFilters();
      });
      syncPlacement(row, placement.value);
    }

    const detailsButton = row.querySelector("[data-details-target]");
    const details = document.getElementById(detailsButton ? detailsButton.dataset.detailsTarget : "");
    if (detailsButton && details) {
      detailsButton.addEventListener("click", () => {
        details.open = !details.open;
        detailsButton.setAttribute("aria-expanded", String(details.open));
        if (details.open) details.querySelector("summary").focus();
      });
      details.addEventListener("toggle", () => {
        detailsButton.setAttribute("aria-expanded", String(details.open));
      });
    }
  });

  table.querySelectorAll("[data-sort]").forEach((button) => {
    button.addEventListener("click", () => {
      const sortKey = button.dataset.sort;
      const numberSort = button.dataset.sortType === "number";
      const ascending = button.getAttribute("aria-sort") !== "ascending";
      const sortedRows = [...rows].sort((left, right) => {
        const leftValue = left.dataset[`sort${sortKey.charAt(0).toUpperCase()}${sortKey.slice(1)}`] || "";
        const rightValue = right.dataset[`sort${sortKey.charAt(0).toUpperCase()}${sortKey.slice(1)}`] || "";
        if (numberSort) {
          const difference = (Number.parseFloat(leftValue) || 0) - (Number.parseFloat(rightValue) || 0);
          return ascending ? difference : -difference;
        }
        const difference = leftValue.localeCompare(rightValue, undefined, { numeric: true, sensitivity: "base" });
        return ascending ? difference : -difference;
      });

      table.querySelectorAll("[data-sort]").forEach((candidate) => candidate.setAttribute("aria-sort", "none"));
      button.setAttribute("aria-sort", ascending ? "ascending" : "descending");
      sortedRows.forEach((row) => {
        tbody.appendChild(row);
        const pairedDetail = detailRow(row);
        if (pairedDetail) tbody.appendChild(pairedDetail);
      });
    });
  });

  updateSelectionStatus();
  updateFilters();

  const errorSummary = document.getElementById("inventory-errors");
  if (errorSummary) window.requestAnimationFrame(() => errorSummary.focus());
})();
