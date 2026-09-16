"""Try the saved login to the rate application, and say what happened.

This is a PROBE, not a session. It opens the login page in a fresh browser
context, fills in what it was given, presses the one button that submits the
form, looks at where that landed, takes a picture, and closes the browser.
It does not save cookies, it does not click anything after the login, and it
does not read anything off the page beyond what it needs to say "logged in"
or "refused, and here is why". Changing a rate is a different job, for a
driver written against the specific application once we know which one it
is; this is how we find out whether that driver could get in at all.

WHY THE JUDGEMENT IS HEURISTIC
==============================
There is no standard for what a login page looks like, so this one reads the
page the way a person does: the box with the dots in it is the password, the
text boxes beside it are the identifiers, the button under them is the way
in. Which text box is the client number and which the username is decided
from what the box is labelled -- name, id, placeholder, aria-label, the
<label> pointing at it -- and only then from their order. After submitting,
"logged in" means the password box is gone and nothing on the page says
otherwise; "refused" means the box is still there, with whatever the page
said beside it. Every verdict comes with the sentence of evidence it rests
on, so a wrong one can be recognised as wrong.

The password is never logged, never returned, and never written to disk.
The screenshot is taken AFTER the submit, of the landing page, so it cannot
show the password either -- the field is either gone or masked.
"""
from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.config import get_settings

log = structlog.get_logger(__name__)

#: How long to give the page after submitting before deciding. A login round
#: trip is a redirect or two; twenty seconds is generous, and a page still
#: loading after that is reported as such rather than waited on for minutes.
_SETTLE_SECONDS = 20

#: What a text box is called when it wants the client number rather than the
#: user. Channel managers call it a hotel code, a property id, a client id, a
#: company, a customer, a tenant, an account -- one of those words is nearly
#: always in the label, and none of them is "user".
_CLIENT_RE = re.compile(
    r"client|hotel.?(code|id)|property|company|customer|tenant|account|"
    r"organi[sz]ation|corp|entity|resort|(?<![a-z])cid(?![a-z])|"
    r"(?<![a-z])code(?![a-z])",
    re.IGNORECASE,
)
#: No bare "id" here: "hotel id" and "client id" are the OTHER box.
_USER_RE = re.compile(r"user|login|email|e-mail|mobile|phone", re.IGNORECASE)

#: The page saying no, in its own words. Checked only on text near the form
#: and only when the password box is still showing, so a marketing paragraph
#: containing "incorrect" elsewhere on the page cannot turn a login into a
#: refusal.
_REFUSAL_RE = re.compile(
    r"invalid|incorrect|wrong|does not (match|exist)|doesn't (match|exist)|"
    r"not (found|recogni[sz]ed|valid|registered)|failed|unable|denied|locked|"
    r"disabled|expired|blocked|try again|mismatch|unauthori[sz]ed",
    re.IGNORECASE,
)
#: A second factor. The saved login cannot answer it, and saying so is more
#: useful than "the login page was still showing".
_OTP_RE = re.compile(
    r"one.?time|otp|verification code|verify your|two.?factor|2fa|authenticator|"
    r"security code|sent (a|the) code|enter the code",
    re.IGNORECASE,
)
_CAPTCHA_RE = re.compile(r"captcha|i'?m not a robot|recaptcha|hcaptcha", re.IGNORECASE)

_TEXT_INPUTS = (
    "input:not([type]), input[type=text], input[type=email], input[type=tel], "
    "input[type=number], input[type=search]"
)


@dataclass(frozen=True, slots=True)
class LoginProbe:
    """What the attempt found. ``ok`` is the verdict; ``message`` is why."""

    ok: bool
    message: str
    landed_url: str | None = None
    landed_title: str | None = None
    screenshot_path: str | None = None
    #: What each saved value was typed into, by the box's label -- so a wrong
    #: guess ("client number went into the username box") is visible.
    filled: dict[str, str] = field(default_factory=dict)
    #: The browser's cookies after a login that ticked "trust this device",
    #: for the caller to seal and keep. Only on success, only when asked.
    storage_state: dict | None = None
    #: Whether a one-time code was asked for and answered on this attempt.
    used_code: bool = False


