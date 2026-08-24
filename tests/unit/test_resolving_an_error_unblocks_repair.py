"""Resolve, on the Health tab, has to mean something to the repair loop.

THE DEAD END THIS CLOSES
========================
Automatic re-discovery gets three attempts per source. Running out is
deliberate and says "this one needs a person". What was missing was any way for
that person to say they had arrived.

Two hotels sat on Attention repeating

    6 of 11 offers shared an identity with another offer in the same fetch and
    were dropped

every half hour. The repair HAD run: it re-derived the configuration, found it
identical to the stored one, and correctly recorded "no_change" rather than
pretend to fix something -- three times, until the budget was gone. The
selectors were wrong and so was the scanner that produced them, which is a
fault a repair loop cannot see, because it re-derives with the same code.

Fixing the scanner did not help either. Every affected source was locked out of
discovery for good, and the only button on the page closed the alert row and
changed nothing underneath it.

So resolving a selector fault now hands that source its attempts back. The
tests below pin what that must and must not touch.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api.v1.ops import _restore_repair_budget
from app.services.rediscovery import STATE_KEY, RepairState

SPENT = {"attempts": 3, "last_attempt_at": "2026-08-24T12:55:00+00:00",
         "last_outcome": "no_change"}


class FakeSession:
    """Enough of an AsyncSession to answer one lookup."""

    def __init__(self, pairing=None):
        self.pairing = pairing

    async def scalar(self, _statement):
        return self.pairing


def pairing(config: dict | None = None):
    return SimpleNamespace(adapter_config=config)


def run(coroutine):
    """Drive one coroutine to completion.

    Rather than pytest-asyncio: this file tests a decision, not concurrency,
    and a plugin that is absent from an environment turns the whole module
    into a collection error rather than a skip.
    """
    return asyncio.run(coroutine)


def error(error_class: str = "parse_schema_drift", **kwargs):
    return SimpleNamespace(
        error_class=SimpleNamespace(value=error_class),
        hotel_id=kwargs.get("hotel_id", 7),
        source_id=kwargs.get("source_id", 3),
    )


def test_a_selector_fault_gives_the_source_its_attempts_back():
    source = pairing({"room_card": "div.card", STATE_KEY: dict(SPENT)})
    session = FakeSession(source)

    assert run(_restore_repair_budget(session, error())) is True

    state = RepairState.from_config(source.adapter_config)
    assert state.attempts == 0
    assert state.last_attempt_at is None
    assert state.last_outcome == "budget_restored"


def test_the_configuration_itself_is_untouched():
    """Resolve is not a repair. It only says the repair may try again."""
    source = pairing({
        "room_card": "div.card",
        "selectors": {"room_name": "label.fs12", "price": "div.current-price"},
        STATE_KEY: dict(SPENT),
    })
    run(_restore_repair_budget(FakeSession(source), error()))

    assert source.adapter_config["room_card"] == "div.card"
    assert source.adapter_config["selectors"]["room_name"] == "label.fs12"


def test_a_fault_a_repair_could_not_fix_is_left_alone():
    """Resolving a blocked source says nothing about its selectors.

    Handing the budget back here would send a browser at a site that has
    already refused us, which is the one thing the budget exists to prevent.
    """
    source = pairing({STATE_KEY: dict(SPENT)})
    session = FakeSession(source)

    assert run(_restore_repair_budget(session, error("blocked"))) is False
    assert RepairState.from_config(source.adapter_config).attempts == 3


def test_an_untouched_budget_is_not_rewritten():
    """Nothing spent, nothing to give back -- and no pointless write."""
    source = pairing({"room_card": "div.card"})
    assert run(_restore_repair_budget(FakeSession(source), error())) is False
    assert STATE_KEY not in source.adapter_config


def test_an_error_whose_source_is_gone_still_resolves():
    """An error row outlives the pairing it describes; the alert must still close."""
    assert run(_restore_repair_budget(FakeSession(None), error())) is False


def test_an_error_that_names_no_source_still_resolves():
    assert run(_restore_repair_budget(
        FakeSession(pairing({STATE_KEY: dict(SPENT)})),
        error(hotel_id=None, source_id=None),
    )) is False
