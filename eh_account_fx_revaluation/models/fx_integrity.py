# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Shared authorization and company checks for FX accounting engines."""

from odoo import _
from odoo.exceptions import AccessError, MissingError, ValidationError


FX_MOVE_LINK_CAPABILITY = object()


# Account classes used by the FX posting engines.  View domains are only a
# convenience: callers can bypass them through call_kw, imports, or direct ORM
# use, so every model rechecks the accounting class before it can reach a
# posting action.
FX_PNL_ACCOUNT_TYPES = frozenset({
    'income', 'income_other',
    'expense', 'expense_other', 'expense_depreciation',
    'expense_direct_cost',
})
FX_BALANCE_SHEET_ACCOUNT_TYPES = frozenset({
    'asset_receivable', 'asset_cash', 'asset_current', 'asset_non_current',
    'asset_prepayments', 'asset_fixed',
    'liability_payable', 'liability_credit_card', 'liability_current',
    'liability_non_current',
})
FX_EQUITY_ACCOUNT_TYPES = frozenset({'equity', 'equity_unaffected'})

# The narrow income/expense classes appropriate for unrealised FX and CTA
# disposal results. Ordinary operating income/expense accounts are valid
# choices in many local charts; forcing every chart to use the optional
# ``*_other`` classes is a UI convention, not an accounting invariant.
FX_GAIN_ACCOUNT_TYPES = frozenset({'income', 'income_other'})
FX_LOSS_ACCOUNT_TYPES = frozenset({'expense', 'expense_other'})


def lock_report_input_versions(env, company_ids):
    """Serialize an FX transition with its exact report-input epochs.

    ``eh_move_version`` now lives in one global counter row plus one tiny row
    per company. Locking ``res_company`` no longer serializes with ledger or
    rate writers and needlessly blocks unrelated company maintenance. Lock
    the actual version rows instead, in one deterministic order, so the
    compute/post freshness check cannot race a source-ledger or rate change.
    """
    ids = tuple(sorted({int(company_id) for company_id in company_ids
                        if company_id}))
    if not ids:
        return
    env.cr.execute(
        "SELECT to_regclass('eh_account_report_company_version'), "
        "to_regclass('eh_account_report_global_version')"
    )
    company_table, global_table = env.cr.fetchone()
    if not company_table or not global_table:
        # Only reachable while dependency schemas are being initialised.
        return
    env.cr.execute(
        "INSERT INTO eh_account_report_global_version (id, version) "
        "VALUES (1, 0) ON CONFLICT (id) DO NOTHING"
    )
    env.cr.execute(
        "INSERT INTO eh_account_report_company_version "
        "(company_id, version) "
        "SELECT id, 0 FROM res_company WHERE id IN %s "
        "ON CONFLICT (company_id) DO NOTHING",
        (ids,),
    )
    # Global first, then local rows in ascending company order. Every FX
    # engine uses this helper, so cross-engine transitions retain one order.
    env.cr.execute(
        "SELECT id FROM eh_account_report_global_version "
        "WHERE id = 1 FOR UPDATE"
    )
    env.cr.execute(
        "SELECT company_id FROM eh_account_report_company_version "
        "WHERE company_id IN %s ORDER BY company_id FOR UPDATE",
        (ids,),
    )


def assert_company_allowed(env, company_id, label):
    if (
        not env.su
        and company_id
        and int(company_id) not in env.companies.ids
    ):
        raise AccessError(_(
            "%(label)s may only use a company enabled in the caller's "
            "current company context.",
            label=label,
        ))


def record_company_domain(model, company):
    """Build the runtime's native company-consistency search domain.

    Odoo 17+ exposes ``_check_company_domain`` and Odoo 19 expands it for
    branch-owned shared master data. Odoo 16 has no helper and uses the
    classic single ``company_id`` field, so the generated backport needs the
    equivalent exact-company/global fallback.
    """
    domain_builder = getattr(model, '_check_company_domain', None)
    if domain_builder:
        return domain_builder(company)
    if 'company_ids' in model._fields:
        return [('company_ids', 'in', company.ids)]
    if 'company_id' in model._fields:
        return [('company_id', 'in', [company.id, False])]
    return []


def resolve_reference(env, model_name, record_id, label, access='read'):
    record = env[model_name].browse(record_id) if record_id else False
    if not record:
        return record
    try:
        if not env.su:
            record._eh_check_access(access)
        record = record.exists()
    except (AccessError, MissingError):
        raise AccessError(_(
            "%(label)s reference is unavailable to the caller.",
            label=label,
        )) from None
    if not record:
        raise AccessError(_(
            "%(label)s reference is unavailable to the caller.",
            label=label,
        ))
    return record


def record_matches_company(record, company):
    if not record or not company:
        return True
    domain_builder = getattr(record, '_check_company_domain', None)
    if domain_builder:
        try:
            return bool(record.filtered_domain(domain_builder(company)))
        except (AttributeError, TypeError, ValueError):
            pass
    if 'company_ids' in record._fields:
        candidate = company
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


def require_record_company(record, company, label, exact=False):
    valid = (
        record.company_id == company
        if record and exact and 'company_id' in record._fields
        else record_matches_company(record, company)
    )
    if record and not valid:
        raise ValidationError(_(
            "%(label)s must belong to or be available to %(company)s.",
            label=label,
            company=company.display_name,
        ))
    return record


def require_account_type(account, allowed_types, label):
    """Fail closed when an RPC-supplied posting account has the wrong class."""
    if account and account.account_type not in allowed_types:
        raise ValidationError(_(
            "%(label)s has incompatible account type %(account_type)s.",
            label=label,
            account_type=account.account_type,
        ))
    return account
