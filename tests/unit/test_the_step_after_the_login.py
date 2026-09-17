"""What the login probe does with the step that follows a good login.

The RMS driver walks from the landing page to a channel's page. That walk is
handed to the probe as ``after_login``; the probe must run it only on a
login that succeeded, and must not let a stumble on the walk be reported as
the login failing -- getting in and finding the way are different facts.
"""
from __future__ import annotations

from app.services.rate_app_login import LoginProbe, _then


def test_the_step_is_not_taken_after_a_refused_login():
    calls = []
    refused = LoginProbe(ok=False, message="Not logged in: the page says no.")
    out = _then(lambda page, p: calls.append(p) or p, object(), refused)
    assert out is refused
    assert calls == []


def test_the_step_replaces_the_report_of_a_good_login():
    logged_in = LoginProbe(ok=True, message="Logged in.", screenshot_path="landing.png")

    def walk(page, probe):
        return LoginProbe(ok=True, message=probe.message + " Then went further.",
                          screenshot_path="channel.png")

    out = _then(walk, object(), logged_in)
    assert out.ok
    assert out.message == "Logged in. Then went further."
    assert out.screenshot_path == "channel.png"


def test_a_stumble_on_the_walk_keeps_the_login_verdict():
    logged_in = LoginProbe(ok=True, message="Logged in.", screenshot_path="landing.png")

    def walk(page, probe):
        raise RuntimeError("Locator.click: Timeout 10000ms exceeded.\nCall log: ...")

    out = _then(walk, object(), logged_in)
    assert out.ok, "the login stood; only the step after it failed"
    assert out.screenshot_path == "landing.png"
    assert "The step after the login failed: Locator.click: Timeout 10000ms exceeded." in out.message
