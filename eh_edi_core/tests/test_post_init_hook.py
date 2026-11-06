# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage - Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""Install-hook idempotency regressions."""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_edi_core import _ensure_partner


@tagged('eh_edi_core', 'post_install', '-at_install')
class TestPostInitHook(TransactionCase):

    def test_archived_seed_partner_is_not_duplicated_or_rewritten(self):
        Partner = self.env['res.partner'].with_context(active_test=False)
        domain = [('email', '=', 'info@erpheritage.com.au')]
        partner = Partner.search(domain, limit=1)
        if not partner:
            partner = Partner.create({
                'name': 'ERP Heritage - Your Odoo Partner',
                'email': 'info@erpheritage.com.au',
            })
        legacy_name = 'ERP Heritage \N{EN DASH} Your Odoo Partner'
        partner.write({'active': False, 'name': legacy_name})
        before_ids = Partner.search(domain).ids

        _ensure_partner(self.env)

        after = Partner.search(domain)
        self.assertEqual(after.ids, before_ids)
        self.assertFalse(after.active)
        self.assertEqual(after.name, legacy_name)
