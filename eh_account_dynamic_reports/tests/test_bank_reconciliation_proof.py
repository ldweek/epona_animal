# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Bank Reconciliation proof handler tests.

Creates a bank journal + statement (balance_start, lines) + an unreconciled
outstanding receipt + an unreconciled outstanding payment and asserts:

* Last-statement balance = balance_start + in-window line amounts.
* Book GL balance = cumulative aml SUM on the journal's bank account.
* Outstanding items are applied to both statement and book cash.  Legitimate
  in-transit receipts/payments therefore explain, rather than manufacture, a
  difference.  A stray bank-GL entry still emits a non-zero balance_check.
* Multi-journal scope yields one section per journal.
* A journal with no statement yields an empty last-statement section
  (book balance only) without raising.
"""

import json

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestBankReconciliationHandler(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.bank_reconciliation']
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'bank_reconciliation')], limit=1)
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'bank_reconciliation',
                'name': 'Bank Reconciliation',
                'handler_model':
                    'eh.account.dynamic.report.handler.bank_reconciliation',
            })

        # Bank account (GL side) and suspense accounts.
        cls.account_bank = cls._ensure_account(
            cls.env, '1010', 'Bank Current Account', 'asset_cash')
        cls.account_inbound = cls._ensure_account(
            cls.env, '1011', 'Outstanding Receipts', 'asset_current')
        cls.account_outbound = cls._ensure_account(
            cls.env, '1012', 'Outstanding Payments', 'asset_current')
        # Outstanding-payment suspense accounts are reconcilable in a real
        # configuration; mark them so amount_residual computes (the handler
        # also falls back to balance, so the proof holds either way).
        (cls.account_inbound | cls.account_outbound).write(
            {'reconcile': True})

        cls.bank_journal = cls.env['account.journal'].create({
            'name': 'Proof Bank',
            'code': 'PBNK',
            'type': 'bank',
            'company_id': cls.company.id,
            'default_account_id': cls.account_bank.id,
        })
        # Pin the suspense accounts on the journal's payment method lines so
        # the outstanding-account accessors return known accounts.
        cls.bank_journal.inbound_payment_method_line_ids[:1].write({
            'payment_account_id': cls.account_inbound.id,
        })
        cls.bank_journal.outbound_payment_method_line_ids[:1].write({
            'payment_account_id': cls.account_outbound.id,
        })

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'journal_ids': [self.bank_journal.id],
        }

    @staticmethod
    def _line_by_id(result, line_id):
        for line in result['lines']:
            if line['id'] == line_id:
                return line
        return None

    @staticmethod
    def _amount(line):
        if line is None:
            return None
        for col in line['columns']:
            if col['expression_label'] == 'amount':
                return col['value']
        return None

    def _post(self, lines, date_str='2026-06-15'):
        return self.post_balanced_move(
            lines, journal=self.bank_journal,
            date=fields.Date.from_string(date_str))

    def _make_statement(self, balance_start, line_amounts, date_str):
        # A statement binds to its journal through its lines (journal_id is
        # a computed field on the statement). When the caller passes no
        # movement lines we still need at least one line on the target
        # journal so the statement is discoverable; a zero-amount line binds
        # the journal and dates the statement without changing the in-window
        # sum or the bank GL balance.
        amounts = list(line_amounts) if line_amounts else [0.0]
        line_ids = [
            (0, 0, {
                'date': fields.Date.from_string(date_str),
                'amount': amt,
                'payment_ref': 'stmt line %s' % amt,
                'journal_id': self.bank_journal.id,
            })
            for amt in amounts
        ]
        return self.env['account.bank.statement'].create({
            'name': 'Stmt %s' % date_str,
            'balance_start': balance_start,
            'line_ids': line_ids,
        })

    def _make_outstanding(self, account, amount, debit=True,
                          date_str='2026-06-20'):
        """Post a balanced move that leaves an unreconciled residual on the
        given suspense account."""
        if debit:
            lines = [
                {'account': account, 'debit': amount},
                {'account': self.account_revenue, 'credit': amount},
            ]
        else:
            lines = [
                {'account': self.account_expense, 'debit': amount},
                {'account': account, 'credit': amount},
            ]
        return self._post(lines, date_str=date_str)

    def _make_draft(self, lines, journal=None, date_str='2026-06-20'):
        journal = journal or self.bank_journal
        return self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': fields.Date.from_string(date_str),
            'line_ids': [
                (0, 0, {
                    'account_id': line['account'].id,
                    'debit': line.get('debit', 0.0),
                    'credit': line.get('credit', 0.0),
                    'name': line.get('name', '/'),
                })
                for line in lines
            ],
        })

    def _make_shared_account_bank(self):
        bank = self.env['account.journal'].create({
            'name': 'Proof Bank Shared Accounts',
            'code': 'PBSH',
            'type': 'bank',
            'company_id': self.company.id,
            'default_account_id': self.account_bank.id,
        })
        bank.inbound_payment_method_line_ids[:1].write({
            'payment_account_id': self.account_inbound.id,
        })
        bank.outbound_payment_method_line_ids[:1].write({
            'payment_account_id': self.account_outbound.id,
        })
        return bank

    # ---- last statement balance ----

    def test_kwd_precision_preserves_lines_totals_and_half_step_tie(self):
        kwd = self.env.ref('base.KWD')
        self.assertEqual(kwd.decimal_places, 3)

        self.patch(
            type(self.handler), '_outstanding_accounts',
            lambda handler, journal, inbound: [self.account_inbound.id],
        )
        self.patch(
            MoveLineQuery,
            'execute',
            lambda query: [
                {
                    'aml_id': 991001,
                    'date': fields.Date.to_date('2026-06-20'),
                    'line_label': 'Half-step receipt',
                    'move_name': 'KWD/1',
                    'currency_id': kwd.id,
                    'historical_residual': 1.2345,
                },
                {
                    'aml_id': 991002,
                    'date': fields.Date.to_date('2026-06-21'),
                    'line_label': 'One-fil receipt',
                    'move_name': 'KWD/2',
                    'currency_id': kwd.id,
                    'historical_residual': 0.001,
                },
            ],
        )
        lines, total = self.handler._outstanding_section(
            self.bank_journal,
            fields.Date.to_date('2026-12-31'),
            'journal-%s' % self.bank_journal.id,
            inbound=True,
            label='Outstanding receipts',
            company_ids=[self.company.id],
            posted_only=True,
            currency=kwd,
        )

        amounts = {
            line['id']: self._amount(line)
            for line in lines
        }
        self.assertEqual(amounts['outstanding-991001'], 1.235)
        self.assertEqual(amounts['outstanding-991002'], 0.001)
        self.assertEqual(amounts[
            'journal-%s-receipts-header' % self.bank_journal.id], 1.236)
        self.assertEqual(total, 1.236)

    def test_last_statement_balance(self):
        self._make_statement(
            balance_start=1000.0, line_amounts=[500.0, -200.0],
            date_str='2026-06-10')
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        last_stmt = self._line_by_id(result, '%s-last-stmt' % sid)
        # 1000 + 500 - 200 = 1300.
        self.assertAlmostEqual(self._amount(last_stmt), 1300.0, places=2)

    def test_no_statement_journal_yields_empty_last_statement(self):
        # No statement created. Must not raise; last-statement = 0.
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        last_stmt = self._line_by_id(result, '%s-last-stmt' % sid)
        self.assertIsNotNone(last_stmt)
        self.assertAlmostEqual(self._amount(last_stmt), 0.0, places=2)

    # ---- book balance ----

    def test_book_balance_is_cumulative_gl_sum(self):
        # Two postings touching the bank account: +1000 and -300.
        self._post([
            {'account': self.account_bank, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date_str='2026-03-01')
        self._post([
            {'account': self.account_expense, 'debit': 300.0},
            {'account': self.account_bank, 'credit': 300.0},
        ], date_str='2026-04-01')
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        book = self._line_by_id(result, '%s-book' % sid)
        self.assertAlmostEqual(self._amount(book), 700.0, places=2)

    def test_foreign_bank_proof_stays_in_journal_currency(self):
        foreign = self.env['res.currency'].create({
            'name': 'BFX', 'symbol': 'F', 'rounding': 0.01,
        })
        self.env['res.currency.rate'].create({
            'currency_id': foreign.id,
            'company_id': self.company.id,
            'name': '2020-01-01',
            'rate': 0.5,
        })
        foreign_account = self.env['account.account'].create({
            'code': '1019BFX',
            'name': 'Foreign Bank Current Account',
            'account_type': 'asset_cash',
            'company_ids': [(6, 0, self.company.ids)],
            'currency_id': foreign.id,
        })
        foreign_journal = self.env['account.journal'].create({
            'name': 'Foreign Proof Bank',
            'code': 'PBFK',
            'type': 'bank',
            'company_id': self.company.id,
            'currency_id': foreign.id,
            'default_account_id': foreign_account.id,
        })
        statement = self.env['account.bank.statement'].create({
            'name': 'Foreign statement',
            'balance_start': 0.0,
            'line_ids': [(0, 0, {
                'date': fields.Date.to_date('2026-06-10'),
                'amount': 125.0,
                'payment_ref': 'Foreign deposit',
                'journal_id': foreign_journal.id,
            })],
        })
        liquidity = statement.line_ids.move_id.line_ids.filtered(
            lambda line: line.account_id == foreign_account,
        )
        self.assertEqual(len(liquidity), 1)
        self.assertAlmostEqual(liquidity.amount_currency, 125.0, places=2)
        self.assertNotAlmostEqual(liquidity.balance, 125.0, places=2)

        result = self.handler.compute(dict(
            self.options,
            journal_ids=[foreign_journal.id],
        ))
        sid = 'journal-%s' % foreign_journal.id
        self.assertEqual(result['currency']['id'], foreign.id)
        self.assertEqual(result['meta']['proof_currency_id'], foreign.id)
        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, '%s-last-stmt' % sid)),
            125.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, '%s-book' % sid)),
            125.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, '%s-difference' % sid)),
            0.0,
            places=2,
        )

    def test_mixed_journal_currencies_fail_closed(self):
        foreign = self.env['res.currency'].create({
            'name': 'BFY', 'symbol': 'Y', 'rounding': 0.01,
        })
        foreign_journal = self.env['account.journal'].create({
            'name': 'Second Currency Proof Bank',
            'code': 'PBFY',
            'type': 'bank',
            'company_id': self.company.id,
            'currency_id': foreign.id,
            'default_account_id': self.account_bank.id,
        })
        with self.assertRaisesRegex(UserError, 'different currencies'):
            self.handler.compute(dict(
                self.options,
                journal_ids=[self.bank_journal.id, foreign_journal.id],
            ))

    def test_gl_only_filters_are_ignored_for_proof_coherence(self):
        self._post([
            {'account': self.account_bank, 'debit': 500.0},
            {'account': self.account_revenue, 'credit': 500.0},
        ], date_str='2026-03-01')
        self._make_outstanding(self.account_inbound, 250.0, debit=True)
        self._make_outstanding(self.account_outbound, 100.0, debit=False)
        sid = 'journal-%s' % self.bank_journal.id

        baseline = self.handler.compute(self.options)
        filtered = self.handler.compute(dict(self.options, **{
            'partner_ids': [self.partner_a.id],
            'account_ids': [self.account_expense.id],
            'account_type_ids': ['expense'],
            'analytic_account_ids': [999999991],
            'analytic_plan_ids': [999999992],
            'presentation_currency_id': self.env.ref('base.EUR').id,
            'show_zero': True,
        }))
        for suffix in ('book', 'receipts-header', 'payments-header',
                       'difference'):
            self.assertAlmostEqual(
                self._amount(self._line_by_id(filtered, '%s-%s' % (
                    sid, suffix))),
                self._amount(self._line_by_id(baseline, '%s-%s' % (
                    sid, suffix))),
                places=2,
            )
        self.assertIn(
            'presentation_currency_id',
            filtered['meta']['unsupported_option_keys'],
        )

    def test_orchestrator_strips_unsupported_options_before_audit_and_fx(self):
        # Most currencies are archived on a minimal test database.  The
        # unsupported option must be stripped before currency conversion, so
        # an inactive target is both valid and makes this fixture independent
        # from the database's active-currency setup.
        target = self.env['res.currency'].with_context(
            active_test=False,
        ).search([
            ('id', '!=', self.company.currency_id.id),
        ], limit=1)
        self.assertTrue(target)
        payload = self.report.render(dict(self.options, **{
            'account_ids': [self.account_expense.id],
            'presentation_currency_id': target.id,
        }), use_cache=False)
        execution = self.env['eh.account.report.execution'].browse(
            payload['execution_id'])
        snapshot = json.loads(execution.options_snapshot)
        self.assertNotIn('account_ids', snapshot)
        self.assertNotIn('presentation_currency_id', snapshot)
        self.assertEqual(
            payload['currency']['id'], self.company.currency_id.id)
        self.assertNotEqual(payload['currency']['id'], target.id)

    def test_shared_inbound_outbound_account_is_split_by_residual_sign(self):
        self.bank_journal.outbound_payment_method_line_ids[:1].write({
            'payment_account_id': self.account_inbound.id,
        })
        self._make_outstanding(
            self.account_inbound, 60.0, debit=True,
            date_str='2026-04-01',
        )
        self._make_outstanding(
            self.account_inbound, 25.0, debit=False,
            date_str='2026-04-02',
        )

        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        receipt_parent = '%s-receipts-header' % sid
        payment_parent = '%s-payments-header' % sid
        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, receipt_parent)),
            60.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, payment_parent)),
            25.0,
            places=2,
        )
        receipt_ids = {
            line['id'] for line in result['lines']
            if line.get('parent_id') == receipt_parent
        }
        payment_ids = {
            line['id'] for line in result['lines']
            if line.get('parent_id') == payment_parent
        }
        self.assertEqual(len(receipt_ids), 1)
        self.assertEqual(len(payment_ids), 1)
        self.assertFalse(receipt_ids & payment_ids)

    def test_shared_accounts_are_partitioned_by_owning_journal(self):
        bank2 = self._make_shared_account_bank()
        self._post([
            {'account': self.account_bank, 'debit': 100.0},
            {'account': self.account_revenue, 'credit': 100.0},
        ], date_str='2026-03-01')
        self.post_balanced_move([
            {'account': self.account_bank, 'debit': 200.0},
            {'account': self.account_revenue, 'credit': 200.0},
        ], journal=bank2, date=fields.Date.from_string('2026-03-02'))
        self._make_outstanding(
            self.account_inbound, 30.0, debit=True,
            date_str='2026-04-01',
        )
        self.post_balanced_move([
            {'account': self.account_inbound, 'debit': 70.0},
            {'account': self.account_revenue, 'credit': 70.0},
        ], journal=bank2, date=fields.Date.from_string('2026-04-02'))

        result = self.handler.compute(dict(
            self.options,
            journal_ids=[self.bank_journal.id, bank2.id],
        ))
        first = 'journal-%s' % self.bank_journal.id
        second = 'journal-%s' % bank2.id
        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, '%s-book' % first)),
            100.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(result, '%s-book' % second)),
            200.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, '%s-receipts-header' % first)),
            30.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                result, '%s-receipts-header' % second)),
            70.0,
            places=2,
        )

    def test_posted_only_applies_to_book_and_outstanding(self):
        self._make_draft([
            {'account': self.account_bank, 'debit': 300.0},
            {'account': self.account_revenue, 'credit': 300.0},
        ], date_str='2026-03-01')
        self._make_draft([
            {'account': self.account_inbound, 'debit': 200.0},
            {'account': self.account_revenue, 'credit': 200.0},
        ], date_str='2026-04-01')
        sid = 'journal-%s' % self.bank_journal.id

        posted = self.handler.compute(self.options)
        all_entries = self.handler.compute(dict(
            self.options, posted_only=False))
        self.assertAlmostEqual(
            self._amount(self._line_by_id(posted, '%s-book' % sid)),
            0.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                posted, '%s-receipts-header' % sid)),
            0.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(all_entries, '%s-book' % sid)),
            300.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                all_entries, '%s-receipts-header' % sid)),
            200.0,
            places=2,
        )

    def test_outstanding_residual_is_rebuilt_at_cutoff_max_date(self):
        source = self._make_outstanding(
            self.account_inbound, 250.0, debit=True,
            date_str='2026-06-20',
        )
        settlement = self._post([
            {'account': self.account_expense, 'debit': 250.0},
            {'account': self.account_inbound, 'credit': 250.0},
        ], date_str='2027-01-15')
        source_line = source.line_ids.filtered(
            lambda line: line.account_id == self.account_inbound)
        settlement_line = settlement.line_ids.filtered(
            lambda line: line.account_id == self.account_inbound)
        (source_line | settlement_line).reconcile()
        partial = source_line.matched_debit_ids | source_line.matched_credit_ids
        self.assertEqual(len(partial), 1)
        self.assertEqual(
            fields.Date.to_date(partial.max_date),
            fields.Date.to_date('2027-01-15'),
        )
        partial.flush_recordset()
        # ORM creation time deliberately precedes cutoff. max_date remains
        # future accounting evidence and must control historical residual.
        self.env.cr.execute(
            "UPDATE account_partial_reconcile SET create_date = %s "
            "WHERE id = %s",
            ['2026-06-21 12:00:00', partial.id],
        )
        partial.invalidate_recordset(['create_date'])

        sid = 'journal-%s' % self.bank_journal.id
        at_cutoff = self.handler.compute(self.options)
        after_settlement = self.handler.compute(dict(self.options, date={
            'date_from': '2026-01-01',
            'date_to': '2027-12-31',
        }))
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                at_cutoff, '%s-receipts-header' % sid)),
            250.0,
            places=2,
        )
        self.assertAlmostEqual(
            self._amount(self._line_by_id(
                after_settlement, '%s-receipts-header' % sid)),
            0.0,
            places=2,
        )

    def test_book_read_failure_is_not_published_as_zero(self):
        self.patch(
            type(self.handler),
            '_outstanding_accounts',
            lambda handler, journal, inbound: [],
        )

        def fail_execute(query):
            raise RuntimeError('forced accounting read failure')

        self.patch(MoveLineQuery, 'execute', fail_execute)
        with self.assertRaisesRegex(
                RuntimeError, 'forced accounting read failure'):
            self.handler.compute(self.options)

    # ---- the bridge ----

    def test_bridge_ties_to_zero_when_reconciled(self):
        # Statement reports the bank at 700; the GL bank account also nets
        # to 700; no outstanding items -> difference is zero.
        self._make_statement(
            balance_start=700.0, line_amounts=[], date_str='2026-02-01')
        self._post([
            {'account': self.account_bank, 'debit': 700.0},
            {'account': self.account_revenue, 'credit': 700.0},
        ], date_str='2026-03-01')
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        diff = self._line_by_id(result, '%s-difference' % sid)
        self.assertAlmostEqual(self._amount(diff), 0.0, places=2)
        # Bridge identity: adjusted book = adjusted bank + difference.
        book = self._amount(
            self._line_by_id(result, '%s-adjusted-book' % sid))
        adjusted = self._amount(self._line_by_id(result, '%s-adjusted' % sid))
        self.assertAlmostEqual(
            book, adjusted + self._amount(diff), places=2)

    def test_outstanding_items_feed_the_bridge(self):
        # Statement at 1000; GL bank at 1000; plus an outstanding receipt of
        # 250 (deposit in transit) and an outstanding payment of 100.
        self._make_statement(
            balance_start=1000.0, line_amounts=[], date_str='2026-02-01')
        self._post([
            {'account': self.account_bank, 'debit': 1000.0},
            {'account': self.account_revenue, 'credit': 1000.0},
        ], date_str='2026-03-01')
        self._make_outstanding(self.account_inbound, 250.0, debit=True)
        self._make_outstanding(self.account_outbound, 100.0, debit=False)

        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)

        receipts = self._line_by_id(
            result, '%s-receipts-header' % sid)
        payments = self._line_by_id(
            result, '%s-payments-header' % sid)
        self.assertAlmostEqual(self._amount(receipts), 250.0, places=2)
        self.assertAlmostEqual(self._amount(payments), 100.0, places=2)

        # Both sides include outstanding cash: 1000 + 250 - 100 = 1150.
        adjusted = self._amount(self._line_by_id(result, '%s-adjusted' % sid))
        self.assertAlmostEqual(adjusted, 1150.0, places=2)
        adjusted_book = self._amount(
            self._line_by_id(result, '%s-adjusted-book' % sid))
        self.assertAlmostEqual(adjusted_book, 1150.0, places=2)
        diff = self._amount(self._line_by_id(result, '%s-difference' % sid))
        self.assertAlmostEqual(diff, 0.0, places=2)
        self.assertAlmostEqual(
            result['totals']['adjusted_book_balance'], 1150.0, places=2)

    def test_stray_gl_entry_produces_nonzero_difference(self):
        # Tie statement and GL at 500, then inject a stray bank posting the
        # statement never saw -> the difference must surface it.
        self._make_statement(
            balance_start=500.0, line_amounts=[], date_str='2026-02-01')
        self._post([
            {'account': self.account_bank, 'debit': 500.0},
            {'account': self.account_revenue, 'credit': 500.0},
        ], date_str='2026-03-01')
        # Stray entry: bank debited 90, no statement line, no outstanding.
        self._post([
            {'account': self.account_bank, 'debit': 90.0},
            {'account': self.account_revenue, 'credit': 90.0},
        ], date_str='2026-05-01')
        sid = 'journal-%s' % self.bank_journal.id
        result = self.handler.compute(self.options)
        diff_line = self._line_by_id(result, '%s-difference' % sid)
        self.assertAlmostEqual(self._amount(diff_line), 90.0, places=2)
        # A balance_check kind is emitted so the renderer paints it.
        self.assertEqual(
            (diff_line.get('meta') or {}).get('kind'), 'balance_check')

    # ---- multi-journal ----

    def test_multi_journal_one_section_each(self):
        bank2 = self.env['account.journal'].create({
            'name': 'Proof Bank 2',
            'code': 'PBN2',
            'type': 'bank',
            'company_id': self.company.id,
            'default_account_id': self.account_bank.id,
        })
        opts = dict(self.options,
                    journal_ids=[self.bank_journal.id, bank2.id])
        result = self.handler.compute(opts)
        headers = [
            l for l in result['lines']
            if l['id'].endswith('-header')
            and (l.get('meta') or {}).get('kind') == 'section_header']
        # One section header per journal.
        journal_headers = [
            h for h in headers
            if (h.get('meta') or {}).get('journal_id') in (
                self.bank_journal.id, bank2.id)]
        self.assertEqual(len(journal_headers), 2)

    # ---- orchestrator + drilldown ----

    def test_report_renders_through_orchestrator(self):
        self._make_statement(
            balance_start=100.0, line_amounts=[], date_str='2026-02-01')
        payload = self.report.render(self.options)
        self.assertIn('lines', payload)
        self.assertIn('columns', payload)

    def test_outstanding_row_drilldown(self):
        self._make_outstanding(self.account_inbound, 250.0, debit=True)
        result = self.handler.compute(self.options)
        outstanding = [
            l for l in result['lines']
            if l['id'].startswith('outstanding-')]
        self.assertTrue(outstanding)
        action = self.handler.get_drilldown_action(
            self.options, outstanding[0]['id'])
        self.assertIsInstance(action, dict)
        self.assertEqual(action['res_model'], 'account.move.line')
        # Computed proof lines do not drill.
        sid = 'journal-%s' % self.bank_journal.id
        self.assertIsNone(
            self.handler.get_drilldown_action(
                self.options, '%s-difference' % sid))

    def test_empty_scope_does_not_raise(self):
        # A company with no matching journals -> empty lines, note set.
        opts = dict(self.options, journal_ids=[999999999])
        result = self.handler.compute(opts)
        self.assertEqual(result['lines'], [])
        self.assertIn('note', result['meta'])
