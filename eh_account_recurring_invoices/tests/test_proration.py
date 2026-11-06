# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression tests for the proration wizard.

Covers the previously-broken `tpl.cadence` reference (the field is
`interval_unit`), all five interval_units, and the days_remaining=0
guard.
"""

from datetime import date
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_recurring_invoices', 'integration', 'post_install', '-at_install')
class TestProrationWizard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.Template = cls.env['eh.recurring.invoice.template']
        cls.Wizard = cls.env['eh.recurring.invoice.proration.wizard']
        cls.sale_journal = cls.env['account.journal'].search(
            [('company_id', '=', cls.company.id), ('type', '=', 'sale')],
            limit=1,
        )
        if not cls.sale_journal:
            cls.sale_journal = cls.env['account.journal'].create({
                'name': 'Sales', 'code': 'SALE', 'type': 'sale',
                'company_id': cls.company.id,
            })

    def _make_template(self, interval_unit='month', interval=1, price=100.0,
                       next_run='2026-04-01'):
        return self.Template.create({
            'name': 'T_%s_%d' % (interval_unit, interval),
            'code': 't_%s_%d' % (interval_unit, interval),
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'interval': interval,
            'interval_unit': interval_unit,
            'start_date': fields.Date.from_string(next_run),
            'next_run_date': fields.Date.from_string(next_run),
            'line_ids': [(0, 0, {
                'name': 'Service',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': price,
            })],
        })

    def _open_wizard(self, tpl, change_date, new_amount):
        return self.Wizard.create({
            'template_id': tpl.id,
            'change_date': fields.Date.from_string(change_date),
            'new_amount': new_amount,
        })

    def _post_source_period(self, tpl):
        tpl.write({'auto_post': True})
        tpl.action_activate()
        tpl.action_generate_now()
        source = tpl.last_generated_move_id
        self.assertEqual(source.state, 'posted')
        self.assertTrue(source.eh_recurring_period_start)
        self.assertTrue(source.eh_recurring_period_end)
        return source

    # --- regression: ensure no AttributeError on any interval_unit ---

    def test_no_attribute_error_on_monthly(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-04-15', 200.0)
        # Forcing compute. If tpl.cadence regressed, this raises.
        self.assertEqual(wiz.period_end_date, date(2026, 4, 30))
        self.assertEqual(wiz.period_length_days, 30)
        self.assertEqual(wiz.days_remaining, 16)

    def test_no_attribute_error_on_quarterly(self):
        tpl = self._make_template('quarter', 1, 300.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-05-01', 600.0)
        # Q2 = Apr-Jun, period_end = Jun 30
        self.assertEqual(wiz.period_end_date, date(2026, 6, 30))
        self.assertEqual(wiz.period_length_days, 91)

    def test_no_attribute_error_on_yearly(self):
        tpl = self._make_template('year', 1, 1200.0, '2026-01-01')
        wiz = self._open_wizard(tpl, '2026-04-01', 2400.0)
        self.assertEqual(wiz.period_end_date, date(2026, 12, 31))
        self.assertEqual(wiz.period_length_days, 365)

    def test_no_attribute_error_on_weekly(self):
        tpl = self._make_template('week', 2, 50.0, '2026-04-06')
        wiz = self._open_wizard(tpl, '2026-04-08', 80.0)
        # 2 weeks from 2026-04-06 -> 2026-04-19
        self.assertEqual(wiz.period_end_date, date(2026, 4, 19))
        self.assertEqual(wiz.period_length_days, 14)

    def test_no_attribute_error_on_daily(self):
        tpl = self._make_template('day', 30, 100.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-04-15', 200.0)
        # 30 days from Apr 1 -> Apr 30
        self.assertEqual(wiz.period_end_date, date(2026, 4, 30))
        self.assertEqual(wiz.period_length_days, 30)

    # --- proration math sanity ---

    def test_credit_amount_half_period(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        # 15 days remaining of 30 = half. Credit = 50, new_period = 100,
        # net = 50.
        self.assertEqual(wiz.days_remaining, 15)
        self.assertEqual(wiz.credit_amount, 50.0)
        self.assertEqual(wiz.new_period_amount, 100.0)
        self.assertEqual(wiz.net_amount, 50.0)

    def test_proration_uses_currency_rounding(self):
        tpl = self._make_template('month', 1, 5.35, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-04-16', 10.0)
        self.assertEqual(wiz.credit_amount, 2.68)

    def test_new_amount_must_be_positive(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        with self.assertRaisesRegex(ValidationError, 'must be positive'):
            self._open_wizard(tpl, '2026-04-16', 0.0)
        with self.assertRaisesRegex(ValidationError, 'must be positive'):
            self._open_wizard(tpl, '2026-04-16', -1.0)

    def test_proration_uses_posted_period_after_next_run_advanced(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        source = self._post_source_period(tpl)
        self.assertEqual(tpl.next_run_date, date(2026, 5, 1))
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        self.assertEqual(wiz.period_start_date, date(2026, 4, 1))
        self.assertEqual(wiz.period_end_date, date(2026, 4, 30))
        self.assertEqual(wiz.days_remaining, 15)
        self.assertLessEqual(wiz.days_remaining, wiz.period_length_days)
        self.assertEqual(source.eh_recurring_period_end, wiz.period_end_date)

    def test_apply_posts_both_moves(self):
        """The proration credit and catch-up invoice must be POSTED, not left
        in draft where they never reach AR or revenue."""
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        source = self._post_source_period(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        result = wiz.action_apply()
        move_ids = result['domain'][0][2]
        moves = self.env['account.move'].browse(move_ids)
        self.assertEqual(len(moves), 2)
        self.assertTrue(
            all(m.state == 'posted' for m in moves),
            "Proration moves must be posted, got %s"
            % moves.mapped('state'))
        self.assertEqual(
            moves.mapped('eh_recurring_proration_source_move_id'), source,
        )
        self.assertTrue(all(moves.mapped('eh_sealed')))
        self.assertFalse(any(moves.mapped('eh_legacy_unverified_seal')))
        credit = moves.filtered(lambda move: move.move_type == 'out_refund')
        credit_receivable = credit.line_ids.filtered(
            lambda line: line.account_id.account_type == 'asset_receivable'
        )
        self.assertTrue(all(credit_receivable.mapped('reconciled')))
        self.assertEqual(
            credit.eh_recurring_proration_source_move_id, source,
        )
        # A partial proration credit is intentionally not labelled as core's
        # exact full reversal. Its immutable source link plus reconciliation
        # are the truthful accounting relationship.
        self.assertFalse(credit.reversed_entry_id)
        with self.assertRaises(UserError):
            moves[0].invoice_line_ids[0].write({'price_unit': 999.0})
        with self.assertRaises(UserError):
            moves[0].with_context(force_delete=True).unlink()
        # The template price advanced to the new amount.
        self.assertEqual(tpl.line_ids[0].price_unit, 200.0)

    def test_apply_retry_is_idempotent_and_provenance_is_immutable(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        self._post_source_period(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        first = wiz.action_apply()['domain'][0][2]
        second = wiz.action_apply()['domain'][0][2]
        self.assertEqual(set(first), set(second))
        moves = self.env['account.move'].browse(first)
        self.assertEqual(len(moves), 2)
        with self.assertRaisesRegex(AccessError, 'server-owned'):
            moves[0].write({'eh_recurring_proration_role': 'charge'})

    def test_same_source_cannot_receive_second_active_credit(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        source = self._post_source_period(tpl)
        self._open_wizard(tpl, '2026-04-16', 200.0).action_apply()
        second = self._open_wizard(tpl, '2026-04-20', 250.0)
        with self.assertRaisesRegex(UserError, 'cannot be credited twice'):
            second.action_apply()
        credits = self.env['account.move'].search([
            ('eh_recurring_proration_source_move_id', '=', source.id),
            ('eh_recurring_proration_role', '=', 'credit'),
            ('state', '!=', 'cancel'),
        ])
        self.assertEqual(len(credits), 1)

    def test_apply_requires_accounting_manager(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        self._post_source_period(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        user = self.env['res.users'].create({
            'name': 'Recurring proration preparer',
            'login': 'recurring_proration_preparer',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        with self.assertRaises(AccessError):
            wiz.with_user(user).action_apply()
        self.assertFalse(self.env['account.move'].search([
            ('eh_recurring_proration_template_id', '=', tpl.id),
        ]))

    # --- guards ---

    def test_apply_blocks_when_no_days_remaining(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        self._post_source_period(tpl)
        # Change date outside the immutable billed service period. Preview
        # may show the next cadence, but posting must refuse without source
        # invoice evidence covering that date.
        wiz = self._open_wizard(tpl, '2026-05-15', 200.0)
        with self.assertRaises(UserError):
            wiz.action_apply()

    def test_apply_blocks_on_multi_line_template(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        self._post_source_period(tpl)
        tpl.write({'line_ids': [(0, 0, {
            'name': 'Addon',
            'account_id': self.account_revenue.id,
            'quantity': 1.0,
            'price_unit': 50.0,
        })]})
        wiz = self._open_wizard(tpl, '2026-04-15', 200.0)
        with self.assertRaises(UserError):
            wiz.action_apply()

    def test_apply_requires_posted_generated_period_evidence(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        with self.assertRaisesRegex(UserError, 'posted generated invoice'):
            wiz.action_apply()

    def test_proration_pair_and_source_cannot_reset_to_draft(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        source = self._post_source_period(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        moves = self.env['account.move'].browse(
            wiz.action_apply()['domain'][0][2]
        )
        with self.assertRaises(UserError):
            source.button_draft()
        with self.assertRaises(UserError):
            moves[0].button_draft()

    def test_button_draft_resolves_all_proration_sources_in_one_search(self):
        tpl = self._make_template('month', 1, 100.0, '2026-04-01')
        source = self._post_source_period(tpl)
        moves = self.env['account.move'].browse(
            self._open_wizard(
                tpl, '2026-04-16', 200.0,
            ).action_apply()['domain'][0][2]
        )
        MoveClass = type(source)
        real_search = MoveClass.search
        reference_queries = []

        def _counted_search(records, domain, *args, **kwargs):
            if any(
                isinstance(term, (tuple, list))
                and term[0] == 'eh_recurring_proration_source_move_id'
                for term in domain
            ):
                reference_queries.append(domain)
            return real_search(records, domain, *args, **kwargs)

        with patch.object(MoveClass, 'search', new=_counted_search):
            with self.assertRaises(UserError):
                (source | moves).button_draft()
        self.assertEqual(len(reference_queries), 1)
