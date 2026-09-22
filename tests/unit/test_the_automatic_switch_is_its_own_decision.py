"""Turning the rule loose is sent alone, saved alone, and audited alone.

It used to be a checkbox inside the rule form, written by Save rule along
with nine percentages. Two things were wrong with that. Adjusting a floor and
pressing Save was enough to start -- or stop -- rates moving unattended, with
no sign that it had happened. And once the switch moved out of that form, the
form stopped sending the field, so a plain ``bool = False`` on the payload
would have read every save as "turn it off".

So the switch has its own route, and the rule's payload treats an absent
``auto_enabled`` as "leave it alone".
"""
from __future__ import annotations

import pytest

from app.schemas.repricing import AutomaticIn, RepricingSettingsIn


class TestTheRuleFormCannotChangeIt:
    def test_an_absent_switch_is_none_not_false(self):
        """The distinction the whole thing rests on: None means "not
        mentioned", which the route skips. False would mean "turn it off"."""
        payload = RepricingSettingsIn(floor_pct=40)
        assert payload.auto_enabled is None

    def test_a_save_that_omits_it_carries_no_instruction(self):
        payload = RepricingSettingsIn(max_step_pct=50, floor_pct=50)
        assert payload.model_dump()["auto_enabled"] is None

    def test_an_older_caller_that_sends_it_is_still_obeyed(self):
        """The field is kept, not dropped: something that already sends it
        must keep working rather than silently stop having an effect."""
        assert RepricingSettingsIn(auto_enabled=True).auto_enabled is True
        assert RepricingSettingsIn(auto_enabled=False).auto_enabled is False


class TestTheSwitchSendsOneThing:
    def test_it_carries_the_state_asked_for(self):
        assert AutomaticIn(enabled=True).enabled is True
        assert AutomaticIn(enabled=False).enabled is False

    def test_it_must_say_which(self):
        """No default. A request that forgot to say is a bug, not an "off"."""
        with pytest.raises(Exception):
            AutomaticIn()

    def test_it_carries_nothing_else(self):
        """Nothing about the rule can ride along on the request that lets
        rates move by themselves."""
        assert set(AutomaticIn(enabled=True).model_dump()) == {"enabled"}
