"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Chain rules (design/DATA-MODEL.md §21.1):
  * revision id = pre-allocated ``NNNN_slug`` with a 4-digit prefix; never reuse a number;
  * exactly one linear chain (single down_revision, single head); no branches/merges;
  * additive expand/contract only; down-migrations are tested but never run in prod;
  * validate with ``python -m app.migrations.chain_guard`` before opening a PR.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """Apply the additive schema change (expand; never drop/rename in prod)."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Reverse the change for local/dev and migration tests only (no down-migrations in prod)."""
    ${downgrades if downgrades else "pass"}
