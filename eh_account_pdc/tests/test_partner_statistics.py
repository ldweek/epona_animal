# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Partner-list PDC statistics security and empty-data regressions."""

import odoo

from odoo.tests import tagged

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'post_install', '-at_install')
class TestPartnerPdcStatistics(EhPdcTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'PDC Statistics Company B',
            'currency_id': cls.company.currency_id.id,
        })
        cls.shared_partner = cls.env['res.partner'].create({
            'name': 'PDC Statistics Shared Partner',
        })
        cls.empty_partner = cls.env['res.partner'].create({
            'name': 'PDC Statistics Empty Partner',
        })

        group_user = cls.env.ref('eh_account_base.group_eh_user')
        base_user = cls.env.ref('base.group_user')
        cls.pdc_user = cls.env['res.users'].create({
            'name': 'PDC Statistics User',
            'login': 'pdc_statistics_user@test',
            'email': 'pdc_statistics_user@test',
            'company_id': cls.company_a.id,
            'company_ids': [(6, 0, cls.company_a.ids)],
            'group_ids': [(6, 0, (base_user + group_user).ids)],
        })
        cls.unauthorized_user = cls.env['res.users'].create({
            'name': 'PDC Statistics Unauthorized',
            'login': 'pdc_statistics_unauthorized@test',
            'email': 'pdc_statistics_unauthorized@test',
            'company_id': cls.company_a.id,
            'company_ids': [(6, 0, cls.company_a.ids)],
            'group_ids': [(6, 0, base_user.ids)],
        })

        allowed_companies = (cls.company_a + cls.company_b).ids
        env_b = cls.env['res.company'].sudo().with_context(
            allowed_company_ids=allowed_companies,
        ).with_company(cls.company_b).env
        journal_b = cls._ensure_journal(
            env_b, cls.company_b, 'bank', 'PSTB', 'PDC Statistics B',
        )
        cls.cheque_a = cls._create_cheque(
            cls.env, cls.bank_journal, 'PSTAT-A',
        )
        cls.cheque_b = cls._create_cheque(env_b, journal_b, 'PSTAT-B')

    @classmethod
    def _create_cheque(cls, env, journal, number):
        return env['eh.cheque'].sudo().create({
            'direction': 'incoming',
            'state': 'registered',
            'cheque_number': number,
            'journal_id': journal.id,
            'partner_id': cls.shared_partner.id,
            'amount': 100.0,
            'currency_id': env.company.currency_id.id,
            'company_id': env.company.id,
            'issue_date': cls.today,
            'value_date': cls.today,
        })

    @staticmethod
    def _badge(partner):
        partner.invalidate_recordset([
            'application_statistics', 'eh_open_cheque_count',
        ])
        return next((
            item for item in (partner.application_statistics or [])
            if item.get('label') == 'Open Cheques'
        ), None)

    def test_statistics_grouping_column_is_indexed(self):
        self.assertTrue(self.env['eh.cheque']._fields['partner_id'].index)

    def test_statistics_respect_allowed_companies(self):
        partner = self.shared_partner.with_user(
            self.pdc_user,
        ).with_context(allowed_company_ids=self.company_a.ids)
        self.assertEqual(partner.eh_open_cheque_count, 1)
        self.assertEqual(self._badge(partner)['value'], 1)

        self.pdc_user.company_ids = self.company_a + self.company_b
        partner = partner.with_context(
            allowed_company_ids=(self.company_a + self.company_b).ids,
        )
        self.assertEqual(self._badge(partner)['value'], 2)

    def test_badge_click_opens_filtered_list(self):
        expected_list_type = (
            'list' if odoo.release.version_info[0] >= 18 else 'tree'
        )
        partner = self.shared_partner.with_user(
            self.pdc_user,
        ).with_context(allowed_company_ids=self.company_a.ids)
        badge = self._badge(partner)
        self.assertEqual(
            badge['actionMethod'], 'action_view_eh_open_cheques_list',
        )

        action = getattr(partner, badge['actionMethod'])()
        self.assertEqual(action['res_model'], 'eh.cheque')
        self.assertEqual(
            action['view_mode'], '%s,form' % expected_list_type,
        )
        self.assertEqual(
            action['views'],
            [(False, expected_list_type), (False, 'form')],
        )
        self.assertNotIn('res_id', action)
        self.assertEqual(action['domain'], [
            ('partner_id', '=', self.shared_partner.id),
            ('company_id', 'in', self.company_a.ids),
            ('state', 'in', ['registered', 'presented', 'bounced']),
        ])
        cheques = partner.env['eh.cheque'].search(action['domain'])
        self.assertEqual(cheques.ids, self.cheque_a.ids)
        self.assertEqual(len(cheques), badge['value'])
        self.assertNotIn(self.cheque_b.id, cheques.ids)

        # The form smart button intentionally remains kanban-first.
        smart_action = partner.action_view_eh_open_cheques()
        self.assertEqual(
            smart_action['view_mode'],
            'kanban,%s,form' % expected_list_type,
        )

    def test_statistics_omit_zero_values(self):
        partner = self.empty_partner.with_user(self.pdc_user)
        self.assertIsNone(self._badge(partner))

    def test_statistics_omit_data_for_unauthorized_user(self):
        partner = self.shared_partner.with_user(self.unauthorized_user)
        partner.invalidate_recordset(['application_statistics'])
        statistics = partner.application_statistics or []
        self.assertFalse(any(
            item.get('label') == 'Open Cheques' for item in statistics
        ))
