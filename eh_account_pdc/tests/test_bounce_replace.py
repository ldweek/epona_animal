# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Bounce + replacement workflow tests."""

from datetime import timedelta

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'integration', 'post_install', '-at_install')
class TestBounceReplace(EhPdcTestCase):

    def _presented_incoming(self):
        cheque = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'cheque_number': 'CUST-1',
            'issuer_bank_name': 'XYZ Bank',
            'amount': 750.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        })
        cheque.action_register()
        cheque.action_present()
        return cheque

    # ---- bounce ----

    def test_bounce_only_from_presented(self):
        cheque = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'cheque_number': 'CUST-2',
            'amount': 100.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        })
        with self.assertRaises(UserError):
            cheque.action_open_bounce_wizard()

    def test_bounce_via_wizard_records_reason_and_charges(self):
        cheque = self._presented_incoming()
        wizard = self.env['eh.cheque.bounce.wizard'].create({
            'cheque_id': cheque.id,
            'reason_id': self.reason_funds.id,
            'bounce_charges': 25.0,
            'notes': "Bank statement on file.",
        })
        wizard.action_confirm()
        self.assertEqual(cheque.state, 'bounced')
        self.assertEqual(cheque.bounce_reason_id, self.reason_funds)
        self.assertEqual(cheque.bounce_charges, 25.0)
        self.assertEqual(cheque.bounced_by_id, self.env.user)
        self.assertTrue(cheque.bounced_at)

    # ---- replace ----

    def test_replace_only_from_bounced(self):
        cheque = self._presented_incoming()
        with self.assertRaises(UserError):
            cheque.action_open_replace_wizard()

    def test_replace_creates_new_cheque_and_chains(self):
        cheque = self._presented_incoming()
        cheque._mark_bounced(
            reason=self.reason_funds,
            charges=15.0,
            dishonour_date=self.today,
        )
        new_value_date = self.today + timedelta(days=14)
        wizard = self.env['eh.cheque.replace.wizard'].create({
            'cheque_id': cheque.id,
            'new_cheque_number': 'CUST-1-R',
            'new_value_date': new_value_date,
            'new_amount': 765.0,
        })
        result = wizard.action_confirm()
        new_cheque = self.env['eh.cheque'].browse(result['res_id'])

        self.assertEqual(cheque.state, 'replaced')
        self.assertEqual(cheque.replaced_by_id, new_cheque)
        self.assertEqual(new_cheque.replaces_id, cheque)
        self.assertEqual(new_cheque.state, 'registered')
        self.assertEqual(new_cheque.value_date, new_value_date)
        self.assertEqual(new_cheque.partner_id, cheque.partner_id)
        self.assertEqual(new_cheque.direction, 'incoming')

    def test_replaced_cheque_cannot_be_cancelled(self):
        cheque = self._presented_incoming()
        cheque._mark_bounced(
            reason=self.reason_funds,
            dishonour_date=self.today,
        )
        wizard = self.env['eh.cheque.replace.wizard'].create({
            'cheque_id': cheque.id,
            'new_cheque_number': 'CUST-1-R',
            'new_value_date': self.today + timedelta(days=7),
            'new_amount': cheque.amount,
        })
        wizard.action_confirm()
        with self.assertRaises(UserError):
            cheque.action_cancel()
