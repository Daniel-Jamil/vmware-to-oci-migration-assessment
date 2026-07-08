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
  const warningButtons = Array.from(document.querySelectorAll(".warning-filter[data-warning-filter]"));
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
    const noteButton = row.querySelector("[data-note-details-target]");
    return {
      row,
      rowIndex,
      detailRow: detailRowsByIndex.get(rowIndex),
      inclusion: row.querySelector('input[name="included_vm_names"]'),
      placement: placementsByIndex.get(rowIndex),
      placementLabel: row.querySelector("[data-placement-label]"),
      detailsButton,
      noteButton,
      details: document.getElementById(
        detailsButton ? detailsButton.dataset.detailsTarget : noteButton ? noteButton.dataset.noteDetailsTarget : ""
      ),
      warningIds: (row.dataset.warningIds || "").split(/\s+/).filter(Boolean),
      searchText: (row.dataset.search || "").toLowerCase(),
    };
  });
  const sortControls = Array.from(table.querySelectorAll("[data-sort]")).map((button) => ({
    button,
    header: button.closest("th"),
  }));
  let activeWarning = "all";
  let searchTimer = null;
  const requestedWarning = new URLSearchParams(window.location.search).get("warning") || "";

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

  function syncDetailVisibility(record) {
    if (!record.detailRow) return;
    const rowVisible = !record.row.hidden;
    const detailsOpen = Boolean(record.details && record.details.open);
    record.detailRow.hidden = !rowVisible || !detailsOpen;
  }

  function setDetailsOpen(record, open) {
    if (!record.details) return;
    record.details.open = open;
    const expanded = String(open);
    if (record.detailsButton) record.detailsButton.setAttribute("aria-expanded", expanded);
    if (record.noteButton) record.noteButton.setAttribute("aria-expanded", expanded);
    syncDetailVisibility(record);
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
      syncDetailVisibility(record);
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

  function runBulk(rowScope, change) {
    rowScope.forEach(change);
    updateSelectionStatus();
    updateFilters();
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

  function applyWarningFilter(warningId, options = {}) {
    activeWarning = warningId || "all";
    const activeButton = warningButtons.find((button) => button.dataset.warningFilter === activeWarning);
    warningButtons.forEach((candidate) => {
      const active = candidate === activeButton;
      candidate.classList.toggle("is-active", active);
      candidate.setAttribute("aria-pressed", String(active));
    });
    warningItems.forEach((item) => {
      const itemId = item.dataset.warningItem || "all";
      item.hidden = activeWarning === "all" ? itemId !== "all" : itemId !== activeWarning;
    });
    updateFilters();
    if (options.scrollToTable && table instanceof HTMLElement) {
      table.closest(".inventory-list")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (options.focus && activeButton instanceof HTMLElement) {
      activeButton.focus({ preventScroll: true });
    }
  }

  warningButtons.forEach((button) => {
    button.addEventListener("click", () => {
      applyWarningFilter(button.dataset.warningFilter || "all");
    });
  });

  const selectAll = document.querySelector("[data-select-all]");
  if (selectAll) {
    selectAll.addEventListener("click", () => {
      runBulk(
        rowRecords,
        (record) => {
          if (!record.inclusion || record.inclusion.checked) return;
          record.inclusion.checked = true;
          syncInclusion(record);
        }
      );
    });
  }

  const includeFiltered = document.querySelector("[data-include-filtered]");
  if (includeFiltered) {
    includeFiltered.addEventListener("click", () => {
      runBulk(
        visibleRecords(),
        (record) => {
          if (!record.inclusion || record.inclusion.checked) return;
          record.inclusion.checked = true;
          syncInclusion(record);
        }
      );
    });
  }

  const excludeFiltered = document.querySelector("[data-exclude-filtered]");
  if (excludeFiltered) {
    excludeFiltered.addEventListener("click", () => {
      runBulk(
        visibleRecords(),
        (record) => {
          if (!record.inclusion || !record.inclusion.checked) return;
          record.inclusion.checked = false;
          syncInclusion(record);
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
      runBulk(
        visibleRecords(),
        (record) => {
          if (!record.placement || record.placement.value === bulkPlacement.value) return;
          syncPlacement(record, bulkPlacement.value);
        }
      );
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
        setDetailsOpen(record, !record.details.open);
      });
      record.details.addEventListener("toggle", () => {
        setDetailsOpen(record, record.details.open);
      });
    }
    if (record.noteButton && record.details) {
      record.noteButton.addEventListener("click", () => {
        setDetailsOpen(record, !record.details.open);
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
  const requestedWarningButton = requestedWarning
    ? warningButtons.find((button) => button.dataset.warningFilter === requestedWarning)
    : null;
  if (requestedWarningButton) {
    applyWarningFilter(requestedWarningButton.dataset.warningFilter, { focus: true });
  } else {
    updateFilters();
  }

  const errorSummary = document.getElementById("inventory-errors");
  if (errorSummary) window.requestAnimationFrame(() => errorSummary.focus());
})();
