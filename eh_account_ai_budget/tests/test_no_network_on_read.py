# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Regression: the budget-variance commentary compute must never invoke the
AI provider, because a live provider performs a blocking outbound HTTPS
POST and a store=False compute fires on every form/list read. The
provider is allowed only on the explicit Refresh AI commentary button,
which writes the stored eh_ai_variance_narrative field.

The test registers a sentinel non-manual provider whose chat() records
that it was called (and returns a marker). Reading the compute field with
that provider configured on the company must NOT call chat(); pressing the
button MUST call it and store the marker.
"""

from datetime import date

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tools.misc import format_amount

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_ai_agent.tools import provider_registry


_CALLS = []
_SENTINEL_KEY = 'eh_test_sentinel_budget'
_MARKER = "SENTINEL-LLM-NARRATIVE"


class _SentinelProvider:
    """Non-manual provider that flags any chat() invocation.

    A chat() call from the read path is the exact defect under test:
    it means the compute reached the network layer.
    """

    is_manual = False

    def __init__(self, config):
        self.config = config or {}

    def chat(self, messages, **kwargs):
        _CALLS.append(messages)
        return _MARKER


@tagged('eh_account_ai_budget', 'post_install', '-at_install')
class TestNoNetworkOnRead(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._sentinel_provider_scope = provider_registry.temporary_provider(
            _SENTINEL_KEY, _SentinelProvider,
        )
        cls._sentinel_provider_scope.__enter__()
        # Configure the company to use the live (non-manual) provider so
        # the pre-fix compute would have called chat() on read.
        cls.company.eh_ai_provider_key = _SENTINEL_KEY
        cls.company.eh_ai_provider_config = '{"api_key": "x", "model": "y"}'

    @classmethod
    def tearDownClass(cls):
        try:
            cls._sentinel_provider_scope.__exit__(None, None, None)
        finally:
            super().tearDownClass()

    def setUp(self):
        super().setUp()
        _CALLS.clear()

    def _budget(self):
        today = fields.Date.context_today(self.env['res.users'])
        date_from = date(today.year, 1, 1)
        date_to = date(today.year, 12, 31)
        return self.env['eh.budget.budget'].create({
            'name': "Sentinel Budget",
            'code': 'SENTBUD',
            'company_id': self.company.id,
            'date_from': date_from,
            'date_to': date_to,
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'period_from': date_from, 'period_to': date_to,
                    'budgeted_amount': 10000.0,
                }),
                (0, 0, {
                    'account_id': self.account_expense.id,
                    'period_from': date_from, 'period_to': date_to,
                    'budgeted_amount': 5000.0,
                }),
            ],
        })

    def test_read_does_not_call_provider(self):
        """Reading the computed field with a live provider configured
        must render the deterministic template with zero chat() calls."""
        budget = self._budget()
        budget.invalidate_recordset(['eh_ai_variance_commentary'])
        text = budget.eh_ai_variance_commentary
        self.assertTrue(text, "compute must still render deterministic text")
        self.assertEqual(
            _CALLS, [],
            "read path invoked the AI provider (network I/O in compute)",
        )
        self.assertNotIn(
            _MARKER, text,
            "compute field must not contain provider output",
        )

    def test_recompute_on_line_change_still_no_call(self):
        """Editing a line marks the field dirty; the next read recompute
        must still be deterministic-only (no provider call)."""
        budget = self._budget()
        budget.eh_ai_variance_commentary  # prime
        _CALLS.clear()
        budget.line_ids.filtered(
            lambda line: line.account_id == self.account_revenue
        ).budgeted_amount = 88888.0
        budget.invalidate_recordset(['eh_ai_variance_commentary'])
        text = budget.eh_ai_variance_commentary
        self.assertIn(
            format_amount(self.env, 83888.0, budget.currency_id), text,
        )
        self.assertEqual(
            _CALLS, [],
            "recompute after a line edit invoked the AI provider",
        )

    def test_button_invokes_provider_and_stores_narrative(self):
        """The explicit button is the ONLY place the provider runs; it
        writes the enriched narrative to the stored field."""
        budget = self._budget()
        self.assertFalse(budget.eh_ai_variance_narrative)
        budget.action_eh_ai_refresh_commentary()
        self.assertEqual(
            len(_CALLS), 1,
            "button must invoke the configured provider exactly once",
        )
        self.assertEqual(budget.eh_ai_variance_narrative, _MARKER)
        self.assertEqual(
            budget.eh_ai_narrative_provider_key, _SENTINEL_KEY)
        self.assertEqual(len(budget.eh_ai_narrative_fingerprint), 64)
        self.assertTrue(budget.eh_ai_narrative_generated_at)
        self.assertEqual(budget.eh_ai_narrative_generated_by_id, self.env.user)
        self.assertFalse(budget.eh_ai_narrative_stale)
        self.assertIn('BEGIN_UNTRUSTED_BUDGET_DATA',
                      _CALLS[0][1]['content'])
        self.assertIn('Raw snapshot JSON', _CALLS[0][1]['content'])
        # The stored narrative persists across reads without re-calling.
        _CALLS.clear()
        budget.invalidate_recordset(['eh_ai_variance_narrative'])
        self.assertEqual(budget.eh_ai_variance_narrative, _MARKER)
        self.assertEqual(
            _CALLS, [],
            "reading the stored narrative re-invoked the provider",
        )

        budget.action_eh_ai_refresh_commentary()
        self.assertEqual(
            _CALLS, [],
            "unchanged fingerprint must not rebill the provider",
        )

        budget.line_ids[0].budgeted_amount += 1.0
        budget.invalidate_recordset(['eh_ai_narrative_stale'])
        self.assertTrue(budget.eh_ai_narrative_stale)

    def test_ai_narrative_evidence_cannot_be_forged_by_rpc(self):
        budget = self._budget()
        with self.assertRaises(AccessError):
            budget.write({
                'eh_ai_variance_narrative': 'forged provider output',
                'eh_ai_narrative_fingerprint': '0' * 64,
            })

    def test_unauthorised_button_fails_before_provider_call(self):
        budget = self._budget()
        outsider = self.env['res.users'].create({
            'name': 'Budget AI Outsider',
            'login': 'budget_ai_outsider_test',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            budget.with_user(outsider).action_eh_ai_refresh_commentary()
        self.assertEqual(_CALLS, [])

    def test_manual_provider_never_stores_deterministic_text_as_ai(self):
        budget = self._budget()
        budget.action_eh_ai_refresh_commentary()
        self.assertEqual(budget.eh_ai_variance_narrative, _MARKER)
        self.company.eh_ai_provider_key = 'manual'
        _CALLS.clear()

        with self.assertRaisesRegex(UserError, 'No AI narrative was stored'):
            budget.action_eh_ai_refresh_commentary()

        self.assertEqual(
            budget.eh_ai_variance_narrative,
            _MARKER,
        )

    def test_failed_provider_preserves_existing_ai_narrative(self):
        class _FailedProvider:
            is_manual = False

            def __init__(self, config):
                pass

            def chat(self, messages, **kwargs):
                raise provider_registry.ProviderError('provider unavailable')

        budget = self._budget()
        budget.action_eh_ai_refresh_commentary()
        self.assertEqual(budget.eh_ai_variance_narrative, _MARKER)
        budget.line_ids[0].budgeted_amount += 1.0
        budget.invalidate_recordset(['eh_ai_narrative_stale'])
        self.assertTrue(budget.eh_ai_narrative_stale)
        _CALLS.clear()

        with provider_registry.temporary_provider(
            _SENTINEL_KEY, _FailedProvider,
        ):
            with self.assertRaisesRegex(UserError, 'provider unavailable'):
                budget.action_eh_ai_refresh_commentary()

        self.assertEqual(
            budget.eh_ai_variance_narrative,
            _MARKER,
        )
        self.assertIsInstance(
            provider_registry.get_provider(_SENTINEL_KEY, {}),
            _SentinelProvider,
        )
