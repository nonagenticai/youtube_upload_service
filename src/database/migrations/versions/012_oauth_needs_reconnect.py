"""Mark a tenant's dead YouTube grant as reconnect-required instead of deleting it.

When Google refused to refresh a tenant's own grant, ``OAuthService`` DELETED the
``oauth_tokens`` row and published the video to the PLATFORM channel, recording
the switch only in a log line. The tenant's video went to someone else's channel
and the tenant could not tell — ``/api/auth/status`` then reported the platform
channel as the tenant's "connected" account.

The row is now kept and flagged:

    needs_reconnect  boolean NOT NULL DEFAULT false
    reconnect_reason text NULL       -- Google's refusal, for the operator

Uploads for a flagged tenant fail with "reconnect YouTube"; the next successful
consent (``store_tokens`` upsert) clears both columns.

Drift-tolerant: ``ADD COLUMN IF NOT EXISTS`` / ``DROP COLUMN IF EXISTS``, so a
database that already carries the columns (a ``create_all`` test DB, a re-run)
upgrades cleanly. Additive; existing rows read ``false`` — which is their true
state, since every surviving row is one whose refresh has not failed (failed
rows were deleted under the old behaviour).

Revision ID: 012_oauth_needs_reconnect
Revises: 011_add_applied_outcome_columns
Create Date: 2026-10-08
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "012_oauth_needs_reconnect"
down_revision = "011_add_applied_outcome_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE oauth_tokens "
        "ADD COLUMN IF NOT EXISTS needs_reconnect boolean NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE oauth_tokens ADD COLUMN IF NOT EXISTS reconnect_reason text")


def downgrade() -> None:
    op.execute("ALTER TABLE oauth_tokens DROP COLUMN IF EXISTS reconnect_reason")
    op.execute("ALTER TABLE oauth_tokens DROP COLUMN IF EXISTS needs_reconnect")
