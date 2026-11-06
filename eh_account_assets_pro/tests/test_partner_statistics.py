# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Partner-list asset and lease statistics regressions."""

import odoo

from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'post_install', '-at_install')
class TestPartnerAssetStatistics(EhAssetTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'Asset Statistics Company B',
            'currency_id': cls.company.currency_id.id,
        })
        cls.shared_partner = cls.env['res.partner'].create({
            'name': 'Asset Statistics Shared Partner',
        })
        cls.empty_partner = cls.env['res.partner'].create({
            'name': 'Asset Statistics Empty Partner',
        })

        group_user = cls.env.ref('eh_account_base.group_eh_user')
        base_user = cls.env.ref('base.group_user')
        cls.asset_user = cls.env['res.users'].create({
            'name': 'Asset Statistics User',
            'login': 'asset_statistics_user@test',
            'email': 'asset_statistics_user@test',
            'company_id': cls.company_a.id,
            'company_ids': [(6, 0, cls.company_a.ids)],
            'group_ids': [(6, 0, (base_user + group_user).ids)],
        })
        cls.unauthorized_user = cls.env['res.users'].create({
            'name': 'Asset Statistics Unauthorized',
            'login': 'asset_statistics_unauthorized@test',
            'email': 'asset_statistics_unauthorized@test',
            'company_id': cls.company_a.id,
            'company_ids': [(6, 0, cls.company_a.ids)],
            'group_ids': [(6, 0, base_user.ids)],
        })

        cls.asset_a = cls._make_asset(
            code='STAT-ASSET-A',
            partner_id=cls.shared_partner.id,
        )
        cls.disposed_asset_a = cls._make_asset(
            code='STAT-ASSET-A-DISPOSED',
            partner_id=cls.shared_partner.id,
            state='disposed',
        )
        cls.lease_a = cls._make_lease(
            reference='STAT-LEASE-A',
            lessor_id=cls.shared_partner.id,
        )

        allowed_companies = (cls.company_a + cls.company_b).ids
        env_b = cls.env['res.company'].sudo().with_context(
            allowed_company_ids=allowed_companies,
        ).with_company(cls.company_b).env
        cash_b = cls._ensure_account(
            env_b, 'ASB1000', 'Asset Statistics Cash B', 'asset_cash',
        )
        journal_b = cls._ensure_journal(
            env_b, cls.company_b, 'general', 'ASB', 'Asset Statistics B',
        )
        category_b = env_b['eh.asset.category'].create({
            'name': 'Asset Statistics Category B',
            'code': 'ASB',
            'company_id': cls.company_b.id,
        })
        cls.asset_b = env_b['eh.asset'].create({
            'name': '/',
            'code': 'STAT-ASSET-B',
            'category_id': category_b.id,
            'partner_id': cls.shared_partner.id,
            'acquisition_date': '2026-01-01',
            'in_service_date': '2026-01-01',
            'acquisition_cost': 100.0,
            'company_id': cls.company_b.id,
            'currency_id': cls.company_b.currency_id.id,
        })
        cls.lease_b = env_b['eh.lease.contract'].create({
            'name': '/',
            'reference': 'STAT-LEASE-B',
            'lessor_id': cls.shared_partner.id,
            'commencement_date': '2026-01-01',
            'term_months': 12,
            'cadence': 'monthly',
            'payment_timing': 'arrears',
            'payment_amount': 100.0,
            'incremental_borrowing_rate': 5.0,
            'cash_account_id': cash_b.id,
            'journal_id': journal_b.id,
            'company_id': cls.company_b.id,
            'currency_id': cls.company_b.currency_id.id,
        })

    @staticmethod
    def _badges(partner):
        partner.invalidate_recordset([
            'application_statistics', 'eh_asset_count', 'eh_lease_count',
        ])
        return {
            item.get('label'): item
            for item in (partner.application_statistics or [])
        }

    def test_statistics_grouping_columns_are_indexed(self):
        self.assertTrue(self.env['eh.asset']._fields['partner_id'].index)
        self.assertTrue(
            self.env['eh.lease.contract']._fields['lessor_id'].index,
        )

    def test_statistics_respect_allowed_companies(self):
        partner = self.shared_partner.with_user(
            self.asset_user,
        ).with_context(allowed_company_ids=self.company_a.ids)
        badges = self._badges(partner)
        self.assertEqual(badges['Assets']['value'], 1)
        self.assertEqual(badges['Leases']['value'], 1)

        self.asset_user.company_ids = self.company_a + self.company_b
        partner = partner.with_context(
            allowed_company_ids=(self.company_a + self.company_b).ids,
        )
        badges = self._badges(partner)
        self.assertEqual(badges['Assets']['value'], 2)
        self.assertEqual(badges['Leases']['value'], 2)

    def test_badge_click_actions_open_filtered_lists(self):
        expected_list_type = (
            'list' if odoo.release.version_info[0] >= 18 else 'tree'
        )
        partner = self.shared_partner.with_user(
            self.asset_user,
        ).with_context(allowed_company_ids=self.company_a.ids)
        badges = self._badges(partner)
        expected = {
            'Assets': (
                'action_view_eh_assets_list', 'eh.asset', self.asset_a.ids,
                [
                    ('partner_id', '=', self.shared_partner.id),
                    ('state', '!=', 'disposed'),
                    ('company_id', 'in', self.company_a.ids),
                ],
            ),
            'Leases': (
                'action_view_eh_leases',
                'eh.lease.contract',
                self.lease_a.ids,
                [('lessor_id', '=', self.shared_partner.id)],
            ),
        }
        for label, values in expected.items():
            method, model, expected_ids, expected_domain = values
            badge = badges[label]
            self.assertEqual(badge['actionMethod'], method)
            action = getattr(partner, badge['actionMethod'])()
            self.assertEqual(action['res_model'], model)
            self.assertEqual(
                action['view_mode'], '%s,form' % expected_list_type,
            )
            self.assertEqual(
                action['views'],
                [(False, expected_list_type), (False, 'form')],
            )
            self.assertNotIn('res_id', action)
            self.assertEqual(action['domain'], expected_domain)
            records = partner.env[model].search(action['domain'])
            self.assertEqual(records.ids, expected_ids)
            self.assertEqual(len(records), badge['value'])
            self.assertEqual(
                records.mapped('company_id').ids, self.company_a.ids,
            )

        asset_action = getattr(
            partner, badges['Assets']['actionMethod'],
        )()
        self.assertIn(('state', '!=', 'disposed'), asset_action['domain'])
        self.assertNotIn(
            self.disposed_asset_a.id,
            partner.env['eh.asset'].search(asset_action['domain']).ids,
        )

        # The form smart button intentionally remains kanban-first.
        smart_action = partner.action_view_eh_assets()
        self.assertEqual(
            smart_action['view_mode'],
            'kanban,%s,form' % expected_list_type,
        )

    def test_statistics_omit_zero_values(self):
        badges = self._badges(self.empty_partner.with_user(self.asset_user))
        self.assertNotIn('Assets', badges)
        self.assertNotIn('Leases', badges)

    def test_statistics_omit_data_for_unauthorized_user(self):
        partner = self.shared_partner.with_user(self.unauthorized_user)
        partner.invalidate_recordset(['application_statistics'])
        labels = {
            item.get('label') for item in (partner.application_statistics or [])
        }
        self.assertNotIn('Assets', labels)
        self.assertNotIn('Leases', labels)
