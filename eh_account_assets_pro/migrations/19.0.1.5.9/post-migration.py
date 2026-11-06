# -*- coding: utf-8 -*-
"""Retire unsupported UOP inputs and seed lease measurement-term audit data."""

import logging


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # Units of production never generated a primary-book schedule and its
    # activation path raised before posting. Map the obsolete selection to
    # Manual so no accounting policy is invented and the record stays visibly
    # reviewable; users can select a supported method and recompute in draft.
    cr.execute(
        """
        UPDATE eh_asset
           SET method = 'manual',
               write_date = NOW()
         WHERE method = 'units_of_production'
        """,
    )
    migrated_assets = cr.rowcount
    cr.execute(
        """
        UPDATE eh_asset_category
           SET method = 'manual',
               write_date = NOW()
         WHERE method = 'units_of_production'
        """,
    )
    migrated_categories = cr.rowcount

    # Before 1.5.9 the modification workflow overwrote term_months with the
    # latest remaining/revised term. That original total cannot be reconstructed
    # reliably for already-modified legacy rows, so preserve the historical
    # byte and copy it into the new explicit current-measurement audit field.
    # Active unmodified contracts add their reasonably-certain extensions,
    # matching the engine's effective term at activation.
    cr.execute(
        """
        UPDATE eh_lease_contract AS lease
           SET current_measurement_term_months =
               lease.term_months + COALESCE((
                   SELECT SUM(option.extension_months)
                     FROM eh_lease_option AS option
                    WHERE option.lease_id = lease.id
                      AND option.reasonably_certain IS TRUE
                      AND option.option_type = 'extension'
               ), 0),
               write_date = NOW()
         WHERE lease.state <> 'draft'
           AND COALESCE(lease.current_measurement_term_months, 0) = 0
        """,
    )
    seeded_leases = cr.rowcount

    _logger.warning(
        "Assets Pro 19.0.1.5.9 mapped %d legacy UOP assets and %d UOP "
        "categories to Manual, and seeded %d current lease measurement terms. "
        "No journal entry or historical amount was rewritten.",
        migrated_assets, migrated_categories, seeded_leases,
    )
