# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 8 / IAS 10 register tests."""

from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_events', 'integration', 'post_install', '-at_install')
class TestEvents(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    def test_policy_change_is_retrospective(self):
        c = self.env['eh.accounting.change'].create({
            'change_type': 'policy_change'})
        self.assertEqual(c.application, 'retrospective')

    def test_estimate_change_is_prospective(self):
        c = self.env['eh.accounting.change'].create({
            'change_type': 'estimate_change'})
        self.assertEqual(c.application, 'prospective')

    def test_restatement_lines(self):
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'as_previously_reported': 10000.0,
                        'adjustment': -1500.0}),
                (0, 0, {'name': 'Retained earnings',
                        'role': 'retained_earnings',
                        'as_previously_reported': 50000.0,
                        'adjustment': -1500.0}),
            ],
        })
        inv = c.line_ids.filtered(lambda l: l.name == 'Inventory')
        self.assertAlmostEqual(inv.as_restated, 8500.0, places=2)
        self.assertAlmostEqual(c.retained_earnings_impact, -1500.0, places=2)

    def test_adjusting_event(self):
        e = self.env['eh.subsequent.event'].create({
            'name': 'Court settlement', 'reporting_date': '2026-12-31',
            'event_date': '2027-01-20', 'is_adjusting': True,
            'category': 'litigation', 'estimated_effect': 50000.0})
        self.assertEqual(e.treatment, 'adjust')

    def test_non_adjusting_event(self):
        e = self.env['eh.subsequent.event'].create({
            'name': 'Fire at warehouse', 'reporting_date': '2026-12-31',
            'event_date': '2027-02-01', 'is_adjusting': False,
            'category': 'other', 'estimated_effect': 200000.0})
        self.assertEqual(e.treatment, 'disclose')

    def test_cross_company_journal_configuration_is_rejected(self):
        other_company = self.env['res.company'].create({
            'name': 'Events Foreign Company',
        })
        foreign_journal = self.env['account.journal'].with_company(
            other_company,
        ).create({
            'name': 'Foreign Events Journal',
            'code': 'FEV',
            'type': 'general',
            'company_id': other_company.id,
        })
        with self.assertRaises(UserError):
            self.env['eh.accounting.change'].create({
                'company_id': self.company.id,
                'change_type': 'error_correction',
                'journal_id': foreign_journal.id,
            })
        with self.assertRaises(UserError):
            self.env['eh.subsequent.event'].create({
                'name': 'Foreign journal event',
                'company_id': self.company.id,
                'reporting_date': '2026-12-31',
                'event_date': '2027-01-20',
                'journal_id': foreign_journal.id,
            })

    def test_post_restatement_balanced(self):
        # Error correction: inventory written down by 1500, opening retained
        # earnings falls by 1500 (net impact -1500). Posting books a balanced
        # entry: debit retained earnings 1500, credit the inventory account.
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'as_previously_reported': 10000.0,
                        'adjustment': -1500.0}),
            ],
        })
        self.assertEqual(c.state, 'draft')
        self.assertAlmostEqual(c.retained_earnings_impact, -1500.0, places=2)
        c.action_post_restatement()
        self.assertEqual(c.state, 'posted')
        self.assertTrue(c.move_id)
        self.assertEqual(c.move_id.state, 'posted')
        self.assertEqual(c.move_count, 1)
        move = c.move_id
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2)
        re_line = move.line_ids.filtered(
            lambda l: l.account_id == self.account_equity)
        self.assertAlmostEqual(re_line.debit, 1500.0, places=2)
        # Posting again raises.
        with self.assertRaises(UserError):
            c.action_post_restatement()

    def test_post_restatement_requires_manager(self):
        # A non-manager (plain EH accounting user) cannot post the opening
        # retained-earnings restatement; the record stays draft and unposted.
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'as_previously_reported': 10000.0,
                        'adjustment': -1500.0}),
            ],
        })
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'events_plain@test',
            'email': 'events_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            c.with_user(user).action_post_restatement()
        self.assertEqual(c.state, 'draft')
        self.assertFalse(c.move_id)

    def test_posted_change_cannot_be_deleted(self):
        # Once posted, the change carries the restatement audit trail and must
        # not be user-deletable.
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'as_previously_reported': 10000.0,
                        'adjustment': -1500.0}),
            ],
        })
        c.action_post_restatement()
        self.assertEqual(c.state, 'posted')
        with self.assertRaises(UserError):
            c.unlink()

    def test_posted_restatement_frozen_out_of_band(self):
        # A posted restatement must not be silently altered: the fields that
        # feed the opening retained-earnings entry are frozen once posted, so
        # an out-of-band write is rejected.
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'as_previously_reported': 10000.0,
                        'adjustment': -1500.0}),
            ],
        })
        c.action_post_restatement()
        self.assertEqual(c.state, 'posted')
        # Retargeting the entry accounts is blocked.
        with self.assertRaises(UserError):
            c.write({'adjustment_account_id': self.account_equity.id})
        # Re-dating the restatement is blocked.
        with self.assertRaises(UserError):
            c.write({'change_date': '2020-01-01'})
        # Editing the restatement lines is blocked.
        with self.assertRaises(UserError):
            c.write({'line_ids': [
                (0, 0, {'name': 'Extra', 'adjustment': -999.0})]})
        # The record and its move are untouched.
        self.assertEqual(c.adjustment_account_id, self.account_receivable)
        self.assertEqual(len(c.line_ids), 1)
        self.assertEqual(c.move_id.state, 'posted')
        # A DIRECT write/unlink on a child restatement line must also be
        # blocked: line.adjustment feeds the stored retained-earnings impact
        # and the disclosure, so an out-of-band line edit would drift the
        # record off the posted move.
        with self.assertRaises(UserError):
            c.line_ids[0].write({'adjustment': -9999.0})
        with self.assertRaises(UserError):
            c.line_ids[0].unlink()
        self.assertAlmostEqual(c.line_ids[0].adjustment, -1500.0, places=2)
        # A DIRECT create appending a new line to the posted restatement must
        # also be blocked: it would recompute the stored retained-earnings
        # impact off the posted move, drifting the parent figures away from the
        # entry in the ledger. Freezing a posted restatement requires a create
        # guard alongside write/unlink.
        with self.assertRaises(UserError):
            self.env['eh.accounting.change.line'].create({
                'change_id': c.id, 'name': 'Appended', 'adjustment': -777.0})
        self.assertEqual(len(c.line_ids), 1)
        self.assertAlmostEqual(
            c.retained_earnings_impact, -1500.0, places=2)

    def test_reset_to_draft_reverses_move(self):
        # The sanctioned correction path is a manager-gated reset that reverses
        # the posted move and reopens the record; a non-manager cannot use it.
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'as_previously_reported': 10000.0,
                        'adjustment': -1500.0}),
            ],
        })
        c.action_post_restatement()
        original_move = c.move_id
        user = self.env['res.users'].create({
            'name': 'p2', 'login': 'events_plain2@test',
            'email': 'events_plain2@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            c.with_user(user).action_reset_to_draft()
        self.assertEqual(c.state, 'posted')
        # Manager reset reverses the move and reopens the record.
        c.action_reset_to_draft()
        self.assertEqual(c.state, 'draft')
        self.assertFalse(c.move_id)
        self.assertEqual(original_move.state, 'posted')
        reversal = self.env['account.move'].search(
            [('reversed_entry_id', '=', original_move.id)])
        self.assertTrue(reversal)
        self.assertEqual(reversal.state, 'posted')
        # Reopened record is editable again and can be re-posted.
        c.write({'change_date': '2026-06-30'})
        c.action_post_restatement()
        self.assertEqual(c.state, 'posted')

    def test_event_move_provenance_and_reversal_links_are_immutable(self):
        change = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'Inventory', 'adjustment': -1500.0,
            })],
        })
        change.action_post_restatement()
        original = change.move_id
        self.assertEqual(original.eh_accounting_change_id, change)
        self.assertFalse(original.eh_subsequent_event_id)
        self.assertEqual(original.eh_event_provenance_version, 1)

        # A client-supplied truthy context value is not the engine's object
        # capability and cannot forge or clear source evidence.
        with self.assertRaises(AccessError):
            original.with_context(
                eh_event_move_capability=True,
            ).write({'eh_accounting_change_id': False})
        with self.assertRaises(AccessError):
            original.write({'eh_event_provenance_version': 0})
        with self.assertRaises(AccessError):
            self.env['account.move'].with_context(
                eh_event_move_capability=True,
            ).create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': '2026-01-01',
                'eh_accounting_change_id': change.id,
            })

        change.action_reset_to_draft()
        reversal = self.env['account.move'].search([
            ('reversed_entry_id', '=', original.id),
        ])
        self.assertEqual(reversal.eh_accounting_change_id, change)
        self.assertEqual(reversal.reversed_entry_id, original)
        self.assertEqual(reversal.eh_event_provenance_version, 1)
        with self.assertRaises(AccessError):
            reversal.with_context(
                eh_event_move_capability=True,
            ).write({'reversed_entry_id': False})

    def test_parent_write_locks_only_evidence_mutations(self):
        change = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
        })
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            change.write({'notes': 'routine descriptive edit'})
        self.assertFalse(any(
            'eh_accounting_change' in query and 'FOR UPDATE' in query
            for query in queries
        ))
        queries.clear()
        with patch.object(self.env.cr, 'execute', _record_execute):
            change.write({'change_date': '2027-01-01'})
        self.assertTrue(any(
            'eh_accounting_change' in query and 'FOR UPDATE' in query
            for query in queries
        ))

    def test_estimate_change_cannot_post(self):
        c = self.env['eh.accounting.change'].create({
            'change_type': 'estimate_change',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Provision', 'adjustment': -1000.0}),
            ],
        })
        self.assertEqual(c.application, 'prospective')
        with self.assertRaises(UserError):
            c.action_post_restatement()
        self.assertEqual(c.state, 'draft')
        self.assertFalse(c.move_id)

    # --- Per-account comparative trail (IAS 8.22, 42) --------------------

    def test_comparative_multi_account_posts_per_account_balanced(self):
        # Opt-in per-account comparative trail. Two affected lines for the same
        # prior year (2025): inventory written down 1500, a receivable written
        # off 900. Posting books ONE per-account leg each (to the inventory and
        # receivable accounts) plus a single balancing opening-RE leg, all
        # netting to zero. The single lumped adjustment_account_id is NOT used.
        inv_acct = self._ensure_account(
            self.env, '1200', 'Inventory', 'asset_current')
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'comparative_mode': True,
            'retained_earnings_account_id': self.account_equity.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'fiscal_year': 2025,
                        'account_id': inv_acct.id,
                        'as_previously_reported': 10000.0,
                        'adjustment': -1500.0}),
                (0, 0, {'name': 'Trade receivables', 'fiscal_year': 2025,
                        'account_id': self.account_receivable.id,
                        'as_previously_reported': 8000.0,
                        'adjustment': -900.0}),
            ],
        })
        self.assertTrue(c.comparative_mode)
        # No lumped adjustment account is needed in comparative mode.
        self.assertFalse(c.adjustment_account_id)
        self.assertAlmostEqual(c.retained_earnings_impact, -2400.0, places=2)
        c.action_post_restatement()
        self.assertEqual(c.state, 'posted')
        move = c.move_id
        self.assertEqual(move.state, 'posted')
        # Balances by construction.
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2)
        # One leg per affected account, plus the opening-RE leg = 3 lines.
        self.assertEqual(len(move.line_ids), 3)
        inv_leg = move.line_ids.filtered(
            lambda l: l.account_id == inv_acct)
        rec_leg = move.line_ids.filtered(
            lambda l: l.account_id == self.account_receivable)
        re_leg = move.line_ids.filtered(
            lambda l: l.account_id == self.account_equity)
        # Negative adjustments credit the affected assets.
        self.assertAlmostEqual(inv_leg.credit, 1500.0, places=2)
        self.assertAlmostEqual(rec_leg.credit, 900.0, places=2)
        # Net impact of -2400 debits opening retained earnings by 2400.
        self.assertAlmostEqual(re_leg.debit, 2400.0, places=2)
        self.assertAlmostEqual(re_leg.credit, 0.0, places=2)

    def test_comparative_liability_uses_natural_credit_direction(self):
        liability = self._ensure_account(
            self.env, '2198', 'Restated provision', 'liability_current')
        change = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'comparative_mode': True,
            'retained_earnings_account_id': self.account_equity.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'Provision understated',
                'account_id': liability.id,
                'adjustment': 500.0,
            })],
        })
        # Positive presentation movement increases a liability, so its GL
        # effect is a credit and opening retained earnings decreases.
        self.assertAlmostEqual(
            change.retained_earnings_impact, -500.0, places=2)
        change.action_post_restatement()
        liability_line = change.move_id.line_ids.filtered(
            lambda line: line.account_id == liability)
        retained_line = change.move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_equity)
        self.assertAlmostEqual(liability_line.credit, 500.0, places=2)
        self.assertAlmostEqual(liability_line.debit, 0.0, places=2)
        self.assertAlmostEqual(retained_line.debit, 500.0, places=2)
        self.assertAlmostEqual(retained_line.credit, 0.0, places=2)

    def test_comparative_zero_net_balance_sheet_reclassification_posts(self):
        asset = self._ensure_account(
            self.env, '1298', 'Restated asset class', 'asset_current')
        liability = self._ensure_account(
            self.env, '2197', 'Restated liability class', 'liability_current')
        change = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'comparative_mode': True,
            'retained_earnings_account_id': self.account_equity.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'Asset reclassification',
                    'account_id': asset.id,
                    'adjustment': 100.0,
                }),
                (0, 0, {
                    'name': 'Liability reclassification',
                    'account_id': liability.id,
                    'adjustment': 100.0,
                }),
            ],
        })
        self.assertAlmostEqual(change.retained_earnings_impact, 0.0, places=2)
        change.action_post_restatement()
        self.assertEqual(len(change.move_id.line_ids), 2)
        self.assertAlmostEqual(
            sum(change.move_id.line_ids.mapped('debit')), 100.0, places=2)
        self.assertAlmostEqual(
            sum(change.move_id.line_ids.mapped('credit')), 100.0, places=2)
        self.assertFalse(change.move_id.line_ids.filtered(
            lambda line: line.account_id == self.account_equity))

    def test_prior_period_restatement_rejects_current_pnl_accounts(self):
        with self.assertRaisesRegex(
            ValidationError, 'current-period income or expense',
        ):
            self.env['eh.accounting.change'].create({
                'change_type': 'error_correction',
                'retained_earnings_account_id': self.account_equity.id,
                'adjustment_account_id': self.account_expense.id,
                'journal_id': self.journal_misc.id,
                'line_ids': [(0, 0, {
                    'name': 'Prior expense caption',
                    'adjustment': -100.0,
                })],
            })
        with self.assertRaisesRegex(
            ValidationError, 'Prior-period profit or loss',
        ):
            self.env['eh.accounting.change'].create({
                'change_type': 'error_correction',
                'comparative_mode': True,
                'retained_earnings_account_id': self.account_equity.id,
                'journal_id': self.journal_misc.id,
                'line_ids': [(0, 0, {
                    'name': 'Prior expense caption',
                    'account_id': self.account_expense.id,
                    'adjustment': 100.0,
                })],
            })

    def test_comparative_needs_an_affected_account(self):
        # In comparative mode, a line with no account_id contributes nothing to
        # the per-account posting; with no affected account at all, posting is
        # refused rather than silently booking a one-sided entry.
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'comparative_mode': True,
            'retained_earnings_account_id': self.account_equity.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'adjustment': -1500.0}),
            ],
        })
        with self.assertRaises(UserError):
            c.action_post_restatement()
        self.assertEqual(c.state, 'draft')
        self.assertFalse(c.move_id)

    def test_comparative_rejects_any_nonzero_line_omitted_from_gl(self):
        inv_acct = self._ensure_account(
            self.env, '1209', 'Comparative Inventory', 'asset_current')
        for omitted_vals in (
            {},
            {'account_id': self.account_equity.id},
        ):
            with self.subTest(omitted_vals=omitted_vals):
                c = self.env['eh.accounting.change'].create({
                    'change_type': 'error_correction',
                    'comparative_mode': True,
                    'retained_earnings_account_id': self.account_equity.id,
                    'journal_id': self.journal_misc.id,
                    'line_ids': [
                        (0, 0, {
                            'name': 'Posted inventory',
                            'account_id': inv_acct.id,
                            'adjustment': -100.0,
                        }),
                        (0, 0, {
                            'name': 'Previously omitted material line',
                            'adjustment': -50.0,
                            **omitted_vals,
                        }),
                    ],
                })
                with self.assertRaises(UserError):
                    c.action_post_restatement()
                self.assertFalse(c.move_id)

    def test_posting_configuration_is_enforced_server_side(self):
        bank_journal = self.env['account.journal'].search([
            ('company_id', '=', self.company.id),
            ('type', '=', 'bank'),
        ], limit=1)
        if not bank_journal:
            bank_journal = self._ensure_journal(
                self.env, self.company, 'bank', 'EVBK', 'Events Bank',
                default_account=self.account_cash,
            )
        with self.assertRaises(ValidationError):
            self.env['eh.accounting.change'].create({
                'change_type': 'error_correction',
                'journal_id': bank_journal.id,
            })
        with self.assertRaises(ValidationError):
            self.env['eh.accounting.change'].create({
                'change_type': 'error_correction',
                'retained_earnings_account_id': self.account_expense.id,
            })
        with self.assertRaises(ValidationError):
            self.env['eh.accounting.change'].create({
                'change_type': 'error_correction',
                'retained_earnings_account_id': self.account_equity.id,
                'adjustment_account_id': self.account_equity.id,
            })
        with self.assertRaises(ValidationError):
            self.env['eh.subsequent.event'].create({
                'name': 'Invalid journal',
                'reporting_date': '2026-12-31',
                'event_date': '2027-01-01',
                'journal_id': bank_journal.id,
            })
        with self.assertRaises(ValidationError):
            self.env['eh.subsequent.event'].create({
                'name': 'Same-sided adjustment',
                'reporting_date': '2026-12-31',
                'event_date': '2027-01-01',
                'debit_account_id': self.account_expense.id,
                'credit_account_id': self.account_expense.id,
            })

    def test_comparative_restatement_frozen_after_posting(self):
        # A posted per-account comparative restatement and its lines are frozen
        # against create/write/unlink (the same audit-trail guard as the single
        # path), so the booked entry cannot be edited out from under itself.
        inv_acct = self._ensure_account(
            self.env, '1201', 'Inventory 2', 'asset_current')
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'comparative_mode': True,
            'retained_earnings_account_id': self.account_equity.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'fiscal_year': 2025,
                        'account_id': inv_acct.id, 'adjustment': -1500.0}),
                (0, 0, {'name': 'Receivables', 'fiscal_year': 2025,
                        'account_id': self.account_receivable.id,
                        'adjustment': -900.0}),
            ],
        })
        c.action_post_restatement()
        self.assertEqual(c.state, 'posted')
        # Toggling the mode on a posted record is blocked.
        with self.assertRaises(UserError):
            c.write({'comparative_mode': False})
        # Editing an affected line is blocked.
        with self.assertRaises(UserError):
            c.line_ids[0].write({'account_id': self.account_equity.id})
        with self.assertRaises(UserError):
            c.line_ids[0].write({'adjustment': -1.0})
        # Removing a line is blocked.
        with self.assertRaises(UserError):
            c.line_ids[1].unlink()
        # Appending a line via direct create is blocked.
        with self.assertRaises(UserError):
            self.env['eh.accounting.change.line'].create({
                'change_id': c.id, 'name': 'Extra',
                'account_id': inv_acct.id, 'adjustment': -5.0})
        self.assertEqual(len(c.line_ids), 2)
        self.assertAlmostEqual(c.retained_earnings_impact, -2400.0, places=2)

    def test_single_path_default_still_lumped(self):
        # The default (comparative_mode off) path is untouched: one lumped
        # opening-RE plug against the single adjustment_account_id, even when
        # the lines happen to carry affected accounts.
        c = self.env['eh.accounting.change'].create({
            'change_type': 'error_correction',
            'retained_earnings_account_id': self.account_equity.id,
            'adjustment_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'Inventory', 'adjustment': -1500.0}),
                (0, 0, {'name': 'Payables', 'adjustment': -900.0}),
            ],
        })
        self.assertFalse(c.comparative_mode)
        c.action_post_restatement()
        # Two-leg lumped entry regardless of the line count.
        self.assertEqual(len(c.move_id.line_ids), 2)

    # --- IAS 10 adjusting event booking (IAS 10.8) ----------------------

    def test_adjusting_event_books_entry(self):
        # An adjusting event books a balanced entry to the reporting period.
        e = self.env['eh.subsequent.event'].create({
            'name': 'Court settlement confirmed',
            'reporting_date': '2026-12-31', 'event_date': '2027-01-20',
            'is_adjusting': True, 'category': 'litigation',
            'estimated_effect': 50000.0,
            'journal_id': self.journal_misc.id,
            'debit_account_id': self.account_expense.id,
            'credit_account_id': self.account_payable.id,
        })
        self.assertEqual(e.treatment, 'adjust')
        self.assertEqual(e.state, 'draft')
        e.action_book_adjusting_entry()
        self.assertEqual(e.state, 'posted')
        self.assertTrue(e.move_id)
        self.assertEqual(e.move_id.state, 'posted')
        self.assertEqual(e.move_count, 1)
        move = e.move_id
        self.assertEqual(move.eh_subsequent_event_id, e)
        self.assertFalse(move.eh_accounting_change_id)
        self.assertEqual(move.eh_event_provenance_version, 1)
        # Booked to the reporting period, not the event date.
        self.assertEqual(str(move.date), '2026-12-31')
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2)
        debit_leg = move.line_ids.filtered(
            lambda l: l.account_id == self.account_expense)
        self.assertAlmostEqual(debit_leg.debit, 50000.0, places=2)
        # Booking again raises.
        with self.assertRaises(UserError):
            e.action_book_adjusting_entry()

    def test_negative_adjusting_effect_is_rejected_before_move_creation(self):
        event = self.env['eh.subsequent.event'].create({
            'name': 'Negative legacy direction',
            'reporting_date': '2026-12-31',
            'event_date': '2027-01-20',
            'is_adjusting': True,
            'category': 'litigation',
            'estimated_effect': -500.0,
            'journal_id': self.journal_misc.id,
            'debit_account_id': self.account_expense.id,
            'credit_account_id': self.account_payable.id,
        })
        with self.assertRaisesRegex(UserError, 'positive estimated'):
            event.action_book_adjusting_entry()
        self.assertFalse(event.move_id)
        self.assertEqual(event.state, 'draft')

    def test_dividend_declared_cannot_be_adjusting(self):
        """IAS 10.12-13: a dividend declared after the reporting period is a
        non-adjusting event and must not be recognised as a liability at the
        reporting date. Marking it adjusting is refused; disclose-only is
        fine."""
        with self.assertRaises(ValidationError):
            self.env['eh.subsequent.event'].create({
                'name': 'Final dividend', 'reporting_date': '2026-12-31',
                'event_date': '2027-01-15', 'is_adjusting': True,
                'category': 'dividend', 'estimated_effect': 100000.0})
        e = self.env['eh.subsequent.event'].create({
            'name': 'Final dividend', 'reporting_date': '2026-12-31',
            'event_date': '2027-01-15', 'is_adjusting': False,
            'category': 'dividend', 'estimated_effect': 100000.0})
        self.assertEqual(e.treatment, 'disclose')

    def test_posted_event_raw_state_reset_blocked(self):
        """A posted adjusting event may only be reopened through Reset to
        Draft (which reverses its move). A raw ORM state write is refused so
        the freeze cannot be lifted without reversing the entry."""
        e = self.env['eh.subsequent.event'].create({
            'name': 'Settlement', 'reporting_date': '2026-12-31',
            'event_date': '2027-01-20', 'is_adjusting': True,
            'category': 'litigation', 'estimated_effect': 50000.0,
            'journal_id': self.journal_misc.id,
            'debit_account_id': self.account_expense.id,
            'credit_account_id': self.account_payable.id,
        })
        e.action_book_adjusting_entry()
        self.assertEqual(e.state, 'posted')
        # 'state' is owned by eh.workflow.guard (su provenance). A plain,
        # non-superuser RPC write of state is refused with AccessError, so the
        # posted freeze cannot be lifted from a client without going through
        # action_reset_to_draft (which reverses the move). The test env is
        # superuser, for which the guard is correctly a no-op, so the negative
        # path is exercised as a genuine non-superuser.
        guard_user = self.env['res.users'].create({
            'name': 'EH Events Plain User',
            'login': 'eh_events_plain_user',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(AccessError):
            e.with_user(guard_user).write({'state': 'draft'})
        self.assertEqual(e.state, 'posted')
        # The sanctioned reset still works and reverses the move.
        e.action_reset_to_draft()
        self.assertEqual(e.state, 'draft')
        self.assertFalse(e.move_id)

    def test_non_adjusting_event_cannot_book(self):
        # A disclose-only event must not book an entry (IAS 10.10).
        e = self.env['eh.subsequent.event'].create({
            'name': 'Warehouse fire', 'reporting_date': '2026-12-31',
            'event_date': '2027-02-01', 'is_adjusting': False,
            'category': 'other', 'estimated_effect': 200000.0,
            'journal_id': self.journal_misc.id,
            'debit_account_id': self.account_expense.id,
            'credit_account_id': self.account_payable.id,
        })
        self.assertEqual(e.treatment, 'disclose')
        with self.assertRaises(UserError):
            e.action_book_adjusting_entry()
        self.assertEqual(e.state, 'draft')
        self.assertFalse(e.move_id)

    def test_adjusting_event_booking_requires_manager(self):
        e = self.env['eh.subsequent.event'].create({
            'name': 'Settlement', 'reporting_date': '2026-12-31',
            'event_date': '2027-01-20', 'is_adjusting': True,
            'category': 'litigation', 'estimated_effect': 50000.0,
            'journal_id': self.journal_misc.id,
            'debit_account_id': self.account_expense.id,
            'credit_account_id': self.account_payable.id,
        })
        user = self.env['res.users'].create({
            'name': 'p3', 'login': 'events_plain3@test',
            'email': 'events_plain3@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            e.with_user(user).action_book_adjusting_entry()
        self.assertEqual(e.state, 'draft')
        self.assertFalse(e.move_id)

    def test_adjusting_event_frozen_after_booking(self):
        # Once booked, the fields feeding the entry are frozen, the record
        # cannot be deleted, and the sanctioned reset reverses the move.
        e = self.env['eh.subsequent.event'].create({
            'name': 'Settlement', 'reporting_date': '2026-12-31',
            'event_date': '2027-01-20', 'is_adjusting': True,
            'category': 'litigation', 'estimated_effect': 50000.0,
            'journal_id': self.journal_misc.id,
            'debit_account_id': self.account_expense.id,
            'credit_account_id': self.account_payable.id,
        })
        e.action_book_adjusting_entry()
        self.assertEqual(e.state, 'posted')
        original_move = e.move_id
        # Frozen fields are blocked.
        with self.assertRaises(UserError):
            e.write({'estimated_effect': 60000.0})
        with self.assertRaises(UserError):
            e.write({'debit_account_id': self.account_cash.id})
        with self.assertRaises(UserError):
            e.write({'reporting_date': '2025-12-31'})
        # Deletion is blocked.
        with self.assertRaises(UserError):
            e.unlink()
        # Manager reset reverses the move and reopens the record.
        e.action_reset_to_draft()
        self.assertEqual(e.state, 'draft')
        self.assertFalse(e.move_id)
        self.assertEqual(original_move.state, 'posted')
        reversal = self.env['account.move'].search(
            [('reversed_entry_id', '=', original_move.id)])
        self.assertTrue(reversal)
        self.assertEqual(reversal.state, 'posted')
        self.assertEqual(reversal.eh_subsequent_event_id, e)
        self.assertEqual(reversal.reversed_entry_id, original_move)
        self.assertEqual(reversal.eh_event_provenance_version, 1)
