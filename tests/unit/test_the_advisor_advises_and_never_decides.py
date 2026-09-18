"""The model's second opinion is bounded, and it cannot move a rate.

The advisor exists to be compared against the median rule, not to replace it
yet. Two properties have to hold for that comparison to be safe to run beside
a system that sets real rates:

    the answer is always bounded    a model asking for -60% is recorded as
                                    the edge of the band, never acted on
    a failure is always a blank     no network, bad JSON, a refusal, a string
                                    where a number belongs -- none of them
                                    raise, and none of them produce a position

Everything here runs without a key or a network: ``advise`` takes the HTTP
call as an argument precisely so these branches are reachable in a unit test.
"""
from decimal import Decimal

import pytest

from app.services import repricing as rule
from app.services.repricing_advisor import SCHEMA, Advice, Ask, advise, parse

BAND = Decimal("8")


def answer(**over) -> str:
    import json
    body = {
        "position_pct": -3.5,
        "confidence": 0.7,
        "rationale": "Two of the four rivals are above 9,000 and only one is under ours.",
        "key_factors": ["thin supply in tier", "weekend"],
    }
    body.update(over)
    return json.dumps(body)


def ask() -> Ask:
    from datetime import date
    return Ask(
        hotel="MGM WHISPERING NEST",
        room_name="Deluxe Double Room",
        tier_label="Deluxe",
        check_in=date(2026, 9, 19),
        our_price=Decimal("8000"),
        competitors=(
            ("A R Thanga Kottai", "Deluxe Pool View", Decimal("10495")),
            ("Ananthyam Resort", "Deluxe", Decimal("8000")),
            ("ASG HOLIDAY RESORTS", "Deluxe Double", Decimal("9724")),
        ),
        rule_position_pct=Decimal("0"),
        our_recent=(Decimal("8200"), Decimal("8000"), Decimal("8000")),
    )


class TestTheAnswerIsBounded:
    def test_a_reasonable_position_is_kept(self):
        got = parse(answer(), max_position_pct=BAND)
        assert got.position_pct == Decimal("-3.5")
        assert got.clamped is None

    def test_a_wild_position_is_pulled_to_the_edge(self):
        """-60% is not a proposal, it is a bug with a paragraph attached."""
        got = parse(answer(position_pct=-60), max_position_pct=BAND)
        assert got.position_pct == -BAND
        assert "clamped" in got.clamped
        assert got.error is None  # still usable, just bounded

    def test_the_band_holds_upward_too(self):
        got = parse(answer(position_pct=41.2), max_position_pct=BAND)
        assert got.position_pct == BAND

    def test_the_edge_of_the_band_is_not_clamped(self):
        got = parse(answer(position_pct=8), max_position_pct=BAND)
        assert got.position_pct == BAND
        assert got.clamped is None


class TestEveryFailureIsABlank:
    """None of these raise, and none of them yield a position."""

    @pytest.mark.parametrize("payload", [
        "not json at all",
        "[1, 2, 3]",
        '{"confidence": 0.9}',
        '{"position_pct": "cheaper please"}',
        '{"position_pct": null}',
        '{"position_pct": "NaN"}',
    ])
    def test_an_unusable_answer_produces_no_position(self, payload):
        got = parse(payload, max_position_pct=BAND)
        assert got.position_pct is None
        assert not got.usable
        assert got.error

    def test_a_network_failure_is_recorded_not_raised(self):
        def boom(**_):
            raise TimeoutError("read timed out")

        got = advise(ask(), complete=boom, model="m", max_position_pct=BAND)
        assert not got.usable
        assert "TimeoutError" in got.error
        assert got.latency_ms is not None

    def test_a_confidence_outside_zero_to_one_is_dropped_but_the_number_kept(self):
        """A confidence of 87 means it answered a different question.

        The position is still the position; the confidence is the part that
        cannot be believed, so only that is discarded.
        """
        got = parse(answer(confidence=87), max_position_pct=BAND)
        assert got.position_pct == Decimal("-3.5")
        assert got.confidence is None


class TestWhatTheModelIsAsked:
    def test_the_prompt_carries_every_competitor_and_our_price(self):
        text = ask().as_prompt()
        for hotel in ("A R Thanga Kottai", "Ananthyam Resort", "ASG HOLIDAY RESORTS"):
            assert hotel in text
        assert "8,000" in text
        assert "Saturday" in text  # 2026-09-19; day of week is a pricing fact

    def test_the_schema_forbids_extra_keys(self):
        """Strict output is what stops prose arriving wrapped around a number."""
        assert SCHEMA["additionalProperties"] is False
        assert set(SCHEMA["required"]) == {
            "position_pct", "confidence", "rationale", "key_factors"
        }

    def test_the_model_is_never_given_a_rupee_target_to_return(self):
        """It returns a POSITION. Rounding, the step cap, the floor and the
        ceiling all stay in repricing.py, where they are already tested."""
        assert "price" not in SCHEMA["properties"]
        assert "target" not in SCHEMA["properties"]


class TestTheAdvisorCannotMoveARate:
    def test_its_position_goes_through_the_same_limits_as_the_rule(self):
        """The step cap binds the advisor exactly as it binds the rule.

        This is the property the whole shadow design rests on: an advisor
        asking for the edge of its band still cannot produce a bigger move
        than the rule could, because guard() is applied to both.
        """
        from dataclasses import replace

        own = rule.Rule(position_pct=Decimal("0"), max_step_pct=Decimal("10"), round_to=10)
        market = Decimal("9724")
        our = Decimal("8000")

        shadow = replace(own, position_pct=-BAND)
        wanted = rule.aim(market, shadow)
        target, held, capped = rule.guard(our, wanted, shadow)

        assert target <= our * Decimal("1.10")
        assert target >= our * Decimal("0.90")
        assert capped is not None  # the cap did the binding, not the model

    def test_advice_carries_no_way_to_name_a_price(self):
        got = Advice(position_pct=Decimal("-3"))
        assert not hasattr(got, "target")
        assert not hasattr(got, "price")
