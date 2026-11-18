// ShopCo Support chat page. Talks only to this server's /v1/chat.
// All server text is inserted with textContent (never innerHTML).
(function () {
  "use strict";

  var KEY_STORE = "shopco.accessKey";
  var CUST_STORE = "shopco.customer";

  var $ = function (id) { return document.getElementById(id); };
  var log = $("log"), empty = $("empty"), msg = $("msg"), send = $("send");
  var settings = $("settings"), settingsBtn = $("settingsBtn");
  var keyInput = $("key"), customerSel = $("customer"), remember = $("remember");

  var state = { key: "", customer: "C-001", sessionId: null, busy: false };

  function store(k, v) { try { v === null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch (e) {} }
  function load(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } }

  function setWho() {
    var name = customerSel.options[customerSel.selectedIndex].text;
    $("who").textContent = state.key ? "Chatting as " + name : "Add your access key to start";
  }

  function toggleSettings(open) {
    settings.hidden = !open;
    settingsBtn.setAttribute("aria-expanded", String(open));
    if (open) keyInput.focus();
  }

  function resetChat() {
    state.sessionId = null;
    Array.prototype.slice.call(log.querySelectorAll(".msg")).forEach(function (n) { n.remove(); });
    empty.hidden = false;
  }

  function bubble(kind, text) {
    empty.hidden = true;
    var d = document.createElement("div");
    d.className = "msg " + kind;
    d.textContent = text;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }

  function tag(parent, text, cls) {
    var t = document.createElement("span");
    t.className = "tag" + (cls ? " " + cls : "");
    t.textContent = text;
    parent.appendChild(t);
  }

  function label(s) { return String(s || "").replace(/_/g, " "); }

  function sendMessage(text) {
    text = text.trim();
    if (!text || state.busy) return;
    if (!state.key) { toggleSettings(true); return; }

    state.busy = true; send.disabled = true;
    bubble("me", text);
    var wait = bubble("them typing", "Thinking…");

    var body = { customer_id: state.customer, message: text };
    if (state.sessionId) body.session_id = state.sessionId;

    fetch("/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Access-Token": state.key },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, data: j }; });
    }).then(function (res) {
      wait.remove();
      if (res.status === 401) {
        bubble("sys", "That access key was rejected. Check it in Settings.");
        toggleSettings(true);
        return;
      }
      if (res.status === 403) {
        resetChat();
        bubble("sys", "This conversation belongs to a different customer, so a new one was started. Please resend.");
        return;
      }
      if (res.status === 422) { bubble("sys", "The message couldn't be sent (invalid input)."); return; }
      if (res.status !== 200) { bubble("sys", "The server returned an error (" + res.status + "). Please try again."); return; }

      var d = res.data;
      state.sessionId = d.session_id || state.sessionId;
      var b = bubble("them", d.reply || "(no reply)");
      var meta = document.createElement("div");
      meta.className = "meta";
      if (d.agent) tag(meta, label(d.agent));
      if (d.outcome) tag(meta, label(d.outcome), d.outcome);
      (d.approvals || []).forEach(function (a) { tag(meta, "Ref " + a); });
      b.appendChild(meta);
      log.scrollTop = log.scrollHeight;
    }).catch(function () {
      wait.remove();
      bubble("sys", "Couldn't reach the server. Check your connection and try again.");
    }).then(function () {
      state.busy = false; send.disabled = false; msg.focus();
    });
  }

  // --- wiring ---------------------------------------------------------
  settingsBtn.addEventListener("click", function () { toggleSettings(settings.hidden); });

  $("showKey").addEventListener("click", function () {
    var show = keyInput.type === "password";
    keyInput.type = show ? "text" : "password";
    this.textContent = show ? "Hide" : "Show";
    this.setAttribute("aria-pressed", String(show));
  });

  $("forget").addEventListener("click", function () {
    store(KEY_STORE, null);
    state.key = ""; keyInput.value = "";
    setWho(); keyInput.focus();
  });

  $("settingsForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var newKey = keyInput.value.trim();
    var newCust = customerSel.value;
    if (newCust !== state.customer) resetChat();
    state.key = newKey; state.customer = newCust;
    store(KEY_STORE, remember.checked ? newKey : null);
    store(CUST_STORE, newCust);
    setWho(); toggleSettings(false); msg.focus();
  });

  $("composer").addEventListener("submit", function (e) {
    e.preventDefault();
    var t = msg.value; msg.value = ""; msg.style.height = "";
    sendMessage(t);
  });

  msg.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("composer").requestSubmit();
    }
  });
  msg.addEventListener("input", function () {
    msg.style.height = "auto";
    msg.style.height = Math.min(msg.scrollHeight, 140) + "px";
  });

  $("chips").addEventListener("click", function (e) {
    if (e.target.classList.contains("chip")) sendMessage(e.target.textContent);
  });

  // --- start ----------------------------------------------------------
  state.key = load(KEY_STORE);
  var savedCust = load(CUST_STORE);
  if (savedCust && customerSel.querySelector('option[value="' + savedCust + '"]')) customerSel.value = savedCust;
  state.customer = customerSel.value;
  keyInput.value = state.key;
  setWho();
  if (!state.key) toggleSettings(true);
})();