def _describe(handle) -> str:
    """Everything a box is called, in one lowercase string, for the regexes."""
    parts = []
    for attr in ("name", "id", "placeholder", "aria-label", "title", "autocomplete"):
        try:
            value = handle.get_attribute(attr)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            parts.append(value)
    try:
        box_id = handle.get_attribute("id")
        if box_id:
            label = handle.page.locator(f'label[for="{box_id}"]').first
            if label.count():
                parts.append(label.inner_text(timeout=1_000))
    except Exception:  # noqa: BLE001
        pass
    try:
        # A label wrapping the box, or a caption sitting just before it.
        parts.append(handle.evaluate(
            "el => (el.closest('label')?.innerText || "
            "el.parentElement?.previousElementSibling?.innerText || '')"
        ))
    except Exception:  # noqa: BLE001
        pass
    return " ".join(p.strip() for p in parts if p and p.strip()).lower()


def _label_for(desc: str) -> str:
    """A short human name for a box, for the report. The first word-ish thing."""
    return (desc.split("\n")[0][:40] or "unlabelled box").strip()


def _visible(locator) -> list:
    out = []
    try:
        for i in range(min(locator.count(), 12)):
            h = locator.nth(i)
            if h.is_visible():
                out.append(h)
    except Exception:  # noqa: BLE001
        pass
    return out


def _find_form(page):
    """The password box, its form (or the page), and the visible text boxes in it."""
    passwords = _visible(page.locator("input[type=password]"))
    if not passwords:
        return None, page, _visible(page.locator(_TEXT_INPUTS))
    password = passwords[0]
    form = password.locator("xpath=ancestor::form[1]")
    scope = form if form.count() else page
    texts = _visible(scope.locator(_TEXT_INPUTS))
    return password, scope, texts


def _assign(texts, *, want_client: bool) -> tuple[object | None, object | None, dict[str, str]]:
    """Which box takes the client number and which the username.

    By label first, by position second. A page with one text box gets the
    username; the client number then has nowhere to go and the report says
    so, because "logged in without it" and "it was never asked for" are the
    same outcome with different implications for the driver that follows.
    """
    described = [(h, _describe(h)) for h in texts]
    client = username = None
    if want_client:
        for h, d in described:
            if _CLIENT_RE.search(d) and not _USER_RE.search(d):
                client = h
                break
    for h, d in described:
        if h is client:
            continue
        if _USER_RE.search(d):
            username = h
            break
    remaining = [h for h, _ in described if h is not client and h is not username]
    if want_client and client is None and username is None and len(remaining) >= 2:
        client, username = remaining[0], remaining[1]
    elif username is None and remaining:
        username = remaining[0]
    elif want_client and client is None and len(remaining) >= 1 and username is not None:
        # The username was found by label and one box is left over; on a page
        # that asks for two identifiers, the other one is the client number.
        client = remaining[0]
    names = {}
    for key, h in (("client_number", client), ("username", username)):
        if h is not None:
            names[key] = _label_for(next(d for hh, d in described if hh is h))
    return client, username, names


def _submit(scope, page, password) -> str:
    """Press the way in. Returns what was pressed, for the report."""
    buttons = _visible(scope.locator("button[type=submit], input[type=submit]"))
    if not buttons:
        buttons = _visible(scope.locator(
            "button:has-text('Login'), button:has-text('Log in'), button:has-text('Sign in'), "
            "button:has-text('Sign In'), button:has-text('LOGIN'), button:has-text('Submit'), "
            "a:has-text('Login'), a:has-text('Sign in'), [role=button]:has-text('Login')"
        ))
    if buttons:
        try:
            label = (buttons[0].inner_text(timeout=1_000) or buttons[0].get_attribute("value") or "the button").strip()
        except Exception:  # noqa: BLE001
            label = "the button"
        buttons[0].click(timeout=5_000)
        return f"clicked '{label[:30]}'"
    (password if password is not None else page.locator(_TEXT_INPUTS).first).press("Enter")
    return "pressed Enter"


def _settle(page) -> None:
    """Give the login round trip time to finish, without waiting forever."""
    try:
        page.wait_for_load_state("networkidle", timeout=_SETTLE_SECONDS * 1_000)
    except Exception:  # noqa: BLE001
        pass
    # Client-side logins swap the DOM without a load event: a few more seconds
    # for the form to go away on its own before the verdict is read.
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            if not _visible(page.locator("input[type=password]")):
                return
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.5)


_DIALOGS = (
    "[role=dialog], [role=alertdialog], .modal.show, .modal.in, .modal[style*='display: block'], "
    ".swal-modal, .swal2-popup, .k-dialog, .k-window, .ui-dialog, .MuiDialog-paper, .ant-modal"
)


