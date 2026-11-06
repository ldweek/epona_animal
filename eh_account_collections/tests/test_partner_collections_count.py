# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Regression: partner collections counts and list statistics must respect model
access, record rules, active companies, and empty data.
"""

import odoo

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_collections', 'post_install', '-at_install')
class TestPartnerCollectionsCount(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Case = cls.env['eh.collections.case']
        cls.company_a = cls.company
        cls.company_b = cls.env['res.company'].create({
            'name': 'EH Collections Co B',
        })
        # partner_a is a normal partner with company_id = False, i.e. shared
        # across every company - the ordinary case for the leak.
        cls.shared_partner = cls.partner_a

        # Two non-resolved cases against the shared partner, one per company.
        cls.case_a = cls.Case.create({
            'partner_id': cls.shared_partner.id,
            'company_id': cls.company_a.id,
        })
        cls.case_b = cls.Case.create({
            'partner_id': cls.shared_partner.id,
            'company_id': cls.company_b.id,
        })

        # Alice can only see Company A (single company_id => single-company
        # company_ids). She is an EH collections user so she legitimately has
        # read access to the case model within her own company.
        cls.alice = cls.env['res.users'].create({
            'name': 'Alice CoA',
            'login': 'coll_count_alice@test',
            'email': 'coll_count_alice@test',
            'company_id': cls.company_a.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.unauthorized_user = cls.env['res.users'].create({
            'name': 'Collections Statistics Unauthorized',
            'login': 'collections_statistics_unauthorized@test',
            'email': 'collections_statistics_unauthorized@test',
            'company_id': cls.company_a.id,
            'company_ids': [(6, 0, cls.company_a.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
            ])],
        })
        cls.empty_partner = cls.env['res.partner'].create({
            'name': 'Collections Statistics Empty Partner',
        })

    @staticmethod
    def _badge(partner):
        partner.invalidate_recordset([
            'application_statistics', 'eh_active_collections_count',
        ])
        return next((
            item for item in (partner.application_statistics or [])
            if item.get('label') == 'Active Collections'
        ), None)

    def _sanity_non_resolved(self):
        # The default stage that new cases land in must be non-resolved for
        # this fixture to be meaningful.
        self.assertFalse(self.case_a.is_resolved)
        self.assertFalse(self.case_b.is_resolved)

    def test_count_excludes_other_company_cases(self):
        """Alice (Company A only) sees a count of 1, not 2: Company B's case
        against the shared partner must not leak into the smart button."""
        self._sanity_non_resolved()
        partner = self.shared_partner.with_user(self.alice)
        # env.companies for Alice is just Company A.
        self.assertEqual(partner.env.companies.ids, self.company_a.ids)
        partner.invalidate_recordset(['eh_active_collections_count'])
        self.assertEqual(partner.eh_active_collections_count, 1)
        self.assertEqual(self._badge(partner)['value'], 1)

    def test_count_matches_drilldown_domain(self):
        """The count must agree with the record-rule-scoped drill-down: both
        reflect exactly one case for Alice."""
        expected_list_type = (
            'list' if odoo.release.version_info[0] >= 18 else 'tree'
        )
        partner = self.shared_partner.with_user(self.alice)
        partner.invalidate_recordset(['eh_active_collections_count'])
        count = partner.eh_active_collections_count
        badge = self._badge(partner)
        self.assertEqual(
            badge['actionMethod'],
            'action_view_eh_active_collections_list',
        )
        action = getattr(partner, badge['actionMethod'])()
        self.assertEqual(action['res_model'], 'eh.collections.case')
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
            ('is_resolved', '=', False),
            ('company_id', 'in', self.company_a.ids),
        ])
        visible = partner.env['eh.collections.case'].search(action['domain'])
        self.assertEqual(visible.ids, self.case_a.ids)
        self.assertEqual(count, len(visible))
        self.assertEqual(count, 1)
        self.assertEqual(count, badge['value'])

        # The form smart button intentionally remains kanban-first.
        smart_action = partner.action_view_eh_active_collections()
        self.assertEqual(
            smart_action['view_mode'],
            'kanban,%s,form' % expected_list_type,
        )

    def test_count_follows_multi_company_selection(self):
        """When Alice is also granted Company B and both are active, the
        count widens to include Company B's case - proving the figure tracks
        the allowed-company scope rather than a fixed single company."""
        # Grant Alice access to Company B as well (ATTRIBUTE assignment, not
        # a two-element (6,0,...) command on create - see cross-version trap).
        self.alice.company_ids = self.company_a + self.company_b
        partner = self.shared_partner.with_user(self.alice).with_context(
            allowed_company_ids=(self.company_a + self.company_b).ids)
        partner.invalidate_recordset(['eh_active_collections_count'])
        self.assertEqual(partner.eh_active_collections_count, 2)
        self.assertEqual(self._badge(partner)['value'], 2)

    def test_statistics_omit_zero_values(self):
        partner = self.empty_partner.with_user(self.alice)
        self.assertIsNone(self._badge(partner))

    def test_statistics_omit_data_for_unauthorized_user(self):
        partner = self.shared_partner.with_user(self.unauthorized_user)
        partner.invalidate_recordset(['application_statistics'])
        statistics = partner.application_statistics or []
        self.assertFalse(any(
            item.get('label') == 'Active Collections'
            for item in statistics
        ))

    def test_statistics_cache_is_partitioned_by_user_and_company(self):
        self.alice.company_ids = self.company_a + self.company_b
        partner = self.shared_partner.with_user(self.alice).with_context(
            allowed_company_ids=self.company_a.ids,
        )
        partner.invalidate_recordset([
            'application_statistics', 'eh_active_collections_count',
        ])

        company_a_statistics = partner.application_statistics or []
        company_a_value = next(
            item['value'] for item in company_a_statistics
            if item.get('label') == 'Active Collections'
        )
        self.assertEqual(company_a_value, 1)

        # No invalidation: context dependencies must select another cache key.
        widened_statistics = partner.with_context(
            allowed_company_ids=(self.company_a + self.company_b).ids,
        ).application_statistics or []
        widened_value = next(
            item['value'] for item in widened_statistics
            if item.get('label') == 'Active Collections'
        )
        self.assertEqual(widened_value, 2)

        unauthorized_statistics = partner.with_user(
            self.unauthorized_user,
        ).application_statistics or []
        self.assertFalse(any(
            item.get('label') == 'Active Collections'
            for item in unauthorized_statistics
        ))

    def test_statistics_query_count_does_not_scale_per_partner(self):
        partners = self.env['res.partner'].create([
            {'name': 'Collections Statistics Scale %02d' % index}
            for index in range(80)
        ])

        def _measure(records):
            records = records.with_user(self.alice).with_context(
                allowed_company_ids=self.company_a.ids,
            )
            # Warm authorization/context caches. Only statistics fields stay
            # cold, matching repeated list-page reads for the same user.
            self.assertTrue(records.env.user.has_group(
                'eh_account_base.group_eh_user',
            ))
            self.assertEqual(records.env.companies, self.company_a)
            records.invalidate_recordset([
                'application_statistics', 'eh_active_collections_count',
            ])
            start = records.env.cr.sql_log_count
            values = records.read(['application_statistics'])
            query_count = records.env.cr.sql_log_count - start
            for values_by_partner in values:
                self.assertIn('application_statistics', values_by_partner)
            return query_count

        single_count = _measure(partners[:1])
        large_count = _measure(partners)
        self.assertLessEqual(
            large_count,
            single_count + 2,
            'Partner statistics query count must stay fixed, never N+1.',
        )

    def test_empty_recordset_statistics_are_query_free(self):
        partners = self.env['res.partner'].browse().with_user(self.alice)
        self.assertTrue(partners.env.user.has_group(
            'eh_account_base.group_eh_user',
        ))
        start = partners.env.cr.sql_log_count
        partners._compute_eh_active_collections_count()
        partners._compute_application_statistics()
        self.assertEqual(partners.env.cr.sql_log_count - start, 0)
