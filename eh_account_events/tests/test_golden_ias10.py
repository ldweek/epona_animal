# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 10 worked examples: the authorised-for-issue date boundary.

IAS 10.3 defines events after the reporting period as those occurring
between the end of the reporting period and the date the financial
statements are authorised for issue. An adjusting event inside that window
is booked back to the reporting date; an event dated after the
authorisation date belongs to the NEXT reporting period, so booking it into
the closed period is blocked and it is flagged for the next period's
disclosure list instead.

Every expected amount below is stated in the test inputs; the entries are
two-leg by construction (debit account / credit account for the estimated
effect), so the assertions are exact.
"""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_events', 'post_install', '-at_install')
class TestGoldenIas10(EhGoldenTestCase):
    """IAS 10 golden examples for the authorised-for-issue gate."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Booking an adjusting entry is manager-gated. The group_ids /
        # groups_id field split across Odoo series is resolved at runtime.
        field = ('group_ids' if 'group_ids' in cls.env.user._fields
                 else 'groups_id')
        cls.env.user.write({field: [
            (4, cls.env.ref('eh_account_base.group_eh_manager').id)]})

    def _event(self, **vals):
        base = {
            'name': 'Court settlement confirmed',
            'reporting_date': '2026-12-31',
            'is_adjusting': True,
            'category': 'litigation',
            'estimated_effect': 50000.0,
            'journal_id': self.journal_misc.id,
            'debit_account_id': self.account_expense.id,
            'credit_account_id': self.account_payable.id,
        }
        base.update(vals)
        return self.env['eh.subsequent.event'].create(base)

    def test_golden_event_after_authorisation_refused(self):
        """IAS 10.3: an event dated after the authorised-for-issue date is
        not an event after the reporting period for THESE statements.

        Inputs: reporting period ends 2026-12-31; statements authorised for
        issue 2027-03-31; adjusting litigation event dated 2027-04-15 for
        50,000. The event window is 2027-01-01 .. 2027-03-31, so 2027-04-15
        falls outside it: register creation is refused with a clear message,
        so it must be recorded against the following reporting period.
        """
        with self.assertRaisesRegex(ValidationError, 'following reporting'):
            self._event(
                event_date='2027-04-15',
                authorized_for_issue_date='2027-03-31')

    def test_golden_event_before_authorisation_posts_to_reporting_date(self):
        """IAS 10.8 regression: an adjusting event inside the window books
        its balanced entry back to the reporting date.

        Inputs: reporting period ends 2026-12-31; authorised for issue
        2027-03-31; adjusting litigation event dated 2027-01-20 for 50,000.
        2027-01-20 is inside the IAS 10.3 window, so the booking proceeds
        exactly as before the authorisation gate existed:

          Dr Cost of Sales (5000)     50,000.00
          Cr Trade Payables (2100)              50,000.00
          entry dated 2026-12-31 (the reporting date, not the event date)
        """
        e = self._event(
            event_date='2027-01-20',
            authorized_for_issue_date='2027-03-31')
        self.assertFalse(e.next_period)
        e.action_book_adjusting_entry()
        self.assertEqual(e.state, 'posted')
        move = e.move_id
        self.assertEqual(move.state, 'posted')
        self.assertEqual(str(move.date), '2026-12-31')
        self.assertBalanced(move)
        self.assertMoveLines(move, [
            (self.account_expense, 50000.0, 0.0),
            (self.account_payable, 0.0, 50000.0),
        ])

    def test_golden_non_adjusting_after_date_uses_next_reporting_period(self):
        """A non-adjusting (disclose-only) event after the authorisation
        date must be registered against the following reporting period; the
        same event inside the window remains a disclose-only IAS 10 row."""
        with self.assertRaises(ValidationError):
            self._event(
                name='Warehouse fire', is_adjusting=False, category='other',
                event_date='2027-04-10',
                authorized_for_issue_date='2027-03-31',
                estimated_effect=200000.0)
        inside = self._event(
            name='Warehouse fire (early)', is_adjusting=False,
            category='other', event_date='2027-02-01',
            authorized_for_issue_date='2027-03-31',
            estimated_effect=200000.0)
        self.assertEqual(inside.treatment, 'disclose')
        self.assertFalse(inside.next_period)

    def test_authorisation_date_cannot_precede_reporting_date(self):
        """IAS 10.4-6 guardrail: statements are authorised for issue after
        the period they report on ends, so an authorisation date before the
        reporting date is refused."""
        with self.assertRaises(ValidationError):
            self._event(
                event_date='2027-01-20',
                authorized_for_issue_date='2026-06-30')

    def test_no_authorisation_date_preserves_legacy_flow(self):
        """Regression: with no authorised-for-issue date recorded (all
        pre-existing registers), behaviour is exactly as before - the event
        is not flagged and the adjusting entry books to the reporting
        date."""
        e = self._event(event_date='2027-01-20',
                        authorized_for_issue_date=False)
        self.assertFalse(e.next_period)
        e.action_book_adjusting_entry()
        self.assertEqual(e.state, 'posted')
        self.assertEqual(str(e.move_id.date), '2026-12-31')
        self.assertMoveLines(e.move_id, [
            (self.account_expense, 50000.0, 0.0),
            (self.account_payable, 0.0, 50000.0),
        ])

    def test_authorisation_date_frozen_after_booking(self):
        """The gate input is frozen once the adjusting entry is posted, the
        same as every other field feeding the entry."""
        e = self._event(event_date='2027-01-20',
                        authorized_for_issue_date='2027-03-31')
        e.action_book_adjusting_entry()
        with self.assertRaises(UserError):
            e.write({'authorized_for_issue_date': '2027-06-30'})
        self.assertEqual(str(e.authorized_for_issue_date), '2027-03-31')

    def test_stale_default_cleared_on_programmatic_create(self):
        """Production regression: on a database with a posted year-end
        close, the authorised-for-issue default is the prior period's
        posting date. A programmatic create (import, XML-RPC, code) never
        runs the form onchange, so create() must drop that stale default
        for a later reporting period instead of tripping the IAS 10.4-6
        constraint. Simulated by patching the default lookup, since no
        posted year-end run survives a test transaction."""
        Model = type(self.env['eh.subsequent.event'])
        with patch.object(
                Model, '_default_authorized_for_issue_date',
                lambda model: date(2026, 6, 30)):
            # Reporting period 2026-12-31 ends after the stale default:
            # the default is dropped and the create succeeds unbounded.
            e = self._event(event_date='2027-01-20')
            self.assertFalse(e.authorized_for_issue_date)
            self.assertFalse(e.next_period)
            # A default consistent with the reporting period is kept.
            kept = self._event(reporting_date='2026-03-31',
                               event_date='2026-04-10')
            self.assertEqual(
                str(kept.authorized_for_issue_date), '2026-06-30')
            # An EXPLICIT inconsistent date is still a data error.
            with self.assertRaises(ValidationError):
                self._event(event_date='2027-01-20',
                            authorized_for_issue_date='2026-03-01')

    def test_authorisation_date_is_tracked(self):
        # Chatter tracking counts are unobservable in tests; assert the
        # field-level tracking flag instead.
        self.assertTrue(
            self.env['eh.subsequent.event']._fields[
                'authorized_for_issue_date'].tracking)
