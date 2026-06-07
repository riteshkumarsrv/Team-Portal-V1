/**
 * Roster name autocomplete: fetches /api/employees?q=... and shows closest matches only.
 * Expects: input#employee_name, optional ul#name-suggest (created if missing).
 */
(function () {
  function setup(inputId) {
    var input = document.getElementById(inputId);
    if (!input) return;

    var box = document.getElementById("name-suggest");
    if (!box) {
      box = document.createElement("ul");
      box.id = "name-suggest";
      box.className = "name-suggest";
      input.parentNode.appendChild(box);
    }

    var timer = null;

    function hide() {
      box.innerHTML = "";
      box.style.display = "none";
    }

    function render(items) {
      box.innerHTML = "";
      if (!items.length) {
        hide();
        return;
      }
      items.forEach(function (name) {
        var li = document.createElement("li");
        li.textContent = name;
        li.tabIndex = 0;
        li.addEventListener("mousedown", function (e) {
          e.preventDefault();
          input.value = name;
          hide();
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
      fetch("/api/employees?q=" + encodeURIComponent(q), { headers: { Accept: "application/json" } })
        .then(function (r) {
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
        fetchMatches(input.value.trim());
      }, 180);
    });

    input.addEventListener("blur", function () {
      setTimeout(hide, 150);
    });

    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") hide();
    });
  }

  window.setupEmployeeAutocomplete = setup;
})();
