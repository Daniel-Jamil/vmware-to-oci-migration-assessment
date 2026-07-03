(function () {
  "use strict";

  const table = document.getElementById("inventory-table");
  if (!table) return;

  const tbody = table.querySelector("tbody");
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
  const warningItems = Array.from(document.querySelectorAll("[data-warning-item]"));
  const detailRowsByIndex = new Map(
    Array.from(table.querySelectorAll("[data-inventory-detail]")).map((row) => [row.dataset.inventoryDetail, row])
  );
  const placementsByIndex = new Map(
    Array.from(table.querySelectorAll("[data-row-placement]")).map((control) => [control.dataset.rowPlacement, control])
  );
  const rowRecords = Array.from(table.querySelectorAll("[data-inventory-row]")).map((row) => {
    const rowIndex = row.dataset.inventoryRow;
    const detailsButton = row.querySelector("[data-details-target]");
    return {
      row,
      rowIndex,
      detailRow: detailRowsByIndex.get(rowIndex),
      inclusion: row.querySelector('input[name="included_vm_names"]'),
      placement: placementsByIndex.get(rowIndex),
      placementLabel: row.querySelector("[data-placement-label]"),
      detailsButton,
      details: document.getElementById(detailsButton ? detailsButton.dataset.detailsTarget : ""),
      warningIds: (row.dataset.warningIds || "").split(/\s+/).filter(Boolean),
      searchText: (row.dataset.search || "").toLowerCase(),
    };
  });
  const recordByIndex = new Map(rowRecords.map((record) => [record.rowIndex, record]));
  const sortControls = Array.from(table.querySelectorAll("[data-sort]")).map((button) => ({
    button,
    header: button.closest("th"),
  }));
  let activeWarning = "all";
  let undoSnapshot = null;
  let searchTimer = null;

  function isFilterActive() {
    return Boolean(
      (searchInput && searchInput.value.trim()) ||
      (supportFilter && supportFilter.value !== "all") ||
      (powerFilter && powerFilter.value !== "all") ||
      (placementFilter && placementFilter.value !== "all") ||
      activeWarning !== "all"
    );
  }

  function rowMatches(record) {
    const query = searchInput ? searchInput.value.trim().toLowerCase() : "";
    const support = supportFilter ? supportFilter.value : "all";
    const power = powerFilter ? powerFilter.value : "all";
    const placement = placementFilter ? placementFilter.value : "all";

    return (!query || record.searchText.includes(query)) &&
      (support === "all" || record.row.dataset.support === support) &&
      (power === "all" || record.row.dataset.power === power) &&
      (placement === "all" || (record.placement && record.placement.value === placement)) &&
      (activeWarning === "all" || record.warningIds.includes(activeWarning));
  }

  function visibleRecords() {
    return rowRecords.filter((record) => !record.row.hidden);
  }

  function syncInclusion(record) {
    if (record.placement && record.inclusion) {
      record.placement.disabled = !record.inclusion.checked;
    }
  }

  function updateSelectionStatus() {
    if (!selectionStatus) return;
    const selectedCount = rowRecords.filter((record) => record.inclusion && record.inclusion.checked).length;
    selectionStatus.textContent = `${selectedCount} of ${rowRecords.length} included`;
  }

  function updateFilters() {
    let count = 0;
    rowRecords.forEach((record) => {
      const visible = rowMatches(record);
      record.row.hidden = !visible;
      if (record.detailRow) record.detailRow.hidden = !visible;
      if (visible) count += 1;
    });

    if (visibleCount) visibleCount.textContent = `${count} visible`;
    if (emptyFilter) emptyFilter.hidden = count !== 0;
    if (scopeOutput) {
      scopeOutput.textContent = isFilterActive()
        ? `Bulk scope: ${count} filtered of ${rowRecords.length} loaded VMs.`
        : `Bulk scope: all ${rowRecords.length} loaded VMs.`;
    }
  }

  function syncPlacement(record, value) {
    if (!record.placement) return;
    record.placement.value = value;
    record.row.dataset.placement = value;
    const selectedOption = record.placement.options[record.placement.selectedIndex];
    const label = selectedOption ? selectedOption.text : value;
    record.row.dataset.sortPlacement = label;
    if (record.placementLabel) record.placementLabel.textContent = label;
  }

  function showUndo(message, changedRecords) {
    undoSnapshot = changedRecords.length ? changedRecords : null;
    if (!undoRegion || !undoMessage) return;
    undoMessage.textContent = message;
    undoRegion.hidden = !undoSnapshot;
  }

  function runBulk(message, rowScope, change) {
    const changedRecords = [];
    rowScope.forEach((record) => {
      const changedState = change(record);
      if (changedState) changedRecords.push({ rowIndex: record.rowIndex, ...changedState });
    });
    updateSelectionStatus();
    updateFilters();
    showUndo(message(changedRecords.length, rowScope.length), changedRecords);
  }

  if (searchInput) {
    searchInput.addEventListener("input", () => {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = window.setTimeout(updateFilters, 150);
    });
  }
  [supportFilter, powerFilter, placementFilter].forEach((control) => {
    if (control) control.addEventListener("change", updateFilters);
  });

  warningButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeWarning = button.dataset.warningFilter || "all";
      warningButtons.forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("is-active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      warningItems.forEach((item) => {
        item.hidden = activeWarning !== "all" && item.dataset.warningItem !== activeWarning;
      });
      updateFilters();
    });
  });

  const selectAll = document.querySelector("[data-select-all]");
  if (selectAll) {
    selectAll.addEventListener("click", () => {
      runBulk(
        (changed) => `Included ${changed} VMs.`,
        rowRecords,
        (record) => {
          if (!record.inclusion || record.inclusion.checked) return null;
          const previous = record.inclusion.checked;
          record.inclusion.checked = true;
          syncInclusion(record);
          return { included: previous };
        }
      );
    });
  }

  const includeFiltered = document.querySelector("[data-include-filtered]");
  if (includeFiltered) {
    includeFiltered.addEventListener("click", () => {
      runBulk(
        (changed, scoped) => `Included ${changed} of ${scoped} VMs in the current filter.`,
        visibleRecords(),
        (record) => {
          if (!record.inclusion || record.inclusion.checked) return null;
          const previous = record.inclusion.checked;
          record.inclusion.checked = true;
          syncInclusion(record);
          return { included: previous };
        }
      );
    });
  }

  const excludeFiltered = document.querySelector("[data-exclude-filtered]");
  if (excludeFiltered) {
    excludeFiltered.addEventListener("click", () => {
      runBulk(
        (changed, scoped) => `Excluded ${changed} of ${scoped} VMs in the current filter.`,
        visibleRecords(),
        (record) => {
          if (!record.inclusion || !record.inclusion.checked) return null;
          const previous = record.inclusion.checked;
          record.inclusion.checked = false;
          syncInclusion(record);
          return { included: previous };
        }
      );
    });
  }

  const applyPlacement = document.querySelector("[data-apply-placement]");
  if (applyPlacement && bulkPlacement) {
    applyPlacement.addEventListener("click", () => {
      if (!bulkPlacement.value) {
        bulkPlacement.focus();
        return;
      }
      const selectedLabel = bulkPlacement.options[bulkPlacement.selectedIndex].text;
      runBulk(
        (changed, scoped) => `Applied ${selectedLabel} to ${changed} of ${scoped} VMs in the current filter.`,
        visibleRecords(),
        (record) => {
          if (!record.placement || record.placement.value === bulkPlacement.value) return null;
          const previous = record.placement.value;
          syncPlacement(record, bulkPlacement.value);
          return { placement: previous };
        }
      );
    });
  }

  if (undoButton) {
    undoButton.addEventListener("click", () => {
      if (!undoSnapshot) return;
      undoSnapshot.forEach((saved) => {
        const record = recordByIndex.get(saved.rowIndex);
        if (!record) return;
        if (Object.prototype.hasOwnProperty.call(saved, "included") && record.inclusion) {
          record.inclusion.checked = saved.included;
          syncInclusion(record);
        }
        if (Object.prototype.hasOwnProperty.call(saved, "placement")) {
          syncPlacement(record, saved.placement);
        }
      });
      undoSnapshot = null;
      if (undoRegion) undoRegion.hidden = true;
      updateSelectionStatus();
      updateFilters();
      undoButton.focus({ preventScroll: true });
    });
  }

  rowRecords.forEach((record) => {
    if (record.inclusion) {
      record.inclusion.addEventListener("change", () => {
        syncInclusion(record);
        updateSelectionStatus();
      });
      syncInclusion(record);
    }
    if (record.placement) {
      record.placement.addEventListener("change", () => {
        syncPlacement(record, record.placement.value);
        updateFilters();
      });
      syncPlacement(record, record.placement.value);
    }

    if (record.detailsButton && record.details) {
      record.detailsButton.addEventListener("click", () => {
        record.details.open = !record.details.open;
        record.detailsButton.setAttribute("aria-expanded", String(record.details.open));
      });
      record.details.addEventListener("toggle", () => {
        record.detailsButton.setAttribute("aria-expanded", String(record.details.open));
      });
    }
  });

  sortControls.forEach(({ button, header }) => {
    button.addEventListener("click", () => {
      const sortKey = button.dataset.sort;
      const numberSort = button.dataset.sortType === "number";
      const ascending = !header || header.getAttribute("aria-sort") !== "ascending";
      const dataKey = `sort${sortKey.charAt(0).toUpperCase()}${sortKey.slice(1)}`;
      const sortedRecords = [...rowRecords].sort((left, right) => {
        const leftValue = left.row.dataset[dataKey] || "";
        const rightValue = right.row.dataset[dataKey] || "";
        if (numberSort) {
          const difference = (Number.parseFloat(leftValue) || 0) - (Number.parseFloat(rightValue) || 0);
          return ascending ? difference : -difference;
        }
        const difference = leftValue.localeCompare(rightValue, undefined, { numeric: true, sensitivity: "base" });
        return ascending ? difference : -difference;
      });

      sortControls.forEach((control) => {
        if (control.header) control.header.setAttribute("aria-sort", "none");
      });
      if (header) header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
      sortedRecords.forEach((record) => {
        tbody.appendChild(record.row);
        if (record.detailRow) tbody.appendChild(record.detailRow);
      });
      button.focus({ preventScroll: true });
    });
  });

  updateSelectionStatus();
  updateFilters();

  const errorSummary = document.getElementById("inventory-errors");
  if (errorSummary) window.requestAnimationFrame(() => errorSummary.focus());
})();
