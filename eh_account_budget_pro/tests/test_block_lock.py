# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Concurrency guard for the 'block' overrun policy.

available_amount is a non-stored compute over reserved commitment rows,
so nothing at the DB layer serialises two concurrent PO confirms that
resolve to the same 'block' budget line. Without a lock each confirm
reads the pre-confirm availability, both pass the gate, both encumber,
and availability goes negative -- silently breaking the 'block'
guarantee that encumbrance accounting relies on.

The fix takes a SELECT ... FOR UPDATE row lock on every resolved budget
line before reading availability, held through commitment creation to
commit. A true race is not deterministically reproducible in a single
cursor, so these tests assert (a) the lock is actually taken on the
confirm path and (b) the gate re-evaluates cumulatively, i.e. a second
over-limit confirm against the already-encumbered availability raises.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_budget_pro', 'integration', 'post_install', '-at_install')
class TestBlockPolicyLock(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Budget = cls.env['eh.budget.budget']
        cls.Commitment = cls.env['eh.budget.commitment']
        cls.product = cls.env['product.product'].create({
            'name': 'Block Lock Product',
            'type': 'consu',
            'purchase_ok': True,
            'property_account_expense_id': cls.account_expense.id,
        })

    def _make_block_line(self, budgeted, code):
        budget = self.Budget.create({
            'code': code,
            'name': 'Block Lock Budget',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
            'overrun_policy': 'block',
            'line_ids': [(0, 0, {
                'account_id': self.account_expense.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'budgeted_amount': budgeted,
            })],
        })
        budget.action_confirm()
        return budget.line_ids[0]

    def _make_po(self, amount):
        po_form = Form(self.env['purchase.order'])
        po_form.partner_id = self.partner_a
        with po_form.order_line.new() as line:
            line.product_id = self.product
            line.product_qty = 1
            line.price_unit = amount
        return po_form.save()

    def test_block_check_takes_for_update_lock(self):
        # The confirm path must acquire a FOR UPDATE row lock on the
        # resolved budget line so a concurrent confirm is serialised.
        self._make_block_line(budgeted=10000.0, code='block_lock_taken')
        po = self._make_po(3000.0)

        real_execute = self.env.cr.execute
        seen = []

        def _rec(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _rec):
            po.button_confirm()

        self.assertTrue(
            any('FOR UPDATE' in q and 'eh_budget_line' in q for q in seen),
            "block-policy confirm must lock the resolved budget line "
            "with SELECT ... FOR UPDATE before reading availability",
        )

    def test_second_confirm_reevaluates_against_encumbered_availability(self):
        # First PO (8000) fits under the 10000 block line and confirms.
        # A second PO (8000) resolving to the same line must see the
        # 2000 that survives the first encumbrance and be refused: the
        # gate re-reads committed availability, exactly what the lock
        # forces a queued concurrent confirm to do.
        line = self._make_block_line(budgeted=10000.0, code='block_lock_seq')

        po_a = self._make_po(8000.0)
        po_a.button_confirm()
        self.assertIn(po_a.state, ('purchase', 'done'))
        line.invalidate_recordset(['committed_amount', 'available_amount'])
        self.assertAlmostEqual(line.available_amount, 2000.0, 2)

        po_b = self._make_po(8000.0)
        with self.assertRaises(UserError):
            po_b.button_confirm()

        # The refused confirm must leave no commitment behind: the block
        # gate runs before super().button_confirm / _eh_create_commitments.
        self.assertFalse(self.Commitment.search([
            ('source_model', '=', 'purchase.order'),
            ('source_id', '=', po_b.id),
        ]))
        line.invalidate_recordset(['committed_amount', 'available_amount'])
        self.assertAlmostEqual(line.available_amount, 2000.0, 2)
