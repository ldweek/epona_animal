# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tests for fuzzy bank-line duplicate detection.

The exact-file `unique_import_ref` constraint already covers byte-
identical re-imports. This module covers the harder case the new
`_eh_find_probable_duplicate` method handles: two lines on the same
journal that look like the same payment but were imported from
different sources, so their per-line refs differ.

Cases:
* Same date, same amount, no narration on either side: matches.
* Same amount, date within ±3 days, narration substring overlap: matches.
* Same date, amount differs by 1c: no match (amount tolerance is exact).
* Same date, amount, but narration totally disjoint: no match.
* Date outside ±3 day window: no match.
* exclude_id parameter prevents self-match.
"""

from datetime import date, timedelta

from lxml import etree

from odoo.tests import tagged
from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_bank_statement_import', 'integration', 'post_install', '-at_install')
class TestBankDuplicateDetection(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SLine = cls.env['account.bank.statement.line']
        cls.Statement = cls.env['account.bank.statement']
        cls.bank_journal = cls.env['account.journal'].search(
            [('type', '=', 'bank'),
             ('company_id', '=', cls.env.company.id)],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Test Bank',
                'code': 'TBK',
                'type': 'bank',
                'company_id': cls.env.company.id,
            })

    def _make_line(self, **vals):
        # Create one statement to host the lines so the foreign key
        # to account.bank.statement is satisfied. In Odoo 19 the
        # line's `date` is a non-stored related field on move_id, so
        # we cannot set it through line vals: we must back-fill it
        # on the underlying move after the line + move are created.
        line_date = vals.pop('date', date.today())
        statement = self.Statement.create({
            'journal_id': self.bank_journal.id,
            'name': 'Dup test stmt',
            'date': line_date,
        })
        line_vals = dict(vals)
        line_vals['statement_id'] = statement.id
        line_vals.setdefault('journal_id', self.bank_journal.id)
        line_vals.setdefault('payment_ref', '/')
        line = self.SLine.create(line_vals)
        # Force the date on the underlying move so the line's related
        # `date` field reflects what the test expects. The line's
        # state may need toggling to draft to allow the write.
        if line.move_id:
            line.move_id.button_draft()
            line.move_id.date = line_date
            line.move_id.action_post()
        line.invalidate_recordset(['date'])
        return line

    def test_same_date_same_amount_matches(self):
        a = self._make_line(date=date(2026, 5, 1), amount=100.0)
        match = self.SLine._eh_find_probable_duplicate(
            journal_id=self.bank_journal.id,
            line_date=date(2026, 5, 1),
            amount=100.0,
            payment_ref=None,
            narration=None,
            exclude_id=False,
        )
        self.assertEqual(match, a)

    def test_duplicate_form_carries_journal_domain_dependency(self):
        view = self.env.ref(
            'eh_account_bank_statement_import.'
            'view_eh_bsi_duplicate_line_form',
        )
        arch = etree.fromstring(view.arch_db.encode())
        self.assertTrue(
            arch.xpath(
                ".//field[@name='suitable_journal_ids']",
            ),
        )
        self.assertTrue(
            arch.xpath(".//field[@name='company_id'][@invisible='1']"),
        )

    def test_within_3_day_window_matches(self):
        a = self._make_line(date=date(2026, 5, 1), amount=100.0)
        match = self.SLine._eh_find_probable_duplicate(
            journal_id=self.bank_journal.id,
            line_date=date(2026, 5, 4),  # 3 days later
            amount=100.0,
        )
        self.assertEqual(match, a)

    def test_outside_window_no_match(self):
        self._make_line(date=date(2026, 5, 1), amount=100.0)
        match = self.SLine._eh_find_probable_duplicate(
            journal_id=self.bank_journal.id,
            line_date=date(2026, 5, 5),  # 4 days later, outside window
            amount=100.0,
        )
        self.assertFalse(match)

    def test_amount_differs_by_cent_no_match(self):
        self._make_line(date=date(2026, 5, 1), amount=100.0)
        match = self.SLine._eh_find_probable_duplicate(
            journal_id=self.bank_journal.id,
            line_date=date(2026, 5, 1),
            amount=100.01,  # 1c off; tolerance is +/- 0.005 of cents
        )
        self.assertFalse(match)

    def test_narration_substring_matches(self):
        a = self._make_line(
            date=date(2026, 5, 1), amount=200.0,
            narration='ACME PAYMENT REF 123',
        )
        match = self.SLine._eh_find_probable_duplicate(
            journal_id=self.bank_journal.id,
            line_date=date(2026, 5, 1),
            amount=200.0,
            narration='ACME 123',
        )
        # 'ACME 123' is not a substring of the longer narration nor
        # vice versa, so substring matching alone fails - but the
        # validator also accepts equality + substring on payment_ref.
        # This case should not match because neither narration fully
        # contains the other and the test does not provide a ref.
        self.assertFalse(match)

        # Now make one a substring of the other; expect a match.
        match = self.SLine._eh_find_probable_duplicate(
            journal_id=self.bank_journal.id,
            line_date=date(2026, 5, 1),
            amount=200.0,
            narration='ACME PAYMENT',
        )
        self.assertEqual(match, a)

    def test_disjoint_narration_no_match(self):
        self._make_line(
            date=date(2026, 5, 1), amount=300.0,
            narration='ACME PAYMENT',
        )
        match = self.SLine._eh_find_probable_duplicate(
            journal_id=self.bank_journal.id,
            line_date=date(2026, 5, 1),
            amount=300.0,
            narration='UNRELATED VENDOR',
        )
        self.assertFalse(match)

    def test_payment_ref_substring_matches(self):
        a = self._make_line(
            date=date(2026, 5, 1), amount=400.0,
            payment_ref='STR-INV-12345',
        )
        match = self.SLine._eh_find_probable_duplicate(
            journal_id=self.bank_journal.id,
            line_date=date(2026, 5, 1),
            amount=400.0,
            payment_ref='INV-12345',
        )
        self.assertEqual(match, a)

    def test_exclude_id_prevents_self_match(self):
        a = self._make_line(date=date(2026, 5, 1), amount=500.0)
        match = self.SLine._eh_find_probable_duplicate(
            journal_id=self.bank_journal.id,
            line_date=date(2026, 5, 1),
            amount=500.0,
            exclude_id=a.id,
        )
        self.assertFalse(match)

    def test_batch_scan_excludes_same_import_siblings(self):
        first = self._make_line(
            date=date(2026, 5, 7), amount=321.0,
            payment_ref='SAME-FILE-REF',
        )
        second = self._make_line(
            date=date(2026, 5, 7), amount=321.0,
            payment_ref='SAME-FILE-REF',
        )
        matches = self.SLine._eh_find_probable_duplicates(
            first | second, prior_max_id=0,
        )
        self.assertEqual(matches, {})

        prior = self._make_line(
            date=date(2026, 5, 8), amount=777.0,
            payment_ref='PRIOR-REF',
        )
        prior_max_id = prior.id
        inbound = self._make_line(
            date=date(2026, 5, 9), amount=777.0,
            payment_ref='PRIOR-REF',
        ) | self._make_line(
            date=date(2026, 5, 9), amount=888.0,
            payment_ref='UNRELATED',
        )
        matches = self.SLine._eh_find_probable_duplicates(
            inbound, prior_max_id=prior_max_id,
        )
        self.assertEqual(matches[inbound[0].id], prior)
        self.assertNotIn(inbound[1].id, matches)
