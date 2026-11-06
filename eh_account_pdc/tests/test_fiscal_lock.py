# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Fiscal-lock-date behaviour for PDC postings.

Two guarantees:

* A PDC present/clear posting into a locked period is refused.
* A bounce reversal dates to the original present entry's period (or the
  earliest open date when that period is locked), never blindly to today.

Both are field-presence guarded so they stay cross-version safe: the lock
date fields differ between Odoo 16/17/18 and 19.
"""

from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'integration', 'post_install', '-at_install')
class TestFiscalLock(EhPdcTestCase):

    def _redate_present_move_fixture(self, move, move_date):
        """Seed historical evidence through SQL; no production unseal bypass."""
        move.flush_recordset()
        self.env.cr.execute(
            "UPDATE account_move SET date = %s WHERE id = %s",
            (move_date, move.id),
        )
        move.invalidate_recordset(['date'])

    def _lock_field(self):
        """Pick a lock-date field that exists on this Odoo version."""
        company = self.company
        for fname in ('fiscalyear_lock_date', 'period_lock_date',
                      'hard_lock_date'):
            if fname in company._fields:
                return fname
        return None

    def _registered_incoming(self, **overrides):
        vals = {
            'direction': 'incoming',
            'partner_id': self.partner_a.id,
            'journal_id': self.bank_journal.id,
            'cheque_number': overrides.pop('cheque_number', 'LOCK-1'),
            'issuer_bank_name': 'Lock Bank',
            'amount': 400.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        }
        vals.update(overrides)
        cheque = self.env['eh.cheque'].create(vals)
        cheque.action_register()
        return cheque

    def test_present_into_locked_period_is_blocked(self):
        """Presenting a cheque whose accounting date falls on or before the
        fiscal lock date must raise. Without the guard the suspense entry
        posts straight into a closed period."""
        lock_field = self._lock_field()
        if not lock_field:
            self.skipTest("No lock-date field on this Odoo version.")
        cheque = self._registered_incoming(cheque_number='LOCK-BLOCK')
        # Lock everything up to and including today; the present move dates
        # to today, which is now inside the locked window.
        self.company.sudo().write({lock_field: self.today})
        with self.assertRaises(UserError):
            cheque.action_present()
        cheque.invalidate_recordset()
        self.assertEqual(
            cheque.state, 'registered',
            "a blocked present must leave the cheque registered",
        )
        self.assertFalse(
            cheque.present_move_id,
            "no journal entry may be posted into a locked period",
        )

    def test_present_open_period_still_works(self):
        """Opt-in-safe: with no lock set, or a lock strictly before the
        posting date, present proceeds unchanged."""
        lock_field = self._lock_field()
        cheque = self._registered_incoming(cheque_number='LOCK-OPEN')
        if lock_field:
            self.company.sudo().write({
                lock_field: self.today - timedelta(days=30),
            })
        cheque.action_present()
        self.assertEqual(cheque.state, 'presented')
        self.assertTrue(cheque.present_move_id)

    def test_bounce_reversal_dates_to_origin_period_not_today(self):
        """A bounce reversal must land in the original present entry's
        period, not today. We back-date the present move into an earlier
        open period, then bounce; the reversal must inherit that date."""
        cheque = self._registered_incoming(cheque_number='LOCK-BOUNCE')
        cheque.action_present()
        present_move = cheque.present_move_id
        self.assertTrue(present_move)

        # Seed historical evidence directly; production has no unseal bypass.
        origin_date = self.today - timedelta(days=20)
        self._redate_present_move_fixture(present_move, origin_date)

        cheque._mark_bounced(
            reason=self.reason_funds,
            dishonour_date=origin_date,
        )
        self.assertEqual(cheque.state, 'bounced')
        reversal = cheque.bounce_move_id
        self.assertTrue(reversal, "bounce must post a reversal entry")
        self.assertEqual(
            reversal.date, origin_date,
            "bounce reversal must date to the present entry's period, "
            "not today",
        )
        self.assertNotEqual(
            reversal.date, self.today,
            "reversal must not silently date to today",
        )

    def test_bounce_reversal_requires_current_date_when_dishonour_locked(self):
        """Locked dishonour date needs explicit current-period treatment."""
        lock_field = self._lock_field()
        if not lock_field:
            self.skipTest("No lock-date field on this Odoo version.")
        cheque = self._registered_incoming(cheque_number='LOCK-ROLL')
        cheque.action_present()
        present_move = cheque.present_move_id

        origin_date = self.today - timedelta(days=40)
        self._redate_present_move_fixture(present_move, origin_date)
        # Lock the origin period but leave a recent open window.
        lock_date = self.today - timedelta(days=10)
        self.company.sudo().write({lock_field: lock_date})

        with self.assertRaises(UserError):
            cheque._mark_bounced(
                reason=self.reason_funds,
                dishonour_date=origin_date,
            )
        cheque._mark_bounced(
            reason=self.reason_funds,
            dishonour_date=origin_date,
            force_current_date=True,
        )
        reversal = cheque.bounce_move_id
        self.assertTrue(reversal)
        self.assertEqual(
            reversal.date, self.today,
            "explicit current-date treatment must use the operator date",
        )
