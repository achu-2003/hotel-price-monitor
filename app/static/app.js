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

      const result = await api(form.dataset.endpoint, form.dataset.method || "POST", payload);
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
})();
