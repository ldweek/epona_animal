# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Cross-version company checks for accounting configuration records.

Odoo 16-18 expose ``account.account.company_id`` while Odoo 19 can share an
account across a company hierarchy through ``company_ids``.  Assets Pro is
maintained from one common implementation, so posting models use this helper
instead of baking either schema into their constraints.
"""

from odoo import _, fields
from odoo.exceptions import UserError, ValidationError


def _eh_record_matches_company(record, company):
    """Return whether ``record`` is valid accounting setup for ``company``."""
    if not record or not company:
        return True
    record = record.sudo()
    domain_builder = getattr(record, '_check_company_domain', None)
    if domain_builder:
        try:
            return bool(record.filtered_domain(domain_builder(company)))
        except (AttributeError, TypeError, ValueError):
            # Fall through to stable field checks for older Odoo releases.
            pass
    if 'company_ids' in record._fields:
        candidate = company.sudo()
        while candidate:
            if candidate in record.company_ids:
                return True
            candidate = (
                candidate.parent_id
                if 'parent_id' in candidate._fields
                else candidate.browse()
            )
        return False
    if 'company_id' in record._fields:
        return record.company_id == company
    return True


def _eh_validate_accounting_company(owner, field_names):
    """Raise when any configured account/journal belongs to another company."""
    for record in owner:
        company = record.company_id
        invalid = []
        for field_name in field_names:
            value = record[field_name]
            if value and not _eh_record_matches_company(value, company):
                invalid.append(record._fields[field_name].string or field_name)
        if invalid:
            raise ValidationError(_(
                "%(record)s uses accounting configuration from another "
                "company: %(fields)s. Select accounts and journals available "
                "to %(company)s before saving or posting.",
                record=record.display_name,
                fields=', '.join(invalid),
                company=company.display_name,
            ))
    return True


def _eh_require_exact_posting_date(company, posting_date, journal, source):
    """Refuse Odoo's silent move-date shift for dated subledger evidence.

    Odoo moves a journal entry to the first open date when ``action_post``
    encounters a lock.  That is useful for ordinary data entry, but it breaks
    the evidential link for a persisted schedule row: the row continues to say
    one period while its sealed move lands in another.  Assets Pro therefore
    fails closed.  A user-specific lock exception remains the explicit,
    audited way to post the row on its contractual date.

    The company API and tuple format are shared by Odoo 18 and 19.  The small
    fallback keeps the common code importable on older supported branches.
    """
    if not company or not posting_date:
        return True
    company.ensure_one()
    posting_date = fields.Date.to_date(posting_date)
    checker = getattr(company, '_get_violated_lock_dates', None)
    if checker:
        try:
            # Odoo 18-19: journal-aware fiscal/sale/purchase locks.
            lock_dates = checker(posting_date, False, journal)
        except TypeError as journal_signature_error:
            try:
                # Odoo 16-17: the company method takes only date + tax flag.
                lock_dates = checker(posting_date, False)
            except TypeError:
                raise journal_signature_error
    else:
        fiscal_checker = company._get_user_fiscal_lock_date
        try:
            fiscal_lock = fiscal_checker(journal)
        except TypeError as journal_signature_error:
            try:
                # Odoo 16-17 expose the same fallback without a journal arg.
                fiscal_lock = fiscal_checker()
            except TypeError:
                raise journal_signature_error
        lock_dates = (
            [(fiscal_lock, 'fiscalyear_lock_date')]
            if fiscal_lock and posting_date <= fiscal_lock
            else []
        )
    if not lock_dates:
        return True
    formatter = getattr(company, '_format_lock_dates', None)
    lock_description = (
        formatter(lock_dates)
        if formatter
        else ', '.join(str(lock_date) for lock_date, _field in lock_dates)
    )
    raise UserError(_(
        "%(source)s is dated %(date)s, but that exact accounting date is "
        "sealed by %(locks)s. Odoo would silently move the journal entry "
        "into a later open period while leaving the source row on its "
        "original date. Assets Pro refuses that loss of provenance. Obtain "
        "an authorised lock-date exception for this user, or record an "
        "explicit current-period correction instead.",
        source=source,
        date=posting_date,
        locks=lock_description,
    ))
