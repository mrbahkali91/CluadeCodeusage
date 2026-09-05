"""One current opportunity score, enforced by the database.

`opportunity_scores` is append-only: a new score is inserted and the previous
one is marked `superseded_at`. Every reader in the platform treats
`superseded_at IS NULL` as "the current score" and joins on it.

Superseding was the *caller's* responsibility, and one caller -- the
"identical content is a no-op" re-evaluation path in `ingest.py` -- did not do
it. Running the seed twice therefore left two live rows for the same
opportunity, and because readers join on a predicate that was no longer
unique, each duplicate fanned a row out of every query: 56 opportunities
rendered as 95 map markers, `LIMIT` truncated an arbitrary subset, and the
list view emitted duplicate React keys. No error anywhere -- just quietly
doubled evidence.

The writer is fixed, but a convention that must be remembered will be
forgotten again. This makes the invariant the database's: a partial unique
index means a second live row cannot be inserted at all.

Backfill keeps the most recently computed row live and supersedes the rest.
It does not delete anything -- the history is the audit trail, and the older
rows remain readable with their original values, now correctly marked as no
longer current.

Revision ID: ea3f7d5c91b2
Revises: 20e079503186
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "ea3f7d5c91b2"
down_revision = "20e079503186"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Supersede every live row except the newest per opportunity. `computed_at`
    # can tie, so `id` breaks it: without a total order the "winner" would vary
    # between runs and the index below would fail on some of them.
    op.execute(
        sa.text("""
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY opportunity_id
                       ORDER BY computed_at DESC, id DESC
                   ) AS rn
            FROM opportunity_scores
            WHERE superseded_at IS NULL
        )
        UPDATE opportunity_scores s
        SET superseded_at = now()
        FROM ranked r
        WHERE s.id = r.id AND r.rn > 1
        """)
    )
    op.create_index(
        "uq_opportunity_scores_one_current",
        "opportunity_scores",
        ["opportunity_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    # Only the constraint is reversible. The backfill is not: which rows were
    # live before it ran is not recoverable from the data it wrote, and
    # inventing a reversal would be worse than declining one.
    op.drop_index("uq_opportunity_scores_one_current", table_name="opportunity_scores")
