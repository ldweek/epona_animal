# -*- encoding: utf-8 -*-
"""Replace legacy company-closure rules despite their noupdate flag."""

from odoo import SUPERUSER_ID, api


_COMPANY_SCOPE_DOMAIN = "[('eh_company_scope_allowed', '=', True)]"
_RULE_XMLIDS = (
    'eh_account_consolidation.eh_consol_entity_company_rule',
    'eh_account_consolidation.eh_consol_member_company_rule',
    'eh_account_consolidation.eh_consol_run_company_rule',
    'eh_account_consolidation.eh_consol_run_line_company_rule',
    'eh_account_consolidation.eh_consol_elimination_company_rule',
    'eh_account_consolidation.eh_consol_elimination_line_company_rule',
    'eh_account_consolidation.eh_consol_unrealised_profit_company_rule',
)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    rules = env['ir.rule']
    missing = []
    for xmlid in _RULE_XMLIDS:
        rule = env.ref(xmlid, raise_if_not_found=False)
        if not rule or rule._name != 'ir.rule':
            missing.append(xmlid)
        else:
            rules |= rule
    if missing:
        raise RuntimeError(
            "Missing consolidation company-closure rule(s): %s"
            % ', '.join(missing)
        )
    if len(rules) != len(_RULE_XMLIDS):
        raise RuntimeError(
            "Consolidation company-closure rule XML IDs are not unique."
        )
    stale_rules = rules.filtered(
        lambda rule: rule.domain_force != _COMPANY_SCOPE_DOMAIN,
    )
    if stale_rules:
        stale_rules.write({'domain_force': _COMPANY_SCOPE_DOMAIN})
