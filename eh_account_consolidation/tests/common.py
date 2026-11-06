# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Cross-version accounting fixtures shared by consolidation tests."""


def account_company_field(env):
    """Return the account/company relation used by the running Odoo."""
    Account = env['account.account']
    return (
        'company_ids'
        if 'company_ids' in Account._fields
        else 'company_id'
    )


def make_account(
    env, company, code, name, account_type, *, reconcile=False,
):
    """Create or return a chart account for ``company``."""
    Account = env['account.account'].with_company(company)
    company_field = account_company_field(env)
    company_value = (
        [(6, 0, company.ids)]
        if company_field == 'company_ids'
        else company.id
    )
    existing = Account.search([
        ('code', '=', code),
        (company_field, 'in', company.ids),
    ], limit=1)
    if existing:
        if reconcile and not existing.reconcile:
            existing.reconcile = True
        return existing
    values = {
        'code': code,
        'name': name,
        'account_type': account_type,
        company_field: company_value,
    }
    if reconcile:
        values['reconcile'] = True
    return Account.create(values)


def make_unaffected_account(env, company, code, name):
    """Reuse the chart's unique current-year-earnings account if seeded."""
    Account = env['account.account'].with_company(company)
    company_field = account_company_field(env)
    existing = Account.search([
        ('account_type', '=', 'equity_unaffected'),
        (company_field, 'in', company.ids),
    ], limit=1)
    return existing or make_account(
        env, company, code, name, 'equity_unaffected',
    )


def make_general_journal(env, company):
    """Create or return a general journal for ``company``."""
    Journal = env['account.journal'].with_company(company)
    journal = Journal.search([
        ('company_id', '=', company.id),
        ('type', '=', 'general'),
    ], limit=1)
    if journal:
        return journal
    return Journal.create({
        'name': 'Misc %s' % company.name,
        'code': 'MI%s' % company.id,
        'type': 'general',
        'company_id': company.id,
    })


def post_move(env, company, journal, date, lines):
    """Post a balanced entry; each line is account/debit/credit/partner."""
    move = env['account.move'].with_company(company).create({
        'company_id': company.id,
        'move_type': 'entry',
        'date': date,
        'journal_id': journal.id,
        'line_ids': [(0, 0, {
            'account_id': account.id,
            'debit': debit,
            'credit': credit,
            'partner_id': partner.id if partner else False,
        }) for account, debit, credit, partner in lines],
    })
    move.action_post()
    return move


def add_acquisition_component(
    member, source_account, amount, *, target_account=None, name=None,
):
    """Attach a structured acquisition-date equity component to a member."""
    return member.env['eh.consol.acquisition.equity.component'].create({
        'member_id': member.id,
        'source_account_id': source_account.id,
        'target_account_id': target_account.id if target_account else False,
        'amount': amount,
        'notes': name or source_account.display_name,
    })
