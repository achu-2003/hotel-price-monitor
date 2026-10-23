"""Agoda listings read whether a rate includes breakfast

Agoda names no board, but every rate carries ``isBreakfastIncluded``. Without
it a room's breakfast and room-only rates were one offer, ``rooms_dedupe:
cheapest`` kept the room-only one, and a rule comparing on breakfast had no
Agoda price to compare.

The Agoda engine profile now maps it (``fields.breakfast_included``), but a
listing's adapter_config is copied from the profile when it is attached, so
the listings already attached are given the same field here -- every Agoda
listing, not one hotel's. A listing that already maps a board is left alone.

Agoda offers stored before this have no board; from the next check each room
arrives as its "Breakfast" and "Room Only" rates and the board-less series
go missing in the ordinary way.
"""

from alembic import op

revision = "0034_agoda_reads_breakfast"
down_revision = "0033_repricing_channel_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE hotel_sources hs
           SET adapter_config = jsonb_set(
                   hs.adapter_config, '{fields,breakfast_included}', '"isBreakfastIncluded"'::jsonb
               )
          FROM sources s
         WHERE s.id = hs.source_id
           AND s.code = 'agoda'
           AND hs.adapter_config ? 'fields'
           AND NOT (hs.adapter_config -> 'fields') ? 'breakfast_included'
           AND NOT (hs.adapter_config -> 'fields') ? 'meal_plan'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE hotel_sources hs
           SET adapter_config = hs.adapter_config #- '{fields,breakfast_included}'
          FROM sources s
         WHERE s.id = hs.source_id
           AND s.code = 'agoda'
        """
    )
