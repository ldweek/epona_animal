# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Regression guard: the collections next-action compute must never invoke
a live AI provider on the read path.

Action and priority are stored operational keys; rationale is live translated
text. Both deterministic compute paths must stay separate from provider I/O.
If either passed the live company provider into next_action_suggester.suggest,
a configured non-manual provider would fire a blocking outbound HTTPS POST
from an ordinary ORM recompute or form read and starve the HTTP workers. Both
paths force provider_key='manual'; the live provider belongs only to the
explicit action_eh_ai_refresh_suggestion button, which writes a stored
narrative.
"""

import ast
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import get_module_path
from odoo.tests import tagged

from odoo.addons.eh_account_ai_agent.tools import next_action_suggester
from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_ai_collections.models.control import (
    AI_COLLECTIONS_EVIDENCE_CAPABILITY,
    AI_COLLECTIONS_EVIDENCE_CONTEXT,
)


@tagged('eh_account_ai_collections', 'post_install', '-at_install')
class TestNoNetworkCompute(EhAccountIntegrationTestCase):

    def _case(self, days=200, total=5000.0):
        return self.env['eh.collections.case'].create({
            'partner_id': self.partner_a.id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'days_overdue_max': days,
            'total_overdue_amount': total,
        })

    def _spy_suggest(self):
        """Wrap next_action_suggester.suggest to record the provider it
        is called with, always delegating with the manual provider so no
        real network provider can ever be reached from the test.
        """
        real = next_action_suggester.suggest
        calls = []

        def spy(case, provider_key='manual', provider_config=None):
            calls.append((provider_key, provider_config))
            return real(case, provider_key='manual')

        return calls, patch.object(
            next_action_suggester, 'suggest', side_effect=spy,
        )

    def _spy_provider_suggest(self):
        calls = []

        def spy(case, provider_key='', provider_config=None):
            calls.append((provider_key, provider_config))
            return next_action_suggester.Suggestion(
                action='provider_action',
                rationale='Verified provider narrative',
                priority='medium',
            )

        return calls, patch.object(
            next_action_suggester, 'provider_suggest', side_effect=spy,
        )

    def _write_existing_evidence(self, case, narrative):
        provider_key = 'openai'
        case.with_context(**{
            AI_COLLECTIONS_EVIDENCE_CONTEXT:
                AI_COLLECTIONS_EVIDENCE_CAPABILITY,
        }).write({
            'eh_ai_suggestion_narrative': narrative,
            'eh_ai_narrative_generated_at': fields.Datetime.now(),
            'eh_ai_narrative_generated_by_id': self.env.user.id,
            'eh_ai_narrative_provider_key': provider_key,
            'eh_ai_narrative_source_fingerprint':
                case._eh_ai_source_fingerprint(provider_key),
        })

    def test_compute_forces_manual_provider_on_read(self):
        # A live provider is configured on the company, exactly as the
        # UI instructs.
        self.company.eh_ai_provider_key = 'openai'
        self.company.sudo().eh_ai_provider_config = '{"api_key": "x"}'
        case = self._case()
        stored_keys = [
            'eh_ai_suggested_action', 'eh_ai_suggested_priority',
        ]
        case.flush_recordset(stored_keys)
        case.invalidate_recordset([
            *stored_keys,
            'eh_ai_suggestion_rationale',
        ])
        calls, cm = self._spy_suggest()
        with cm:
            # Stored keys load directly. Live rationale executes its distinct,
            # deterministic compute exactly once.
            action = case.eh_ai_suggested_action
            rationale = case.eh_ai_suggestion_rationale
        self.assertEqual(len(calls), 1)
        for provider_key, provider_config in calls:
            self.assertEqual(
                provider_key, 'manual',
                "read-path compute passed a live provider (%s); it would "
                "fire a blocking HTTPS POST and starve workers"
                % provider_key,
            )
            self.assertIsNone(
                provider_config,
                "read-path compute leaked provider config to the suggester",
            )
        # The deterministic suggestion still renders.
        self.assertEqual(action, 'consider_write_off')
        self.assertTrue(rationale)

    def test_button_uses_live_provider_and_stores_narrative(self):
        self.company.eh_ai_provider_key = 'openai'
        self.company.sudo().eh_ai_provider_config = '{"api_key": "x"}'
        case = self._case()
        self.assertFalse(
            case.eh_ai_suggestion_narrative,
            "narrative must be empty until the button is pressed",
        )
        calls, cm = self._spy_provider_suggest()
        with cm:
            case.action_eh_ai_refresh_suggestion()
            case.action_eh_ai_refresh_suggestion()
        # The button, and only the button, hands the live provider to the
        # suggester.
        self.assertTrue(calls, "button did not call the suggester")
        self.assertEqual(calls[-1][0], 'openai')
        self.assertEqual(calls[-1][1], '{"api_key": "x"}')
        self.assertEqual(len(calls), 1, "unchanged refresh rebilled provider")
        # The enriched result is written to the stored field.
        self.assertTrue(
            case.eh_ai_suggestion_narrative,
            "button did not store a narrative",
        )
        self.assertIn('provider_action', case.eh_ai_suggestion_narrative)
        self.assertEqual(case.eh_ai_narrative_provider_key, 'openai')
        self.assertEqual(case.eh_ai_narrative_generated_by_id, self.env.user)
        self.assertTrue(case.eh_ai_narrative_generated_at)
        self.assertEqual(len(case.eh_ai_narrative_source_fingerprint), 64)
        self.assertFalse(case.eh_ai_narrative_stale)

        case.total_overdue_amount += 1.0
        self.assertTrue(case.eh_ai_narrative_stale)

    def test_bulk_refresh_is_refused_before_provider_call(self):
        first = self._case()
        second = self.env['eh.collections.case'].create({
            'partner_id': self.env['res.partner'].create({
                'name': 'AI Collections Bulk Second',
            }).id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'days_overdue_max': 200,
            'total_overdue_amount': 5000.0,
        })
        calls, cm = self._spy_provider_suggest()
        with cm, self.assertRaisesRegex(UserError, 'one collections case'):
            (first | second).action_eh_ai_refresh_suggestion()
        self.assertEqual(calls, [])

    def test_unauthorised_button_fails_before_provider_call(self):
        case = self._case()
        outsider = self.env['res.users'].create({
            'name': 'Collections AI Outsider',
            'login': 'collections_ai_outsider_test',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        calls, cm = self._spy_provider_suggest()
        with cm, self.assertRaises(AccessError):
            case.with_user(outsider).action_eh_ai_refresh_suggestion()
        self.assertEqual(calls, [])

    def test_ordinary_eh_user_cannot_spend_provider_budget(self):
        case = self._case()
        user = self.env['res.users'].create({
            'name': 'Collections AI Ordinary User',
            'login': 'collections_ai_ordinary_test',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        calls, cm = self._spy_provider_suggest()
        with cm, self.assertRaisesRegex(AccessError, 'accounting manager'):
            case.with_user(user).action_eh_ai_refresh_suggestion()
        self.assertEqual(calls, [])

    def test_live_provider_cannot_override_suspended_case_control(self):
        self.company.eh_ai_provider_key = 'openai'
        self.company.sudo().eh_ai_provider_config = '{"api_key": "x"}'
        case = self._case()
        case.followup_paused = True
        self._write_existing_evidence(case, 'Previously verified narrative')
        with self.assertRaisesRegex(UserError, 'workflow control'):
            case.action_eh_ai_refresh_suggestion()
        self.assertEqual(
            case.eh_ai_suggestion_narrative,
            'Previously verified narrative',
        )

    def test_manual_provider_does_not_store_deterministic_as_ai(self):
        self.company.eh_ai_provider_key = 'manual'
        case = self._case(days=95)
        self._write_existing_evidence(case, 'Previously verified narrative')

        with self.assertRaisesRegex(UserError, 'No live AI provider'):
            case.action_eh_ai_refresh_suggestion()

        self.assertEqual(
            case.eh_ai_suggestion_narrative,
            'Previously verified narrative',
        )

    def test_ai_evidence_fields_reject_rpc_forgery(self):
        case = self._case()
        with self.assertRaisesRegex(AccessError, 'server-generated'):
            case.write({
                'eh_ai_suggestion_narrative': 'Forged narrative',
                'eh_ai_narrative_provider_key': 'forged',
            })
        with self.assertRaisesRegex(AccessError, 'server-generated'):
            case.with_context(**{
                AI_COLLECTIONS_EVIDENCE_CONTEXT: True,
            }).write({'eh_ai_suggestion_narrative': 'Still forged'})

    def test_refresh_button_is_manager_only_confirmed_and_state_bounded(self):
        root = Path(get_module_path('eh_account_ai_collections'))
        xml = ElementTree.parse(
            root / 'views/collections_case_views.xml'
        ).getroot()
        button = xml.find(
            ".//button[@name='action_eh_ai_refresh_suggestion']"
        )
        self.assertIsNotNone(button)
        self.assertEqual(button.attrib['groups'],
                         'eh_account_base.group_eh_manager')
        invisible = button.attrib.get('invisible')
        if invisible is None:
            invisible = button.attrib.get('attrs', '')
        self.assertIn('is_resolved', invisible)
        self.assertTrue(button.attrib.get('confirm'))

    def test_listing_and_template_match_live_evidence_contract(self):
        root = Path(get_module_path('eh_account_ai_collections'))
        manifest = ast.literal_eval((root / '__manifest__.py').read_text())
        listing = (root / 'static/description/index.html').read_text()
        template = (
            root / 'i18n/eh_account_ai_collections.pot'
        ).read_text()

        self.assertIn('v%s' % manifest['version'], listing)
        self.assertIn('No duplicate provider charge', listing)
        self.assertIn('stored and filterable', listing)
        self.assertNotIn('silently returns', listing)
        self.assertNotIn('No stored suggestions', listing)
        self.assertIn('AI narrative source fingerprint', template)
        self.assertIn('Treat as demand letter', template)
