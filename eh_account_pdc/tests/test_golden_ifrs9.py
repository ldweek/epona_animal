# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IFRS 9 worked examples for eh_account_pdc.

Every expected amount below is hand-computed from the inputs stated in the
test, with the derivation in a comment; assertions are exact to company
currency rounding and nothing is read back from the engine under test.

Rate convention (from EhGoldenTestCase._set_rate): the pinned rate is units
of foreign currency per 1 company currency (USD). A rate of 0.8 EUR per USD
means EUR 1,000 converts to 1,000 / 0.8 = USD 1,250.00.

Covered mechanics:

* Bounce reversal dated at the bank dishonour date, not the operator
  entry date (IFRS 9 reinstatement in the period of the dishonour).
* Bounce charge journal entry: Dr bounce charges expense / Cr outstanding
  payments at dishonour; bank GL waits for statement reconciliation.
* Foreign currency present/clear entries carry the signed foreign amount
  in amount_currency on both legs; full transit reconciliation recognises
  clear-date exchange gain/loss (IAS 21.8).
* eh_ecl_exposure_lines(): open transit exposures of presented incoming
  cheques with days outstanding, for the loss allowance population.
"""

from datetime import date

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

try:  # Odoo 18+ re-exports freeze_time; 16/17 pull it from freezegun directly
    from odoo.tests import freeze_time
except ImportError:  # pragma: no cover - version shim
    from freezegun import freeze_time

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases

from .common import configure_pdc_journal


@tagged('eh_golden', 'eh_account_pdc', 'post_install', '-at_install')
class TestGoldenPdcIfrs9(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_journal = cls.env['account.journal'].search(
            [
                ('company_id', '=', cls.company.id),
                ('type', '=', 'bank'),
            ],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Golden Bank',
                'code': 'GBNK',
                'type': 'bank',
                'company_id': cls.company.id,
            })
        if not cls.bank_journal.suspense_account_id:
            cls.bank_journal.suspense_account_id = cls._ensure_account(
                cls.env, '1099', 'Bank Suspense', 'asset_current')
        accounts = configure_pdc_journal(cls, cls.bank_journal)
        # Keep short alias used throughout incoming worked examples.
        cls.suspense = accounts['incoming_transit']
        cls.incoming_transit = accounts['incoming_transit']
        cls.outgoing_transit = accounts['outgoing_transit']
        cls.outstanding_receipts = accounts['outstanding_receipts']
        cls.outstanding_payments = accounts['outstanding_payments']
        cls.statement_suspense = accounts['statement_suspense']
        cls.bank_account = cls.bank_journal.default_account_id
        cls.receivable = cls.partner_a.with_company(
            cls.company).property_account_receivable_id
        cls.payable = cls.partner_a.with_company(
            cls.company).property_account_payable_id

        cls.book = cls.env['eh.cheque.book'].search([
            ('journal_id', '=', cls.bank_journal.id),
            ('company_id', '=', cls.company.id),
            ('state', '=', 'in_use'),
        ], limit=1)
        if not cls.book:
            cls.book = cls.env['eh.cheque.book'].create({
                'name': 'Golden Book 1-50',
                'journal_id': cls.bank_journal.id,
                'start_number': 1,
                'end_number': 50,
            })
            cls.book.action_activate()

        cls.reason_funds = cls.env.ref(
            'eh_account_pdc.bounce_reason_insufficient_funds')
        cls.charges_expense = cls._ensure_account(
            cls.env, '5710', 'Bank Charges', 'expense')
        cls.eur = cls.env.ref('base.EUR')
        cls.usd = cls.company.currency_id
        cls.today = fields.Date.context_today(cls.env['eh.cheque'])

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _pin_eur(self, day, rate):
        """Pin a single EUR rate; stray demo rates would shift conversions."""
        self.env['res.currency.rate'].sudo().search([
            ('currency_id', '=', self.eur.id),
            ('company_id', 'in', [self.company.id, False]),
        ]).unlink()
        self._set_rate(self.eur, day, rate)

    def _incoming(self, number, amount, currency=None, value_date=None):
        cheque = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'cheque_number': number,
            'issuer_bank_name': 'Golden Bank',
            'amount': amount,
            'currency_id': (currency or self.usd).id,
            'company_id': self.company.id,
            'issue_date': value_date or self.today,
            'value_date': value_date or self.today,
        })
        cheque.action_register()
        return cheque

    def _outgoing(self, amount, currency=None, value_date=None):
        cheque = self.env['eh.cheque'].create({
            'direction': 'outgoing',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'book_id': self.book.id,
            'cheque_number': str(self.book.next_number),
            'amount': amount,
            'currency_id': (currency or self.usd).id,
            'company_id': self.company.id,
            'issue_date': value_date or self.today,
            'value_date': value_date or self.today,
        })
        cheque.action_register()
        return cheque

    def _bounce_via_wizard(self, cheque, dishonour, charges=0.0, force=False):
        wizard = self.env['eh.cheque.bounce.wizard'].create({
            'cheque_id': cheque.id,
            'reason_id': self.reason_funds.id,
            'dishonour_date': dishonour,
            'bounce_charges': charges,
            'force_current_date': force,
        })
        wizard.action_confirm()
        return cheque

    # ------------------------------------------------------------------
    # golden: bounce reversal dated at the bank dishonour date
    # ------------------------------------------------------------------
    def test_golden_bounce_reversal_dated_at_dishonour(self):
        """Incoming USD 500 presented 2026-03-01 books Dr transit 500 /
        Cr receivable 500. The bank dishonours it on 2026-03-10; the
        operator only records the bounce on 2026-03-25.

        Expected reversal (exact mirror of the present entry) dated at the
        dishonour date, not the entry date:
        2026-03-10  Dr receivable 500.00 / Cr transit 500.00.
        """
        with freeze_time('2026-03-01'):
            cheque = self._incoming(
                'GLD-1', 500.0, value_date=date(2026, 3, 1))
            cheque.action_present()
        present = cheque.present_move_id
        self.assertEqual(present.date, date(2026, 3, 1))
        self.assertMoveLines(present, [
            (self.suspense, 500.0, 0.0),
            (self.receivable, 0.0, 500.0),
        ])

        with freeze_time('2026-03-25'):
            self._bounce_via_wizard(cheque, date(2026, 3, 10))

        self.assertEqual(cheque.state, 'bounced')
        self.assertEqual(cheque.dishonour_date, date(2026, 3, 10))
        reversal = cheque.bounce_move_id
        self.assertTrue(reversal, "bounce must post a reversal entry")
        self.assertEqual(reversal.state, 'posted',
                         "the reinstatement must be recognised, not draft")
        self.assertEqual(
            reversal.date, date(2026, 3, 10),
            "reversal must be dated at the bank dishonour date, not the "
            "operator entry date")
        self.assertMoveLines(reversal, [
            (self.receivable, 500.0, 0.0),
            (self.suspense, 0.0, 500.0),
        ])
        self.assertBalanced(reversal)

    def test_dishonour_before_presentation_rejected(self):
        """A bank cannot dishonour a cheque before it was presented:
        present 2026-03-01, dishonour 2026-02-20 must raise."""
        with freeze_time('2026-03-01'):
            cheque = self._incoming(
                'GLD-2', 300.0, value_date=date(2026, 3, 1))
            cheque.action_present()
        with freeze_time('2026-03-25'):
            with self.assertRaises(UserError):
                self._bounce_via_wizard(cheque, date(2026, 2, 20))
        self.assertEqual(cheque.state, 'presented')
        self.assertFalse(cheque.bounce_move_id)

    def test_missing_or_future_dishonour_date_is_rejected_atomically(self):
        with freeze_time('2026-03-01'):
            cheque = self._incoming(
                'GLD-DATE-GUARD', 300.0,
                value_date=date(2026, 3, 1),
            )
            cheque.action_present()
            with self.assertRaises(UserError):
                cheque._mark_bounced(self.reason_funds)
            with self.assertRaises(UserError):
                cheque._mark_bounced(
                    self.reason_funds,
                    dishonour_date=date(2026, 3, 2),
                )

        cheque.invalidate_recordset()
        self.assertEqual(cheque.state, 'presented')
        self.assertFalse(cheque.bounce_move_id)

    def test_dishonour_into_locked_period_requires_force(self):
        """Backdating is warning-free only within an open period. With the
        lock at 2026-03-15, a dishonour date of 2026-03-10 is refused
        unless Post at Current Date is set, which books the reversal at
        the entry date (2026-03-25) while keeping the dishonour date on
        the record for disclosure."""
        lock_field = next(
            (f for f in ('fiscalyear_lock_date', 'period_lock_date',
                         'hard_lock_date') if f in self.company._fields),
            None)
        if not lock_field:
            self.skipTest("No lock-date field on this Odoo version.")
        with freeze_time('2026-03-01'):
            cheque = self._incoming(
                'GLD-3', 200.0, value_date=date(2026, 3, 1))
            cheque.action_present()
        self.company.sudo().write({lock_field: date(2026, 3, 15)})
        with freeze_time('2026-03-25'):
            with self.assertRaises(UserError):
                self._bounce_via_wizard(cheque, date(2026, 3, 10))
            self.assertEqual(cheque.state, 'presented')
            self._bounce_via_wizard(cheque, date(2026, 3, 10), force=True)
        self.assertEqual(cheque.state, 'bounced')
        self.assertEqual(cheque.dishonour_date, date(2026, 3, 10))
        self.assertEqual(
            cheque.bounce_move_id.date, date(2026, 3, 25),
            "with force_current_date the reversal posts at the entry date "
            "because the dishonour period is locked")

    # ------------------------------------------------------------------
    # golden: bounce charges journal entry
    # ------------------------------------------------------------------
    def test_golden_bounce_charges_expense_entry(self):
        """Bounce charges of USD 45 (company currency, no conversion) with
        the expense account configured on the journal post a second entry
        at the dishonour date, linked to the cheque:
        2026-03-10  Dr bank charges 45.00 / Cr outstanding payments 45.00.
        """
        self.bank_journal.eh_pdc_bounce_charge_account_id = (
            self.charges_expense)
        with freeze_time('2026-03-01'):
            cheque = self._incoming(
                'GLD-4', 800.0, value_date=date(2026, 3, 1))
            cheque.action_present()
        with freeze_time('2026-03-25'):
            self._bounce_via_wizard(
                cheque, date(2026, 3, 10), charges=45.0)

        self.assertEqual(cheque.bounce_charges, 45.0)
        charge_move = cheque.bounce_charge_move_id
        self.assertTrue(charge_move,
                        "nonzero charges must post an expense entry")
        self.assertEqual(charge_move.state, 'posted')
        self.assertEqual(charge_move.date, date(2026, 3, 10),
                         "charges are expensed at the dishonour date")
        self.assertMoveLines(charge_move, [
            (self.charges_expense, 45.0, 0.0),
            (self.outstanding_payments, 0.0, 45.0),
        ])
        self.assertBalanced(charge_move)
        self.assertIn(cheque.name, charge_move.ref)

    def test_bounce_charges_company_fallback_account(self):
        """With no journal-level account, the company fallback account is
        used: same entry, Dr bank charges 45.00 / Cr outstanding payments
        45.00."""
        self.bank_journal.eh_pdc_bounce_charge_account_id = False
        self.company.eh_pdc_bounce_charge_account_id = self.charges_expense
        with freeze_time('2026-03-01'):
            cheque = self._incoming(
                'GLD-5', 800.0, value_date=date(2026, 3, 1))
            cheque.action_present()
        with freeze_time('2026-03-25'):
            self._bounce_via_wizard(
                cheque, date(2026, 3, 10), charges=45.0)
        charge_move = cheque.bounce_charge_move_id
        self.assertTrue(charge_move)
        self.assertMoveLines(charge_move, [
            (self.charges_expense, 45.0, 0.0),
            (self.outstanding_payments, 0.0, 45.0),
        ])

    def test_bounce_charges_without_config_stay_informational(self):
        """Pre-existing behaviour preserved: with no expense account
        configured anywhere, charges stay informational on the cheque and
        no charge entry is posted."""
        self.bank_journal.eh_pdc_bounce_charge_account_id = False
        self.company.eh_pdc_bounce_charge_account_id = False
        with freeze_time('2026-03-01'):
            cheque = self._incoming(
                'GLD-6', 400.0, value_date=date(2026, 3, 1))
            cheque.action_present()
        with freeze_time('2026-03-25'):
            self._bounce_via_wizard(
                cheque, date(2026, 3, 10), charges=25.0)
        self.assertEqual(cheque.bounce_charges, 25.0)
        self.assertFalse(cheque.bounce_charge_move_id)

    # ------------------------------------------------------------------
    # regression: FC amount_currency sign on present/clear, both directions
    # ------------------------------------------------------------------
    def test_golden_fc_amount_currency_incoming(self):
        """EUR 1,000 incoming cheque at 0.8 EUR per USD.

        Company amounts: 1,000 / 0.8 = USD 1,250.00.
        Present: Dr incoming transit 1,250.00 (amount_currency +1,000 EUR)
                 Cr receivable 1,250.00 (amount_currency -1,000 EUR).
        Clear:   Dr outstanding receipts 1,250.00 (+1,000) /
                 Cr incoming transit 1,250.00 (-1,000).
        """
        self._pin_eur(date(2026, 3, 1), 0.8)
        with freeze_time('2026-03-01'):
            cheque = self._incoming(
                'GLD-FC1', 1000.0, currency=self.eur,
                value_date=date(2026, 3, 1))
            cheque.action_present()

            present = cheque.present_move_id
            susp = present.line_ids.filtered(
                lambda l: l.account_id == self.suspense)
            recv = present.line_ids.filtered(
                lambda l: l.account_id == self.receivable)
            self.assertEqual(len(susp), 1)
            self.assertEqual(len(recv), 1)
            self.assertAlmostEqual(susp.debit, 1250.0, places=2)
            self.assertAlmostEqual(susp.amount_currency, 1000.0, places=2)
            self.assertEqual(susp.currency_id, self.eur)
            self.assertAlmostEqual(recv.credit, 1250.0, places=2)
            self.assertAlmostEqual(recv.amount_currency, -1000.0, places=2)
            self.assertEqual(recv.currency_id, self.eur)

            cheque.action_clear()
            clear = cheque.clear_move_id
            outstanding = clear.line_ids.filtered(
                lambda l: l.account_id == self.outstanding_receipts)
            susp2 = clear.line_ids.filtered(
                lambda l: l.account_id == self.suspense)
            self.assertAlmostEqual(outstanding.debit, 1250.0, places=2)
            self.assertAlmostEqual(
                outstanding.amount_currency, 1000.0, places=2)
            self.assertAlmostEqual(susp2.credit, 1250.0, places=2)
            self.assertAlmostEqual(susp2.amount_currency, -1000.0, places=2)

    def test_golden_fc_amount_currency_outgoing(self):
        """EUR 1,000 issued cheque at 0.8 EUR per USD (USD 1,250.00).

        Present: Dr payable 1,250.00 (+1,000 EUR)
                 Cr issued-cheque transit 1,250.00 (-1,000 EUR).
        """
        self._pin_eur(date(2026, 3, 1), 0.8)
        with freeze_time('2026-03-01'):
            cheque = self._outgoing(
                1000.0, currency=self.eur, value_date=date(2026, 3, 1))
            cheque.action_present()
        present = cheque.present_move_id
        pay = present.line_ids.filtered(
            lambda l: l.account_id == self.payable)
        susp = present.line_ids.filtered(
            lambda l: l.account_id == self.outgoing_transit)
        self.assertEqual(len(pay), 1)
        self.assertEqual(len(susp), 1)
        self.assertAlmostEqual(pay.debit, 1250.0, places=2)
        self.assertAlmostEqual(pay.amount_currency, 1000.0, places=2)
        self.assertEqual(pay.currency_id, self.eur)
        self.assertAlmostEqual(susp.credit, 1250.0, places=2)
        self.assertAlmostEqual(susp.amount_currency, -1000.0, places=2)
        self.assertEqual(susp.currency_id, self.eur)

    def test_pairwise_fc_sign_invariants(self):
        """Pairwise sweep over direction x currency x transition.

        Invariants for every posted PDC entry with cheque amount 1,000:
        * exactly two lines, balanced;
        * both lines carry the cheque currency and abs(amount_currency)
          equal to the cheque amount;
        * the debit line carries +1,000, the credit line -1,000;
        * company-side amount is 1,250.00 when the cheque is EUR at the
          pinned 0.8 EUR-per-USD rate (1,000 / 0.8), else 1,000.00;
        * cheque-transit leg sits on expected side:
          incoming present -> transit debit, incoming clear -> credit,
          outgoing present -> transit credit, outgoing clear -> debit.
        """
        self._pin_eur(self.today, 0.8)
        suspense_side = {
            ('incoming', 'present'): 'debit',
            ('incoming', 'clear'): 'credit',
            ('outgoing', 'present'): 'credit',
            ('outgoing', 'clear'): 'debit',
        }
        axes = {
            'direction': ['incoming', 'outgoing'],
            'fx': [False, True],
            'transition': ['present', 'clear'],
        }
        for idx, case in enumerate(pairwise_cases(axes)):
            with self.subTest(case=case):
                currency = self.eur if case['fx'] else self.usd
                if case['direction'] == 'incoming':
                    cheque = self._incoming(
                        'PW-%s' % idx, 1000.0, currency=currency)
                else:
                    cheque = self._outgoing(1000.0, currency=currency)
                cheque.action_present()
                if case['transition'] == 'present':
                    move = cheque.present_move_id
                else:
                    cheque.action_clear()
                    move = cheque.clear_move_id
                self.assertEqual(move.state, 'posted')
                self.assertEqual(len(move.line_ids), 2)
                self.assertBalanced(move)
                expected_company = 1250.0 if case['fx'] else 1000.0
                for line in move.line_ids:
                    self.assertEqual(line.currency_id, currency)
                    self.assertAlmostEqual(
                        abs(line.amount_currency), 1000.0, places=2)
                    if line.debit:
                        self.assertAlmostEqual(
                            line.amount_currency, 1000.0, places=2)
                        self.assertAlmostEqual(
                            line.debit, expected_company, places=2)
                    else:
                        self.assertAlmostEqual(
                            line.amount_currency, -1000.0, places=2)
                        self.assertAlmostEqual(
                            line.credit, expected_company, places=2)
                transit_account = (
                    self.incoming_transit
                    if case['direction'] == 'incoming'
                    else self.outgoing_transit
                )
                susp_line = move.line_ids.filtered(
                    lambda l: l.account_id == transit_account)
                self.assertEqual(len(susp_line), 1)
                side = suspense_side[
                    (case['direction'], case['transition'])]
                if side == 'debit':
                    self.assertAlmostEqual(
                        susp_line.debit, expected_company, places=2)
                else:
                    self.assertAlmostEqual(
                        susp_line.credit, expected_company, places=2)

    # ------------------------------------------------------------------
    # loss allowance provider hook
    # ------------------------------------------------------------------
    def test_ecl_exposure_hook(self):
        """Two open EUR cheques in transit at reporting date.

        Rate pinned 0.8 EUR per USD, so EUR 1,000 = USD 1,250.00 and
        EUR 2,000 = USD 2,500.00. 2026 is not a leap year, February has
        28 days:
        * cheque A: value/presented 2026-02-01, reporting 2026-03-01
          -> 28 days outstanding, exposure USD 1,250.00 (EUR 1,000);
        * cheque B: value/presented 2026-02-15 -> 14 days outstanding,
          exposure USD 2,500.00 (EUR 2,000).
        A registered-only incoming cheque is excluded (its receivable is
        still in the standard ECL population) and an outgoing presented
        cheque is excluded (liability, out of ECL scope).
        """
        self._pin_eur(date(2026, 2, 1), 0.8)
        with freeze_time('2026-02-01'):
            cheque_a = self._incoming(
                'ECL-A', 1000.0, currency=self.eur,
                value_date=date(2026, 2, 1))
            cheque_a.action_present()
        with freeze_time('2026-02-15'):
            cheque_b = self._incoming(
                'ECL-B', 2000.0, currency=self.eur,
                value_date=date(2026, 2, 15))
            cheque_b.action_present()
            cheque_c = self._incoming(
                'ECL-C', 700.0, value_date=date(2026, 2, 15))
            cheque_d = self._outgoing(
                900.0, value_date=date(2026, 2, 15))
            cheque_d.action_present()

        cheques = cheque_a | cheque_b | cheque_c | cheque_d
        exposures = cheques.eh_ecl_exposure_lines(
            reporting_date=date(2026, 3, 1))
        self.assertEqual(len(exposures), 2,
                         "only presented incoming cheques are exposures")
        by_cheque = {e['cheque_id']: e for e in exposures}
        self.assertIn(cheque_a.id, by_cheque)
        self.assertIn(cheque_b.id, by_cheque)

        exp_a = by_cheque[cheque_a.id]
        self.assertEqual(exp_a['days_outstanding'], 28)
        self.assertEqual(exp_a['due_date'], date(2026, 2, 1))
        self.assertAlmostEqual(exp_a['amount_residual'], 1250.0, places=2)
        self.assertAlmostEqual(
            exp_a['amount_residual_currency'], 1000.0, places=2)
        self.assertEqual(exp_a['account_id'], self.suspense.id)
        self.assertEqual(exp_a['partner_id'], self.partner_a.id)
        self.assertEqual(exp_a['currency_id'], self.eur.id)

        exp_b = by_cheque[cheque_b.id]
        self.assertEqual(exp_b['days_outstanding'], 14)
        self.assertEqual(exp_b['due_date'], date(2026, 2, 15))
        self.assertAlmostEqual(exp_b['amount_residual'], 2500.0, places=2)
        self.assertAlmostEqual(
            exp_b['amount_residual_currency'], 2000.0, places=2)

        # days_outstanding on the record follows the same ageing basis.
        with freeze_time('2026-03-01'):
            cheque_a.invalidate_recordset()
            self.assertEqual(cheque_a.days_outstanding, 28)
            self.assertEqual(cheque_b.days_outstanding, 14)

        # Clearance settles the exposure: only B remains.
        with freeze_time('2026-03-05'):
            cheque_a.action_clear()
        exposures = cheques.eh_ecl_exposure_lines(
            reporting_date=date(2026, 3, 10))
        self.assertEqual([e['cheque_id'] for e in exposures],
                         [cheque_b.id])

    # ------------------------------------------------------------------
    # golden: a real ECL run ingests presented-cheque transit exposures
    # ------------------------------------------------------------------
    def _ecl_matrix_run(self, reporting):
        """A simplified ECL run with a 3-band provision matrix.

        Bands (loss rate as a percentage): 0-30 at 1%, 31-90 at 5%,
        91+ at 25%. Returns the run record.
        """
        return self.env['eh.ecl.run'].create({
            'reporting_date': reporting,
            'measurement_approach': 'simplified',
            'company_id': self.company.id,
            'journal_id': self.env['account.journal'].search([
                ('company_id', '=', self.company.id),
                ('type', '=', 'general')], limit=1).id,
            'bucket_ids': [
                (0, 0, {'name': '0-30', 'days_from': 0, 'days_to': 30,
                        'loss_rate': 1.0}),
                (0, 0, {'name': '31-90', 'days_from': 31, 'days_to': 90,
                        'loss_rate': 5.0}),
                (0, 0, {'name': '91+', 'days_from': 91, 'days_to': 0,
                        'loss_rate': 25.0}),
            ],
        })

    def test_golden_ecl_run_ingests_cheque_exposures(self):
        """A live ECL run picks up presented-cheque transit exposures.

        Two USD incoming cheques are presented (no invoice link, so the
        cheque's own receivable leg is a credit that the receivables sweep
        skips; exposure lives on incoming-cheque transit debit):
        * cheque A: USD 1,000, value/presented 2026-03-01. At reporting
          date 2026-03-10 it is 9 days outstanding -> the 0-30 band.
        * cheque B: USD 3,000, value/presented 2025-12-01. From 2025-12-01
          to 2026-03-10 is 99 days (Dec 30 + Jan 31 + Feb 28 + Mar 10, with
          2026 a non-leap year) -> the 91+ band.

        populate-from-receivables alone leaves every band at 0.00: the
        cheques are invisible to it (the finding being closed). Only
        populate-including-exposures ingests them:
            0-30 gross 1,000.00, 31-90 gross 0.00, 91+ gross 3,000.00.
        Simplified undiscounted closing allowance
            = 1,000 x 1% + 3,000 x 25% = 10.00 + 750.00 = 760.00.
        Re-running the populate stays idempotent: same gross, same 760.00.
        """
        if 'eh.ecl.run' not in self.env.registry:
            self.skipTest("eh_account_ecl not installed.")

        with freeze_time('2026-03-01'):
            cheque_a = self._incoming(
                'ECLRUN-A', 1000.0, value_date=date(2026, 3, 1))
            cheque_a.action_present()
        with freeze_time('2025-12-01'):
            cheque_b = self._incoming(
                'ECLRUN-B', 3000.0, value_date=date(2025, 12, 1))
            cheque_b.action_present()

        run = self._ecl_matrix_run(date(2026, 3, 10))
        by_name = {b.name: b for b in run.bucket_ids}

        # The plain receivables sweep never sees the cheques.
        run.action_populate_from_receivables()
        self.assertAlmostEqual(by_name['0-30'].gross_carrying, 0.0, places=2)
        self.assertAlmostEqual(by_name['31-90'].gross_carrying, 0.0, places=2)
        self.assertAlmostEqual(by_name['91+'].gross_carrying, 0.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 0.0, places=2)

        # Populate including exposures ingests both cheques, aged.
        run.action_populate_from_exposures()
        self.assertAlmostEqual(
            by_name['0-30'].gross_carrying, 1000.0, places=2)
        self.assertAlmostEqual(
            by_name['31-90'].gross_carrying, 0.0, places=2)
        self.assertAlmostEqual(
            by_name['91+'].gross_carrying, 3000.0, places=2)
        # 1,000 x 1% + 3,000 x 25% = 10.00 + 750.00 = 760.00.
        self.assertAlmostEqual(run.closing_allowance, 760.0, places=2)

        # Idempotent: a repopulate re-totals from source, no double count.
        run.action_populate_from_exposures()
        self.assertAlmostEqual(
            by_name['0-30'].gross_carrying, 1000.0, places=2)
        self.assertAlmostEqual(
            by_name['91+'].gross_carrying, 3000.0, places=2)
        self.assertAlmostEqual(run.closing_allowance, 760.0, places=2)

        # eh.cheque is discovered as an exposure provider by the scan.
        self.assertIn('eh.cheque', run._eh_ecl_exposure_providers())
        self.assertTrue(run.has_exposure_providers)
