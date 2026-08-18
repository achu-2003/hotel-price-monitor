/*
 * Dashboard behaviour. No framework and no build step.
 *
 * Everything mutating posts to /api/v1/..., never to a parallel set of
 * dashboard handlers, so validation, authorisation and the audit trail cannot
 * drift between the two entry points. The session cookie authenticates these
 * calls; it is HttpOnly, so this file never sees or handles the token.
 *
 * Served from /static because the Content-Security-Policy allows scripts only
 * from 'self' — no CDN, no inline handlers.
 */
(function () {
  "use strict";

  /** POST/PATCH JSON, returning {ok, status, body}. */
  async function api(path, method, body) {
    const response = await fetch(path, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: body ? JSON.stringify(body) : undefined,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (e) {
      payload = null;
    }
    return { ok: response.ok, status: response.status, body: payload };
  }

  /** RFC 7807 problem documents carry the useful text in `detail`. */
  function problemText(result) {
    if (!result.body) return "Request failed (" + result.status + ")";
    if (result.body.errors && result.body.errors.length) {
      return result.body.errors
        .map(function (e) { return e.field + ": " + e.message; })
        .join("; ");
    }
    return result.body.detail || result.body.title || "Request failed";
  }

  // -- login ---------------------------------------------------------
  const loginForm = document.getElementById("login-form");
  if (loginForm) {
    loginForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      const error = document.getElementById("login-error");
      error.hidden = true;

      const result = await api("/api/v1/auth/login", "POST", {
        email: loginForm.email.value,
        password: loginForm.password.value,
      });

      if (result.ok) {
        // The cookie is set by the response; a full navigation follows so the
        // server renders the target page with the session already in place.
        window.location.href = loginForm.dataset.next || "/";
        return;
      }
      error.textContent = problemText(result);
      error.hidden = false;
    });
  }

  // -- generic API-backed forms --------------------------------------
  // Any <form class="api-form" data-endpoint data-method>. Checkboxes become
  // booleans, empty strings become omitted fields (so an untouched optional
  // input is not sent as "").
  document.querySelectorAll("form.api-form").forEach(function (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const status = form.querySelector(".form-status");
      const payload = {};

      let jsonError = null;
      new FormData(form).forEach(function (value, key) {
        if (value === "") return;
        // Fields marked data-json hold JSON (adapter_config), and must be sent
        // as an object. Parsed here so a typo is caught before the request,
        // with a message pointing at the field rather than a 422 from Pydantic.
        const field = form.querySelector('[name="' + key + '"][data-json]');
        if (field) {
          try {
            payload[key] = JSON.parse(value);
          } catch (e) {
            jsonError = key + " is not valid JSON: " + e.message;
          }
          return;
        }
        if (/^-?\d+$/.test(value)) { payload[key] = Number(value); return; }
        payload[key] = value === "true" ? true : value;
      });

      const status0 = form.querySelector(".form-status");
      if (jsonError) {
        if (status0) {
          status0.hidden = false;
          status0.textContent = jsonError;
          status0.className = "form-status error";
        }
        return;
      }

      // Some submits open a browser server-side and take the better part of a
      // minute. Silence for that long reads as a hang, so the button says what
      // is happening and stops a second click landing on top of the first.
      const submit = form.querySelector('button[type="submit"]');
      const original = submit ? submit.textContent : null;
      if (submit) {
        submit.disabled = true;
        submit.textContent = form.dataset.busy || "Working…";
      }
      if (status && form.dataset.busyNote) {
        status.hidden = false;
        status.textContent = form.dataset.busyNote;
        status.className = "form-status";
      }

      const result = await api(form.dataset.endpoint, form.dataset.method || "POST", payload);

      if (submit) {
        submit.disabled = false;
        submit.textContent = original;
      }
      if (status) {
        status.hidden = false;
        status.textContent = result.ok ? "Saved." : problemText(result);
        status.className = "form-status " + (result.ok ? "ok" : "error");
      }
      // data-redirect sends the browser somewhere specific after success —
      // the change-password form must leave its own page, not reload it.
      if (result.ok) {
        setTimeout(function () {
          if (form.dataset.redirect) {
            window.location.href = form.dataset.redirect;
          } else {
            window.location.reload();
          }
        }, 600);
      }
    });
  });

  // -- manual run, with polling --------------------------------------
  document.querySelectorAll("button.run-now").forEach(function (button) {
    button.addEventListener("click", async function () {
      const targetId = button.dataset.targetId;
      const slot = document.querySelector('.run-status[data-for="' + targetId + '"]');
      button.disabled = true;
      if (slot) slot.textContent = "queueing…";

      const result = await api("/api/v1/monitor-targets/" + targetId + "/run", "POST");
      if (!result.ok) {
        if (slot) slot.textContent = problemText(result);
        button.disabled = false;
        return;
      }
      pollCheckRun(result.body.check_run_id, slot, button);
    });
  });

  /*
   * Poll until the run finishes or we give up.
   *
   * Every 2s for up to 2 minutes: a browser fetch is 20-40s, plus up to 3
   * minutes of dispatch jitter is possible on a scheduled run — but a manual
   * run skips the jitter, so two minutes covers it with room to spare. Giving
   * up only stops the polling; the run itself continues and its result is on
   * the hotel page.
   */
  function pollCheckRun(checkRunId, slot, button) {
    let attempts = 0;
    const timer = setInterval(async function () {
      attempts += 1;
      const response = await fetch("/check-runs/" + checkRunId, {
        credentials: "same-origin",
      });
      if (response.ok && slot) slot.innerHTML = await response.text();

      const finished = slot && !slot.textContent.includes("running");
      if (finished || attempts > 60) {
        clearInterval(timer);
        if (button) button.disabled = false;
        if (attempts > 60 && slot && !finished) {
          slot.textContent = "still running — see the hotel page";
        }
      }
    }, 2000);
  }

  // -- one-click actions ---------------------------------------------
  bindAction("button.resolve-error", function (button) {
    return { path: "/api/v1/errors/" + button.dataset.errorId + "/resolve", method: "POST" };
  });

  bindAction("button.resend-notification", function (button) {
    return {
      path: "/api/v1/notifications/" + button.dataset.notificationId + "/resend",
      method: "POST",
    };
  });

  bindAction("button.test-notify", function (button) {
    return {
      path: "/api/v1/notifications/test",
      method: "POST",
      body: {
        recipient_id: Number(button.dataset.recipientId),
        channel: button.dataset.channel || "email",
      },
      keepPage: true,
    };
  });

  bindAction("button.resume-target", function (button) {
    // Closing the circuit also clears the failure counter and makes the target
    // due immediately — see the API handler.
    return {
      path: "/api/v1/monitor-targets/" + button.dataset.targetId,
      method: "PATCH",
      body: { circuit_state: "closed" },
    };
  });

  function bindAction(selector, describe) {
    document.querySelectorAll(selector).forEach(function (button) {
      button.addEventListener("click", async function () {
        const action = describe(button);
        // Only for actions that change something a person would miss. A
        // dialog on every button trains people to dismiss all of them.
        if (action.confirm && !window.confirm(action.confirm)) return;
        button.disabled = true;
        const original = button.textContent;
        button.textContent = "…";

        const result = await api(action.path, action.method, action.body);
        if (!result.ok) {
          button.textContent = original;
          button.disabled = false;
          window.alert(problemText(result));
          return;
        }
        if (action.keepPage) {
          button.textContent = "sent ✓";
        } else {
          window.location.reload();
        }
      });
    });
  }

  // -- mapping an unmatched room -------------------------------------
  document.querySelectorAll("form.resolve-unmatched").forEach(function (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const roomTypeId = form.room_type_id.value;
      if (!roomTypeId) return;

      const result = await api(
        "/api/v1/prices/unmatched/" + form.dataset.unmatchedId + "/resolve",
        "POST",
        { room_type_id: Number(roomTypeId) }
      );
      if (result.ok) {
        window.location.reload();
      } else {
        window.alert(problemText(result));
      }
    });
  });
  // -- stopping and deleting a hotel ---------------------------------
  /*
   * Two verbs, deliberately unequal. Stopping is a DELETE that only clears
   * is_active and disables the targets, so it asks with a plain confirm.
   * Erasing goes through /purge and makes you type the name — see below.
   */
  bindAction("button.deactivate-hotel", function (button) {
    return {
      path: "/api/v1/hotels/" + button.dataset.hotelId,
      method: "DELETE",
      confirm:
        "Stop monitoring " + button.dataset.hotelName + "?\n\n" +
        "Checks stop and it leaves the Matrix. Everything already collected is " +
        "kept, and resuming picks up where it left off.",
    };
  });

  bindAction("button.reactivate-hotel", function (button) {
    return {
      path: "/api/v1/hotels/" + button.dataset.hotelId,
      method: "PATCH",
      body: { is_active: true },
    };
  });

  /*
   * No confirm dialog here on purpose. A dialog is dismissed by reflex; typing
   * the hotel's name is the only confirmation that costs enough attention to
   * be worth anything, and this destroys data nothing in the application can
   * give back. The server checks the name again — the input is a courtesy to
   * the operator, not the safeguard.
   */
  document.querySelectorAll("form.purge-hotel").forEach(function (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const status = form.querySelector(".form-status");
      const button = form.querySelector("button[type='submit']");

      button.disabled = true;
      const result = await api(
        "/api/v1/hotels/" + form.dataset.hotelId + "/purge",
        "POST",
        { confirm_name: form.querySelector("input[name='confirm_name']").value }
      );

      status.hidden = false;
      if (!result.ok) {
        status.textContent = problemText(result);
        status.className = "form-status error";
        button.disabled = false;
        return;
      }

      const body = result.body || {};
      status.className = "form-status ok";
      // Said once, because after the redirect there is nothing left to look at.
      status.textContent =
        "Deleted " + body.name + " — " + body.series_deleted + " series, " +
        body.observations_deleted + " observations, " + body.changes_deleted +
        " recorded changes.";
      setTimeout(function () { window.location.href = "/hotels"; }, 2500);
    });
  });

  // -- replacing a hotel's booking link ------------------------------
  /*
   * Two steps, and only when they are needed. The API refuses with a 409 when
   * the pasted URL turns out to be a different property, because prices already
   * collected belong to the old one — and that refusal is what reveals the
   * confirmation. Someone fixing a typo in a link never sees it.
   *
   * Not a form.api-form: the useful part of the answer is what it did (how many
   * baselines were dropped), and the generic handler reloads the page half a
   * second after saying "Saved.", which would take that with it.
   */
  document.querySelectorAll("form.replace-url").forEach(function (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const status = form.querySelector(".form-status");
      const confirmRow = form.querySelector(".confirm-discard");
      const confirmBox = form.querySelector('input[name="discard_history"]');

      const payload = { url: form.querySelector('input[name="url"]').value };
      if (confirmBox && confirmBox.checked) payload.discard_history = true;

      const result = await api(
        "/api/v1/hotel-sources/" + form.dataset.hotelSourceId + "/replace-url",
        "POST",
        payload
      );

      status.hidden = false;
      if (!result.ok) {
        status.textContent = problemText(result);
        status.className = "form-status error";
        // 409 is the one refusal an operator can answer. A 422 means the URL
        // itself is unusable — a checkbox would not help, and offering one
        // would only invite ticking it.
        if (result.status === 409 && confirmRow) confirmRow.hidden = false;
        return;
      }

      const body = result.body || {};
      status.className = "form-status ok";
      status.textContent = body.property_changed
        ? "Repointed. " + body.series_reset + " stored baseline" +
          (body.series_reset === 1 ? "" : "s") +
          " dropped, so the next check starts fresh."
        : "Link updated. Same property, so the price history is untouched.";
      // Long enough to read the sentence above before the page redraws.
      setTimeout(function () { window.location.reload(); }, 2500);
    });
  });

  // -- deep link to a collapsed panel ---------------------------------
  /*
   * "Edit" in a page head points at a <details> further down. Following the
   * anchor scrolls to it but leaves it shut, which reads as a broken link.
   */
  function openPanelFromHash() {
    if (!window.location.hash) return;
    let panel = null;
    try {
      panel = document.querySelector(window.location.hash);
    } catch (e) {
      return;   // a hash that is not a valid selector is just a hash
    }
    if (panel && panel.tagName === "DETAILS") {
      panel.open = true;
      panel.scrollIntoView({ block: "start" });
    }
  }
  openPanelFromHash();
  window.addEventListener("hashchange", openPanelFromHash);

  // -- error detail rows ---------------------------------------------
  /*
   * The Errors table on Attention shows one line per failure and expands the
   * rest underneath: full message, scrubbed context, and the screenshot taken
   * at the moment it failed.
   *
   * Everything but the screenshot is already in the page, so expanding costs
   * no request. The screenshot's src is withheld until the first expand —
   * forty unresolved errors would otherwise mean forty PNGs fetched to show a
   * table nobody has clicked into yet.
   */
  document.querySelectorAll("button.error-expand").forEach(function (button) {
    button.addEventListener("click", function () {
      const detail = document.getElementById(button.getAttribute("aria-controls"));
      if (!detail) return;

      const opening = detail.hidden;
      detail.hidden = !opening;
      // aria-expanded, not just a class: the caret is decorative and a screen
      // reader has nothing else to go on.
      button.setAttribute("aria-expanded", String(opening));
      button.classList.toggle("is-open", opening);

      if (opening) {
        detail.querySelectorAll("img[data-src]").forEach(function (img) {
          img.src = img.dataset.src;
          img.removeAttribute("data-src");
        });
      }
    });
  });

  // -- price-change popups -------------------------------------------
  /*
   * Until email and WhatsApp are switched on, this is how a confirmed change
   * reaches a person: a toast in whichever dashboard tab is open.
   *
   * The cursor is the highest price_changes.id this browser has been shown,
   * kept in localStorage. Deliberately NOT the `notified` column: that one
   * belongs to the dispatcher, which sets it even when no recipient exists,
   * so sharing it would let the two silence each other.
   *
   * Names in these payloads are scraped from other people's pages, so every
   * one of them goes in through textContent. No innerHTML in this section.
   */
  const CURSOR_KEY = "hpm.changes.cursor";
  const POLL_MS = 30000;
  const TOAST_TTL_MS = 20000;
  const MAX_ON_SCREEN = 4;

  const toastStack = document.getElementById("toast-stack");
  let polling = false;

  async function pollChanges() {
    // A slow request must not queue a second one behind it; the next tick
    // will pick up whatever this one misses.
    if (polling) return;
    polling = true;
    try {
      const stored = window.localStorage.getItem(CURSOR_KEY);
      const query = stored === null ? "" : "?since_id=" + encodeURIComponent(stored);
      const response = await fetch("/changes/recent" + query, {
        credentials: "same-origin",
      });
      // 401 after the session expires: stay quiet. The next navigation gets
      // the login redirect, which is the right place to notice.
      if (!response.ok) return;

      const data = await response.json();
      (data.alerts || []).forEach(showChangeToast);
      if (data.more) showOverflowToast(data.more);
      if (data.cursor !== undefined && data.cursor !== null) {
        window.localStorage.setItem(CURSOR_KEY, String(data.cursor));
      }
    } catch (e) {
      // Offline, or the server is restarting. The cursor is left untouched,
      // so nothing is lost — the next successful poll shows it.
    } finally {
      polling = false;
    }
  }

  const TONES = {
    increase: { tone: "up", word: "Price up" },
    decrease: { tone: "down", word: "Price down" },
    became_unavailable: { tone: "gone", word: "Sold out" },
    became_available: { tone: "back", word: "Back on sale" },
  };

  function showChangeToast(alert) {
    // The word carries the meaning and the colour only reinforces it — the
    // same rule the tables follow, for anyone who cannot tell red from green.
    const kind = TONES[alert.direction] || { tone: "up", word: "Changed" };

    const toast = element("article", "toast toast-" + kind.tone);

    const head = element("div", "toast-head");
    head.appendChild(element("span", "toast-label", kind.word));
    const close = element("button", "toast-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    close.addEventListener("click", function () { dismissToast(toast); });
    head.appendChild(close);
    toast.appendChild(head);

    const hotel = element("a", "toast-hotel", alert.hotel);
    hotel.href = "/hotels/" + alert.hotel_id;
    toast.appendChild(hotel);

    const room = alert.room + (alert.stay ? " · " + alert.stay : "");
    toast.appendChild(element("div", "toast-room", room));

    const prices = element("div", "toast-prices");
    prices.appendChild(element("s", "toast-was", alert.was));
    prices.appendChild(document.createTextNode(" → "));
    prices.appendChild(element("strong", "toast-now", alert.now));
    if (alert.delta) {
      const text = alert.delta + (alert.delta_pct ? " (" + alert.delta_pct + ")" : "");
      prices.appendChild(element("span", "toast-delta", text));
    }
    toast.appendChild(prices);

    toast.appendChild(element("div", "toast-when", alert.when));
    pushToast(toast);
  }

  function showOverflowToast(count) {
    const toast = element("article", "toast toast-more");
    const link = element("a", "toast-hotel", count + " more change" + (count === 1 ? "" : "s"));
    link.href = "/changes";
    toast.appendChild(link);
    toast.appendChild(element("div", "toast-room", "Too many to show — open Changes."));
    pushToast(toast);
  }

  function pushToast(toast) {
    if (!toastStack) return;
    toastStack.appendChild(toast);
    // Beyond four the stack runs off the screen and the oldest is the least
    // interesting, so it goes rather than the newest being pushed out of view.
    while (toastStack.children.length > MAX_ON_SCREEN) {
      toastStack.removeChild(toastStack.firstElementChild);
    }
    setTimeout(function () { dismissToast(toast); }, TOAST_TTL_MS);
  }

  function dismissToast(toast) {
    if (!toast.parentNode) return;
    toast.classList.add("toast-leaving");
    setTimeout(function () {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 200);
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  if (toastStack) {
    pollChanges();
    setInterval(function () {
      // A hidden tab has nobody looking at it, and a toast shown there would
      // expire unseen while still costing a query every 30 seconds.
      if (!document.hidden) pollChanges();
    }, POLL_MS);
    // Returning to a tab left open all afternoon should catch up immediately
    // rather than waiting out the interval.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) pollChanges();
    });
  }

})();
