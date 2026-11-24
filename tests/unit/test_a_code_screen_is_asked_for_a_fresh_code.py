"""The two-factor screen: ask for a fresh code, and never save the password.

The page here is RMS's code screen as captured on the server on 21 Nov 2026,
cut down: a pop-up with the code box, "Trust this device", a "Resend Code"
link and a Login button, plus the script RMS ships inside it that holds the
user name and password in plain text. Pressing Resend shows a second pop-up
("One-Time Passcode Email has been sent.") over a modal overlay, which blocks
the code box until its Ok is pressed -- as RMS's does.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app.services import rate_app_login as login  # noqa: E402

PAGE = """
<html><body>
<form class="login-form"><input type="password" style="display:none"></form>
<div role="dialog" class="ui-dialog" id="code">
  <div class="twoFactorModal">
    <label>Enter the one-time passcode that has been sent to your email address</label>
    <input id="TwoFactorPassCodeModal" type="tel" autocomplete="off">
    <a href="#" onclick="event.preventDefault(); resend()">Resend Code</a>
    <label><input type="checkbox" id="ByPassTwoFactorModal">Trust this device</label>
    <a href="#">I can't access my email</a>
    <script>
      function SubmitBtnClick() { document.title = "submitted"; }
      var user = "SOMEUSER"; var pw = "NOT-A-REAL-PASSWORD";
    </script>
  </div>
  <div><button type="button" onclick="SubmitBtnClick()">Login</button></div>
</div>
<script>
  var asked = 0;
  function resend() {
    asked += 1;
    setTimeout(function () {
      var o = document.createElement("div");
      o.id = "overlay";
      o.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:50";
      document.body.appendChild(o);
      var d = document.createElement("div");
      d.setAttribute("role", "dialog");
      d.style.cssText = "position:fixed;top:20px;left:20px;z-index:60;background:#fff";
      d.innerHTML = "<p>One-Time Passcode Email has been sent.</p><button type=button>Ok</button>";
      d.querySelector("button").onclick = function () { d.remove(); o.remove(); };
      document.body.appendChild(d);
    }, 300);
  }
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except PlaywrightError as exc:  # no browser installed here
            pytest.skip(f"no Chromium: {str(exc).splitlines()[0]}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page()
    pg.set_content(PAGE)
    yield pg
    pg.close()


def test_resend_is_pressed_and_its_answer_read_and_closed(page):
    box, scope = login._code_screen(page)
    assert box is not None and scope is not page

    said = login._request_code(scope, page, box)

    assert said == "One-Time Passcode Email has been sent."
    assert page.evaluate("asked") == 1
    # The answer's pop-up is gone, so the code box takes a click again.
    box.click(timeout=2_000)
    box.press_sequentially("123456")
    assert box.input_value() == "123456"


def test_a_screen_without_a_resend_link_is_left_alone(page):
    page.evaluate("document.querySelector('#code a').remove()")
    box, scope = login._code_screen(page)
    assert login._request_code(scope, page, box) is None
    assert page.evaluate("asked") == 0


def test_the_saved_copy_has_no_script_and_no_password(page, tmp_path, monkeypatch):
    monkeypatch.setattr(login, "get_settings", lambda: type("S", (), {"artifact_dir": str(tmp_path)})())
    _, scope = login._code_screen(page)
    scope.locator("#TwoFactorPassCodeModal").fill("999999")
    scope.evaluate("el => el.querySelector('#TwoFactorPassCodeModal').setAttribute('value', '999999')")

    login._keep_dialog_html(scope, 7)

    saved = (tmp_path / "rate-app-2fa-7.html").read_text(encoding="utf-8")
    assert "TwoFactorPassCodeModal" in saved, "the panel itself is kept"
    assert "<script" not in saved
    assert "NOT-A-REAL-PASSWORD" not in saved and "SOMEUSER" not in saved
    assert "999999" not in saved
