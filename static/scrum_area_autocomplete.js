/**
 * Area field autocomplete: GET kb_api_urls.area_suggest?q=… → { matches: string[] }.
 * Binds all input[name="area"] on the Kanban page (Add sticky + per-card Do edit).
 */
(function () {
  function ensureParentPosition(input) {
    var p = input.closest(".field") || input.parentNode;
    if (p && window.getComputedStyle(p).position === "static") {
      p.style.position = "relative";
    }
    return p;
  }

  function ensureBox(input) {
    if (input._areaSuggestUl) return input._areaSuggestUl;
    ensureParentPosition(input);
    var box = document.createElement("ul");
    box.className = "name-suggest scrum-area-suggest";
    box.setAttribute("role", "listbox");
    input.insertAdjacentElement("afterend", box);
    input._areaSuggestUl = box;
    return box;
  }

  function bindAreaInput(input, apiUrl) {
    if (!input || input.disabled || !apiUrl) return;
    var timer = null;

    function hide() {
      var b = input._areaSuggestUl;
      if (!b) return;
      b.innerHTML = "";
      b.style.display = "none";
    }

    function render(items) {
      var box = ensureBox(input);
      box.innerHTML = "";
      if (!items.length) {
        hide();
        return;
      }
      items.forEach(function (txt) {
        var li = document.createElement("li");
        li.textContent = txt;
        li.setAttribute("role", "option");
        li.tabIndex = 0;
        li.addEventListener("mousedown", function (e) {
          e.preventDefault();
          input.value = txt;
          hide();
          try {
            input.dispatchEvent(new Event("input", { bubbles: true }));
          } catch (_e) {
            /* ignore */
          }
        });
        box.appendChild(li);
      });
      box.style.display = "block";
    }

    function fetchMatches(q) {
      if (!q || q.length < 1) {
        hide();
        return;
      }
      var url = apiUrl + (apiUrl.indexOf("?") >= 0 ? "&" : "?") + "q=" + encodeURIComponent(q);
      fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) return { matches: [] };
          return r.json();
        })
        .then(function (data) {
          render(data.matches || []);
        })
        .catch(function () {
          hide();
        });
    }

    input.addEventListener("input", function () {
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        fetchMatches((input.value || "").trim());
      }, 180);
    });

    input.addEventListener("blur", function () {
      setTimeout(hide, 150);
    });

    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") hide();
    });
  }

  function initFromKbApiUrls() {
    var urlsEl = document.getElementById("kb-api-urls");
    if (!urlsEl) return;
    var urls;
    try {
      urls = JSON.parse(urlsEl.textContent);
    } catch (_e) {
      return;
    }
    var apiUrl = urls.area_suggest;
    if (!apiUrl) return;
    document.querySelectorAll('input[name="area"]').forEach(function (inp) {
      bindAreaInput(inp, apiUrl);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initFromKbApiUrls);
  } else {
    initFromKbApiUrls();
  }
})();