def _dialog_text(page) -> str:
    """What a pop-up on the page says, if one is showing. Empty otherwise.

    RMS Cloud answers a login in a modal -- "Your Client ID, user name and/or
    password was incorrect - Error Code: 3" -- that sits outside the form,
    so reading the form's own text found nothing and the verdict said the
    page had said nothing. A visible dialog is the page's answer, whatever
    it is, and is quoted as such.
    """
    try:
        for h in _visible(page.locator(_DIALOGS)):
            text = " ".join(h.inner_text(timeout=1_000).split())
            if text:
                return text[:300]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _near_form_text(page) -> str:
    """The words on the page around the login form, for reading a refusal."""
    for selector in (
        "[role=alert]", ".alert", ".error", ".errors", ".invalid-feedback", ".text-danger",
        ".validation-summary-errors", ".message", ".msg", "#error", "#errorMessage",
        ".toast", ".notification", ".swal2-html-container", ".ant-message", ".MuiAlert-message",
    ):
        try:
            for h in _visible(page.locator(selector)):
                text = h.inner_text(timeout=1_000).strip()
                if text:
                    return text
        except Exception:  # noqa: BLE001
            continue
    try:
        passwords = _visible(page.locator("input[type=password]"))
        if passwords:
            form = passwords[0].locator("xpath=ancestor::form[1]")
            if form.count():
                return form.inner_text(timeout=2_000)
        return page.inner_text("body", timeout=2_000)[:3_000]
    except Exception:  # noqa: BLE001
        return ""


