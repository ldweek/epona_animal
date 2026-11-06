# -*- encoding: utf-8 -*-
"""Focused regressions for the revalidated Grants medium/low backlog."""

import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_grants', 'post_install', '-at_install')
class TestGrantMediumLowBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager'
        )
        cls.deferred = cls._ensure_account(
            cls.env, '2610', 'Backlog Deferred Grant', 'liability_current',
        )
        cls.income = cls._ensure_account(
            cls.env, '4660', 'Backlog Grant Income', 'income_other',
        )
        cls.asset = cls._ensure_account(
            cls.env, '1665', 'Backlog Granted Asset', 'asset_non_current',
        )
        cls.expense = cls._ensure_account(
            cls.env, '5660', 'Backlog Grant Repayment', 'expense',
        )
        cls.liability = cls._ensure_account(
            cls.env, '2660', 'Backlog Clawback', 'liability_current',
        )
        cls.grantor = cls.env['res.partner'].create({
            'name': 'State Grants Authority',
        })
        cls.plain_user = new_test_user(
            cls.env,
            login='grant_backlog_plain',
            groups='eh_account_base.group_eh_user',
        )
        cls.reviewer = new_test_user(
            cls.env,
            login='grant_backlog_reviewer',
            groups='eh_account_grants.group_eh_grant_reviewer',
        )
        cls.manager = new_test_user(
            cls.env,
            login='grant_backlog_manager',
            groups='eh_account_base.group_eh_manager',
        )
        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.1.6' / 'post-migration.py'
        )
        spec = spec_from_file_location(
            'eh_grants_migration_116_post_test', migration_path,
        )
        cls.post_migration_116 = module_from_spec(spec)
        spec.loader.exec_module(cls.post_migration_116)

    def _grant(self, **values):
        payload = {
            'grant_type': 'income_related',
            'amount': 1000.0,
            'cash_account_id': self.account_cash.id,
            'deferred_income_account_id': self.deferred.id,
            'grant_income_account_id': self.income.id,
            'asset_account_id': self.asset.id,
            'received_asset_account_id': self.asset.id,
            'repayment_expense_account_id': self.expense.id,
            'clawback_liability_account_id': self.liability.id,
            'journal_id': self.journal_misc.id,
            'grantor_id': self.grantor.id,
            'agreement_reference': 'SGA-AWARD-2026-0042',
        }
        payload.update(values)
        return self.env['eh.gov.grant'].create(payload)

    def _move_signature(self, move):
        return sorted(
            (
                line.account_id.id,
                self.company.currency_id.round(line.debit),
                self.company.currency_id.round(line.credit),
            )
            for line in move.line_ids
            if not self.company.currency_id.is_zero(
                line.debit - line.credit
            )
        )

    def test_partial_clawback_settlement_uses_amount_and_event_dates(self):
        grant = self._grant(
            grant_date='2026-01-15',
            clawback_amount=400.0,
            clawback_date='2026-05-10',
            repayment_date='2026-06-10',
            condition_ids=[(0, 0, {'name': 'Maintain employment'})],
        )
        grant.action_receive()
        grant.condition_ids.action_breach()
        accrual = grant.move_ids.sorted('id')[-1]
        self.assertEqual(accrual.date, fields.Date.to_date('2026-05-10'))
        self.assertEqual(grant.clawback_accrued, 400.0)

        grant.repayment_amount = 150.0
        grant.action_repay()
        settlement = grant.move_ids.sorted('id')[-1]
        self.assertEqual(settlement.date, fields.Date.to_date('2026-06-10'))
        self.assertEqual(self._move_signature(settlement), sorted([
            (self.liability.id, 150.0, 0.0),
            (self.account_cash.id, 0.0, 150.0),
        ]))
        self.assertEqual(grant.clawback_accrued, 250.0)
        self.assertEqual(grant.clawback_settled, 150.0)
        self.assertEqual(grant.state, 'repayable')

        grant.repayment_amount = 300.0
        with self.assertRaisesRegex(UserError, 'only 250.00'):
            grant.action_repay()
        grant.repayment_amount = 0.0
        grant.repayment_date = '2026-06-30'
        grant.action_repay()
        self.assertEqual(grant.clawback_accrued, 0.0)
        self.assertEqual(grant.clawback_settled, 400.0)
        self.assertEqual(grant.state, 'received')
        self.assertEqual(
            grant.move_ids.sorted('id')[-1].date,
            fields.Date.to_date('2026-06-30'),
        )

    def test_non_monetary_repayments_derecognise_received_asset(self):
        direct = self._grant(
            grant_kind='non_monetary', amount=0.0,
            asset_fair_value=1000.0, cash_account_id=False,
        )
        direct.action_receive()
        direct.repayment_amount = 300.0
        direct.action_repay()
        self.assertEqual(
            self._move_signature(direct.move_ids.sorted('id')[-1]),
            sorted([
                (self.deferred.id, 300.0, 0.0),
                (self.asset.id, 0.0, 300.0),
            ]),
        )

        accrued = self._grant(
            grant_kind='non_monetary', amount=0.0,
            asset_fair_value=1000.0, cash_account_id=False,
            clawback_amount=250.0,
            condition_ids=[(0, 0, {'name': 'Retain granted equipment'})],
        )
        accrued.action_receive()
        accrued.condition_ids.action_breach()
        accrued.action_repay()
        self.assertEqual(
            self._move_signature(accrued.move_ids.sorted('id')[-1]),
            sorted([
                (self.liability.id, 250.0, 0.0),
                (self.asset.id, 0.0, 250.0),
            ]),
        )

    def test_manifest_and_listing_publish_only_shipped_capabilities(self):
        module = Path(__file__).resolve().parents[1]
        manifest = ast.literal_eval((module / '__manifest__.py').read_text())
        listing = (module / 'static' / 'description' / 'index.html').read_text()
        self.assertLessEqual(len(manifest['name']), 25)
        self.assertLessEqual(len(manifest['summary']), 160)
        self.assertEqual(manifest['version'], '19.0.1.1.6')
        self.assertEqual(listing.count('v%s' % manifest['version']), 2)
        for unsupported in (
            'which reduces later depreciation',
            'so it flows through reduced depreciation',
            'grant amortisation schedule',
        ):
            self.assertNotIn(unsupported, manifest['description'] + listing)
        self.assertIn(
            'does not generate an amortisation schedule',
            manifest['description'],
        )

    def test_form_freeze_and_search_view_match_server_lifecycle(self):
        form = etree.fromstring(self.env.ref(
            'eh_account_grants.view_eh_gov_grant_form'
        ).arch_db.encode())
        received_asset = form.xpath(
            "//field[@name='received_asset_account_id']"
        )[0]
        self.assertIn(
            'state',
            received_asset.get('readonly') or received_asset.get('attrs', ''),
        )
        condition_fields = form.xpath("//field[@name='condition_ids']")
        self.assertEqual(len(condition_fields), 2)
        posted_list = condition_fields[1].xpath('./list | ./tree')[0]
        self.assertEqual(posted_list.get('create'), '0')
        self.assertEqual(posted_list.get('edit'), '0')
        self.assertEqual(posted_list.get('delete'), '0')

        search = etree.fromstring(self.env.ref(
            'eh_account_grants.view_eh_gov_grant_search'
        ).arch_db.encode())
        for name in (
            'filter_received', 'filter_repayable', 'filter_breached',
            'filter_non_monetary', 'group_grantor', 'group_company',
        ):
            self.assertEqual(len(search.xpath(
                "//filter[@name='%s']" % name,
            )), 1)

    def test_condition_mutations_accept_allowed_non_active_company(self):
        other = self.env['res.company'].create({
            'name': 'Allowed Grants Subsidiary',
            'currency_id': self.company.currency_id.id,
        })
        self.plain_user.write({'company_ids': [(4, other.id)]})
        self.manager.write({'company_ids': [(4, other.id)]})
        grant = self.env['eh.gov.grant'].with_company(other).create({
            'company_id': other.id,
            'amount': 100.0,
            'grantor_id': self.grantor.id,
            'agreement_reference': 'OTHER-COMPANY-AWARD',
        })
        actor = self.env['eh.gov.grant.condition'].with_user(
            self.plain_user,
        ).with_context(allowed_company_ids=[self.company.id, other.id])
        self.assertEqual(actor.env.company, self.company)
        condition = actor.create({
            'grant_id': grant.id,
            'name': 'Subsidiary reporting condition',
        })
        condition.write({'name': 'Updated subsidiary condition'})
        condition.with_user(self.manager).with_context(
            allowed_company_ids=[self.company.id, other.id],
        ).unlink()
        self.assertFalse(condition.exists())

    def test_authorisation_failures_are_access_errors(self):
        grant = self._grant()
        with self.assertRaises(AccessError):
            grant.with_user(self.plain_user).action_receive()
        grant.action_receive()
        grant.repayment_amount = 100.0
        with self.assertRaises(AccessError):
            grant.with_user(self.plain_user).action_repay()

    def test_duplicate_resets_condition_review_outcomes(self):
        grant = self._grant(condition_ids=[
            (0, 0, {'name': 'Fulfilled source condition'}),
            (0, 0, {'name': 'Breached source condition'}),
        ])
        grant.condition_ids[0].action_fulfil()
        grant.condition_ids[1].action_breach()
        duplicate = grant.copy()
        self.assertEqual(set(duplicate.condition_ids.mapped('state')), {'open'})
        self.assertFalse(any(duplicate.condition_ids.mapped('fulfilled_date')))

    def test_auditor_reviewer_menu_acl_and_buttons_are_aligned(self):
        menu = self.env.ref('eh_account_grants.menu_eh_gov_grant')
        menu_groups = (
            menu.group_ids if 'group_ids' in menu._fields else menu.groups_id
        )
        auditor = self.env.ref('eh_account_base.group_eh_auditor')
        reviewer_group = self.env.ref(
            'eh_account_grants.group_eh_grant_reviewer'
        )
        self.assertIn(auditor, menu_groups)
        self.assertIn(reviewer_group, menu_groups)
        parent = self.env.ref('eh_account_base.menu_eh_ifrs')
        parent_groups = (
            parent.group_ids
            if 'group_ids' in parent._fields else parent.groups_id
        )
        self.assertIn(auditor, parent_groups)
        self.assertIn(reviewer_group, parent_groups)
        self.assertNotIn(
            self.env.ref('eh_account_base.group_eh_user'),
            reviewer_group.implied_ids,
        )
        self.assertEqual(
            reviewer_group.implied_ids,
            self.env.ref('account.group_account_readonly'),
        )
        grant_acl = self.env.ref(
            'eh_account_grants.access_eh_gov_grant_reviewer'
        )
        condition_acl = self.env.ref(
            'eh_account_grants.access_eh_gov_grant_condition_reviewer'
        )
        self.assertTrue(grant_acl.perm_read)
        self.assertFalse(grant_acl.perm_write)
        self.assertTrue(condition_acl.perm_write)
        self.assertFalse(condition_acl.perm_create)

        form = etree.fromstring(self.env.ref(
            'eh_account_grants.view_eh_gov_grant_form'
        ).arch_db.encode())
        buttons = form.xpath(
            "//button[@name='action_fulfil' or @name='action_reopen' "
            "or @name='action_breach']"
        )
        self.assertTrue(buttons)
        for button in buttons:
            groups = button.get('groups', '')
            self.assertIn(
                'eh_account_grants.group_eh_grant_reviewer', groups,
            )
            self.assertIn('eh_account_base.group_eh_manager', groups)

    def test_breach_button_capability_tracks_posting_requirement(self):
        grant = self._grant(
            clawback_amount=200.0,
            condition_ids=[(0, 0, {'name': 'Operational condition'})],
        )
        condition = grant.condition_ids
        self.assertTrue(condition.with_user(self.reviewer).can_breach)
        self.assertFalse(condition.with_user(self.plain_user).can_breach)
        grant.action_receive()
        self.assertFalse(condition.with_user(self.reviewer).can_breach)
        self.assertTrue(condition.can_breach)

    def test_company_sequence_uses_grant_date_and_independent_counters(self):
        companies = self.env['res.company'].create([
            {
                'name': 'Grant Sequence Company A',
                'currency_id': self.company.currency_id.id,
            },
            {
                'name': 'Grant Sequence Company B',
                'currency_id': self.company.currency_id.id,
            },
        ])
        names = []
        for company in companies:
            grant = self.env['eh.gov.grant'].with_company(company).create({
                'company_id': company.id,
                'grant_date': '2032-04-05',
                'amount': 100.0,
                'grantor_id': self.grantor.id,
                'agreement_reference': 'SEQUENCE-AWARD',
            })
            names.append(grant.name)
        self.assertEqual(names, ['GRANT/2032/0001', 'GRANT/2032/0001'])
        for company in companies:
            sequence = self.env['ir.sequence'].with_context(
                active_test=False,
            ).search([
                ('code', '=', 'eh.gov.grant'),
                ('company_id', '=', company.id),
            ])
            self.assertEqual(len(sequence), 1)
            self.assertTrue(sequence.use_date_range)

    def test_agreement_provenance_and_partner_reach_every_move_line(self):
        attachment = self.env['ir.attachment'].create({
            'name': 'signed-award.txt',
            'raw': b'signed government award',
        })
        grant = self._grant(
            agreement_attachment_ids=[(6, 0, attachment.ids)],
        )
        grant.action_receive()
        self.assertEqual(
            set(grant.move_ids.line_ids.mapped('partner_id').ids),
            {self.grantor.id},
        )
        with self.assertRaises(UserError):
            grant.grantor_id = self.env['res.partner'].create({
                'name': 'Forged replacement grantor',
            })
        with self.assertRaises(UserError):
            grant.agreement_reference = 'REWRITTEN-AWARD'
        with self.assertRaises(UserError):
            grant.agreement_attachment_ids = [(5, 0, 0)]

        incomplete = self._grant(
            grantor_id=False, agreement_reference=False,
        )
        with self.assertRaisesRegex(UserError, 'grantor'):
            incomplete.action_receive()

    def test_migration_retires_global_sequence_and_provisions_companies(self):
        company = self.env['res.company'].create({
            'name': 'Grant Migration Company',
            'currency_id': self.company.currency_id.id,
        })
        main_sequence = self.env['ir.sequence'].with_context(
            active_test=False,
        ).search([
            ('code', '=', 'eh.gov.grant'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        main_sequence.date_range_ids.unlink()
        main_sequence.write({
            'name': 'Legacy XMLID Grant Sequence',
            'prefix': 'LEGACY/%(year)s/',
            'use_date_range': False,
            'number_next': 73,
            'company_id': False,
        })
        global_sequence = main_sequence
        duplicate_global = self.env['ir.sequence'].create({
            'name': 'Legacy Duplicate Global Grant Sequence',
            'code': 'eh.gov.grant',
            'prefix': 'DUPLICATE/%(year)s/',
            'padding': 4,
            'number_next': 91,
            'company_id': False,
        })
        self.assertEqual(global_sequence.number_next_actual, 73)
        self.assertEqual(duplicate_global.number_next_actual, 91)
        self.post_migration_116.migrate(self.env.cr, '19.0.1.1.5')
        global_sequence.invalidate_recordset([
            'active', 'company_id', 'prefix', 'use_date_range',
        ])
        self.assertTrue(global_sequence.active)
        self.assertEqual(global_sequence.company_id, self.company)
        self.assertEqual(global_sequence.prefix, 'GRANT/%(year)s/')
        self.assertTrue(global_sequence.use_date_range)
        duplicate_global.invalidate_recordset(['active'])
        self.assertFalse(duplicate_global.active)
        self.assertEqual(
            self.env.ref('eh_account_grants.seq_eh_gov_grant'),
            global_sequence,
        )
        current_range = self.env['ir.sequence.date_range'].search([
            ('sequence_id', '=', global_sequence.id),
            ('date_from', '<=', fields.Date.today()),
            ('date_to', '>=', fields.Date.today()),
        ], limit=1)
        self.assertTrue(current_range)
        self.assertGreaterEqual(current_range.number_next, 91)
        current_range.invalidate_recordset(['number_next_actual'])
        self.assertGreaterEqual(current_range.number_next_actual, 91)
        self.assertEqual(self.env['ir.sequence'].search_count([
            ('code', '=', 'eh.gov.grant'),
            ('company_id', '=', company.id),
        ]), 1)
