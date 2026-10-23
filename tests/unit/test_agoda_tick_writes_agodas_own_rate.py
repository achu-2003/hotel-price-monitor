"""The rate beside Agoda's tick is Agoda's own rule, not Booking.com's share.

On 23 Oct the box carried the share Booking.com moved by (+28.5%), and ticking
Agoda would have written 10,280 -- about 750 ABOVE Sterling on Agoda, where the
rule wants ASG a hundred under it. ``_lane_rms`` is the one sum the room's
Agoda row, the Agoda section and that box all read.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.dashboard.routes import _lane_rms
from app.db.models.repricing import RmsRoomMapping
from app.services.repricing import to_rms

AGODA = SimpleNamespace(channel="Agoda", benchmark_meal_plan="Breakfast")


def _classic(**bounds) -> RmsRoomMapping:
    return RmsRoomMapping(rms_room="CLASSIC", rate_type_ep="Online Rate EP", rate_type_cp="Online Rate CP",
                          rate_type_map="Online Rate MAP", **bounds)


def _deluxe_double(target: str | None = "6698.75") -> SimpleNamespace:
    # ASG 5,880 on Agoda, Sterling 6,798.75: the rule wants 6,698.75.
    return SimpleNamespace(room_type_id=25, board="Breakfast", our_price=Decimal("5880"),
                           target=Decimal(target) if target else None, exact=True, with_tax=True)


READ = {("Agoda", "CLASSIC", "Online Rate CP"): Decimal("8000")}


def test_the_rate_is_sterling_on_agoda_less_the_gap_scaled_by_agodas_own_row():
    got = _lane_rms(AGODA, _deluxe_double(), _classic(), READ, {}, 1)

    assert got.anchor == "CP"
    assert got.cur == Decimal("8000")
    assert got.rms_new == to_rms(Decimal("6698.75"), Decimal("5880"), Decimal("8000"),
                                 round_to=1, exact=True, with_tax=True)
    assert got.rms_new == Decimal("9113")
    assert got.bounded is None and not got.settled


def test_booking_coms_reading_of_the_same_row_is_not_used():
    # Booking.com's CLASSIC CP holds a different number; Agoda's must be read.
    got = _lane_rms(AGODA, _deluxe_double(), _classic(),
                    {("Booking.com", "CLASSIC", "Online Rate CP"): Decimal("10278")}, {}, 1)

    assert got.cur is None and got.rms_new is None


def test_a_channel_without_the_anchor_row_scales_the_first_plan_it_sells():
    got = _lane_rms(AGODA, _deluxe_double(), _classic(),
                    {("Agoda", "CLASSIC", "Online Rate MAP"): Decimal("11000")}, {}, 1)

    assert got.anchor == "MAP"
    assert got.cur == Decimal("11000")


def test_the_rooms_floor_still_holds():
    got = _lane_rms(AGODA, _deluxe_double(), _classic(floor_amount=Decimal("9500")), READ, {}, 1)

    assert got.rms_new == Decimal("9500")
    assert "floor" in got.bounded


def test_a_held_room_proposes_no_rate():
    got = _lane_rms(AGODA, _deluxe_double(target=None), _classic(), READ, {}, 1)

    assert got.cur == Decimal("8000") and got.rms_new is None
