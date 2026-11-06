# -*- encoding: utf-8 -*-
"""Quarantine every pre-control approved budget basis.

Before 1.3.5, ordinary writers could add, delete, reparent, or rewrite lines
after approval. No durable provenance stamp distinguishes a legitimate basis
from a client-forged one, so structural matching cannot safely bless history.
Preserve all rows and fail closed until an independent reviewed replacement is
created.
"""

import re


_BASIS_REASON = (
    'Legacy approved header and line basis predates immutable budget controls; '
    'rows were preserved as untrusted audit evidence and excluded from '
    'commitment and revision workflows.'
)

_CONTROL_VERSION = (1, 3, 5)


def _normalise_version(version):
    """Return the comparable numeric module version used by Odoo upgrades."""
    parts = tuple(int(part) for part in re.findall(r'\d+', str(version)))
    # Compare the module-local suffix. Backported migrations receive target
    # series versions (16/17/18.0.1.3.5), while standalone regressions may
    # pass only 1.3.5. A hard-coded major would quarantine a controlled O16-
    # O18 basis during upgrade.
    return parts[-3:]


def _table_exists(cr, table_name):
    cr.execute("SELECT to_regclass(%s)", (table_name,))
    return bool(cr.fetchone()[0])


def migrate(cr, version):
    if not version:
        return
    if not _table_exists(cr, 'eh_budget_budget'):
        return
    cr.execute(
        'ALTER TABLE eh_budget_budget ADD COLUMN IF NOT EXISTS '
        'eh_basis_integrity_review_required boolean DEFAULT FALSE'
    )
    cr.execute(
        'ALTER TABLE eh_budget_budget ADD COLUMN IF NOT EXISTS '
        'eh_basis_integrity_review_reason text'
    )
    if _normalise_version(version) >= _CONTROL_VERSION:
        return
    cr.execute("""
        UPDATE eh_budget_budget
           SET eh_basis_integrity_review_required = TRUE,
               eh_basis_integrity_review_reason = COALESCE(
                   NULLIF(eh_basis_integrity_review_reason, ''), %s
               )
         WHERE state != 'draft'
    """, (_BASIS_REASON,))
