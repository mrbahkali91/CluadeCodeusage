"""record the valuation-v2 backfill decision

Revision ID: 20e079503186
Revises: 16d761315006
Create Date: 2026-09-04 19:56:52.120487

This migration changes no schema and no data. It exists because
docs/architecture/valuation-and-scoring.md section 5.5 rule 3 requires it:

    "Changing a method version requires a new version identifier and a backfill
     decision recorded in the migration. Silent recalculation of historical
     scores is prohibited."

`valuation-v1` became `valuation-v2`. Two things changed:

  * the interval is the weighted (Q05, Q95) pair with no `1 + 0.6/sqrt(n_eff)`
    inflation, taking the median band from 58.8% of value to 27.9% while
    coverage moved from 98.7% to 71.7% -- both spec targets now met, where v1
    met coverage only by making the band too wide to act on;
  * valuation confidence weights comparable agreement at 0.40 and similarity to
    the subject at 0.10, inverting v1's 0.15/0.25. On the back-test corpus this
    turned Spearman(confidence, |error|) from +0.151 -- the wrong sign, higher
    confidence going with larger error -- to -0.193, and point-error AUC from
    0.476 to 0.603.

THE DECISION: existing valuation and score rows are NOT recalculated.

Rationale. Score rows are append-only by design (section 5.5 rule 4: "A score
row is never updated"), because the *sequence* of scores over time is itself
the opportunity signal -- rewriting history would destroy the only record of
what the platform actually told a user on a given day. A v1 row is not wrong;
it is a correct record of a v1 judgement. Every row already carries its
`method_version`, so a consumer can tell the two apart, and a mixed-version
table is the honest state of a system whose method improved.

Consequence to be aware of: any comparison across rows spanning the change
must group by `method_version`. The back-test harness records
`valuation_method_version` per run for exactly this reason.

Re-evaluating a property inserts a fresh v2 row and supersedes the prior one
through the normal path; nothing here forces that, and no bulk re-scoring is
performed.
"""

from __future__ import annotations

revision = "20e079503186"
down_revision = "16d761315006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Deliberately empty. See the module docstring: this revision is the
    record of a decision not to touch data, which the specification requires be
    recorded here rather than only in prose."""


def downgrade() -> None:
    """Deliberately empty, and symmetric: since `upgrade` recalculated nothing,
    there is nothing to undo. Rolling back past this revision does not restore
    v1 scores, because v1 scores were never removed."""
