# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Uniform fold-consistency tests.

The server-side normalization pass (_eh_normalize_fold on
eh.account.dynamic.report) enforces one invariant identically across every
report when options['lazy_expand'] is set:

    a line is unfoldable IFF (it is a lazy leaf OR it has a child line in
    the payload, i.e. some other line's parent_id == this line's id).

These tests render each report through the orchestrator with lazy_expand and
assert that invariant holds with zero exceptions, so:

* no line is left unfoldable with zero children and not lazy (no stray
  caret on a flat row: cash-flow / executive-summary / bank-reconciliation
  section headers, an empty bank-rec section line, partner/opening/total
  rows);
* no line that actually nests rows is left non-foldable.

cash_flow / executive_summary / bank_reconciliation get a dedicated check
that they expose NO foldable line except where real children exist.
"""

from odoo import fields
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration',
        'post_install', '-at_install')
class TestFoldConsistency(EhAccountIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'mode': 'range',
                     'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
            'lazy_expand': True,
        }
        # A small but representative posting so every report has lines:
        # a partner-attached receivable + revenue, a cash payment, and a
        # prior-period opening so opening rows exist too.
        self.post_balanced_move(
            [
                {'account': self.account_receivable, 'debit': 500.0,
                 'partner': self.partner_a},
                {'account': self.account_revenue, 'credit': 500.0},
            ],
            date=fields.Date.from_string('2025-12-15'),
        )
        self.post_balanced_move(
            [
                {'account': self.account_receivable, 'debit': 1000.0,
                 'partner': self.partner_a},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 300.0,
                 'partner': self.partner_a},
                {'account': self.account_receivable, 'credit': 300.0,
                 'partner': self.partner_a},
            ],
            date=fields.Date.from_string('2026-06-20'),
        )
        self.post_balanced_move(
            [
                {'account': self.account_expense, 'debit': 200.0,
                 'partner': self.partner_b},
                {'account': self.account_payable, 'credit': 200.0,
                 'partner': self.partner_b},
            ],
            date=fields.Date.from_string('2026-07-01'),
        )

    def _render(self, code):
        report = self.env['eh.account.dynamic.report'].search(
            [('code', '=', code)], limit=1)
        self.assertTrue(report, "report %s must be seeded" % code)
        return report.render(self.options, use_cache=False)

    @staticmethod
    def _parents_with_children(lines):
        parents = set()
        for line in lines:
            parent_id = line.get('parent_id')
            if parent_id:
                parents.add(parent_id)
        return parents

    def _assert_fold_invariant(self, code):
        payload = self._render(code)
        lines = payload['lines']
        parents = self._parents_with_children(lines)
        for line in lines:
            line_id = line.get('id')
            is_lazy = line.get('lazy') is True
            has_child = line_id in parents
            should_fold = is_lazy or has_child
            self.assertEqual(
                bool(line.get('unfoldable')), should_fold,
                "[%s] line %r: unfoldable=%r but lazy=%r has_child=%r; "
                "a caret must appear IFF (lazy OR has child)" % (
                    code, line_id, line.get('unfoldable'),
                    is_lazy, has_child),
            )
            # A foldable structural group (has children, not lazy) must
            # default OPEN; a lazy leaf must start collapsed.
            if has_child and not is_lazy:
                self.assertNotEqual(
                    line.get('unfolded'), False,
                    "[%s] group %r with children must default open" % (
                        code, line_id),
                )
            if is_lazy:
                self.assertFalse(
                    line.get('unfolded'),
                    "[%s] lazy leaf %r must start collapsed" % (
                        code, line_id),
                )
        return payload

    # ---- the invariant holds across every report ----

    def test_fold_invariant_trial_balance(self):
        self._assert_fold_invariant('trial_balance')

    def test_fold_invariant_profit_and_loss(self):
        self._assert_fold_invariant('profit_and_loss')

    def test_fold_invariant_balance_sheet(self):
        self._assert_fold_invariant('balance_sheet')

    def test_fold_invariant_general_ledger(self):
        self._assert_fold_invariant('general_ledger')

    def test_fold_invariant_aged_receivable(self):
        self._assert_fold_invariant('aged_receivable')

    def test_fold_invariant_partner_ledger(self):
        self._assert_fold_invariant('partner_ledger')

    def test_fold_invariant_cash_flow(self):
        self._assert_fold_invariant('cash_flow')

    def test_fold_invariant_executive_summary(self):
        self._assert_fold_invariant('executive_summary')

    def test_fold_invariant_bank_reconciliation(self):
        self._assert_fold_invariant('bank_reconciliation')

    # ---- flat reports expose no stray carets ----

    def _assert_no_stray_caret(self, code):
        payload = self._assert_fold_invariant(code)
        lines = payload['lines']
        parents = self._parents_with_children(lines)
        foldables = [
            l for l in lines if l.get('unfoldable')
        ]
        for line in foldables:
            # Every foldable line here must be foldable for a REAL reason:
            # it is lazy, or it genuinely has a child in the payload.
            self.assertTrue(
                line.get('lazy') is True or line.get('id') in parents,
                "[%s] line %r has a caret with nothing to expand" % (
                    code, line.get('id')),
            )

    def test_cash_flow_no_stray_carets(self):
        # cash_flow is flat: section_header + section_line + section_total,
        # none with children -> zero foldable lines.
        payload = self._render('cash_flow')
        self.assertFalse(
            any(l.get('unfoldable') for l in payload['lines']),
            "cash_flow must have no foldable rows (it is flat)",
        )

    def test_executive_summary_no_stray_carets(self):
        payload = self._render('executive_summary')
        self.assertFalse(
            any(l.get('unfoldable') for l in payload['lines']),
            "executive_summary must have no foldable rows (it is flat)",
        )

    def test_bank_reconciliation_no_stray_carets(self):
        # A bank-rec section header / section line with no open items must
        # NOT carry a caret; only a section line that nests outstanding
        # items stays foldable.
        self._assert_no_stray_caret('bank_reconciliation')

    # ---- eager / non-lazy paths are NOT normalized ----

    def test_eager_path_not_normalized(self):
        # With eager_expand the normalization is a no-op: the partner-ledger
        # partner header keeps its legacy unfoldable=False shape and the aml
        # rows are inlined.
        report = self.env['eh.account.dynamic.report'].search(
            [('code', '=', 'partner_ledger')], limit=1)
        opts = dict(self.options, eager_expand=True)
        payload = report.render(opts, use_cache=False)
        kinds = [(l.get('meta') or {}).get('kind') for l in payload['lines']]
        self.assertIn('aml', kinds, "eager path must inline aml rows")
        header = next(
            l for l in payload['lines']
            if (l.get('meta') or {}).get('kind') == 'partner_header')
        self.assertFalse(header.get('unfoldable'))
