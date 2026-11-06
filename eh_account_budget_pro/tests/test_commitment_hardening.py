# -*- encoding: utf-8 -*-
"""Line-specific, currency-safe, reversible encumbrance regressions."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

import odoo
from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase

from odoo.addons.eh_account_budget_pro.models.budget_commitment import (
    EH_BUDGET_COMMITMENT_CAPABILITY,
    _CAPABILITY_CONTEXT_KEY,
)


@tagged('eh_account_budget_pro', 'post_install', '-at_install')
class TestCommitmentHardening(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.account_b = cls._ensure_account(
            cls.env, '5797', 'Budget secondary expense', 'expense')
        cls.product_a = cls.env['product.product'].create({
            'name': 'Budget product A',
            'type': 'consu',
            'purchase_ok': True,
            'property_account_expense_id': cls.account_expense.id,
        })
        cls.product_b = cls.env['product.product'].create({
            'name': 'Budget product B',
            'type': 'consu',
            'purchase_ok': True,
            'property_account_expense_id': cls.account_b.id,
        })
        cls.analytic_plan = cls.env['account.analytic.plan'].create({
            'name': 'Commitment release plan',
        })
        cls.analytic_a = cls.env['account.analytic.account'].create({
            'name': 'Commitment release A',
            'plan_id': cls.analytic_plan.id,
        })
        cls.analytic_b = cls.env['account.analytic.account'].create({
            'name': 'Commitment release B',
            'plan_id': cls.analytic_plan.id,
        })
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Budget Hardening User',
            'login': 'budget_hardening_user',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.purchase_ops_user = cls.env['res.users'].create({
            'name': 'Budget Purchase Operations User',
            'login': 'budget_purchase_operations_user',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('purchase.group_purchase_user').id,
            ])],
        })

    def _budget(self, accounts=None, code='hardening'):
        accounts = accounts or [self.account_expense]
        budget = self.env['eh.budget.budget'].create({
            'code': code,
            'name': 'Commitment hardening',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'overrun_policy': 'warn',
            'line_ids': [(0, 0, {
                'account_id': account.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 100000.0,
            }) for account in accounts],
        })
        budget.action_confirm()
        return budget

    def _po(self, lines, date='2026-03-01'):
        form = Form(self.env['purchase.order'])
        form.partner_id = self.partner_a
        form.date_order = date
        for product, qty, price in lines:
            with form.order_line.new() as line:
                line.product_id = product
                line.product_qty = qty
                line.price_unit = price
                line.date_planned = date
        po = form.save()
        po.button_confirm()
        return po

    def _analytic_budget(self, analytics, code):
        budget = self.env['eh.budget.budget'].create({
            'code': code,
            'name': 'Analytic commitment release',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'overrun_policy': 'warn',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'analytic_account_id': analytic.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 100000.0,
            }) for analytic in analytics],
        })
        budget.action_confirm()
        return budget

    def _analytic_po(self, qty, price, distribution):
        form = Form(self.env['purchase.order'])
        form.partner_id = self.partner_a
        form.date_order = '2026-03-01'
        with form.order_line.new() as line:
            line.product_id = self.product_a
            line.product_qty = qty
            line.price_unit = price
            line.date_planned = '2026-03-01'
        po = form.save()
        po.order_line.write({'analytic_distribution': distribution})
        po.button_confirm()
        return po

    def _move(
        self, po_line, move_type, qty, price, date='2026-03-15',
        *, post=True,
    ):
        move = self.env['account.move'].create({
            'move_type': move_type,
            'partner_id': po_line.order_id.partner_id.id,
            'currency_id': po_line.order_id.currency_id.id,
            'invoice_date': date,
            'invoice_line_ids': [(0, 0, {
                'product_id': po_line.product_id.id,
                'name': po_line.name,
                'quantity': qty,
                'price_unit': price,
                'account_id': (
                    po_line.product_id.property_account_expense_id.id
                    or po_line.product_id.categ_id
                    .property_account_expense_categ_id.id
                ),
                'purchase_line_id': po_line.id,
                'tax_ids': [(6, 0, [])],
            })],
        })
        if post:
            move.action_post()
        return move

    def _rows(self, po):
        return self.env['eh.budget.commitment'].search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', '=', po.id),
        ])

    def test_refund_reopens_fully_and_partially_released_commitment(self):
        self._budget(code='refund_reopens')
        po = self._po([(self.product_a, 10.0, 1000.0)])
        row = self._rows(po)
        self.assertEqual(row.state, 'reserved')
        self.assertAlmostEqual(row.amount, 10000.0, places=2)

        self._move(po.order_line, 'in_invoice', 10.0, 1000.0)
        row.invalidate_recordset()
        self.assertEqual(row.state, 'released')
        self.assertAlmostEqual(row.amount, 0.0, places=2)

        self._move(po.order_line, 'in_refund', 10.0, 1000.0)
        row.invalidate_recordset()
        self.assertEqual(row.state, 'reserved')
        self.assertAlmostEqual(row.amount, row.original_amount, places=2)

        self._move(po.order_line, 'in_invoice', 6.0, 1000.0)
        self._move(po.order_line, 'in_refund', 2.0, 1000.0)
        row.invalidate_recordset()
        self.assertEqual(row.state, 'reserved')
        self.assertAlmostEqual(row.amount, 6000.0, places=2)

    def test_bill_draft_and_cancel_recompute_released_commitment(self):
        self._budget(code='bill_state_reopens')
        po = self._po([(self.product_a, 10.0, 100.0)])
        row = self._rows(po)
        bill = self._move(
            po.order_line, 'in_invoice', 10.0, 100.0,
        )
        row.invalidate_recordset(['state', 'amount'])
        self.assertEqual(row.state, 'released')

        bill.button_draft()
        row.invalidate_recordset(['state', 'amount'])
        self.assertEqual(row.state, 'reserved')
        self.assertAlmostEqual(row.amount, row.original_amount, places=2)

        bill.action_post()
        row.invalidate_recordset(['state', 'amount'])
        self.assertEqual(row.state, 'released')
        bill.button_cancel()
        row.invalidate_recordset(['state', 'amount'])
        self.assertEqual(row.state, 'reserved')
        self.assertAlmostEqual(row.amount, row.original_amount, places=2)

    def test_billing_releases_only_exact_purchase_line(self):
        self._budget(
            [self.account_expense, self.account_b],
            code='line_specific',
        )
        po = self._po([
            (self.product_a, 3.0, 1000.0),
            (self.product_b, 7.0, 1000.0),
        ])
        lines = po.order_line.sorted('id')
        rows = self._rows(po)
        by_source = {row.source_line_id: row for row in rows}
        self._move(lines[0], 'in_invoice', 3.0, 1000.0)
        rows.invalidate_recordset()
        self.assertEqual(by_source[lines[0].id].state, 'released')
        self.assertAlmostEqual(by_source[lines[0].id].amount, 0.0, places=2)
        self.assertEqual(by_source[lines[1].id].state, 'reserved')
        self.assertAlmostEqual(by_source[lines[1].id].amount, 7000.0, places=2)

    def test_draft_bill_quantity_never_releases_commitment(self):
        self._budget(code='draft_bill_ignored')
        po = self._po([(self.product_a, 10.0, 1000.0)])
        row = self._rows(po)

        self._move(po.order_line, 'in_invoice', 6.0, 1000.0)
        self._move(
            po.order_line, 'in_invoice', 4.0, 1000.0, post=False,
        )
        # Posting another unit recomputes the whole source. Standard
        # qty_invoiced includes the four draft units, but encumbrance release
        # must use posted GL evidence only: seven posted, three still reserved.
        self._move(po.order_line, 'in_invoice', 1.0, 1000.0)

        row.invalidate_recordset()
        self.assertEqual(row.state, 'reserved')
        self.assertAlmostEqual(row.amount, 3000.0, places=2)

    def test_bill_post_after_po_cancel_never_reopens_commitment(self):
        self._budget(code='cancelled_po_bill_post')
        po = self._po([(self.product_a, 10.0, 100.0)])
        draft = self._move(
            po.order_line, 'in_invoice', 5.0, 100.0, post=False,
        )
        row = self._rows(po)

        po.button_cancel()
        row.invalidate_recordset()
        self.assertEqual(row.state, 'released')
        released_amount = row.amount

        draft.action_post()

        row.invalidate_recordset()
        self.assertEqual(row.state, 'released')
        self.assertAlmostEqual(row.amount, released_amount, places=2)

    def test_batch_bill_post_scans_each_po_line_once(self):
        self._budget(code='batch_bill_scan_once')
        po = self._po([(self.product_a, 2.0, 50.0)])
        bills = (
            self._move(
                po.order_line, 'in_invoice', 1.0, 50.0, post=False,
            )
            | self._move(
                po.order_line, 'in_invoice', 1.0, 50.0, post=False,
            )
        )
        model_class = type(bills)
        original = model_class._eh_posted_billing_for_po_line
        calls = []

        def _counted(move, po_line, currency):
            calls.append((po_line.id, currency.id))
            return original(move, po_line, currency)

        with patch.object(
            model_class, '_eh_posted_billing_for_po_line', _counted,
        ):
            bills.action_post()

        self.assertEqual(calls, [(po.order_line.id, self.company.currency_id.id)])
        row = self._rows(po)
        row.invalidate_recordset()
        self.assertEqual(row.state, 'released')
        self.assertAlmostEqual(row.amount, 0.0, places=2)

    def test_billing_uses_authoritative_move_state(self):
        self._budget(code='authoritative_bill_state')
        po = self._po([(self.product_a, 10.0, 10.0)])
        posted = self._move(
            po.order_line, 'in_invoice', 2.0, 10.0,
        )
        draft = self._move(
            po.order_line, 'in_invoice', 5.0, 10.0, post=False,
        )
        posted_line = posted.invoice_line_ids.filtered('purchase_line_id')
        draft_line = draft.invoice_line_ids.filtered('purchase_line_id')
        self.env.flush_all()
        # Reproduce stale denormalised state in both directions. Only
        # account.move.state may decide whether billing evidence is posted.
        self.env.cr.execute(
            "UPDATE account_move_line SET parent_state = 'draft' "
            "WHERE id = %s",
            (posted_line.id,),
        )
        self.env.cr.execute(
            "UPDATE account_move_line SET parent_state = 'posted' "
            "WHERE id = %s",
            (draft_line.id,),
        )
        posted_line.invalidate_recordset(['parent_state'])
        draft_line.invalidate_recordset(['parent_state'])

        amount, quantity = posted._eh_posted_billing_for_po_line(
            po.order_line, self.company.currency_id,
        )

        self.assertAlmostEqual(amount, 20.0, places=2)
        self.assertAlmostEqual(quantity, 2.0, places=2)

    def test_split_analytic_bill_releases_each_original_share(self):
        self._analytic_budget(
            [self.analytic_a, self.analytic_b], 'split_release',
        )
        po = self._analytic_po(2.0, 50.0, {
            str(self.analytic_a.id): 60.0,
            str(self.analytic_b.id): 40.0,
        })
        rows = self._rows(po)
        self.assertEqual(len(rows), 2)

        self._move(po.order_line, 'in_invoice', 1.0, 50.0)

        rows.invalidate_recordset()
        remaining_by_analytic = {
            row.budget_line_id.analytic_account_id.id: row.amount
            for row in rows
        }
        self.assertAlmostEqual(
            remaining_by_analytic[self.analytic_a.id], 30.0, places=2,
        )
        self.assertAlmostEqual(
            remaining_by_analytic[self.analytic_b.id], 20.0, places=2,
        )
        self.assertAlmostEqual(sum(rows.mapped('amount')), 50.0, places=2)

    def test_partial_resolved_scope_follows_full_line_progress(self):
        self._analytic_budget([self.analytic_a], 'partial_scope_release')
        po = self._analytic_po(2.0, 50.0, {
            str(self.analytic_a.id): 50.0,
            str(self.analytic_b.id): 50.0,
        })
        row = self._rows(po)
        self.assertEqual(len(row), 1)
        self.assertAlmostEqual(row.original_amount, 50.0, places=2)

        self._move(po.order_line, 'in_invoice', 1.0, 50.0)

        row.invalidate_recordset()
        self.assertEqual(row.state, 'reserved')
        self.assertAlmostEqual(row.amount, 25.0, places=2)

    def test_split_release_preserves_currency_minor_units(self):
        self._analytic_budget(
            [self.analytic_a, self.analytic_b], 'minor_unit_release',
        )
        po = self._analytic_po(3.0, 0.01, {
            str(self.analytic_a.id): 50.0,
            str(self.analytic_b.id): 50.0,
        })
        rows = self._rows(po)
        self.assertEqual(
            self.company.currency_id.compare_amounts(
                sum(rows.mapped('original_amount')), 0.03,
            ),
            0,
        )

        self._move(po.order_line, 'in_invoice', 1.0, 0.01)

        rows.invalidate_recordset()
        self.assertEqual(
            self.company.currency_id.compare_amounts(
                sum(rows.mapped('amount')), 0.02,
            ),
            0,
        )

    def test_foreign_po_is_reserved_in_budget_currency(self):
        eur = self.env.ref('base.EUR')
        eur.active = True
        self.partner_a.with_company(self.company).write({
            'property_purchase_currency_id': eur.id,
        })
        self.env['res.currency.rate'].create({
            'name': '2026-02-15',
            'currency_id': eur.id,
            'company_id': self.company.id,
            'rate': 2.0,
        })
        self._budget(code='foreign_currency')
        po = self._po([(self.product_a, 2.0, 100.0)])
        self.assertEqual(po.currency_id, eur)
        row = self._rows(po)
        expected = eur._convert(
            po.order_line.price_subtotal,
            self.company.currency_id,
            self.company,
            fields.Date.to_date(po.date_order),
        )
        self.assertEqual(row.currency_id, self.company.currency_id)
        self.assertAlmostEqual(row.original_amount, expected, places=2)
        self._move(po.order_line, 'in_invoice', 2.0, 100.0)
        row.invalidate_recordset()
        self.assertEqual(row.state, 'released')

    def test_downward_reconfirmation_replaces_original_baseline(self):
        self._budget(code='downward_amendment')
        po = self._po([(self.product_a, 1.0, 100.0)])
        row = self._rows(po)
        self.assertAlmostEqual(row.original_amount, 100.0, places=2)
        with self.assertRaises(UserError):
            po.order_line.write({'price_unit': 90.0})
        po.button_cancel()
        po.button_draft()
        po.order_line.price_unit = 80.0
        po.button_confirm()
        row.invalidate_recordset()
        self.assertEqual(row.state, 'reserved')
        self.assertAlmostEqual(row.amount, 80.0, places=2)
        self.assertAlmostEqual(row.original_amount, 80.0, places=2)

    def test_active_source_key_is_database_unique(self):
        budget = self._budget(code='source_unique')
        po = self._po([(self.product_a, 1.0, 100.0)])
        row = self._rows(po)
        engine = self.env['eh.budget.commitment'].sudo().with_context(**{
            _CAPABILITY_CONTEXT_KEY: EH_BUDGET_COMMITMENT_CAPABILITY,
        })
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                duplicate = engine.create({
                    'budget_line_id': budget.line_ids.id,
                    'amount': 100.0,
                    'source_model': row.source_model,
                    'source_id': row.source_id,
                    'source_line_id': row.source_line_id,
                    'source_ref': row.source_ref,
                    'state': 'reserved',
                })
                duplicate.flush_recordset()

    def test_reserved_manual_commitment_cannot_be_archived(self):
        budget = self._budget(code='manual_archive')
        row = self.env['eh.budget.commitment'].create({
            'budget_line_id': budget.line_ids.id,
            'amount': 100.0,
        })
        row.action_reserve()
        with self.assertRaises(AccessError):
            row.with_user(self.plain_user).write({'active': False})
        row.invalidate_recordset()
        self.assertTrue(row.active)
        self.assertEqual(row.state, 'reserved')

    def test_confirmed_out_of_period_po_date_rebuilds_commitment(self):
        self._budget(code='proposed_basis')
        po = self._po(
            [(self.product_a, 1.0, 100.0)], date='2025-03-01',
        )
        self.assertFalse(self._rows(po))
        po.order_line.write({'date_planned': '2026-03-01'})
        self.assertEqual(po.order_line.date_planned.date().year, 2026)
        self.assertEqual(self._rows(po).state, 'reserved')

    def test_planned_date_move_preserves_old_row_and_rebudgets(self):
        budget_q1 = self.env['eh.budget.budget'].create({
            'code': 'planned_date_q1',
            'name': 'Planned date Q1',
            'date_from': '2026-01-01',
            'date_to': '2026-03-31',
            'overrun_policy': 'warn',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-03-31',
                'budgeted_amount': 1000.0,
            })],
        })
        budget_q2 = self.env['eh.budget.budget'].create({
            'code': 'planned_date_q2',
            'name': 'Planned date Q2',
            'date_from': '2026-04-01',
            'date_to': '2026-06-30',
            'overrun_policy': 'warn',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-04-01',
                'period_to': '2026-06-30',
                'budgeted_amount': 1000.0,
            })],
        })
        (budget_q1 | budget_q2).action_confirm()
        po = self._po(
            [(self.product_a, 1.0, 100.0)], date='2026-02-01',
        )
        first = self._rows(po)
        self.assertEqual(first.budget_id, budget_q1)

        po.order_line.write({'date_planned': '2026-05-01'})
        rows = self._rows(po).sorted('id')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].budget_id, budget_q1)
        self.assertEqual(rows[0].state, 'released')
        self.assertEqual(rows[1].budget_id, budget_q2)
        self.assertEqual(rows[1].state, 'reserved')

        self._move(po.order_line, 'in_invoice', 1.0, 100.0)
        with self.assertRaisesRegex(UserError, 'vendor bill or refund'):
            po.order_line.write({'date_planned': '2026-06-01'})

    def test_off_policy_quarantine_does_not_govern_purchase(self):
        budget = self.env['eh.budget.budget'].create({
            'code': 'off_quarantine_scope',
            'name': 'Off quarantine scope',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'overrun_policy': 'off',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 1000.0,
            })],
        })
        budget.action_confirm()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_budget_budget "
            "SET eh_basis_integrity_review_required = TRUE WHERE id = %s",
            (budget.id,),
        )
        self.env.invalidate_all()

        po = self._po([(self.product_a, 1.0, 100.0)])
        self.assertEqual(po.state, 'purchase')
        self.assertFalse(self._rows(po))

    def test_ungoverned_bulk_basis_write_skips_purchase_source_locks(self):
        """128 PO writes add one budget probe and no budget source lock."""
        self.company.invalidate_recordset(['eh_budget_governance_active'])
        self.assertFalse(self.company.eh_budget_governance_active)
        orders = self.env['purchase.order'].create([{
            'partner_id': self.partner_a.id,
            'date_order': '2026-01-01',
            'company_id': self.company.id,
        } for _index in range(128)])

        real_execute = self.env.cr.execute
        seen = []

        def _record_query(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_query):
            orders.write({'date_order': '2026-02-01'})

        source_locks = [
            query for query in seen
            if 'FOR UPDATE' in query
            # Other installed purchase-order extensions may correctly lock
            # their own provenance columns. This assertion owns only the
            # budget engine's source-lock budget.
            and 'eh_ic_mirror_' not in query
            and (
                'FROM purchase_order WHERE' in query
                or 'FROM purchase_order_line WHERE' in query
            )
        ]
        scope_locks = [
            query for query in seen
            if 'pg_advisory_xact_lock_shared' in query
        ]
        self.assertFalse(source_locks)
        self.assertEqual(len(scope_locks), 1)

    def test_governance_discriminator_tracks_policy_lifecycle(self):
        budget = self._budget(code='governance_discriminator')
        self.company.invalidate_recordset(['eh_budget_governance_active'])
        self.assertTrue(self.company.eh_budget_governance_active)

        with self.assertRaisesRegex(AccessError, 'budget engine'):
            self.company.write({'eh_budget_governance_active': False})

        budget.action_close()
        self.company.invalidate_recordset(['eh_budget_governance_active'])
        self.assertFalse(self.company.eh_budget_governance_active)

    def test_reserved_source_stays_locked_after_budget_closes(self):
        budget = self._budget(code='closed_budget_live_commitment')
        po = self._po([(self.product_a, 1.0, 100.0)])
        self.assertEqual(self._rows(po).state, 'reserved')
        budget.action_close()
        self.company.invalidate_recordset(['eh_budget_governance_active'])
        self.assertFalse(self.company.eh_budget_governance_active)

        real_execute = self.env.cr.execute
        seen = []

        def _record_query(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_query):
            with self.assertRaisesRegex(UserError, 'governed budget basis'):
                po.write({'date_order': '2026-04-01'})
        self.assertTrue(any(
            'FOR UPDATE' in query and 'FROM purchase_order WHERE' in query
            for query in seen
        ))

    def test_planned_date_block_failure_rolls_back_source_and_reserve(self):
        budget_q1 = self.env['eh.budget.budget'].create({
            'code': 'planned_date_block_q1',
            'name': 'Planned date block Q1',
            'date_from': '2026-01-01',
            'date_to': '2026-03-31',
            'overrun_policy': 'block',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-03-31',
                'budgeted_amount': 1000.0,
            })],
        })
        budget_q2 = self.env['eh.budget.budget'].create({
            'code': 'planned_date_block_q2',
            'name': 'Planned date block Q2',
            'date_from': '2026-04-01',
            'date_to': '2026-06-30',
            'overrun_policy': 'block',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-04-01',
                'period_to': '2026-06-30',
                'budgeted_amount': 50.0,
            })],
        })
        (budget_q1 | budget_q2).action_confirm()
        po = self._po(
            [(self.product_a, 1.0, 100.0)], date='2026-02-01',
        )
        original_date = po.order_line.date_planned
        original_row = self._rows(po)

        with self.assertRaisesRegex(UserError, 'would exceed available'):
            with self.env.cr.savepoint():
                po.order_line.write({'date_planned': '2026-05-01'})

        po.order_line.invalidate_recordset(['date_planned'])
        original_row.invalidate_recordset(['state', 'amount'])
        self.assertEqual(po.order_line.date_planned, original_date)
        self.assertEqual(original_row.state, 'reserved')
        self.assertEqual(len(self._rows(po)), 1)

    def test_purchase_operator_can_update_operational_planned_date(self):
        self._budget(code='purchase_operator_planned_date')
        po = self._po(
            [(self.product_a, 1.0, 100.0)], date='2026-02-01',
        )
        row = self._rows(po)

        po.order_line.with_user(self.purchase_ops_user).write({
            'date_planned': '2026-04-01',
        })

        po.order_line.invalidate_recordset(['date_planned'])
        row.invalidate_recordset(['state', 'amount'])
        self.assertEqual(po.order_line.date_planned.date().month, 4)
        self.assertEqual(row.state, 'reserved')
        self.assertEqual(row.amount, 100.0)

    def test_plain_user_can_create_off_policy_but_not_governance(self):
        Budget = self.env['eh.budget.budget'].with_user(self.plain_user)
        common = {
            'name': 'Plain user draft',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
        }
        explicit = Budget.create(dict(
            common, code='plain_explicit_off', overrun_policy='off',
        ))
        defaulted = Budget.create(dict(common, code='plain_default_off'))
        self.assertEqual(explicit.overrun_policy, 'off')
        self.assertEqual(defaulted.overrun_policy, 'off')
        with self.assertRaises(AccessError):
            Budget.create(dict(
                common, code='plain_warn', overrun_policy='warn',
            ))
        with self.assertRaises(AccessError):
            Budget.with_context(default_overrun_policy='block').create(dict(
                common, code='plain_context_block',
            ))

    def test_fiscal_position_mapped_expense_drives_commitment(self):
        budget = self._budget(
            accounts=[self.account_b], code='mapped_expense_budget',
        )
        fiscal_position = self.env['account.fiscal.position'].create({
            'name': 'Budget expense map',
            'account_ids': [(0, 0, {
                'account_src_id': self.account_expense.id,
                'account_dest_id': self.account_b.id,
            })],
        })
        form = Form(self.env['purchase.order'])
        form.partner_id = self.partner_a
        form.fiscal_position_id = fiscal_position
        form.date_order = '2026-03-01'
        with form.order_line.new() as line:
            line.product_id = self.product_a
            line.product_qty = 1.0
            line.price_unit = 100.0
            line.date_planned = '2026-03-01'
        po = form.save()
        po.button_confirm()
        row = self._rows(po)
        self.assertEqual(row.budget_id, budget)
        self.assertEqual(row.budget_line_id.account_id, self.account_b)

    def test_plain_user_cannot_approve_or_change_budget_policy(self):
        budget = self.env['eh.budget.budget'].create({
            'code': 'manager_policy_gate',
            'name': 'Manager policy gate',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 1000.0,
            })],
        })
        ordinary = budget.with_user(self.plain_user)
        with self.assertRaises(AccessError):
            ordinary.write({'overrun_policy': 'off'})
        with self.assertRaises(AccessError):
            ordinary.action_confirm()
        with self.assertRaises(AccessError):
            ordinary.action_create_version()
        self.assertEqual(budget.state, 'draft')

    def test_confirmed_budget_header_and_lines_are_immutable(self):
        budget = self._budget(code='confirmed_basis_freeze')
        line = budget.line_ids
        with self.assertRaises(UserError):
            budget.write({'overrun_policy': 'off'})
        with self.assertRaises(UserError):
            line.write({'budgeted_amount': 999999.0})
        with self.assertRaises(UserError):
            line.unlink()
        with self.assertRaises(UserError):
            self.env['eh.budget.line'].create({
                'budget_id': budget.id,
                'account_id': self.account_b.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 100.0,
            })
        budget.invalidate_recordset()
        self.assertEqual(budget.overrun_policy, 'warn')
        self.assertEqual(len(budget.line_ids), 1)
        self.assertAlmostEqual(
            budget.line_ids.budgeted_amount, 100000.0, places=2,
        )

    def test_confirmed_line_cannot_be_reparented_from_draft(self):
        confirmed = self._budget(code='confirmed_reparent_target')
        draft = self.env['eh.budget.budget'].create({
            'code': 'draft_reparent_source',
            'name': 'Draft reparent source',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_b.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 50.0,
            })],
        })
        with self.assertRaises(UserError):
            draft.line_ids.write({'budget_id': confirmed.id})
        self.assertEqual(draft.line_ids.budget_id, draft)

    def test_activity_baseline_freezes_but_actual_remains_updateable(self):
        budget = self.env['eh.budget.budget'].create({
            'code': 'activity_basis_freeze',
            'name': 'Activity basis freeze',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 1000.0,
            })],
            'activity_period_ids': [(0, 0, {
                'name': 'Annual units',
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_activity': 100.0,
                'actual_activity': 10.0,
            })],
        })
        budget.action_confirm()
        activity = budget.activity_period_ids.with_user(self.plain_user)
        activity.write({'actual_activity': 25.0})
        self.assertEqual(activity.actual_activity, 25.0)
        with self.assertRaises(UserError):
            activity.write({'budgeted_activity': 999.0})
        with self.assertRaises(UserError):
            activity.unlink()
        with self.assertRaises(UserError):
            self.env['eh.budget.activity'].with_user(
                self.plain_user
            ).create({
                'budget_id': budget.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_activity': 1.0,
            })

    def test_forecast_snapshot_is_manager_generated_and_immutable(self):
        budget = self._budget(code='forecast_snapshot_guard')
        with self.assertRaises(AccessError):
            budget.with_user(self.plain_user).action_reforecast()
        with self.assertRaises(AccessError):
            self.env['eh.budget.forecast.revision'].create({
                'budget_id': budget.id,
                'name': 'FORGED',
                'revision_date': '2026-01-01',
            })
        action = budget.action_reforecast()
        revision = self.env['eh.budget.forecast.revision'].browse(
            action['res_id']
        )
        with self.assertRaises(AccessError):
            revision.write({'name': 'rewritten'})
        with self.assertRaises(AccessError):
            revision.unlink()

    def test_version_lineage_is_engine_owned_and_retained(self):
        budget = self._budget(code='version_lineage_guard')
        action = budget.action_create_version()
        child = self.env['eh.budget.budget'].browse(action['res_id'])
        with self.assertRaises(AccessError):
            child.with_user(self.plain_user).write({'parent_id': False})
        with self.assertRaises(UserError):
            child.unlink()
        with self.assertRaises(UserError):
            budget.unlink()

    def test_upgrade_quarantines_legacy_activity_revision_and_lineage(self):
        budget = self.env['eh.budget.budget'].create({
            'code': 'legacy_basis_evidence',
            'name': 'Legacy basis evidence',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 1000.0,
                'behaviour': 'variable',
            })],
            'activity_period_ids': [(0, 0, {
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_activity': 100.0,
                'actual_activity': 50.0,
            })],
        })
        budget.action_confirm()
        revision_action = budget.action_reforecast()
        revision = self.env['eh.budget.forecast.revision'].browse(
            revision_action['res_id']
        )
        child = self.env['eh.budget.budget'].browse(
            budget.action_create_version()['res_id']
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.5' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_budget_pre_135', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.3.4')
        self.env.invalidate_all()
        self.assertTrue(budget.eh_activity_integrity_review_required)
        self.assertTrue(budget.eh_version_integrity_review_required)
        self.assertTrue(child.eh_version_integrity_review_required)
        self.assertTrue(revision.eh_integrity_review_required)
        self.assertFalse(budget.active_revision_id)
        self.assertEqual(budget.line_ids._activity_pair(), (0.0, 0.0))

    def test_upgrade_quarantines_plain_approved_legacy_basis(self):
        budget = self._budget(code='legacy_plain_approved_basis')
        line = budget.line_ids
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_budget_line SET budgeted_amount = %s WHERE id = %s",
            (99999999.0, line.id),
        )
        self.env.cr.execute(
            "SELECT state FROM eh_budget_budget WHERE id = %s",
            (budget.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), ('confirmed',))
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.6' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_budget_pre_136', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.3.4')
        self.env.cr.execute(
            "SELECT eh_basis_integrity_review_required "
            "FROM eh_budget_budget WHERE id = %s",
            (budget.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (True,))
        self.env.invalidate_all()
        self.assertTrue(budget.eh_basis_integrity_review_required)
        self.assertIn(
            'predates immutable budget controls',
            budget.eh_basis_integrity_review_reason,
        )
        self.assertEqual(line.budgeted_amount, 99999999.0)
        with self.assertRaisesRegex(UserError, 'basis covering'):
            self.env['eh.budget.commitment']._resolve_budget_line(
                self.account_expense,
                fields.Date.to_date('2026-03-01'),
                self.env.company,
            )
        with self.assertRaisesRegex(UserError, 'basis or version lineage'):
            budget.action_create_version()

    def test_upgrade_preserves_basis_from_controlled_135_source(self):
        budget = self._budget(code='controlled_135_basis')
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.6' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_budget_pre_136_controlled', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)

        migration.migrate(self.env.cr, '19.0.1.3.5')
        migration.migrate(self.env.cr, '1.3.5')

        self.env.invalidate_all()
        self.assertFalse(budget.eh_basis_integrity_review_required)
        self.assertFalse(budget.eh_basis_integrity_review_reason)

    def test_upgrade_quarantines_only_approved_overlapping_actual_scopes(self):
        overlapping = self.env['eh.budget.budget'].create({
            'code': 'legacy_overlapping_scopes',
            'name': 'Legacy overlapping scopes',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': amount,
            }) for amount in (100.0, 200.0)],
        })
        unaffected = self._budget(code='legacy_nonoverlapping_scope')
        self.env.flush_all()
        # Reproduce an approved pre-1.3.8 row without crossing current
        # action_confirm validation.
        self.env.cr.execute(
            "UPDATE eh_budget_budget SET state = 'confirmed' WHERE id = %s",
            (overlapping.id,),
        )

        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.8' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_budget_pre_138', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.3.7')
        migration.migrate(self.env.cr, '19.0.1.3.7')

        self.env.invalidate_all()
        self.assertTrue(overlapping.eh_basis_integrity_review_required)
        self.assertIn(
            'overlapping account/analytic actual scopes',
            overlapping.eh_basis_integrity_review_reason,
        )
        self.assertEqual(len(overlapping.line_ids), 2)
        self.assertFalse(unaffected.eh_basis_integrity_review_required)

    def test_upgrade_quarantines_duplicate_source_rows_without_deleting(self):
        budget = self._budget(code='migration_duplicate')
        po = self._po([(self.product_a, 1.0, 100.0)])
        row = self._rows(po)
        self.env.cr.execute(
            "DROP INDEX eh_budget_commitment_source_uniq"
        )
        engine = self.env['eh.budget.commitment'].sudo().with_context(**{
            _CAPABILITY_CONTEXT_KEY: EH_BUDGET_COMMITMENT_CAPABILITY,
        })
        duplicate = engine.create({
            'budget_line_id': budget.line_ids.id,
            'amount': row.amount,
            'source_model': row.source_model,
            'source_id': row.source_id,
            'source_line_id': row.source_line_id,
            'source_ref': row.source_ref,
            'state': 'reserved',
        })
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.2' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_budget_pre_132', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.3.1')
        quarantined = self.env['eh.budget.commitment'].with_context(
            active_test=False,
        ).browse([row.id, duplicate.id])
        quarantined.invalidate_recordset()
        self.assertEqual(len(quarantined), 2)
        self.assertFalse(any(quarantined.mapped('active')))
        self.assertEqual(set(quarantined.mapped('state')), {'released'})
        self.assertEqual(set(quarantined.mapped(
            'eh_integrity_review_reason'
        )), {'legacy-duplicate-source-key-preserved-for-review'})
        self.env['eh.budget.commitment'].init()

    def test_upgrade_quarantines_forged_related_company_scope(self):
        self._budget(code='migration_scope')
        po = self._po([(self.product_a, 1.0, 100.0)])
        row = self._rows(po)
        other = self.env['res.company'].create({
            'name': 'Forged Commitment Scope Company',
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_budget_commitment SET company_id = %s WHERE id = %s",
            (other.id, row.id),
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.3' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_budget_pre_133', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.3.2')
        self.env.cr.execute(
            "SELECT active, state, eh_integrity_review_reason "
            "FROM eh_budget_commitment WHERE id = %s",
            (row.id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (
            False,
            'released',
            'legacy-related-budget-company-currency-mismatch-'
            'preserved-for-review',
        ))

    def test_upgrade_quarantines_all_unversioned_source_provenance(self):
        self._budget(code='migration_source_provenance')
        valid_po = self._po([(self.product_a, 1.0, 100.0)])
        forged_po = self._po([(self.product_a, 2.0, 100.0)])
        valid_row = self._rows(valid_po)
        forged_row = self._rows(forged_po)
        budget_id = valid_row.budget_id.id
        manual_row = self.env['eh.budget.commitment'].create({
            'budget_line_id': valid_row.budget_line_id.id,
            'amount': 75.0,
        })
        manual_row.action_reserve()
        # Pre-hardening RPC could forge a singleton source key. Keep its PO
        # line and amount but point the header anchor at another real PO; no
        # duplicate key exists, yet the claimed source graph is false.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_budget_commitment SET source_id = %s WHERE id = %s",
            (valid_po.id, forged_row.id),
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.4' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_budget_pre_134', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.3.3')
        self.env.invalidate_all()
        self.env.cr.execute(
            "SELECT id, active, state, eh_integrity_review_reason "
            "FROM eh_budget_commitment WHERE id = ANY(%s) ORDER BY id",
            ([valid_row.id, forged_row.id, manual_row.id],),
        )
        rows = {row[0]: row[1:] for row in self.env.cr.fetchall()}
        expected = (
            False,
            'released',
            'Legacy commitments predate durable actor and engine provenance; '
            'all rows were preserved and removed from live availability for '
            'review.',
        )
        self.assertEqual(rows[valid_row.id], expected)
        self.assertEqual(rows[forged_row.id], expected)
        self.assertEqual(rows[manual_row.id], expected)
        self.env.cr.execute(
            "SELECT eh_commitment_integrity_review_required "
            "FROM eh_budget_budget WHERE id = %s",
            (budget_id,),
        )
        self.assertEqual(self.env.cr.fetchone(), (True,))

        form = Form(self.env['purchase.order'])
        form.partner_id = self.partner_a
        form.date_order = '2026-04-01'
        with form.order_line.new() as line:
            line.product_id = self.product_a
            line.product_qty = 1.0
            line.price_unit = 100.0
            line.date_planned = '2026-04-01'
        blocked_po = form.save()
        with self.assertRaisesRegex(UserError, 'quarantined legacy'):
            blocked_po.button_confirm()

    def test_confirmed_quantity_actuals_are_updateable_and_audited(self):
        budget = self.env['eh.budget.budget'].create({
            'code': 'quantity_actual_entry',
            'name': 'Quantity actual entry',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': 1000.0,
                'behaviour': 'variable',
                'budgeted_qty': 10.0,
                'budgeted_unit_price': 100.0,
            })],
        })
        budget.action_confirm()
        line = budget.line_ids
        prior_cache_version = budget.eh_report_cache_version

        line.with_user(self.plain_user).write({
            'actual_qty': 9.0,
            'actual_unit_price': 110.0,
            'quantity_evidence_provided': True,
        })
        budget.invalidate_recordset(['eh_report_cache_version'])
        self.assertEqual(line.actual_qty, 9.0)
        self.assertEqual(line.actual_unit_price, 110.0)
        self.assertTrue(line.quantity_evidence_provided)
        self.assertGreater(
            budget.eh_report_cache_version, prior_cache_version,
        )
        with self.assertRaises(UserError):
            line.with_user(self.plain_user).write({
                'budgeted_qty': 11.0,
            })

        action = budget.with_user(
            self.plain_user,
        ).action_open_quantity_actuals()
        expected_view = self.env.ref(
            'eh_account_budget_pro.view_eh_budget_line_quantity_actuals',
        )
        expected_view_mode = (
            'list' if odoo.release.version_info[0] >= 18 else 'tree'
        )
        self.assertEqual(
            action['views'], [(expected_view.id, expected_view_mode)],
        )
        budget.action_close()
        with self.assertRaisesRegex(UserError, 'revised or closed'):
            line.with_user(self.plain_user).write({'actual_qty': 8.0})

    def test_budget_navigation_and_purchase_smart_button_are_loaded(self):
        self.assertTrue(self.env.ref(
            'eh_account_budget_pro.menu_eh_budget_commitment',
        ).action)
        self.assertTrue(self.env.ref(
            'eh_account_budget_pro.menu_eh_budget_line_analytics',
        ).action)
        purchase_view = self.env.ref(
            'eh_account_budget_pro.view_purchase_order_form_budget_commitments',
        )
        self.assertIn('action_eh_view_commitments', purchase_view.arch_db)

        self._budget(code='purchase_smart_button')
        po = self._po([(self.product_a, 1.0, 100.0)])
        action = po.action_eh_view_commitments()
        self.assertEqual(action['res_model'], 'eh.budget.commitment')
        self.assertEqual(po.eh_budget_commitment_count, 1)

    def test_pre_migrations_are_safe_when_new_tables_do_not_exist(self):
        migrations = Path(__file__).parents[1] / 'migrations'
        versions = (
            '19.0.1.3.2', '19.0.1.3.3', '19.0.1.3.4',
            '19.0.1.3.5', '19.0.1.3.6', '19.0.1.3.8',
            '19.0.1.3.10', '19.0.1.3.11',
        )
        self.env.cr.execute("SET LOCAL search_path TO pg_temp")
        try:
            for version in versions:
                path = migrations / version / 'pre-migration.py'
                module_name = 'eh_budget_missing_%s' % version.replace(
                    '.', '_',
                )
                spec = spec_from_file_location(module_name, path)
                migration = module_from_spec(spec)
                spec.loader.exec_module(migration)
                migration.migrate(self.env.cr, '19.0.1.0.0')
        finally:
            self.env.cr.execute(
                'SET LOCAL search_path TO "$user", public'
            )

    def test_upgrade_backfills_stable_versions_and_governance_scope(self):
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.11' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_budget_pre_1311', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)

        self.env.cr.execute("SET LOCAL search_path TO pg_temp")
        try:
            self.env.cr.execute("""
                CREATE TEMP TABLE res_company (
                    id integer PRIMARY KEY
                )
            """)
            self.env.cr.execute("""
                CREATE TEMP TABLE eh_budget_budget (
                    id integer PRIMARY KEY,
                    parent_id integer,
                    code varchar NOT NULL,
                    company_id integer NOT NULL,
                    state varchar NOT NULL,
                    overrun_policy varchar NOT NULL,
                    version_label varchar
                )
            """)
            self.env.cr.execute(
                "INSERT INTO res_company(id) VALUES (1), (2)"
            )
            self.env.cr.execute("""
                INSERT INTO eh_budget_budget(
                    id, parent_id, code, company_id, state,
                    overrun_policy, version_label
                ) VALUES
                    (10, NULL, 'annual', 1, 'revised', 'warn', 'v1'),
                    (11, 10, 'annual_v1_1', 1, 'revised', 'warn', 'v1.1'),
                    (12, 11, 'annual_v1_1_v1_2', 1, 'confirmed', 'warn',
                     'v1.2'),
                    (20, NULL, 'off_only', 2, 'confirmed', 'off', 'v1')
            """)

            migration.migrate(self.env.cr, '19.0.1.3.10')
            self.env.cr.execute("""
                SELECT id, code, version_root_code, version_sequence,
                       version_label
                  FROM eh_budget_budget
                 ORDER BY id
            """)
            self.assertEqual(self.env.cr.fetchall(), [
                (10, 'annual', 'annual', 1, 'v1'),
                (11, 'annual_v1_1', 'annual', 2, 'v2'),
                (12, 'annual_v1_1_v1_2', 'annual', 3, 'v3'),
                (20, 'off_only', 'off_only', 1, 'v1'),
            ])
            self.env.cr.execute("""
                SELECT id, eh_budget_governance_active
                  FROM res_company
                 ORDER BY id
            """)
            self.assertEqual(self.env.cr.fetchall(), [(1, True), (2, False)])
        finally:
            self.env.cr.execute(
                "DROP TABLE IF EXISTS pg_temp.eh_budget_budget"
            )
            self.env.cr.execute("DROP TABLE IF EXISTS pg_temp.res_company")
            self.env.cr.execute(
                'SET LOCAL search_path TO "$user", public'
            )

    def test_upgrade_restores_only_provable_premature_retirement(self):
        source = self._budget(code='premature_retirement')
        source_line_id = source.line_ids.id
        source_amount = source.line_ids.budgeted_amount
        replacement = self.env['eh.budget.budget'].browse(
            source.action_create_version()['res_id'],
        )
        self.assertEqual(source.state, 'confirmed')
        self.assertEqual(replacement.state, 'draft')

        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_budget_budget SET state = 'revised' WHERE id = %s",
            (source.id,),
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.10' / 'pre-migration.py'
        )
        spec = spec_from_file_location('eh_budget_pre_1310', path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        restored = migration._restore_prematurely_retired_budgets(
            self.env.cr,
        )
        self.assertEqual(restored, [source.id])
        self.assertEqual(
            migration._restore_prematurely_retired_budgets(self.env.cr),
            [],
        )

        self.env.invalidate_all()
        self.assertEqual(source.state, 'confirmed')
        self.assertEqual(replacement.state, 'draft')
        self.assertEqual(source.revised_budget_id, replacement)
        self.assertEqual(replacement.parent_id, source)
        self.assertEqual(source.line_ids.id, source_line_id)
        self.assertEqual(source.line_ids.budgeted_amount, source_amount)
