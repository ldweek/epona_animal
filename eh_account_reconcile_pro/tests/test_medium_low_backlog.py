# -*- encoding: utf-8 -*-
"""Focused regressions for the medium/low reconciliation audit backlog."""

import ast
from pathlib import Path

from odoo import fields, release
from odoo.exceptions import UserError, ValidationError
from odoo.tests import new_test_user, tagged

from .common import EhReconcileIntegrationTestCase


MODULE_ROOT = Path(__file__).resolve().parents[1]


@tagged('eh_account_reconcile_pro', 'integration', 'post_install', '-at_install')
class TestReconcileMediumLowBacklog(EhReconcileIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = new_test_user(
            cls.env,
            login='eh_reconcile_backlog_operator',
            groups='eh_account_base.group_eh_user',
        )

    def _foreign_currency(self, date='2026-06-15'):
        currency = self.env.ref('base.EUR')
        currency.sudo().active = True
        Rate = self.env['res.currency.rate']
        rate = Rate.search([
            ('currency_id', '=', currency.id),
            ('company_id', '=', self.company.id),
            ('name', '=', date),
        ], limit=1)
        vals = {
            'currency_id': currency.id,
            'company_id': self.company.id,
            'name': date,
            'rate': 0.5,
        }
        if rate:
            rate.rate = vals['rate']
        else:
            Rate.create(vals)
        return currency

    def _foreign_receivable(self, currency, amount_currency, date):
        company_amount = currency._convert(
            amount_currency, self.company.currency_id, self.company, date,
        )
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': date,
            'ref': 'FX-MATCH-001',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                    'name': 'FX-MATCH-001',
                    'debit': company_amount,
                    'credit': 0.0,
                    'currency_id': currency.id,
                    'amount_currency': amount_currency,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'name': 'FX-MATCH-001',
                    'debit': 0.0,
                    'credit': company_amount,
                    'currency_id': currency.id,
                    'amount_currency': -amount_currency,
                }),
            ],
        })
        move.action_post()
        return move.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable
        )

    def test_multicurrency_scoring_payload_and_reclassification(self):
        date = fields.Date.to_date('2026-06-15')
        currency = self._foreign_currency()
        self.bank_journal.currency_id = currency
        statement_line = self.make_statement_line(
            100.0, partner=self.partner_a, date=date,
            payment_ref='FX-MATCH-001',
        )
        aml = self._foreign_receivable(currency, 100.0, date)
        engine = self.env['eh.reconciliation.suggestion.engine']

        self.assertEqual(engine._score_amount(statement_line, aml), 1.0)
        payload = self.env[
            'eh.reconciliation.session'
        ].get_suggestions_for_line(statement_line.id, threshold=0.0)
        candidate = next(row for row in payload if row['aml_id'] == aml.id)
        self.assertEqual(candidate['currency_code'], currency.name)
        self.assertEqual(
            candidate['company_currency_code'], self.company.currency_id.name,
        )
        self.assertAlmostEqual(
            candidate['amount_residual'], aml.amount_residual_currency,
            places=2,
        )
        self.assertAlmostEqual(
            candidate['amount_residual_company'], aml.amount_residual,
            places=2,
        )

        session = self.env['eh.reconciliation.session'].with_user(
            self.operator
        ).open_or_create(self.bank_journal.id)
        result = session.with_user(self.operator).apply_match(
            statement_line.id, aml.ids, source='suggestion',
        )
        self.assertEqual(result['source'], 'suggestion')
        audit = self.env['eh.reconciliation.audit'].search([
            ('statement_line_id', '=', statement_line.id),
            ('decision', '=', 'match'),
        ], limit=1)
        foreign_legs = audit.adjusting_move_id.line_ids.filtered(
            lambda line: line.currency_id == currency
        )
        self.assertEqual(len(foreign_legs), 2)
        self.assertTrue(all(foreign_legs.mapped('amount_currency')))

    def test_suggestion_query_and_regex_work_is_batched(self):
        date = fields.Date.to_date('2026-06-15')
        statement_line = self.make_statement_line(
            100.0, partner=self.partner_a, date=date,
            payment_ref='BATCH-REGEX',
        )
        candidates = self.env['account.move.line']
        for amount in (100.0, 110.0, 120.0):
            candidates |= self.make_open_invoice_line(
                self.partner_a, amount, date=date, ref='BATCH-REGEX',
            )
        self.env['eh.reconciliation.rule'].create({
            'name': 'Batch regex',
            'code': 'batch_regex',
            'company_id': self.company.id,
            'payment_ref_regex': '^BATCH-REGEX$',
            'partner_id': self.partner_a.id,
        })
        RuleClass = type(self.env['eh.reconciliation.rule'])
        AuditClass = type(self.env['eh.reconciliation.audit'])
        original_matches = RuleClass.matches_statement_line
        original_search = AuditClass.search
        calls = {'regex': 0, 'history_search': 0}

        def counted_matches(rule_self, line):
            calls['regex'] += len(rule_self)
            return original_matches(rule_self, line)

        def counted_history_search(audit_self, *args, **kwargs):
            calls['history_search'] += 1
            return original_search(audit_self, *args, **kwargs)

        self.patch(RuleClass, 'matches_statement_line', counted_matches)
        self.patch(AuditClass, 'search', counted_history_search)
        self.env['eh.reconciliation.suggestion.engine'].find_suggestions(
            statement_line, threshold=0.0, candidate_amls=candidates,
        )
        self.assertEqual(calls['regex'], 1)
        self.assertEqual(calls['history_search'], 1)

    def test_unverified_rpc_suggestion_is_downgraded(self):
        statement_line = self.make_statement_line(
            100.0, partner=self.partner_a, date='2026-06-15',
            payment_ref='NO-SUGGESTION',
        )
        aml = self.make_open_invoice_line(
            self.partner_b, 250.0, date='2025-01-01', ref='UNRELATED',
        )
        session = self.env['eh.reconciliation.session'].with_user(
            self.operator
        ).open_or_create(self.bank_journal.id)
        result = session.with_user(self.operator).apply_match(
            statement_line.id, aml.ids, source='suggestion',
        )
        self.assertEqual(result['source'], 'manual')
        audit = self.env['eh.reconciliation.audit'].search([
            ('statement_line_id', '=', statement_line.id),
        ], limit=1)
        self.assertEqual(audit.source, 'manual')

    def test_partial_candidate_and_statements_processed(self):
        statement_line = self.make_statement_line(
            60.0, partner=self.partner_a, payment_ref='PARTIAL-60',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 100.0, ref='PARTIAL-60',
        )
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id,
        )
        session.apply_match(statement_line.id, aml.ids, source='manual')
        aml.invalidate_recordset(['amount_residual', 'reconciled'])
        self.assertFalse(aml.reconciled)
        self.assertAlmostEqual(aml.amount_residual, 40.0, places=2)
        self.assertEqual(session.statements_processed, 1)
        self.assertEqual(
            session._serialize_session(session)['statements_processed'], 1,
        )

    def test_adjustment_rejects_locked_date_before_move_creation(self):
        statement_line = self.make_statement_line(
            100.0, partner=self.partner_a, payment_ref='LOCKED-001',
        )
        aml = self.make_open_invoice_line(
            self.partner_a, 100.0, ref='LOCKED-001',
        )
        session = self.env['eh.reconciliation.session'].open_or_create(
            self.bank_journal.id,
        )
        MoveClass = type(self.env['account.move'])
        lock_date = statement_line.date

        def locked(_move, _date, _has_tax):
            return [(lock_date, 'fiscalyear')]

        self.patch(MoveClass, '_get_violated_lock_dates', locked)
        before = self.env['account.move'].search_count([
            ('journal_id', '=', self.bank_journal.id),
        ])
        with self.assertRaisesRegex(UserError, 'is locked'):
            session.apply_match(statement_line.id, aml.ids, source='manual')
        self.assertEqual(self.env['account.move'].search_count([
            ('journal_id', '=', self.bank_journal.id),
        ]), before)

    def test_workspace_search_direction_dates_and_pagination(self):
        self.make_statement_line(
            100.0, payment_ref='PAGE-ALPHA', date='2026-06-01',
        )
        self.make_statement_line(
            -80.0, payment_ref='PAGE-BETA', date='2026-06-02',
        )
        self.make_statement_line(
            60.0, payment_ref='PAGE-GAMMA', date='2026-06-03',
        )
        Session = self.env['eh.reconciliation.session']
        page = Session.load_workspace(self.bank_journal.id, 0, 2)
        self.assertEqual(page['pagination']['total'], 3)
        self.assertTrue(page['pagination']['has_next'])
        self.assertEqual(len(page['statement_lines']), 2)
        second = Session.load_workspace(self.bank_journal.id, 2, 2)
        self.assertEqual(len(second['statement_lines']), 1)
        self.assertTrue(second['pagination']['has_previous'])
        searched = Session.load_workspace(
            self.bank_journal.id, 0, 100, 'PAGE-BETA',
            '2026-06-01', '2026-06-30', 'out',
        )
        self.assertEqual(searched['pagination']['total'], 1)
        self.assertEqual(
            searched['statement_lines'][0]['payment_ref'], 'PAGE-BETA',
        )

        # Empty OWL select values travel over JSON-RPC as false on supported
        # Odoo series.  Opening a single-journal workspace must therefore load
        # unfiltered data instead of raising the direction validation error.
        unfiltered = Session.load_workspace(
            self.bank_journal.id, 0, 100, '', False, False, False,
        )
        self.assertEqual(unfiltered['pagination']['total'], 3)
        self.assertEqual(
            {line['payment_ref'] for line in unfiltered['statement_lines']},
            {'PAGE-ALPHA', 'PAGE-BETA', 'PAGE-GAMMA'},
        )

    def test_exception_amounts_are_gross_and_currency_bucketed(self):
        currency = self._foreign_currency()
        other_bank = self.env['account.journal'].create({
            'name': 'EUR Exception Bank',
            'code': 'EUEX',
            'type': 'bank',
            'company_id': self.company.id,
            'currency_id': currency.id,
        })
        self.make_statement_line(
            100.0, journal=self.bank_journal, date='2026-06-01',
        )
        self.make_statement_line(
            -80.0, journal=self.bank_journal, date='2026-06-02',
        )
        self.make_statement_line(
            50.0, journal=other_bank, date='2026-06-03',
        )
        wizard = self.env['eh.reconciliation.exception.wizard'].create({
            'date_from': '2026-06-01',
            'date_to': '2026-06-30',
            'journal_ids': [(6, 0, (self.bank_journal | other_bank).ids)],
        })
        data = wizard.compute_exception_data()
        usd_row = next(
            row for row in data['journals']
            if row['currency'] == self.company.currency_id.name
        )
        self.assertEqual(usd_row['unmatched_amount'], 180.0)
        self.assertEqual(usd_row['unmatched_net'], 20.0)
        self.assertIsNone(data['totals']['unmatched_amount'])
        self.assertEqual(
            {row['currency'] for row in
             data['totals']['unmatched_amount_by_currency']},
            {self.company.currency_id.name, currency.name},
        )

    def test_server_evidence_views_and_rule_company_guards_are_wired(self):
        session_arch = (
            MODULE_ROOT / 'views' / 'reconciliation_session_views.xml'
        ).read_text()
        for field_name in (
            'user_id', 'journal_id', 'company_id', 'opened_at', 'closed_at',
            'duration_seconds', 'statements_processed', 'matches_made',
            'matches_via_suggestion', 'matches_manual', 'write_offs', 'skips',
        ):
            self.assertRegex(
                session_arch,
                r'<field name="%s"[^>]*readonly="1"' % field_name,
            )
        statement_view = self.env.ref(
            'eh_account_reconcile_pro.view_bank_statement_list_reconcile_pro'
        )
        self.assertIn('action_view_eh_unmatched_lines', statement_view.arch_db)
        rule_arch = (
            MODULE_ROOT / 'views' / 'reconciliation_rule_views.xml'
        ).read_text()
        self.assertIn('writeoff_amount_type', rule_arch)
        self.assertIn('writeoff_amount', rule_arch)
        self.assertIn("('company_id', '=', company_id)", rule_arch)
        other_company = self.env['res.company'].create({
            'name': 'Rule Scope Other Company',
        })
        other_journal = self.env['account.journal'].create({
            'name': 'Rule Scope Other Bank',
            'code': 'RSOB',
            'type': 'bank',
            'company_id': other_company.id,
        })
        invalid_rule = self.env['eh.reconciliation.rule'].new({
            'name': 'Invalid cross-company rule',
            'code': 'invalid_cross_company_rule',
            'company_id': self.company.id,
            'journal_ids': [(6, 0, other_journal.ids)],
        })
        with self.assertRaisesRegex(ValidationError, 'rule company'):
            invalid_rule._check_rule_company_scope()

    def test_static_workspace_tour_theme_and_listing_contract(self):
        js = (
            MODULE_ROOT / 'static' / 'src' / 'components'
            / 'reconcile_workspace' / 'reconcile_workspace.js'
        ).read_text()
        scss = (
            MODULE_ROOT / 'static' / 'src' / 'components'
            / 'reconcile_workspace' / 'reconcile_workspace.scss'
        ).read_text()
        tour = (
            MODULE_ROOT / 'static' / 'src' / 'js' / 'tours'
            / 'reconcile_tour.js'
        ).read_text()
        listing = (
            MODULE_ROOT / 'static' / 'description' / 'index.html'
        ).read_text()
        session_source = (
            MODULE_ROOT / 'models' / 'reconciliation_session.py'
        ).read_text()
        report_source = (
            MODULE_ROOT / 'wizards' / 'exception_report_wizard.py'
        ).read_text()
        self.assertIn('import { _t }', js)
        self.assertNotIn('this.notification.add("', js)
        self.assertIn('state.pagination', js)
        self.assertNotIn('DM Sans', scss)
        self.assertNotIn('DM Mono', scss)
        self.assertIn('--o-view-background-color', scss)
        self.assertIn('@media (max-width: 520px)', scss)
        self.assertIn(
            '.eh_rec_statement_filter_row { flex-wrap: wrap; }', scss,
        )
        self.assertNotIn('.o_main_navbar', tour)
        self.assertNotIn('run: () => {}', tour)
        self.assertNotIn('\N{EM DASH}', tour)
        self.assertIn(
            'eh_account_reconcile_pro.action_eh_reconcile_workspace', tour,
        )
        # The listing badge must advertise the exact release the manifest
        # ships, on this server series.
        manifest_version = ast.literal_eval((
            MODULE_ROOT / '__manifest__.py'
        ).read_text())['version']
        self.assertTrue(manifest_version.startswith(
            '%s.0.' % release.version_info[0],
        ))
        self.assertIn('v%s' % manifest_version, listing)
        for stale_claim in (
            'set to draft', 'internal context flag', 'fetch caps at 200',
            'no pagination', 'Matches are not applied automatically',
            'few cents short', 'summed totals',
        ):
            self.assertNotIn(stale_claim, listing)
        self.assertNotIn(
            'account_journal WHERE id = %s FOR UPDATE', session_source,
        )
        self.assertIn('pg_advisory_xact_lock', session_source)
        self.assertEqual(report_source.count('def _get_report_values'), 1)
