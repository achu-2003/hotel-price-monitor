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
        username: loginForm.username.value,
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

      // An unchecked box is simply absent from FormData, so a PATCH assembled
      // from FormData alone can switch a flag ON and never OFF again — the
      // server sees no key and leaves the old value. Collected explicitly and
      // first, so the loop below overwrites the checked ones with true.
      form.querySelectorAll('input[type="checkbox"][name]').forEach(function (box) {
        payload[box.name] = box.checked;
      });

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

  bindAction("button.unassign-hotel", function (button) {
    // Removing the assignment, not the person: their delivery history stays,
    // and they may still cover other hotels.
    return {
      path:
        "/api/v1/hotels/" + button.dataset.hotelId +
        "/recipients/" + button.dataset.recipientId,
      method: "DELETE",
      confirm:
        "Stop telling this person about " + button.dataset.hotelName + "?",
    };
  });

  bindAction("button.toggle-recipient", function (button) {
    const activating = button.dataset.active === "true";
    return {
      path: "/api/v1/recipients/" + button.dataset.recipientId,
      method: "PATCH",
      body: { is_active: activating },
      // Deactivating is silent by design — nothing errors, the messages just
      // stop — so it is the one worth asking about.
      confirm: activating
        ? null
        : "Deactivate " + button.dataset.name + "?\n\n" +
          "Their assignments are kept, but nothing will be sent to them until " +
          "they are reactivated.",
    };
  });

  /*
   * The irreversible half of the pair above. Deactivating keeps the person and
   * their log; this removes both, so the dialog spells out what goes rather
   * than asking "are you sure?" about an amount nobody can see.
   *
   * No typed-name gate, unlike a hotel purge: that erases months of collected
   * prices, which is the product and cannot be re-gathered. This erases one
   * contact and the record of what was sent to them, which is smaller, and the
   * reversible alternative is the button directly above it.
   */
  bindAction("button.delete-recipient", function (button) {
    const hotels = Number(button.dataset.hotels || 0);
    const sent = Number(button.dataset.sent || 0);
    const goes = [];
    if (hotels) goes.push(hotels + (hotels === 1 ? " hotel assignment" : " hotel assignments"));
    if (sent) goes.push(sent + (sent === 1 ? " sent message" : " sent messages"));

    return {
      path: "/api/v1/recipients/" + button.dataset.recipientId,
      method: "DELETE",
      confirm:
        "Delete " + button.dataset.name + " permanently?\n\n" +
        (goes.length
          ? "This also deletes their " + goes.join(" and ") + ".\n\n"
          : "") +
        "Nothing can bring this back. To stop the messages without losing " +
        "anything, use Deactivate instead.",
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

  // -- phone numbers -------------------------------------------------
  /*
   * Meta's Cloud API accepts E.164 and nothing else, so the stored value has
   * to be +919876543210. Demanding that people TYPE it that way is a different
   * decision, and a bad one: nobody has the country code in their head for a
   * number they read off a business card, and a form that rejects the number
   * as written is a form people give up on.
   *
   * So the field takes whatever is natural — 9876543210, 098765 43210,
   * +91 98765-43210, 0091 98765 43210 — and rewrites it on the way out.
   * The API stays strict; this is the one place that does the translating.
   */
  const DEFAULT_CC = "+91";

  function normalizePhone(raw, defaultCc) {
    const trimmed = (raw || "").trim();
    if (!trimmed) return "";

    const explicit = trimmed.charAt(0) === "+";
    let digits = trimmed.replace(/\D/g, "");
    if (!digits) return trimmed;
    if (explicit) return "+" + digits;

    // 00 is the other way of writing +, dialled from most of the world.
    if (digits.slice(0, 2) === "00") return "+" + digits.slice(2);

    // A leading 0 is the domestic trunk prefix and has no place once the
    // country code goes on: 09876543210 is +919876543210, not +9109876543210.
    digits = digits.replace(/^0+/, "");

    const cc = (defaultCc || DEFAULT_CC).replace(/\D/g, "");
    // Already carries the country code, just without the +. Length is what
    // separates that from a local number that happens to start with 91.
    if (digits.slice(0, cc.length) === cc && digits.length > 10) {
      return "+" + digits;
    }
    return "+" + cc + digits;
  }

  function looksLikeE164(value) {
    return /^\+[1-9]\d{7,14}$/.test(value);
  }

  /* Rewrite in place, but only when the result is plausible. Turning a
     half-typed "98765" into "+9198765" while someone is still typing would
     fight the person rather than help them. */
  function tidyPhoneField(field) {
    const tidied = normalizePhone(field.value, field.dataset.defaultCc);
    if (tidied && tidied !== field.value && looksLikeE164(tidied)) {
      field.value = tidied;
    }
  }

  document.querySelectorAll("input[data-phone]").forEach(function (field) {
    field.addEventListener("blur", function () { tidyPhoneField(field); });
  });

  /* Capture phase, so the value is already E.164 by the time the generic
     api-form handler reads it — submitting with Enter does not always blur
     the field first. */
  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form || !form.querySelectorAll) return;
    form.querySelectorAll("input[data-phone]").forEach(tidyPhoneField);
  }, true);

  // -- registering a recipient ---------------------------------------
  /*
   * Contact details only. Which hotels the person watches is a second call to
   * POST /hotels/{id}/recipients, made from their row once they exist — and
   * until one is made the dispatcher, which reads hotel_recipients, sends them
   * nothing. The row says so; this form does not pretend otherwise.
   *
   * Not a form.api-form: that handler reloads the page 600ms after "Saved.",
   * which would take the "not assigned to anything yet" warning with it.
   */
  function fieldValue(form, name) {
    const field = form.querySelector('[name="' + name + '"]');
    return field ? field.value.trim() : "";
  }

  function checkedChannels(form) {
    return Array.prototype.map.call(
      form.querySelectorAll('input[name="channels"]:checked'),
      function (box) { return box.value; }
    );
  }

  /** The thresholds, omitted rather than sent as null when left blank. */
  function thresholds(form) {
    const payload = {};
    const abs = fieldValue(form, "min_delta_abs");
    const pct = fieldValue(form, "min_delta_pct");
    if (abs !== "") payload.min_delta_abs = Number(abs);
    if (pct !== "") payload.min_delta_pct = Number(pct);
    return payload;
  }

  function say(status, text, kind) {
    if (!status) return;
    status.hidden = false;
    status.textContent = text;
    status.className = "form-status " + (kind || "");
  }

  /** Assign one person to one hotel. Resolves to an error string, or null. */
  async function assignHotel(hotelId, body) {
    const result = await api("/api/v1/hotels/" + hotelId + "/recipients", "POST", body);
    return result.ok ? null : problemText(result);
  }

  document.querySelectorAll("form.create-recipient").forEach(function (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const status = form.querySelector(".form-status");
      const submit = form.querySelector('button[type="submit"]');

      const email = fieldValue(form, "email");
      const phoneField = form.querySelector('[name="phone_e164"]');
      const phone = phoneField
        ? normalizePhone(phoneField.value, phoneField.dataset.defaultCc)
        : "";

      // The same rules the API enforces, checked before the request so they
      // read as sentences rather than as a 422 about a regular expression.
      if (!email && !phone) {
        say(
          status,
          "A recipient needs an email address or a phone number — otherwise " +
          "there is no way to tell them anything.",
          "error"
        );
        return;
      }
      if (phone && !looksLikeE164(phone)) {
        say(
          status,
          "That does not look like a phone number — ten digits for an Indian " +
          "mobile, or the whole number including its country code.",
          "error"
        );
        return;
      }

      const payload = { name: fieldValue(form, "name") };
      if (email) payload.email = email;
      if (phone) payload.phone_e164 = phone;

      const original = submit.textContent;
      submit.disabled = true;
      submit.textContent = "Creating…";

      const created = await api("/api/v1/recipients", "POST", payload);
      submit.textContent = original;
      if (!created.ok) {
        submit.disabled = false;
        say(status, problemText(created), "error");
        return;
      }

      say(
        status,
        created.body.name + " created. Expand their row to choose the hotels " +
        "they watch — nothing is sent until one is assigned.",
        "ok"
      );
      // Long enough to read that sentence, because the next thing to do is in
      // it and the reload scrolls away from this panel.
      setTimeout(function () { window.location.reload(); }, 2500);
    });
  });

  // -- assigning an existing recipient to one more hotel --------------
  document.querySelectorAll("form.assign-hotel").forEach(function (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const status = form.querySelector(".form-status");
      const submit = form.querySelector('button[type="submit"]');
      const hotelId = fieldValue(form, "hotel_id");
      const channels = checkedChannels(form);

      if (!hotelId) { say(status, "Choose a hotel.", "error"); return; }
      if (!channels.length) { say(status, "Pick at least one channel.", "error"); return; }

      submit.disabled = true;
      const body = Object.assign(
        { recipient_id: Number(form.dataset.recipientId), channels: channels },
        thresholds(form)
      );
      const error = await assignHotel(Number(hotelId), body);

      if (error) {
        submit.disabled = false;
        say(status, error, "error");
        return;
      }
      // The endpoint upserts, so re-assigning a hotel already on the list is
      // how its channels and thresholds get edited.
      say(status, "Assigned.", "ok");
      setTimeout(function () { window.location.reload(); }, 600);
    });
  });

  // -- assigning one person to every hotel ----------------------------
  /*
   * The endpoint takes one hotel at a time, so this is a loop rather than a
   * bulk call. Kept in the browser instead of adding a bulk endpoint: each
   * POST is separately validated and separately audited, and a partial result
   * is honest -- three of thirty refused because the channel is unconfigured
   * is worth seeing, not worth rolling back.
   */
  document.querySelectorAll("button.assign-all-hotels").forEach(function (button) {
    button.addEventListener("click", async function () {
      const form = button.closest("form.assign-hotel");
      if (!form) return;
      const status = form.querySelector(".form-status");
      const channels = checkedChannels(form);
      const hotelIds = Array.prototype.map.call(
        form.querySelectorAll('select[name="hotel_id"] option[value]:not([value=""])'),
        function (option) { return Number(option.value); }
      );

      if (!channels.length) { say(status, "Pick at least one channel.", "error"); return; }
      if (!hotelIds.length) { say(status, "There are no active hotels.", "error"); return; }
      if (!window.confirm(
        "Alert " + button.dataset.name + " about all " + hotelIds.length +
        " hotels, on " + channels.join(" and ") + "?"
      )) return;

      const original = button.textContent;
      button.disabled = true;
      const failures = [];
      for (let i = 0; i < hotelIds.length; i += 1) {
        button.textContent = (i + 1) + " of " + hotelIds.length + "…";
        const body = Object.assign(
          { recipient_id: Number(form.dataset.recipientId), channels: channels },
          thresholds(form)
        );
        const error = await assignHotel(hotelIds[i], body);
        if (error) failures.push(error);
      }
      button.textContent = original;

      if (failures.length) {
        // Named once rather than per hotel: the same refusal thirty times over
        // is one problem, and thirty lines of it hides that.
        say(
          status,
          failures.length + " of " + hotelIds.length + " could not be assigned — " +
          failures[0],
          "error"
        );
        setTimeout(function () { window.location.reload(); }, 4000);
        return;
      }
      say(status, "Assigned to all " + hotelIds.length + " hotels.", "ok");
      setTimeout(function () { window.location.reload(); }, 700);
    });
  });

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
  /*
   * "None of these." Confirmed, because it closes a row a person will not be
   * shown again -- but no alias is written, so nothing is taught and nothing
   * is merged, and a name that is real comes straight back next time it is
   * seen. That makes it the recoverable action of the two on this row.
   */
  document.querySelectorAll("button.dismiss-unmatched").forEach(function (button) {
    button.addEventListener("click", async function () {
      const name = button.dataset.rawName || "this name";
      if (!window.confirm(
        "Close \u201c" + name + "\u201d without mapping it to a room?\n\n" +
        "Nothing is merged and no alias is written. If the site really does " +
        "have a room by this name, it will appear here again."
      )) return;

      const result = await api(
        "/api/v1/prices/unmatched/" + button.dataset.unmatchedId + "/dismiss",
        "POST",
        {}
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
  document.querySelectorAll("button.error-expand, button.row-expand").forEach(function (button) {
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

    // An overnight move compares two different nights. Saying so on the toast
    // keeps a reader from assuming the hotel repriced in the last half hour.
    if (alert.basis === "overnight") {
      toast.appendChild(element("div", "toast-basis", "vs last night"));
    }

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

  // -- alert sensitivity ---------------------------------------------
  // One number: how many rupees a price has to move before anybody is told.
  //
  // The comparison engine also carries a percentage floor and requires BOTH
  // to be cleared. Leaving a percentage in place while only the rupee amount
  // is editable makes the visible setting a lie -- 100 rupees would be silent
  // on a 25,000 rupee suite, because 0.4% is under a 2% floor, and nothing on
  // the page would say why. So saving here sets the percentage to zero and
  // the rupee amount decides on its own.
  document.querySelectorAll("form.alert-defaults-form").forEach(function (form) {
    const status = form.querySelector(".form-status");

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const number = function (name) {
        const input = form.querySelector("input[name=" + name + "]");
        return input ? Number(input.value) : null;
      };
      say(status, "Saving...", "");
      const result = await api("/api/v1/alert-defaults", "PUT", {
        min_delta_abs: number("min_delta_abs"),
        min_delta_pct: 0,
        confirm_checks: number("confirm_checks"),
      });
      if (!result.ok) {
        say(status, problemText(result), "error");
        return;
      }
      // "Within a minute" rather than "saved": the workers cache this, so a
      // change is not instant and saying so prevents a second save when the
      // next alert still uses the old figure.
      say(status, "Saved — in effect within a minute.", "ok");
    });
  });

  // -- per-hotel alert sensitivity -----------------------------------
  // Not the generic api-form: clearing an override means sending null, and
  // that handler omits empty fields rather than sending them. An omitted
  // field is "leave it alone" to a PATCH, so a hotel could be given its own
  // sensitivity and never handed back to the default again.
  document.querySelectorAll("form.target-sensitivity").forEach(function (form) {
    const status = form.querySelector(".form-status");
    const own = form.querySelector("input[name=own_sensitivity]");
    const fields = form.querySelector(".sensitivity-fields");

    function reflect() {
      if (!fields) return;
      const using = own && own.checked;
      fields.classList.toggle("is-inherited", !using);
      fields.querySelectorAll("input").forEach(function (input) {
        input.disabled = !using;
      });
    }
    if (own) own.addEventListener("change", reflect);
    reflect();

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const using = own && own.checked;
      const value = function (name) {
        const input = form.querySelector("input[name=" + name + "]");
        return input && input.value !== "" ? Number(input.value) : null;
      };
      // Nulls, explicitly, when the box is unchecked: that is what hands the
      // hotel back to the deployment default.
      const payload = using
        ? {
            min_delta_abs: value("min_delta_abs"),
            // Zero, not null: null would inherit the deployment percentage and
            // silently raise the bar this hotel was just given.
            min_delta_pct: 0,
            confirm_checks: value("confirm_checks"),
          }
        : { min_delta_abs: null, min_delta_pct: null, confirm_checks: null };

      say(status, "Saving...", "");
      const result = await api(
        "/api/v1/monitor-targets/" + form.dataset.targetId, "PATCH", payload);
      if (!result.ok) {
        say(status, problemText(result), "error");
        return;
      }
      say(status, using ? "Saved — this hotel uses its own." : "Saved — back to the default.", "ok");
    });
  });

  // -- WhatsApp alert numbers ----------------------------------------
  // The whole list is PUT on every save, never a delta: a cleared row is how
  // "stop this number" is expressed, and it only reads as removal if the
  // server sees the complete set. Sending just the filled rows would leave a
  // number switched on with no way to switch it off.
  document.querySelectorAll("form.alert-numbers-form").forEach(function (form) {
    const rows = form.querySelector(".alert-number-rows");
    const rowTemplate = form.querySelector(".alert-number-row-template");
    const addButton = form.querySelector(".alert-number-add");
    const countLabel = form.querySelector(".alert-number-count");
    const max = Number(form.dataset.max || 5);

    /** Renumber the visible rows and tell the operator how many are left.
     *
     * Called after every add and remove: the labels are positional, so a row
     * removed from the middle would otherwise leave a gap in the numbering
     * and make the list look broken.
     */
    function refresh() {
      const all = rows.querySelectorAll(".alert-number-row");
      all.forEach(function (row, index) {
        const label = row.querySelector(".row-number");
        if (label) label.textContent = index + 1;
      });
      // The last remaining row is emptied rather than deleted -- a form with
      // no rows at all offers nowhere to type and looks broken.
      const only = all.length === 1;
      all.forEach(function (row) {
        const remove = row.querySelector(".alert-number-remove");
        if (remove) remove.title = only ? "Clear this number" : "Remove this number";
      });
      if (addButton) addButton.disabled = all.length >= max;
      if (countLabel) {
        countLabel.textContent = all.length >= max
          ? "Five is the maximum."
          : all.length + " of " + max;
      }
    }

    if (addButton && rowTemplate) {
      addButton.addEventListener("click", function () {
        if (rows.querySelectorAll(".alert-number-row").length >= max) return;
        const row = rowTemplate.content.cloneNode(true);
        rows.appendChild(row);
        refresh();
        // Focus the field that was just revealed, so the button press and the
        // typing are one continuous action.
        const added = rows.lastElementChild.querySelector("input[name=phone]");
        if (added) {
          added.addEventListener("blur", function () { tidyPhoneField(added); });
          added.focus();
        }
      });
    }

    /* The x on a SAVED row deletes that number, there and then.
     *
     * It used to only take the row off the screen, leaving the actual deletion
     * to the Save button. That reads as done -- the row is gone -- so anybody
     * who navigated away at that point had changed nothing, and a number they
     * believed they had stopped went on receiving every price change on every
     * hotel. The screen and the truth disagreed, silently, in the direction
     * that costs money.
     *
     * A row that has never been saved has no recipient id and nothing to
     * delete, so it keeps the old behaviour: removed from the form, or
     * emptied when it is the only one left.
     */
    rows.addEventListener("click", async function (event) {
      const button = event.target.closest(".alert-number-remove");
      if (!button) return;
      const row = button.closest(".alert-number-row");
      const recipientId = row.dataset.recipientId;

      function dropRow() {
        if (rows.querySelectorAll(".alert-number-row").length === 1) {
          row.querySelectorAll("input").forEach(function (i) { i.value = ""; });
          delete row.dataset.recipientId;
        } else {
          row.remove();
        }
        refresh();
      }

      if (!recipientId) {
        dropRow();
        return;
      }

      const who = row.dataset.name || "this number";
      if (!window.confirm(
        "Stop sending to " + who + "?\n\n" +
        "It stops receiving immediately. What was already sent to it stays in " +
        "the delivery history on Alerts, and adding the number again later " +
        "reconnects to the same record."
      )) return;

      const status = form.querySelector(".form-status");
      button.disabled = true;
      say(status, "Removing " + who + "…", "");
      const result = await api("/api/v1/alert-numbers/" + recipientId, "DELETE");
      if (!result.ok) {
        button.disabled = false;
        say(status, problemText(result), "error");
        return;
      }
      dropRow();
      say(status, who + " will not be messaged again.", "ok");
    });

    refresh();

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      const status = form.querySelector(".form-status");

      // Normalise before reading, not on blur alone: a number pasted into the
      // field and submitted with Enter never fires a blur event, and would
      // otherwise be rejected by the E.164 pattern on the server.
      form.querySelectorAll("input[data-phone]").forEach(tidyPhoneField);

      const numbers = [];
      const seen = {};
      let duplicate = null;
      let unnamed = null;
      let numberless = null;
      form.querySelectorAll(".alert-number-row").forEach(function (row) {
        const phone = row.querySelector("input[name=phone]").value.trim();
        const label = row.querySelector("input[name=label]").value.trim();
        // A name typed with no number is not an empty row: somebody meant to
        // add that person and the digits never landed. Silently dropping it
        // saves a list they believe has one more entry than it does.
        if (!phone) {
          if (label) numberless = label;
          return;
        }
        if (!label) { unnamed = phone; return; }
        if (seen[phone]) { duplicate = phone; return; }
        seen[phone] = true;
        numbers.push({ phone_e164: phone, name: label });
      });

      // Caught here as well as on the server, because the server's version
      // rejects the whole save and these name the offending row while the
      // operator is still looking at it.
      if (unnamed) {
        say(status, "Say whose number " + unnamed + " is — the list is unusable "
                  + "later if nobody can tell whose is whose.", "error");
        return;
      }
      if (numberless) {
        say(status, "No number for " + numberless + ".", "error");
        return;
      }
      if (duplicate) {
        say(status, duplicate + " is entered twice. Each number may appear once.", "error");
        return;
      }

      say(status, "Saving…");
      const result = await api("/api/v1/alert-numbers", "PUT", { numbers: numbers });
      if (!result.ok) {
        say(status, problemText(result), "error");
        return;
      }

      const saved = (result.body && result.body.numbers) || [];
      const ready = result.body && result.body.whatsapp_ready;
      if (!saved.length) {
        say(status, "All alert numbers removed. Nothing will be sent to them.", "ok");
      } else if (ready) {
        say(status, saved.length + " number(s) saved. They now get every price change.", "ok");
      } else {
        // Saved, but inert. Reported as a warning rather than a success, so
        // nobody walks away believing alerts are live when they are not.
        say(status, saved.length + " number(s) saved, but WhatsApp is not "
                  + "configured yet — nothing will reach them until it is.", "error");
      }
      // Reloaded so the summary count and the row values come from the
      // server's view rather than from what was typed.
      setTimeout(function () { window.location.reload(); }, 1200);
    });
  });

  // -- lists that stop after N rows ----------------------------------
  // The height comes from the rows, not from a number of pixels: these tables
  // wrap, so the twelfth row is not at a predictable offset and a fixed height
  // shows eleven and a half rows on the day somebody's room name got longer.
  //
  // Measured by adding up the heights rather than by comparing positions,
  // because a position inside a box that is already scrolled is relative to
  // where the reader left it -- which made the box shrink a little every time
  // the window was resized mid-list.
  document.querySelectorAll("[data-rows]").forEach(function (box) {
    const wanted = Number(box.dataset.rows || 0);
    if (!wanted) return;

    function fit() {
      const rows = box.querySelectorAll("tbody tr");
      // Shorter than the cap already: no scrollbar, and no empty band under
      // the last row either.
      if (rows.length <= wanted) {
        box.style.maxHeight = "none";
        return;
      }
      const head = box.querySelector("thead");
      let height = head ? head.offsetHeight : 0;
      for (let i = 0; i < wanted; i += 1) height += rows[i].offsetHeight;
      box.style.maxHeight = height + "px";
    }

    fit();

    // Rows rewrap when the window narrows, and a height measured at the old
    // width then hides part of the twelfth row.
    let pending = null;
    window.addEventListener("resize", function () {
      window.clearTimeout(pending);
      pending = window.setTimeout(fit, 150);
    });
  });

})();
