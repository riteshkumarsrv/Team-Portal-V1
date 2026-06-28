(function (global) {
  "use strict";

  function getCsrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.getAttribute("content") || "";
    var inp = document.querySelector('input[name="csrf_token"]');
    return inp ? inp.value : "";
  }

  function apiPost(url, body) {
    var token = getCsrfToken();
    body.csrf_token = token;
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": token },
      body: JSON.stringify(body),
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () {
          return { ok: false, error: "http_" + r.status };
        });
      }
      return r.json();
    });
  }

  function showErr(wrap, msg) {
    var p = wrap.querySelector(".scrum-checklist-err-msg");
    if (!p) return;
    p.textContent = msg || "";
    p.hidden = !msg;
  }

  function checklistPayload(wrap, body) {
    var out = body || {};
    var sid = wrap.getAttribute("data-sprint-id");
    if (sid) {
      var sprintId = parseInt(sid, 10);
      if (!isNaN(sprintId)) out.sprint_id = sprintId;
    }
    return out;
  }

  function cellValue(td) {
    var ta = td.querySelector(".scrum-checklist-textarea");
    return ta ? ta.value.trim() : td.textContent.trim();
  }

  function saveCellNow(td, wrap, rowId, field) {
    if (td._saveTimer) {
      clearTimeout(td._saveTimer);
      td._saveTimer = null;
    }
    if (td._saveInFlight) {
      return td._saveInFlight;
    }
    var itemId = parseInt(wrap.getAttribute("data-item-id"), 10);
    var url = wrap.getAttribute("data-url-update");
    if (!url || isNaN(itemId) || isNaN(rowId) || !field) {
      return Promise.resolve();
    }
    var value = cellValue(td);
    td.classList.add("scrum-checklist-saving");
    td._saveInFlight = apiPost(
      url,
      checklistPayload(wrap, { id: rowId, item_id: itemId, field: field, value: value })
    )
      .then(function (j) {
        td.classList.remove("scrum-checklist-saving");
        if (!j || !j.ok) {
          td.classList.add("scrum-checklist-err");
          showErr(wrap, "Save failed: " + (j && j.error ? j.error : "unknown"));
        } else {
          td.classList.remove("scrum-checklist-err");
          showErr(wrap, null);
        }
        return j;
      })
      .catch(function () {
        td.classList.remove("scrum-checklist-saving");
        showErr(wrap, "Network error saving cell.");
      })
      .finally(function () {
        td._saveInFlight = null;
      });
    return td._saveInFlight;
  }

  function scheduleCellSave(td, wrap, rowId, field) {
    if (td._saveTimer) clearTimeout(td._saveTimer);
    td._saveTimer = setTimeout(function () {
      td._saveTimer = null;
      saveCellNow(td, wrap, rowId, field);
    }, 700);
  }

  function wireRow(tr, wrap) {
    var rowId = parseInt(tr.getAttribute("data-row-id"), 10);
    tr.querySelectorAll(".scrum-checklist-cell[contenteditable='true']").forEach(function (td) {
      var field = td.getAttribute("data-field");
      td.addEventListener("input", function () {
        scheduleCellSave(td, wrap, rowId, field);
      });
      td.addEventListener("blur", function () {
        saveCellNow(td, wrap, rowId, field);
      });
      td.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          td.blur();
        }
        if (e.key === "Tab") {
          e.preventDefault();
          var cells = Array.from(tr.querySelectorAll(".scrum-checklist-cell"));
          var idx = cells.indexOf(td);
          var next = cells[idx + 1];
          if (next) {
            var ta = next.querySelector(".scrum-checklist-textarea");
            if (ta) ta.focus();
            else next.focus();
          }
        }
      });
    });
    var commentsTd = tr.querySelector(".scrum-checklist-cell--textarea");
    var commentsTa = commentsTd && commentsTd.querySelector(".scrum-checklist-textarea");
    if (commentsTa && !commentsTa.readOnly) {
      commentsTa.addEventListener("input", function () {
        scheduleCellSave(commentsTd, wrap, rowId, "done_till_date");
      });
      commentsTa.addEventListener("blur", function () {
        saveCellNow(commentsTd, wrap, rowId, "done_till_date");
      });
    }
    var delBtn = tr.querySelector(".scrum-checklist-del-btn");
    if (delBtn) {
      delBtn.addEventListener("click", function () {
        if (!confirm("Remove this checklist row?")) return;
        var itemId = parseInt(wrap.getAttribute("data-item-id"), 10);
        var url = wrap.getAttribute("data-url-delete");
        apiPost(url, checklistPayload(wrap, { id: rowId, item_id: itemId })).then(function (j) {
          if (j && j.ok) {
            tr.remove();
            var tbody = wrap.querySelector(".scrum-checklist-tbody");
            if (tbody && !tbody.querySelector("tr[data-row-id]")) {
              var et = document.createElement("tr");
              et.className = "scrum-checklist-empty-row";
              et.innerHTML =
                '<td colspan="5" class="scrum-checklist-empty-msg">No checklist items. Click "+ Add row".</td>';
              tbody.appendChild(et);
            }
          } else {
            showErr(wrap, "Delete failed: " + (j && j.error ? j.error : "unknown"));
          }
        }).catch(function () {
          showErr(wrap, "Network error deleting row.");
        });
      });
    }
  }

  function appendChecklistRow(wrap, rowId, itemName) {
    var tbody = wrap.querySelector(".scrum-checklist-tbody");
    var emptyRow = tbody && tbody.querySelector(".scrum-checklist-empty-row");
    if (emptyRow) emptyRow.remove();
    var tr = document.createElement("tr");
    tr.setAttribute("data-row-id", rowId);
    tr.innerHTML =
      '<td class="scrum-checklist-cell" data-field="items_to_finish" contenteditable="true" spellcheck="false">' +
      itemName.replace(/</g, "&lt;").replace(/>/g, "&gt;") +
      '</td><td class="scrum-checklist-cell scrum-checklist-cell--status" data-field="status" contenteditable="true" spellcheck="false"></td>' +
      '<td class="scrum-checklist-cell" data-field="le_to_complete" contenteditable="true" spellcheck="false"></td>' +
      '<td class="scrum-checklist-cell scrum-checklist-cell--done scrum-checklist-cell--textarea" data-field="done_till_date">' +
      '<textarea class="scrum-checklist-textarea" rows="2" spellcheck="false"></textarea></td>' +
      '<td class="scrum-checklist-del-col"><button type="button" class="scrum-checklist-del-btn" title="Remove row">✕</button></td>';
    if (tbody) {
      tbody.appendChild(tr);
      tbody.scrollTop = tbody.scrollHeight;
    }
    wireRow(tr, wrap);
    var firstCell = tr.querySelector("[data-field='items_to_finish']");
    if (firstCell) {
      if (itemName) {
        saveCellNow(firstCell, wrap, rowId, "items_to_finish");
      } else if (typeof firstCell.focus === "function") {
        firstCell.focus();
      }
    }
    return tr;
  }

  function flushChecklistWrap(wrap) {
    if (!wrap) return Promise.resolve();
    var pending = [];
    wrap.querySelectorAll("tr[data-row-id]").forEach(function (tr) {
      var rowId = parseInt(tr.getAttribute("data-row-id"), 10);
      tr.querySelectorAll(".scrum-checklist-cell[data-field]").forEach(function (td) {
        var field = td.getAttribute("data-field");
        pending.push(saveCellNow(td, wrap, rowId, field));
      });
    });
    if (!pending.length) return Promise.resolve();
    return Promise.all(pending);
  }

  function flushScrumChecklistWraps(root) {
    var scope = root && root.querySelectorAll ? root : document;
    var wraps = scope.querySelectorAll(".scrum-checklist-wrap");
    if (!wraps.length) return Promise.resolve();
    return Promise.all(Array.from(wraps).map(flushChecklistWrap));
  }

  function initScrumChecklistWrap(wrap) {
    if (!wrap || wrap.getAttribute("data-checklist-wired") === "1") return;
    wrap.setAttribute("data-checklist-wired", "1");
    wrap.querySelectorAll("tr[data-row-id]").forEach(function (tr) {
      wireRow(tr, wrap);
    });
    var addBtn = wrap.querySelector(".scrum-checklist-add-btn");
    if (!addBtn) return;
    addBtn.addEventListener("click", function () {
      var itemId = parseInt(wrap.getAttribute("data-item-id"), 10);
      if (isNaN(itemId)) {
        showErr(wrap, "Invalid item id.");
        return;
      }
      var url = wrap.getAttribute("data-url-add");
      addBtn.disabled = true;
      showErr(wrap, null);
      apiPost(url, checklistPayload(wrap, { item_id: itemId, items_to_finish: "" }))
        .then(function (j) {
          addBtn.disabled = false;
          if (!j || !j.ok) {
            showErr(wrap, "Could not add row: " + (j && j.error ? j.error : "unknown error"));
            return;
          }
          appendChecklistRow(wrap, j.id, "");
        })
        .catch(function () {
          addBtn.disabled = false;
          showErr(wrap, "Network error adding row.");
        });
    });
  }

  function initScrumChecklistWraps(root) {
    var scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll(".scrum-checklist-wrap").forEach(initScrumChecklistWrap);
  }

  global.initScrumChecklistWraps = initScrumChecklistWraps;
  global.initScrumChecklistWrap = initScrumChecklistWrap;
  global.flushScrumChecklistWraps = flushScrumChecklistWraps;
})(window);
