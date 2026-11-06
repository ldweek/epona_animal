"""Country-aware onboarding finalizer for aggregate suite installs."""

import logging

from odoo import fields


_logger = logging.getLogger(__name__)

_AUTO_SKIP_NOTE = (
    "Automatically skipped by ERP Heritage Accounting: company fiscal "
    "country is explicitly outside Australia. The task returns to To do if "
    "the effective fiscal country becomes Australia or unknown."
)


def _fiscal_country(company):
    """Return company fiscal country without leaking active-company context."""
    scoped = company.with_company(company)
    fiscal_field = scoped._fields.get('account_fiscal_country_id')
    return (
        scoped.account_fiscal_country_id
        if fiscal_field and scoped.account_fiscal_country_id
        else scoped.country_id
    )


def finalize_bas_onboarding(env, companies=None):
    """Apply AU BAS task applicability without overwriting operator work.

    An unset country is unknown, not non-Australian, so its task stays open.
    Only rows carrying this helper's exact note may be reopened after a fiscal
    country change. User-completed or user-skipped evidence stays untouched.
    """
    task = env.ref(
        'eh_account_suite.task_au_bas_onboarding',
        raise_if_not_found=False,
    )
    if not task:
        return env['eh.account.setup.task.line']
    companies = (
        companies.sudo()
        if companies is not None
        else env['res.company'].sudo().with_context(active_test=False).search([])
    )
    if not companies:
        return env['eh.account.setup.task.line']

    task._eh_seed_company_lines(companies)
    australia = env.ref('base.au')
    lines = env['eh.account.setup.task.line'].sudo().search([
        ('task_id', '=', task.id),
        ('company_id', 'in', companies.ids),
    ])

    auto_skipped = lines.filtered(
        lambda line: line.state == 'skipped'
        and not line.completed_by_id
        and line.notes == _AUTO_SKIP_NOTE
    )
    reopened = auto_skipped.filtered(
        lambda line: not _fiscal_country(line.company_id)
        or _fiscal_country(line.company_id) == australia
    )
    if reopened:
        reopened.write({
            'state': 'todo',
            'completed_at': False,
            'completed_by_id': False,
            'notes': False,
        })

    newly_skipped = lines.filtered(
        lambda line: line.state == 'todo'
        and not line.notes
        and not line.completed_at
        and not line.completed_by_id
        and _fiscal_country(line.company_id)
        and _fiscal_country(line.company_id) != australia
    )
    if newly_skipped:
        newly_skipped.write({
            'state': 'skipped',
            'completed_at': fields.Datetime.now(),
            'completed_by_id': False,
            'notes': _AUTO_SKIP_NOTE,
        })
    _logger.info(
        "eh_account_suite onboarding: %d AU/unknown task(s) pending, %d "
        "non-AU task(s) pre-skipped, %d task(s) reopened",
        len(lines.filtered(lambda line: line.state == 'todo')),
        len(newly_skipped),
        len(reopened),
    )
    return lines


def post_init_hook(cr_or_env, registry=None):
    """Seed suite tasks and pre-skip AU-only setup outside Australia.

    Odoo 16 calls post-init hooks with ``(cr, registry)``; later series pass
    an environment. Supporting both keeps generated ports behaviorally exact.
    Existing operator progress is never reset.
    """
    if registry is not None:
        from odoo import SUPERUSER_ID, api
        env = api.Environment(cr_or_env, SUPERUSER_ID, {})
    else:
        env = cr_or_env

    finalize_bas_onboarding(env)
