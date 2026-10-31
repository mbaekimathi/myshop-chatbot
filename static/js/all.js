/* All-shops assistant — company-wide chat for staff (admin/manager/IT).
   Stateless: POST /api/all/chat/ → {reply} bubbles. */
(function () {
  "use strict";

  var stage = document.getElementById("allStage");
  var form = document.getElementById("allForm");
  var input = document.getElementById("allInput");
  var csrf = form.querySelector("[name=csrfmiddlewaretoken]").value;
  var sugs = document.getElementById("allSugs");

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }[c];
    });
  }

  function fmt(t) {
    var h = esc(t);
    h = h.replace(/\*([^*\n]+)\*/g, "<strong>$1</strong>");
    h = h.replace(/_([^_\n]+)_/g, "<em>$1</em>");
    h = h.replace(/~([^~\n]+)~/g, "<s>$1</s>");
    return h.replace(/\n/g, "<br>");
  }

  function bubble(text, mine) {
    var row = document.createElement("div");
    row.className = "wa-bubble-row " + (mine ? "is-out" : "is-in");
    row.innerHTML =
      '<div class="wa-bubble ' + (mine ? "is-out" : "is-in") + '"><p>' +
      fmt(text) + "</p></div>";
    stage.appendChild(row);
    stage.scrollTop = stage.scrollHeight;
  }

  function send(text) {
    text = (text || "").trim();
    if (!text) return;
    bubble(text, true);
    input.value = "";
    fetch("/api/all/chat/", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: JSON.stringify({ text: text })
    })
      .then(function (r) { return r.json(); })
      .then(function (resp) {
        bubble(resp.ok ? resp.reply : resp.error || "Error", false);
      })
      .catch(function () {
        bubble("Connection problem — try again.", false);
      });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    send(input.value);
  });

  sugs.addEventListener("click", function (e) {
    var b = e.target.closest("[data-sug]");
    if (b) send(b.dataset.sug);
  });

  // Theme toggle
  var themeBtn = document.querySelector("[data-theme-toggle]");
  function paint() {
    var dark = document.documentElement.getAttribute("data-theme") === "dark";
    if (themeBtn) {
      themeBtn.querySelector("[data-icon-dark]").hidden = dark;
      themeBtn.querySelector("[data-icon-light]").hidden = !dark;
    }
  }
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var dark = document.documentElement.getAttribute("data-theme") === "dark";
      var next = dark ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("myshop-theme", next); } catch (e) {}
      paint();
    });
  }
  paint();

  if (window.lucide && window.lucide.createIcons) window.lucide.createIcons();
})();
