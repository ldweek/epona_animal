# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Anomaly detector tests (deterministic + LLM-fallback safety).
"""

import datetime
from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_ai_agent.tools.anomaly_detector import (
    detect, JournalEntrySummary,
)


def _make_entries():
    """Five entries: median is around 175."""
    return [
        JournalEntrySummary(
            id=1, date=datetime.date(2026, 6, 1),
            amount=100.0, posted_by='alice',
            journal_id=1, account_codes=('5100',),
        ),
        JournalEntrySummary(
            id=2, date=datetime.date(2026, 6, 2),
            amount=200.0, posted_by='alice', journal_id=1,
        ),
        JournalEntrySummary(
            id=3, date=datetime.date(2026, 6, 7),  # Sunday
            amount=150.0, posted_by='bob', journal_id=1,
            posted_at=datetime.datetime(2026, 6, 7, 10, 0),
        ),
        JournalEntrySummary(
            id=4, date=datetime.date(2026, 6, 4),
            amount=10000.0, posted_by='alice', journal_id=1,
            # round outlier
        ),
        JournalEntrySummary(
            id=5, date=datetime.date(2026, 6, 5),
            amount=4900.0, posted_by='bob', journal_id=1,
            # just under 5000
        ),
    ]


@tagged('post_install', '-at_install')
class AnomalyDetectorTest(TransactionCase):

    def test_round_outlier_flagged(self):
        findings = detect(_make_entries(), approval_threshold=0)
        rules = {f.rule for f in findings}
        self.assertIn('round_outlier_v2', rules)

    def test_weekend_post_flagged(self):
        findings = detect(_make_entries(), approval_threshold=0)
        weekend = [f for f in findings if f.rule == 'weekend_post_v2']
        self.assertGreater(len(weekend), 0)

    def test_weekend_days_are_caller_configurable(self):
        friday = JournalEntrySummary(
            id=101,
            date=datetime.date(2026, 6, 5),
            posted_at=datetime.datetime(2026, 6, 5, 10, 0),
            amount=100.0,
        )
        sunday = JournalEntrySummary(
            id=102,
            date=datetime.date(2026, 6, 7),
            posted_at=datetime.datetime(2026, 6, 7, 10, 0),
            amount=100.0,
        )
        flagged = {
            finding.move_id for finding in detect(
                [friday, sunday], weekend_days=(4, 5),
            )
            if finding.rule == 'weekend_post_v2'
        }
        self.assertEqual(flagged, {101})

    def test_just_under_threshold_flagged(self):
        findings = detect(_make_entries(), approval_threshold=5000.0)
        rules = {f.rule for f in findings}
        self.assertIn('just_under_threshold', rules)

    def test_threshold_zero_disables_rule(self):
        findings = detect(_make_entries(), approval_threshold=0)
        rules = {f.rule for f in findings}
        self.assertNotIn('just_under_threshold', rules)

    def test_reversal_pair_flagged(self):
        # Only exact signed opposing vectors count. Same-direction duplicates
        # do not become reversal candidates merely because amounts match.
        same_day = datetime.date(2026, 6, 10)
        entries = [
            JournalEntrySummary(
                id=10, date=same_day, amount=500.0,
                posted_by='alice', journal_id=3,
                account_codes=('1100', '5200'),
                account_balances=(('1100', 500.0), ('5200', -500.0)),
            ),
            JournalEntrySummary(
                id=11, date=same_day, amount=500.0,
                posted_by='alice', journal_id=3,
                account_codes=('5200', '1100'),
                account_balances=(('1100', -500.0), ('5200', 500.0)),
            ),
            JournalEntrySummary(
                id=12, date=same_day, amount=300.0,
                posted_by='bob',
            ),
        ]
        findings = detect(entries, approval_threshold=0)
        reversal = [f for f in findings if f.rule == 'reversal_pair_v2']
        self.assertEqual(len(reversal), 2)

    def test_same_direction_duplicates_are_not_reversal_pairs(self):
        date = datetime.date(2026, 6, 10)
        vector = (('1100', 500.0), ('5200', -500.0))
        entries = [
            JournalEntrySummary(
                id=20, date=date, amount=500.0, posted_by='alice',
                journal_id=3, account_balances=vector,
            ),
            JournalEntrySummary(
                id=21, date=date, amount=500.0, posted_by='alice',
                journal_id=3, account_balances=vector,
            ),
        ]

        self.assertFalse([
            finding for finding in detect(entries)
            if finding.rule == 'reversal_pair_v2'
        ])

    def test_reversal_pair_matching_is_one_to_one(self):
        date = datetime.date(2026, 6, 10)
        debit = (('1100', 500.0), ('5200', -500.0))
        credit = (('1100', -500.0), ('5200', 500.0))
        entries = [
            JournalEntrySummary(
                id=22, date=date, amount=500.0, posted_by='alice',
                journal_id=3, account_balances=debit,
            ),
            JournalEntrySummary(
                id=23, date=date, amount=500.0, posted_by='alice',
                journal_id=3, account_balances=credit,
            ),
            JournalEntrySummary(
                id=24, date=date, amount=500.0, posted_by='alice',
                journal_id=3, account_balances=credit,
            ),
        ]

        flagged = {
            finding.move_id for finding in detect(entries)
            if finding.rule == 'reversal_pair_v2'
        }

        self.assertEqual(flagged, {22, 23})

    def test_core_reversal_link_is_excluded(self):
        date = datetime.date(2026, 6, 10)
        entries = [
            JournalEntrySummary(
                id=30, date=date, amount=500.0, posted_by='alice',
                journal_id=3,
                account_balances=(('1100', 500.0), ('5200', -500.0)),
            ),
            JournalEntrySummary(
                id=31, date=date, amount=500.0, posted_by='alice',
                journal_id=3, reversed_entry_id=30,
                account_balances=(('1100', -500.0), ('5200', 500.0)),
            ),
        ]

        self.assertFalse([
            finding for finding in detect(entries)
            if finding.rule == 'reversal_pair_v2'
        ])

    def test_weekend_rule_uses_posting_instant_not_accounting_date(self):
        entry = JournalEntrySummary(
            id=40,
            date=datetime.date(2026, 1, 3),  # Saturday accounting date
            posted_at=datetime.datetime(2026, 1, 5, 9, 0),  # Monday post
            amount=100.0,
        )

        self.assertFalse([
            finding for finding in detect([entry])
            if finding.rule == 'weekend_post_v2'
        ])

    def test_round_baseline_does_not_mix_journals(self):
        date = datetime.date(2026, 6, 10)
        entries = [
            JournalEntrySummary(id=50, date=date, amount=1000.0, journal_id=1),
            JournalEntrySummary(id=51, date=date, amount=1000.0, journal_id=1),
            JournalEntrySummary(id=52, date=date, amount=1000.0, journal_id=1),
            JournalEntrySummary(id=53, date=date, amount=10000.0, journal_id=2),
            JournalEntrySummary(id=54, date=date, amount=11000.0, journal_id=2),
            JournalEntrySummary(id=55, date=date, amount=12000.0, journal_id=2),
        ]

        self.assertFalse([
            finding for finding in detect(entries)
            if finding.rule == 'round_outlier_v2'
        ])

    def test_severity_assigned(self):
        findings = detect(_make_entries(), approval_threshold=5000.0)
        for f in findings:
            self.assertIn(f.severity, ('low', 'medium', 'high'))

    def test_empty_input_returns_empty(self):
        self.assertEqual(detect([]), [])

    def test_llm_provider_failure_safe(self):
        """A failing LLM provider must not break the deterministic path."""
        findings_manual = detect(_make_entries(),
                                 approval_threshold=5000.0)
        findings_with_stub = detect(_make_entries(),
                                    approval_threshold=5000.0,
                                    provider_key='claude',
                                    provider_config={'api_key': 'x',
                                                     'model': 'y'})
        # The Claude stub raises ProviderError; the deterministic
        # findings should still flow through identically.
        self.assertEqual(len(findings_manual), len(findings_with_stub))