def _screenshot(page, owner_user_id: int) -> str | None:
    try:
        directory = Path(get_settings().artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rate-app-login-{owner_user_id}-{uuid.uuid4().hex[:8]}.png"
        page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("rate_app_screenshot_failed", error=str(exc))
        return None


#: The box a one-time code goes into: whatever single input the code screen
#: shows. RMS uses a text box with a padlock; others use "number" or "tel".
_CODE_INPUTS = (
    "input:not([type]), input[type=text], input[type=number], input[type=tel], "
    "input[type=password]"
)


def _typed(filled: dict[str, str]) -> str:
    return "; ".join(f"{k.replace('_', ' ')} → {v}" for k, v in filled.items())


def _verdict(page, owner_user_id: int, *, filled: dict, pressed: str, unasked: str,
             used_code: bool, keep_state: bool) -> LoginProbe:
    """Read the page after a submit and say what it means."""
    still_login = bool(_visible(page.locator("input[type=password]")))
    title = page.title()
    url = page.url
    shot = _screenshot(page, owner_user_id)
    typed = _typed(filled)
    dialog = _dialog_text(page)

    if not still_login and not (dialog and _REFUSAL_RE.search(dialog)):
        state = None
        if keep_state:
            try:
                state = page.context.storage_state()
            except Exception as exc:  # noqa: BLE001
                log.warning("rate_app_storage_state_failed", error=str(exc))
        via = " The one-time code was accepted." if used_code else ""
        return LoginProbe(
            ok=True,
            message=(
                f"Logged in. Landed on \"{title or url}\" and the sign-in form is "
                f"gone.{via} (Typed: {typed}; {pressed}.){unasked}"
            ),
            landed_url=url, landed_title=title, screenshot_path=shot, filled=filled,
            storage_state=state, used_code=used_code,
        )

    nearby = _near_form_text(page)
    if _CAPTCHA_RE.search(nearby):
        why = "the page now asks for a CAPTCHA"
    elif dialog:
        why = f"the page says: “{dialog}”"
    else:
        match = _REFUSAL_RE.search(nearby)
        if match:
            start = max(0, match.start() - 60)
            snippet = " ".join(nearby[start:match.end() + 80].split())
            why = f"the page says: “{snippet}”"
        else:
            why = (
                "the sign-in form is still showing with no error message we "
                "could read -- the details may be wrong, or the page may need "
                "something typed differently"
            )
    return LoginProbe(
        ok=False,
        message=f"Not logged in: {why}. (Typed: {typed}; {pressed}.){unasked}",
        landed_url=url, landed_title=title, screenshot_path=shot, filled=filled,
        used_code=used_code,
    )


def _code_screen(page):
    """The one-time-code box and the panel it sits in, or ``(None, None)``.

    LOOKS IN A POP-UP FIRST. RMS draws its two-factor screen as a modal OVER
    the login form, which stays in the page underneath -- password box,
    Login button and all. Read the page as a whole, the password box is
    "still visible" and the verdict was "still on the login page" while the
    screenshot plainly showed "Enter the one-time passcode". So a visible
    dialog is read on its own, and only when there is none does the whole
    page count, where a password box still showing means step one failed.

    The panel comes back with the box because the Login button to press is
    the dialog's, not the one under it.
    """
    scopes = _visible(page.locator(_DIALOGS)) + [page]
    for scope in scopes:
        try:
            text = scope.inner_text(timeout=2_000)[:3_000]
        except Exception:  # noqa: BLE001
            continue
        if not _OTP_RE.search(text):
            continue
        if scope is page and _visible(page.locator("input[type=password]")):
            return None, None
        boxes = _visible(scope.locator(_CODE_INPUTS))
        if len(boxes) == 1:
            return boxes[0], scope
        for box in boxes:
            if re.search(r"code|otp|passcode|token|verif", _describe(box)):
                return box, scope
    return None, None


def _tick_trust(scope, page) -> bool:
    """Tick "Trust this device" if the code panel offers it. Says whether it did.

    The real checkbox is often invisible -- a styled span drawn over an
    ``<input>`` at opacity 0 -- so it is found by description whether visible
    or not, and ticked through its label when it cannot be clicked itself.
    """
    boxes = scope.locator("input[type=checkbox]")
    try:
        count = boxes.count()
    except Exception:  # noqa: BLE001
        return False
    for i in range(min(count, 8)):
        box = boxes.nth(i)
        if not re.search(r"trust|remember", _describe(box)):
            continue
        try:
            if box.is_checked():
                return True
        except Exception:  # noqa: BLE001
            pass
        for action in (
            lambda: box.check(timeout=2_000),
            lambda: box.click(timeout=2_000, force=True),
            lambda: page.locator(f'label[for="{box.get_attribute("id")}"]').first.click(timeout=2_000),
            lambda: scope.get_by_text(re.compile(r"trust this device|remember", re.I)).first.click(timeout=2_000),
        ):
            try:
                action()
                if box.is_checked():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False
    # No checkbox by description: try the words themselves, once.
    try:
        label = scope.get_by_text(re.compile(r"trust this device", re.I)).first
        if label.count() and label.is_visible():
            label.click(timeout=2_000)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _keep_dialog_html(scope, owner_user_id: int) -> None:
    """The code panel's markup, for reading what a refused code was typed into.

    No secret is in it: the code has not been typed yet, and the password
    field is behind it, masked, and not part of the panel.
    """
    try:
        directory = Path(get_settings().artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        html = scope.evaluate("el => el.outerHTML") if scope is not None else ""
        (directory / f"rate-app-2fa-{owner_user_id}.html").write_text(html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log.warning("rate_app_dialog_capture_failed", error=str(exc))


def attempt_login(
    *,
    login_url: str,
    client_number: str,
    username: str,
    password: str,
    owner_user_id: int,
    storage_state: dict | None = None,
    on_code_needed: Callable[[str], None] | None = None,
    wait_for_code: Callable[[], str | None] | None = None,
) -> LoginProbe:
    """Open the page, sign in, report. Never raises for anything the page did.

    ``storage_state`` is the browser's memory of an earlier visit -- the
    cookie that says this device answered the one-time code before -- and is
    what makes the application not ask again.

    THE ONE-TIME CODE IS THE OWNER'S TO TYPE. When the page asks for one,
    ``on_code_needed`` is told (so the dashboard can show a box) and
    ``wait_for_code`` is called, blocking with the browser open until the
    owner has typed it or the wait gives up. Without those two callbacks a
    code screen is reported as the end of the road, which is what it is for
    an unattended login.
    """
    from app.adapters.playwright_base import open_page
    from app.core.errors import FetchError

    try:
        # As a person, not as the scraper: see ``BrowserPool.context``.
        with open_page(
            login_url, artifact_label="rate-app-login", as_person=True,
            storage_state=storage_state,
        ) as fetch:
            page = fetch.page
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001
                pass

            password_box, scope, texts = _find_form(page)
            filled: dict[str, str] = {}

            # A two-step page: identifier first, password on the next screen.
            if password_box is None and texts:
                _, user_box, names = _assign(texts, want_client=False)
                if user_box is not None:
                    user_box.fill(username)
                    filled["username"] = names.get("username", "the first box")
                    _submit(scope, page, None)
                    _settle(page)
                    password_box, scope, texts = _find_form(page)

            if password_box is None:
                if storage_state and not _visible(page.locator(_TEXT_INPUTS)):
                    # No form at all, and we arrived with cookies: the site
                    # let the remembered session straight in.
                    return _verdict(
                        page, owner_user_id, filled={}, pressed="opened the link",
                        unasked=" The saved session was still valid, so no login was needed.",
                        used_code=False, keep_state=True,
                    )
                return LoginProbe(
                    ok=False,
                    message=(
                        "Opened the link but could not find a password box on it, so "
                        "this does not look like the login page. Check the link opens "
                        "the sign-in form when you paste it into a browser."
                    ),
                    landed_url=page.url,
                    landed_title=page.title(),
                    screenshot_path=_screenshot(page, owner_user_id),
                    filled=filled,
                )

            if _visible(page.locator("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], .g-recaptcha, .h-captcha")):
                return LoginProbe(
                    ok=False,
                    message=(
                        "The login page has a CAPTCHA on it. A saved login cannot get "
                        "past one, so this application can only be driven if the "
                        "CAPTCHA can be switched off for this account."
                    ),
                    landed_url=page.url,
                    landed_title=page.title(),
                    screenshot_path=_screenshot(page, owner_user_id),
                )

            client_box, user_box, names = _assign(
                list(texts), want_client=bool(client_number.strip())
            )
            if client_box is not None:
                client_box.fill(client_number)
                filled["client_number"] = names["client_number"]
            if user_box is not None and "username" not in filled:
                user_box.fill(username)
                filled["username"] = names["username"]
            password_box.fill(password)
            filled["password"] = "the password box"

            pressed = _submit(scope, page, password_box)
            _settle(page)
            unasked = (
                " The page had no box for the client number, so it was not entered."
                if client_number.strip() and "client_number" not in filled
                else ""
            )

            code_box, code_scope = _code_screen(page)
            if code_box is None:
                return _verdict(
                    page, owner_user_id, filled=filled, pressed=pressed, unasked=unasked,
                    used_code=False, keep_state=True,
                )

            # The application wants the one-time code it just sent.
            prompt = " ".join((page.inner_text("body", timeout=2_000) or "").split())[:200]
            if on_code_needed is None or wait_for_code is None:
                return LoginProbe(
                    ok=False,
                    message=(
                        f"The password was accepted, then the application asked for a "
                        f"one-time code (“{prompt[:120]}”). Nobody was there to "
                        f"type it. (Typed: {_typed(filled)}; {pressed}.)"
                    ),
                    landed_url=page.url, landed_title=page.title(),
                    screenshot_path=_screenshot(page, owner_user_id), filled=filled,
                )

            on_code_needed(prompt)
            code = wait_for_code()
            if not code:
                return LoginProbe(
                    ok=False,
                    message=(
                        "The application asked for a one-time code and none was "
                        "entered in time, so the login was abandoned. Test again "
                        "when you have the email open."
                    ),
                    landed_url=page.url, landed_title=page.title(),
                    screenshot_path=_screenshot(page, owner_user_id), filled=filled,
                )

            _keep_dialog_html(code_scope if code_scope is not page else None, owner_user_id)
            trusted = _tick_trust(code_scope, page)
            # Typed, not filled: ``fill`` sets the value in one go without a
            # keystroke, and a page that copies the code across on keyup never
            # sees it -- RMS answered such a fill with "Invalid Passcode".
            code_box.click(timeout=3_000)
            code_box.fill("")
            code_box.press_sequentially(code.strip(), delay=40)
            filled["one-time code"] = "the code box" + (", trust this device ticked" if trusted else "")
            pressed = _submit(code_scope, page, code_box)
            _settle(page)

            if _code_screen(page)[0] is not None:
                dialog = _dialog_text(page)
                why = f"the page says: “{dialog}”" if dialog else "the code box is still showing"
                return LoginProbe(
                    ok=False,
                    message=(
                        f"The one-time code was not accepted: {why}. Codes expire "
                        f"quickly; test again and type the newest one."
                    ),
                    landed_url=page.url, landed_title=page.title(),
                    screenshot_path=_screenshot(page, owner_user_id), filled=filled,
                    used_code=True,
                )
            return _verdict(
                page, owner_user_id, filled=filled, pressed=pressed, unasked=unasked,
                used_code=True, keep_state=trusted,
            )
    except FetchError as exc:
        # Already a sentence for an operator: which bot wall, how long it waited.
        return LoginProbe(ok=False, message=f"Could not open the login page. {exc}")
    except Exception as exc:  # noqa: BLE001 - reported, never leaked raw
        reason = str(exc).splitlines()[0].strip() or type(exc).__name__
        log.warning("rate_app_login_probe_failed", error=reason)
        return LoginProbe(ok=False, message=f"The login attempt failed before it could finish: {reason}")
