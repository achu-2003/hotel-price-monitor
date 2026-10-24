"""Agoda's room id reaches the offer, and keeps two same-named rooms apart."""
from app.adapters.engines import ENGINES
from app.adapters.mapping import dedupe_offers, offer_from_mapping


def _agoda():
    return next(p for p in ENGINES if p.key == "agoda").adapter_config


def _node(room_id, name="Deluxe Room", price=6300.0, breakfast=False):
    return {"id": room_id, "name": name, "isFit": True, "isBreakfastIncluded": breakfast,
            "quantity": 1, "perNightPrice": {"display": price},
            "inclusivePriceWithoutExtraBed": {"display": price * 1.05}}


def test_the_room_id_is_read_into_the_offer():
    offer = offer_from_mapping(_node(275801276), _agoda()["fields"])
    assert offer.site_room_id == "275801276"


def test_two_rooms_sharing_a_name_are_not_deduped_into_one():
    fields = _agoda()["fields"]
    offers = [offer_from_mapping(_node(275801276, price=6300.0), fields),
              offer_from_mapping(_node(262752532, price=7200.0), fields)]
    kept = dedupe_offers(offers, "cheapest")
    assert {o.site_room_id for o in kept} == {"275801276", "262752532"}


def test_the_same_room_from_two_suppliers_is_still_one():
    fields = _agoda()["fields"]
    offers = [offer_from_mapping(_node(275801276, price=6300.0), fields),
              offer_from_mapping(_node(275801276, price=6900.0), fields)]
    kept = dedupe_offers(offers, "cheapest")
    assert len(kept) == 1 and kept[0].price_exclusive == 6300
