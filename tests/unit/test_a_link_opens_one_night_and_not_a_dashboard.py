"""What the link in an alert is allowed to open, and what it must not.

The recipients of a WhatsApp alert are phone numbers, not accounts, so the page
they open cannot be behind a login. That makes it a bearer link, and a bearer
link is only as safe as the two things pinned down here: it opens ONE night of
ONE owner's hotels, and it can never be turned into a session.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.services import comparison_links


# -- a link is not a login -------------------------------------------
@pytest.mark.asyncio
async def test_a_token_that_is_not_an_access_token_is_not_a_session():
    """The escalation this feature would otherwise have opened.

    ``dashboard_user`` used to accept anything the secret had signed and load
    ``sub`` as a user id, which was safe only while access tokens were the only
    thing signed with it. The guard is checked here rather than left to the
    sharing code to avoid ``sub``, because that is a rule every future token
    would have to remember and this is a rule none of them can break.
    """
    from app.core.security import ALGORITHM
    import jwt

    from app.dashboard.routes import dashboard_user

    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "sub": "1",
            "role": "admin",
            "iat": now,
            "exp": now + timedelta(days=1),
            # Anything but "access".
            "typ": "comparison_link",
        },
        get_settings().secret_key.get_secret_value(),
        algorithm=ALGORITHM,
    )

    class _Session:
        async def get(self, model, pk):  # pragma: no cover - must never run
            raise AssertionError(
                "the token was accepted far enough to look a user up"
            )

    assert await dashboard_user(_Session(), forged) is None


@pytest.mark.asyncio
async def test_an_ordinary_session_cookie_still_works():
    """The guard must not sign anybody out by arriving.

    Every cookie already in a browser carries typ=access, which is the whole
    reason this could be tightened without a migration or a forced re-login.
    """
    from app.core.security import create_access_token
    from app.dashboard.routes import dashboard_user

    token = create_access_token("7", "admin", expires_minutes=60)
    user = SimpleNamespace(id=7, is_active=True)

    class _Session:
        async def get(self, model, pk):
            assert pk == 7
            return user

    assert await dashboard_user(_Session(), token) is user


# -- where the link points -------------------------------------------
def test_no_public_address_means_no_link_rather_than_a_local_one(monkeypatch):
    """A message carrying http://127.0.0.1:8000 is worse than one carrying none.

    It is a dead tap for every reader, and it makes the feature look like it is
    working. A worker has no request to take a Host header from, so an unset
    ``public_base_url`` has exactly one honest answer.
    """
    monkeypatch.setattr(
        comparison_links, "get_settings",
        lambda: get_settings().model_copy(update={"public_base_url": ""}),
    )
    assert comparison_links.public_url("8Kd2xQ1mZpVr4tLa") is None


def test_a_trailing_slash_does_not_become_a_double_one(monkeypatch):
    """Somebody will paste the domain with a slash. Both must work."""
    for base in ("https://monitor.example.com", "https://monitor.example.com/"):
        monkeypatch.setattr(
            comparison_links, "get_settings",
            lambda base=base: get_settings().model_copy(
                update={"public_base_url": base}
            ),
        )
        assert (
            comparison_links.public_url("abc")
            == "https://monitor.example.com/c/abc"
        )


# -- which night it opens on -----------------------------------------
def _series(check_in, adults=2):
    return SimpleNamespace(
        check_in=check_in,
        check_out=check_in + timedelta(days=1),
        adults=adults,
    )


class _Session:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return SimpleNamespace(all=lambda: self._rows)


def test_the_link_opens_on_the_night_most_of_the_message_is_about():
    """The commonest stay, not the earliest.

    A summary can carry a weekend-wide reprice alongside one hotel's
    next-Tuesday move. Opening on Tuesday because it sorts first would send the
    reader to a page that explains one line of a message about eight.
    """
    weekend = date(2026, 9, 12)
    tuesday = date(2026, 9, 15)
    rows = [_series(weekend)] * 5 + [_series(tuesday)]

    stay = comparison_links.stay_of(
        _Session(rows), [SimpleNamespace(offer_key="k")]
    )

    assert stay == (weekend, weekend + timedelta(days=1), 2)


def test_a_tie_opens_on_the_earlier_night():
    """The one closest to being actionable today."""
    early, late = date(2026, 9, 12), date(2026, 9, 20)
    rows = [_series(early), _series(late)]

    stay = comparison_links.stay_of(
        _Session(rows), [SimpleNamespace(offer_key="k")]
    )

    assert stay[0] == early


def test_moves_with_no_series_behind_them_get_no_link():
    """Rather than a link to a night nobody was quoted for."""
    assert comparison_links.stay_of(_Session([]), [SimpleNamespace(offer_key="k")]) is None
    assert comparison_links.stay_of(_Session([]), []) is None


# -- the page itself --------------------------------------------------
class _Request:
    url = SimpleNamespace(path="/c/8Kd2xQ1mZpVr4tLa")


def test_the_shared_page_offers_no_way_into_the_dashboard():
    """It is read by somebody with no account.

    A nav bar would be six links to a login form they cannot pass, on a page
    that is otherwise theirs. The check is on the rendered HTML rather than on
    the template not extending base.html, because the failure mode is the same
    however it arrives.
    """
    from app.dashboard.routes import templates

    from app.services.rate_gap import Grid

    html = templates.get_template("shared_comparison.html").render(
        request=_Request(),
        grid=Grid(columns=(), baseline=None, rivals=()),
        baseline=None,
        full=[],
        check_in=date(2026, 9, 11),
        check_out=date(2026, 9, 12),
        adults=2,
        show_with_tax=True,
        expires_at=datetime(2026, 10, 11, tzinfo=UTC),
    )

    assert 'href="/login"' not in html
    assert 'href="/matrix"' not in html
    assert 'href="/settings"' not in html
    # And it says what the reader is holding, so nobody treats a dead link
    # later as the business having gone offline.
    assert "11 Oct 2026" in html
    assert "noindex" in html


def test_an_expired_link_explains_itself():
    """A bare 404 on a phone reads as the business having gone offline."""
    from app.dashboard.routes import templates

    html = templates.get_template("shared_gone.html").render(request=_Request())

    assert "expired" in html.lower()
    assert "noindex" in html
