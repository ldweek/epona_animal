# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Browser tour run for the consolidation entity register (IFRS 10/10 UI
layer).

Runs the eh_consolidation_test_tour in a real headless Chrome via HttpCase.
The eh_tour tag keeps browser cycles out of the default per-module runs;
the matrix runner selects them with --tours (test-tags eh_tour).
"""

from odoo import release
from odoo.tests import HttpCase, tagged

from .common import make_account, make_general_journal, post_move


@tagged('eh_tour', 'eh_account_consolidation', 'post_install', '-at_install')
class TestConsolidationTour(HttpCase):

    def test_consol_entity_create_tour(self):
        before = self.env['eh.consol.entity'].search([])
        self.start_tour('/odoo', 'eh_consolidation_test_tour', login='admin')
        entity = self.env['eh.consol.entity'].search([]) - before
        self.assertEqual(len(entity), 1,
                         'tour did not create the consolidation entity')
        self.assertEqual(entity.name, 'Tour Group Consolidated')
        self.assertEqual(entity.code, 'tour_group')
        self.assertTrue(entity.parent_company_id,
                        'default parent company was not applied')
        self.assertTrue(entity.presentation_currency_id,
                        'default presentation currency was not applied')

    def test_consol_membership_compute_review_and_post_tour(self):
        parent = self.env.company
        currency = parent.currency_id
        subsidiary = self.env['res.company'].create({
            'name': 'Tour Workflow Subsidiary',
            'currency_id': currency.id,
        })
        ledger = self.env['res.company'].create({
            'name': 'Tour Workflow Consolidation Ledger',
            'currency_id': currency.id,
        })
        admin = self.env.ref('base.user_admin')
        admin.sudo().write({
            'company_ids': [(4, subsidiary.id), (4, ledger.id)],
        })
        source_cash = make_account(
            self.env, subsidiary, 'TCA100', 'Tour Cash', 'asset_current')
        source_income = make_account(
            self.env, subsidiary, 'TIN400', 'Tour Revenue', 'income')
        source_journal = make_general_journal(self.env, subsidiary)
        make_account(
            self.env, ledger, 'TCA100', 'Tour Cash', 'asset_current')
        make_account(
            self.env, ledger, 'TIN400', 'Tour Revenue', 'income')
        ledger_journal = make_general_journal(self.env, ledger)
        post_move(
            self.env, subsidiary, source_journal, '2026-06-30', [
                (source_cash, 250.0, 0.0, False),
                (source_income, 0.0, 250.0, False),
            ],
        )
        entity = self.env['eh.consol.entity'].create({
            'name': 'Tour Workflow Group',
            'code': 'tour_workflow_group',
            'parent_company_id': parent.id,
            'presentation_currency_id': currency.id,
            'consolidation_company_id': ledger.id,
            'consolidation_journal_id': ledger_journal.id,
        })
        run = self.env['eh.consol.run'].create({
            'entity_id': entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        separator = '-' if release.version_info[0] >= 18 else ','
        company_ids = separator.join(
            map(str, (parent.id, subsidiary.id, ledger.id)),
        )
        self.start_tour(
            '/odoo',
            'eh_consolidation_workflow_test_tour',
            login='admin',
            cookies={'cids': company_ids},
        )
        member = entity.member_ids
        self.assertEqual(member.company_id, subsidiary)
        self.assertEqual(run.state, 'reviewed')
        self.assertEqual(run.move_state, 'posted')
        self.assertEqual(len(run.line_ids), 2)
        self.assertEqual(
            sorted(run.line_ids.mapped('amount')),
            [-250.0, 250.0],
        )
