# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Live anomaly-wiring integration tests.

These prove the deterministic detector is actually reachable inside
Odoo: posted journal entries get scanned, findings are persisted
against the move, the scan is idempotent, the evidence is immutable,
and the scheduled cron surfaces an outlier unattended.
"""

import ast
import datetime
import importlib.util
from pathlib import Path
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged
from odoo.modules.module import get_module_path

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_ai_agent.models import account_move
from odoo.addons.eh_account_ai_agent.tools.provider_registry import (
    ProviderError, register_provider,
)


@tagged('eh_account_ai_agent', 'integration', 'post_install', '-at_install')
class TestAnomalyLive(EhAccountIntegrationTestCase):

    WEEKDAY = '2026-04-15'   # Wednesday
    WEEKEND = '2026-01-03'   # Saturday

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The integration fixture uses a dedicated accounting user. Promote it
        # explicitly so existing positive-path scans exercise the manager gate;
        # the negative cases below use separately provisioned users.
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )
        cls.eh_user = new_test_user(
            cls.env,
            login='eh_anomaly_security_user',
            groups='eh_account_base.group_eh_user',
        )
        cls.auditor = new_test_user(
            cls.env,
            login='eh_anomaly_readonly_auditor',
            groups='eh_account_base.group_eh_auditor',
        )
        cls.scanner = new_test_user(
            cls.env,
            login='eh_anomaly_designated_scanner',
            groups='eh_account_ai_agent.group_eh_ai_scanner',
        )
        cls.reviewer = new_test_user(
            cls.env,
            login='eh_anomaly_designated_reviewer',
            groups='eh_account_ai_agent.group_eh_ai_reviewer',
        )

    def _entry(
        self, amount, date, debit_account=None, credit_account=None,
        posted_at=None,
    ):
        debit_account = debit_account or self.account_expense
        credit_account = credit_account or self.account_revenue
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': date,
            'line_ids': [
                (0, 0, {'name': 'D', 'account_id': debit_account.id,
                        'debit': amount, 'credit': 0.0}),
                (0, 0, {'name': 'C', 'account_id': credit_account.id,
                        'debit': 0.0, 'credit': amount}),
            ],
        })
        accounting_date = fields.Date.to_date(date)
        posted_at = posted_at or datetime.datetime.combine(
            accounting_date, datetime.time(12, 0),
        )
        with patch.object(
                account_move.fields.Datetime, 'now', return_value=posted_at):
            move.action_post()
        return move

    def _findings(self, move, rule=None):
        findings = move.eh_anomaly_finding_ids
        if rule:
            findings = findings.filtered(lambda f: f.rule == rule)
        return findings

    # ---- rule coverage ----

    def test_round_outlier_flagged(self):
        base = (
            self._entry(100.0, self.WEEKDAY)
            + self._entry(200.0, self.WEEKDAY)
            + self._entry(300.0, self.WEEKDAY)
        )
        outlier = self._entry(60000.0, self.WEEKDAY)
        # The baseline comes from the posted journal/month population, not
        # from whichever records happened to be selected for this scan.
        outlier.eh_scan_anomalies()
        self.assertTrue(
            self._findings(outlier, 'round_outlier_v2'),
            "round-number outlier must be flagged",
        )
        for m in base:
            self.assertFalse(
                self._findings(m, 'round_outlier_v2'),
                "baseline entries must not be flagged as outliers",
            )

    def test_weekend_post_flagged(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        self.assertTrue(self._findings(move, 'weekend_post_v2'))

    def test_weekend_accounting_date_posted_on_weekday_is_not_flagged(self):
        move = self._entry(
            125.0,
            self.WEEKEND,
            posted_at=datetime.datetime(2026, 1, 5, 9, 0),
        )

        move.eh_scan_anomalies()

        self.assertFalse(self._findings(move, 'weekend_post_v2'))

    def test_weekend_post_uses_posters_timezone_at_utc_boundary(self):
        self.env.user.tz = 'Australia/Melbourne'
        move = self._entry(
            125.0,
            '2026-01-02',  # Friday accounting date
            # Friday UTC, but Saturday morning for the posting user.
            posted_at=datetime.datetime(2026, 1, 2, 18, 0),
        )

        move.eh_scan_anomalies()

        self.assertTrue(self._findings(move, 'weekend_post_v2'))

    def test_company_weekend_configuration_controls_live_scan(self):
        self.company.write({
            'eh_ai_anomaly_use_company_calendar': False,
            'eh_ai_anomaly_weekend_days': '4,5',
        })
        friday = self._entry(125.0, '2026-01-02')
        sunday = self._entry(150.0, '2026-01-04')

        (friday + sunday).eh_scan_anomalies()

        self.assertTrue(self._findings(friday, 'weekend_post_v2'))
        self.assertFalse(self._findings(sunday, 'weekend_post_v2'))

    def test_invalid_weekend_configuration_fails_when_saved(self):
        for value in ('Friday,Saturday', '4,,5', '7', '-1'):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.company.eh_ai_anomaly_weekend_days = value

    def test_just_under_threshold_flagged(self):
        self.company.eh_ai_anomaly_approval_threshold = 10000.0
        move = self._entry(9700.0, self.WEEKDAY)
        move.eh_scan_anomalies()
        self.assertTrue(self._findings(move, 'just_under_threshold'))

    def test_threshold_disabled_by_default(self):
        # Default threshold 0 means the structuring rule never fires.
        self.company.eh_ai_anomaly_approval_threshold = 0.0
        move = self._entry(9700.0, self.WEEKDAY)
        move.eh_scan_anomalies()
        self.assertFalse(self._findings(move, 'just_under_threshold'))

    def test_reversal_pair_flagged(self):
        a = self._entry(500.0, self.WEEKDAY)
        b = self._entry(500.0, self.WEEKDAY)
        (a + b).eh_scan_anomalies()
        self.assertFalse(self._findings(a, 'reversal_pair_v2'))
        self.assertFalse(self._findings(b, 'reversal_pair_v2'))

    def test_exact_opposing_manual_pair_flagged(self):
        a = self._entry(500.0, self.WEEKDAY)
        b = self._entry(
            500.0, self.WEEKDAY,
            debit_account=self.account_revenue,
            credit_account=self.account_expense,
        )

        (a + b).eh_scan_anomalies()

        self.assertTrue(self._findings(a, 'reversal_pair_v2'))
        self.assertTrue(self._findings(b, 'reversal_pair_v2'))

    def test_odoo_linked_reversal_is_not_an_anomaly(self):
        original = self._entry(500.0, self.WEEKDAY)
        reversal = original._reverse_moves([{'date': self.WEEKDAY}])
        reversal.action_post()

        (original + reversal).eh_scan_anomalies()

        self.assertEqual(reversal.reversed_entry_id, original)
        self.assertFalse(self._findings(original, 'reversal_pair_v2'))
        self.assertFalse(self._findings(reversal, 'reversal_pair_v2'))

    def test_draft_moves_not_scanned(self):
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': self.WEEKEND,
            'line_ids': [
                (0, 0, {'name': 'D', 'account_id': self.account_expense.id,
                        'debit': 125.0, 'credit': 0.0}),
                (0, 0, {'name': 'C', 'account_id': self.account_revenue.id,
                        'debit': 0.0, 'credit': 125.0}),
            ],
        })
        # left in draft on purpose
        move.eh_scan_anomalies()
        self.assertFalse(move.eh_anomaly_finding_ids)

    # ---- persistence behaviour ----

    def test_rescan_is_idempotent(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        count = len(move.eh_anomaly_finding_ids)
        self.assertTrue(count)
        move.eh_scan_anomalies()
        move.invalidate_recordset(['eh_anomaly_finding_ids'])
        self.assertEqual(
            len(move.eh_anomaly_finding_ids), count,
            "re-scan must not duplicate findings",
        )

    def test_rescan_withdraws_and_reactivates_changed_rule_evidence(self):
        self.company.eh_ai_anomaly_approval_threshold = 10000.0
        move = self._entry(9700.0, self.WEEKDAY)
        move.eh_scan_anomalies()
        finding = self._findings(move, 'just_under_threshold')
        self.assertEqual(finding.state, 'open')

        self.company.eh_ai_anomaly_approval_threshold = 0.0
        move.eh_scan_anomalies()
        self.assertEqual(finding.state, 'withdrawn')
        self.assertEqual(finding.review_event_ids.action, 'withdrawn')

        self.company.eh_ai_anomaly_approval_threshold = 10000.0
        move.eh_scan_anomalies()
        self.assertEqual(finding.state, 'open')
        self.assertEqual(
            set(finding.review_event_ids.mapped('action')),
            {'withdrawn', 'reactivated'},
        )

    def test_count_compute(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        move.invalidate_recordset(['eh_anomaly_count', 'eh_anomaly_open_count'])
        self.assertEqual(move.eh_anomaly_count, 1)
        self.assertEqual(move.eh_anomaly_open_count, 1)
        finding = move.eh_anomaly_finding_ids.with_user(self.reviewer)
        finding.review_note = "False positive confirmed"
        finding.action_dismiss()
        move.invalidate_recordset(['eh_anomaly_open_count'])
        self.assertEqual(move.eh_anomaly_open_count, 0)

    def test_evidence_immutable_triage_editable(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        finding = move.eh_anomaly_finding_ids[:1]
        self.assertTrue(finding)
        user_finding = finding.with_user(self.eh_user)
        with self.assertRaises(AccessError):
            user_finding.description = "tampered"
        with self.assertRaises(AccessError):
            user_finding.amount = 1.0
        with self.assertRaises(AccessError):
            user_finding.write({'company_id': self.company.id})
        with self.assertRaises(AccessError):
            user_finding.review_note = "unauthorized review"
        reviewer_finding = finding.with_user(self.reviewer)
        reviewer_finding.review_note = "looked into it"
        reviewer_finding.action_mark_reviewed()
        self.assertEqual(finding.state, 'reviewed')
        self.assertEqual(finding.reviewed_by_id, self.reviewer)
        with self.assertRaises(AccessError):
            move.write({'eh_anomaly_posted_by_id': self.eh_user.id})

    def test_readonly_auditor_cannot_trigger_scan_side_effects(self):
        move = self._entry(125.0, self.WEEKEND)
        with self.assertRaises(AccessError):
            move.with_user(self.auditor).eh_scan_anomalies()
        self.assertFalse(move.eh_anomaly_finding_ids)

    def test_ordinary_eh_user_cannot_trigger_scan_side_effects(self):
        move = self._entry(125.0, self.WEEKEND)
        with self.assertRaises(AccessError):
            move.with_user(self.eh_user).eh_scan_anomalies()
        self.assertFalse(move.eh_anomaly_finding_ids)

    def test_designated_scanner_can_run_bounded_scan(self):
        move = self._entry(125.0, self.WEEKEND)
        move.with_user(self.scanner).eh_scan_anomalies()
        self.assertTrue(self._findings(move, 'weekend_post_v2'))

    def test_designated_scanner_cannot_cross_company_boundary(self):
        other_company = self.env['res.company'].create({
            'name': 'EH Anomaly Isolated Company',
        })
        other_env = self.env['res.company'].with_company(
            other_company,
        ).with_context(
            allowed_company_ids=[other_company.id],
        ).env
        other_expense = self._ensure_account(
            other_env, 'EH991001', 'Other AI Expense', 'expense',
        )
        other_revenue = self._ensure_account(
            other_env, 'EH991002', 'Other AI Revenue', 'income',
        )
        other_journal = self._ensure_journal(
            other_env, other_company, 'general', 'EHAI',
            'Other AI Journal',
        )
        other_move = other_env['account.move'].create({
            'move_type': 'entry',
            'company_id': other_company.id,
            'journal_id': other_journal.id,
            'date': self.WEEKEND,
            'line_ids': [
                (0, 0, {
                    'name': 'D', 'account_id': other_expense.id,
                    'debit': 125.0,
                }),
                (0, 0, {
                    'name': 'C', 'account_id': other_revenue.id,
                    'credit': 125.0,
                }),
            ],
        })
        other_move.action_post()
        own_move = self._entry(125.0, self.WEEKEND)

        own_move.with_user(self.scanner).eh_scan_anomalies()
        self.assertTrue(self._findings(own_move, 'weekend_post_v2'))
        with self.assertRaises(AccessError):
            other_move.with_user(self.scanner).with_context(
                allowed_company_ids=[self.company.id],
            ).eh_scan_anomalies()
        self.assertFalse(other_move.eh_anomaly_finding_ids)

    def test_manual_selection_limit_is_enforced_before_scan(self):
        moves = (
            self._entry(125.0, self.WEEKEND)
            + self._entry(150.0, self.WEEKEND)
        )
        with patch.object(account_move, 'MAX_MANUAL_SCAN_MOVES', 1):
            with self.assertRaises(UserError):
                moves.eh_scan_anomalies()
        self.assertFalse(moves.mapped('eh_anomaly_finding_ids'))

    def test_provider_call_is_locked_bounded_and_cached(self):
        calls = []

        class CachedProvider:
            is_manual = False

            def __init__(self, config):
                self.config = config

            def chat(self, messages, **kwargs):
                calls.append(messages)
                return '[]'

        register_provider('eh_test_anomaly_cache', CachedProvider)
        self.company.write({
            'eh_ai_provider_key': 'eh_test_anomaly_cache',
            'eh_ai_provider_config': '{"api_key":"never-persist-this"}',
        })
        moves = (
            self._entry(125.0, self.WEEKEND)
            + self._entry(150.0, self.WEEKEND)
        )
        queries = []
        real_execute = self.env.cr.execute

        def record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(account_move, 'MAX_PROVIDER_MOVES_PER_CALL', 1), \
                patch.object(self.env.cr, 'execute', record_execute):
            moves.eh_scan_anomalies()
            moves.eh_scan_anomalies()

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            self.env['eh.account.anomaly.scan.cache'].search_count([
                ('company_id', '=', self.company.id),
                ('provider_key', '=', 'eh_test_anomaly_cache'),
            ]),
            2,
        )
        self.assertTrue(any(
            'pg_advisory_xact_lock' in query for query in queries
        ))
        cache_rows = self.env['eh.account.anomaly.scan.cache'].search([
            ('provider_key', '=', 'eh_test_anomaly_cache'),
        ])
        self.assertNotIn(
            'never-persist-this', ' '.join(cache_rows.mapped('fingerprint')),
        )

    def test_provider_cache_is_per_entry_not_sliding_chunk(self):
        calls = []

        class CachedProvider:
            is_manual = False

            def __init__(self, config):
                pass

            def chat(self, messages, **kwargs):
                calls.append(messages)
                return '[]'

        register_provider('eh_test_anomaly_incremental', CachedProvider)
        self.company.eh_ai_provider_key = 'eh_test_anomaly_incremental'
        first = (
            self._entry(125.0, self.WEEKEND)
            + self._entry(150.0, self.WEEKEND)
        )
        first.eh_scan_anomalies()
        third = self._entry(175.0, self.WEEKEND)

        (first + third).eh_scan_anomalies()

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            self.env['eh.account.anomaly.scan.cache'].search_count([
                ('provider_key', '=', 'eh_test_anomaly_incremental'),
            ]),
            3,
        )

    def test_failed_provider_call_is_not_cached(self):
        calls = []

        class FailedProvider:
            is_manual = False

            def __init__(self, config):
                pass

            def chat(self, messages, **kwargs):
                calls.append(True)
                raise ProviderError("test failure")

        register_provider('eh_test_anomaly_failure', FailedProvider)
        self.company.eh_ai_provider_key = 'eh_test_anomaly_failure'
        move = self._entry(125.0, self.WEEKEND)

        move.eh_scan_anomalies()
        move.eh_scan_anomalies()

        self.assertEqual(len(calls), 2)
        self.assertFalse(self.env['eh.account.anomaly.scan.cache'].search([
            ('provider_key', '=', 'eh_test_anomaly_failure'),
        ]))

    def test_provider_finding_is_bounded_and_marked_as_ai_evidence(self):
        class EvidenceProvider:
            is_manual = False

            def __init__(self, config):
                pass

            def chat(self, messages, **kwargs):
                return (
                    '[{"move_id": %d, "severity": "high", '
                    '"description": "%s"}]'
                    % (move.id, 'x' * 1200)
                )

        register_provider('eh_test_anomaly_evidence', EvidenceProvider)
        self.company.eh_ai_provider_key = 'eh_test_anomaly_evidence'
        move = self._entry(125.0, self.WEEKDAY)

        move.eh_scan_anomalies()

        finding = self._findings(move, 'llm_flag')
        self.assertEqual(finding.source, 'provider')
        self.assertEqual(finding.provider_key, 'eh_test_anomaly_evidence')
        self.assertEqual(len(finding.description), 1000)
        self.assertEqual(len(finding.provider_evidence_hash), 64)

    def test_finding_creation_review_provenance_and_unlink_are_guarded(self):
        move = self._entry(125.0, self.WEEKEND)
        with self.assertRaises(AccessError):
            self.env['eh.account.anomaly.finding'].with_user(
                self.eh_user
            ).create({
                'move_id': move.id,
                'rule': 'forged',
                'severity': 'high',
                'description': 'forged evidence',
            })
        with self.assertRaises(AccessError):
            self.env['eh.account.anomaly.finding'].sudo().with_context(
                eh_anomaly_engine_capability=True,
            ).create({
                'move_id': move.id,
                'rule': 'forged_sudo_context',
                'severity': 'high',
                'description': 'still forged evidence',
            })

        move.eh_scan_anomalies()
        finding = move.eh_anomaly_finding_ids[:1]
        self.assertTrue(finding)
        for vals in (
            {'state': 'dismissed'},
            {'reviewed_by_id': self.env.user.id},
            {'reviewed_at': fields.Datetime.now()},
            {'detected_at': fields.Datetime.now()},
        ):
            with self.assertRaises(AccessError):
                finding.with_user(self.eh_user).write(vals)
        with self.assertRaises(AccessError):
            finding.with_user(self.eh_user).action_dismiss()
        reviewer_finding = finding.with_user(self.reviewer)
        reviewer_finding.review_note = "Not an exception"
        reviewer_finding.action_dismiss()
        self.assertEqual(finding.state, 'dismissed')
        self.assertEqual(finding.reviewed_by_id, self.reviewer)
        with self.assertRaises(UserError):
            finding.sudo().unlink()

    def test_move_delete_cannot_cascade_immutable_anomaly_evidence(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        finding = move.eh_anomaly_finding_ids[:1]
        reviewer_finding = finding.with_user(self.reviewer)
        reviewer_finding.review_note = "Reviewed evidence must survive"
        reviewer_finding.action_dismiss()
        self.assertTrue(finding.review_event_ids)

        move.button_draft()
        with self.assertRaisesRegex(UserError, 'immutable anomaly evidence'):
            move.unlink()
        self.assertTrue(finding.exists())
        self.assertTrue(finding.review_event_ids.exists())

    def test_poster_cannot_self_review(self):
        move = self._entry(125.0, self.WEEKEND)
        move.eh_scan_anomalies()
        finding = move.eh_anomaly_finding_ids[:1]
        self.assertEqual(move.eh_anomaly_posted_by_id, self.env.user)
        self.assertEqual(finding.detected_by_id, self.env.user)

        with self.assertRaises(UserError):
            finding.action_mark_reviewed()

        finding.with_user(self.reviewer).action_mark_reviewed()
        self.assertEqual(finding.reviewed_by_id, self.reviewer)
        event = finding.review_event_ids
        self.assertEqual(len(event), 1)
        self.assertEqual(event.action, 'reviewed')
        self.assertEqual(event.actor_id, self.reviewer)
        with self.assertRaises(AccessError):
            event.with_user(self.reviewer).write({'note': 'rewritten'})
        with self.assertRaises(UserError):
            event.sudo().unlink()

    def test_authorized_scanner_may_review_when_not_the_poster(self):
        self.scanner.group_ids |= self.env.ref(
            'eh_account_ai_agent.group_eh_ai_reviewer',
        )
        move = self._entry(125.0, self.WEEKEND)
        move.with_user(self.scanner).eh_scan_anomalies()
        finding = move.eh_anomaly_finding_ids[:1]
        self.assertEqual(finding.detected_by_id, self.scanner)

        finding.with_user(self.scanner).action_mark_reviewed()

        self.assertEqual(finding.reviewed_by_id, self.scanner)

    def test_public_cron_entrypoint_rejects_ordinary_user(self):
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(
                self.eh_user
            )._cron_scan_anomalies()

    def test_cron_failure_log_includes_bounded_diagnostic_message(self):
        MoveClass = type(self.env['account.move'])
        with patch.object(
            MoveClass,
            'eh_scan_anomalies',
            side_effect=RuntimeError('specific diagnostic\nsecond line'),
        ), patch.object(account_move._logger, 'warning') as warning:
            self.env['account.move']._cron_scan_anomalies()
        self.assertTrue(warning.called)
        self.assertTrue(any(
            call.args[-1] == 'specific diagnostic second line'
            for call in warning.call_args_list
        ))

    def test_upgrade_preserves_and_supersedes_legacy_evidence(self):
        move = self._entry(125.0, self.WEEKEND)
        Finding = self.env['eh.account.anomaly.finding'].sudo().with_context(
            **{
                account_move.ANOMALY_ENGINE_CONTEXT:
                    account_move.ANOMALY_ENGINE_CAPABILITY,
            }
        )
        legacy = Finding.create({
            'move_id': move.id,
            'rule': 'weekend_post',
            'severity': 'low',
            'description': 'legacy bytes must survive unchanged',
            'date': move.date,
        })
        provider = Finding.create({
            'move_id': move.id,
            'rule': 'llm_flag',
            'severity': 'medium',
            'description': 'legacy provider bytes',
            'date': move.date,
        })
        path = Path(get_module_path('eh_account_ai_agent')) / (
            'migrations/19.0.1.3.0/post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_ai_agent_post_1_3_0', path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        migration.migrate(self.env.cr, '19.0.1.2.5')
        legacy.invalidate_recordset()
        provider.invalidate_recordset()

        self.assertEqual(legacy.state, 'withdrawn')
        self.assertEqual(
            legacy.description, 'legacy bytes must survive unchanged',
        )
        self.assertEqual(legacy.review_event_ids.action, 'withdrawn')
        self.assertEqual(provider.source, 'provider')
        self.assertEqual(provider.provider_key, 'legacy_unknown')
        self.assertEqual(provider.description, 'legacy provider bytes')

    def test_listing_matches_bounded_stored_workflow(self):
        root = Path(get_module_path('eh_account_ai_agent'))
        manifest = ast.literal_eval((root / '__manifest__.py').read_text())
        listing = (root / 'static/description/index.html').read_text()

        self.assertIn('v%s' % manifest['version'], listing)
        self.assertIn('up to 200 selected posted entries', listing)
        self.assertIn('up to 500 recent entries per company', listing)
        self.assertIn('Findings are stored against journal entries', listing)
        self.assertNotIn('4,000 posted entries', listing)
        self.assertNotIn('Results are not logged, persisted', listing)

    # ---- cron ----

    def test_cron_surfaces_outlier(self):
        today = fields.Date.context_today(self.env['res.users'])
        # A few small baseline entries plus a very large round outlier,
        # all dated today so the default 30-day lookback catches them.
        self._entry(100.0, today)
        self._entry(200.0, today)
        self._entry(300.0, today)
        outlier = self._entry(9999000.0, today)
        self.env['account.move']._cron_scan_anomalies()
        outlier.invalidate_recordset(['eh_anomaly_finding_ids'])
        self.assertTrue(
            self._findings(outlier, 'round_outlier_v2'),
            "scheduled scan must flag the outlier",
        )
