"""Agoda keeps a rate it prices only for more guests than were searched

ASG Holiday Resorts sells its Deluxe Family Room breakfast rate on Agoda as a
3-adult row and nothing smaller. ``rooms_filter`` kept only ``isFit`` rows, so
for a 2-adult search every breakfast rate for the room was dropped, and a rule
comparing on breakfast had nothing for ASG while the listing showed one.

The Agoda profile's filter now falls back, per room and board, to the
smallest row that still holds the searched adults. Listings copy their
adapter_config from the profile when attached, so the attached Agoda
listings get the same fallback here. A listing whose filter is not the stock
isFit one is left alone.
"""

from alembic import op

revision = "0035_agoda_rates_for_more_guests"
down_revision = "0034_agoda_reads_breakfast"
branch_labels = None
depends_on = None

_FALLBACK = '{"group_by": ["name", "isBreakfastIncluded"], "occupancy": "adults"}'


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE hotel_sources hs
           SET adapter_config = jsonb_set(
                   hs.adapter_config, '{{rooms_filter,fallback}}', '{_FALLBACK}'::jsonb
               )
          FROM sources s
         WHERE s.id = hs.source_id
           AND s.code = 'agoda'
           AND hs.adapter_config -> 'rooms_filter' ->> 'field' = 'isFit'
           AND NOT (hs.adapter_config -> 'rooms_filter') ? 'fallback'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE hotel_sources hs
           SET adapter_config = hs.adapter_config #- '{rooms_filter,fallback}'
          FROM sources s
         WHERE s.id = hs.source_id
           AND s.code = 'agoda'
        """
    )
