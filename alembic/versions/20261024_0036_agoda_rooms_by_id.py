"""Agoda rooms are matched by Agoda's room id, not only by name

On 24 Oct 2026 Agoda printed ASG's CLASSIC room (id 275801276, "Deluxe Family
Room" the day before) as "Deluxe Room" -- the name of a different room it also
lists (262752532). Matched by name, CLASSIC's rate was filed under that other
room, and Deluxe Double Room fell out of the Agoda repricing table. The id is
what stayed put, so the Agoda profile now reads it (``fields.room_id``) and
ingest remembers each id under the room it resolved to.

Two things here, for listings attached before this:

1. ``fields.room_id = "id"`` on every Agoda listing, and ``id`` in the
   breakfast fallback's grouping, so two rooms sharing a name stay two rooms.

2. THE ID LINKS, SEEDED FROM HISTORY. Without this the first fetch after the
   deploy would learn tonight's id from tonight's name -- the wrong room --
   and keep it. Each id seen in stored Agoda observations is linked to:

   * the room a person mapped one of its names to by hand, if any did; else
   * the one room it was ever filed under, if there is only one.

   An id filed under several rooms with no hand-made mapping is left alone;
   ingest learns it by name, exactly as before.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0036_agoda_rooms_by_id"
down_revision = "0035_agoda_rates_for_more_guests"
branch_labels = None
depends_on = None

_OLD_GROUP = '["name", "isBreakfastIncluded"]'
_NEW_GROUP = '["name", "id", "isBreakfastIncluded"]'


def upgrade() -> None:
    op.execute(
        """
        UPDATE hotel_sources hs
           SET adapter_config = jsonb_set(hs.adapter_config, '{fields,room_id}', '"id"'::jsonb)
          FROM sources s
         WHERE s.id = hs.source_id
           AND s.code = 'agoda'
           AND hs.adapter_config ? 'fields'
           AND NOT (hs.adapter_config -> 'fields') ? 'room_id'
        """
    )
    op.execute(
        f"""
        UPDATE hotel_sources hs
           SET adapter_config = jsonb_set(
                   hs.adapter_config, '{{rooms_filter,fallback,group_by}}', '{_NEW_GROUP}'::jsonb)
          FROM sources s
         WHERE s.id = hs.source_id
           AND s.code = 'agoda'
           AND hs.adapter_config -> 'rooms_filter' -> 'fallback' -> 'group_by' = '{_OLD_GROUP}'::jsonb
        """
    )

    conn = op.get_bind()
    seen = conn.execute(sa.text(
        """
        SELECT ps.hotel_id, ps.source_id, o.raw_payload ->> 'id' AS room_id,
               ps.room_type_id, lower(btrim(o.raw_room_name)) AS name,
               max(o.raw_room_name) AS raw_name
          FROM price_observations o
          JOIN price_series ps ON ps.offer_key = o.offer_key
          JOIN sources s ON s.id = ps.source_id
         WHERE s.code = 'agoda'
           AND coalesce(o.raw_payload ->> 'id', '') <> ''
         GROUP BY 1, 2, 3, 4, 5
        """
    )).all()
    by_hand = {
        (row.hotel_id, row.source_id, row.name): row.room_type_id
        for row in conn.execute(sa.text(
            """
            SELECT hotel_id, source_id, lower(btrim(raw_name)) AS name, room_type_id
              FROM room_type_aliases
             WHERE match_method = 'manual'
            """
        ))
    }

    found: dict[tuple, dict] = {}
    for row in seen:
        entry = found.setdefault((row.hotel_id, row.source_id, row.room_id),
                                 {"rooms": set(), "hand": set(), "name": row.raw_name})
        entry["rooms"].add(row.room_type_id)
        hand = by_hand.get((row.hotel_id, row.source_id, row.name))
        if hand is not None:
            entry["hand"].add(hand)

    for (hotel_id, source_id, room_id), entry in found.items():
        if len(entry["hand"]) == 1:
            (room_type_id,) = entry["hand"]
        elif not entry["hand"] and len(entry["rooms"]) == 1:
            (room_type_id,) = entry["rooms"]
        else:
            continue
        conn.execute(sa.text(
            """
            INSERT INTO room_type_aliases
                   (room_type_id, hotel_id, source_id, raw_name, normalized_name,
                    match_method, confidence, created_at, updated_at)
            VALUES (:room, :hotel, :source, :raw, :key, 'exact', 1, now(), now())
            ON CONFLICT (source_id, hotel_id, normalized_name)
            DO UPDATE SET room_type_id = EXCLUDED.room_type_id
            """
        ), {"room": room_type_id, "hotel": hotel_id, "source": source_id,
            "raw": f"{entry['name']} (site room {room_id})"[:300],
            "key": f"#{room_id}"[:300]})


def downgrade() -> None:
    op.execute("DELETE FROM room_type_aliases WHERE normalized_name LIKE '#%'")
    op.execute(
        f"""
        UPDATE hotel_sources hs
           SET adapter_config = jsonb_set(
                   hs.adapter_config #- '{{fields,room_id}}',
                   '{{rooms_filter,fallback,group_by}}', '{_OLD_GROUP}'::jsonb)
          FROM sources s
         WHERE s.id = hs.source_id
           AND s.code = 'agoda'
           AND hs.adapter_config -> 'rooms_filter' -> 'fallback' ? 'group_by'
        """
    )
    op.execute(
        """
        UPDATE hotel_sources hs
           SET adapter_config = hs.adapter_config #- '{fields,room_id}'
          FROM sources s
         WHERE s.id = hs.source_id AND s.code = 'agoda'
        """
    )
