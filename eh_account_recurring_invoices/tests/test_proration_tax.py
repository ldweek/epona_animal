# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression tests for two proration-wizard defects.

1. The prorated credit note and catch-up invoice must carry the template
   line's output tax (and the template's fiscal position), so mid-period
   plan changes do not silently drop GST/VAT. Previously both moves were
   built with no tax_ids, understating output tax on every change.

2. The credit amount must reflect the template line quantity, not just
   price_unit. A per-seat line (quantity>1) was under-credited by the
   quantity factor because the wizard summed price_unit alone.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_recurring_invoices', 'integration',
        'post_install', '-at_install')
class TestProrationTaxAndQuantity(EhAccountIntegrationTestCase):

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
        # account.tax.country_id is NOT NULL on Odoo 16 (nullable/defaulted in
        # 17+). It must also MATCH the company's fiscal country, or the move
        # validation refuses the invoice ("taxes incompatible with your fiscal
        # country"). The base test company can have no country at all, so give
        # it one and point the tax at the same country, keeping them in sync on
        # every version.
        country = (cls.company.account_fiscal_country_id
                   or cls.company.country_id)
        if not country:
            country = (cls.env.ref('base.us', raise_if_not_found=False)
                       or cls.env['res.country'].search([], limit=1))
            cls.company.sudo().write({'country_id': country.id})
        tax_vals = {
            'name': 'EH Proration GST 10%',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': cls.company.id,
        }
        if 'country_id' in cls.env['account.tax']._fields and country:
            tax_vals['country_id'] = country.id
        cls.tax_10 = cls.env['account.tax'].create(tax_vals)
        included_vals = dict(tax_vals)
        included_vals['name'] = 'EH Proration Included GST 10%'
        if 'price_include_override' in cls.env['account.tax']._fields:
            included_vals['price_include_override'] = 'tax_included'
        else:
            included_vals['price_include'] = True
        cls.tax_10_included = cls.env['account.tax'].create(included_vals)

    def _make_template(self, price=100.0, quantity=1.0, taxes=None,
                       next_run='2026-04-01'):
        line = {
            'name': 'Service',
            'account_id': self.account_revenue.id,
            'quantity': quantity,
            'price_unit': price,
        }
        if taxes:
            line['tax_ids'] = [(6, 0, taxes.ids)]
        return self.Template.create({
            'name': 'T_prorate_%s_%s' % (quantity, price),
            'code': 't_prorate_%s_%s' % (
                int(quantity), int(price)),
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'interval': 1,
            'interval_unit': 'month',
            'start_date': fields.Date.from_string(next_run),
            'next_run_date': fields.Date.from_string(next_run),
            'line_ids': [(0, 0, line)],
        })

    def _open_wizard(self, tpl, change_date, new_amount):
        return self.Wizard.create({
            'template_id': tpl.id,
            'change_date': fields.Date.from_string(change_date),
            'new_amount': new_amount,
        })

    def _post_source(self, tpl):
        tpl.write({'auto_post': True})
        tpl.action_activate()
        tpl.action_generate_now()
        self.assertEqual(tpl.last_generated_move_id.state, 'posted')
        return tpl.last_generated_move_id

    # --- Finding 1: output tax is carried onto both prorated moves ---

    def test_proration_moves_carry_output_tax(self):
        tpl = self._make_template(price=100.0, quantity=1.0, taxes=self.tax_10)
        self._post_source(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        # 15 of 30 days remaining -> credit 50 net, catch-up 100 net.
        self.assertEqual(wiz.credit_amount, 50.0)
        self.assertEqual(wiz.new_period_amount, 100.0)

        result = wiz.action_apply()
        moves = self.env['account.move'].browse(result['domain'][0][2])
        credit = moves.filtered(lambda m: m.move_type == 'out_refund')
        invoice = moves.filtered(lambda m: m.move_type == 'out_invoice')
        self.assertEqual(len(credit), 1)
        self.assertEqual(len(invoice), 1)

        # Both prorated lines must carry the template line's tax.
        credit_line = credit.invoice_line_ids
        invoice_line = invoice.invoice_line_ids
        self.assertIn(
            self.tax_10, credit_line.tax_ids,
            "Credit note line dropped the output tax")
        self.assertIn(
            self.tax_10, invoice_line.tax_ids,
            "Catch-up invoice line dropped the output tax")

        # 10% GST must actually be booked, not zero.
        self.assertAlmostEqual(credit.amount_tax, 5.0, places=2)
        self.assertAlmostEqual(invoice.amount_tax, 10.0, places=2)
        self.assertAlmostEqual(credit.amount_total, 55.0, places=2)
        self.assertAlmostEqual(invoice.amount_total, 110.0, places=2)

    def test_proration_no_tax_when_line_untaxed(self):
        """A template line with no taxes must still post cleanly with zero
        tax (no regression on the untaxed path)."""
        tpl = self._make_template(price=100.0, quantity=1.0, taxes=None)
        self._post_source(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        result = wiz.action_apply()
        moves = self.env['account.move'].browse(result['domain'][0][2])
        self.assertTrue(all(m.state == 'posted' for m in moves))
        self.assertTrue(all(m.amount_tax == 0.0 for m in moves))

    # --- Finding 2: credit amount honours the line quantity ---

    def test_proration_credit_honours_quantity(self):
        # Per-seat: quantity 3 x 100 = 300/period. Half period remaining.
        tpl = self._make_template(price=100.0, quantity=3.0)
        self._post_source(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        self.assertEqual(wiz.days_remaining, 15)
        # old period = 3 * 100 = 300; credit = 300 * 0.5 = 150 (NOT 50).
        self.assertEqual(wiz.old_amount, 300.0)
        self.assertEqual(wiz.credit_amount, 150.0)
        # new plan per-unit 200 x 3 seats = 600/period; half = 300.
        self.assertEqual(wiz.new_period_amount, 300.0)
        self.assertEqual(wiz.net_amount, 150.0)

        result = wiz.action_apply()
        moves = self.env['account.move'].browse(result['domain'][0][2])
        credit = moves.filtered(lambda m: m.move_type == 'out_refund')
        self.assertAlmostEqual(
            credit.amount_untaxed, 150.0, places=2,
            msg="Per-seat credit under-credited by the quantity factor")

    def test_proration_quantity_and_tax_combined(self):
        """Quantity>1 and a tax together: credit = qty*price*ratio net plus
        tax on that base."""
        tpl = self._make_template(price=100.0, quantity=3.0, taxes=self.tax_10)
        self._post_source(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        self.assertEqual(wiz.credit_amount, 150.0)
        result = wiz.action_apply()
        moves = self.env['account.move'].browse(result['domain'][0][2])
        credit = moves.filtered(lambda m: m.move_type == 'out_refund')
        # 150 net + 10% = 15 tax.
        self.assertAlmostEqual(credit.amount_tax, 15.0, places=2)
        self.assertAlmostEqual(credit.amount_total, 165.0, places=2)

    def test_price_included_tax_prorates_gross_credit_and_charge(self):
        """Half of a tax-included 100 must remain a gross 50 credit."""
        tpl = self._make_template(
            price=100.0, quantity=1.0, taxes=self.tax_10_included,
        )
        source = self._post_source(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        self.assertEqual(wiz.old_amount, 100.0)
        self.assertEqual(wiz.credit_amount, 50.0)
        self.assertEqual(wiz.new_period_amount, 100.0)

        result = wiz.action_apply()
        moves = self.env['account.move'].browse(result['domain'][0][2])
        credit = moves.filtered(lambda move: move.move_type == 'out_refund')
        charge = moves.filtered(lambda move: move.move_type == 'out_invoice')
        self.assertAlmostEqual(credit.amount_total, 50.0, places=2)
        self.assertAlmostEqual(credit.amount_untaxed, 45.45, places=2)
        self.assertAlmostEqual(credit.amount_tax, 4.55, places=2)
        self.assertAlmostEqual(charge.amount_total, 100.0, places=2)
        self.assertAlmostEqual(charge.amount_untaxed, 90.91, places=2)
        self.assertAlmostEqual(charge.amount_tax, 9.09, places=2)
        credit_receivable = credit.line_ids.filtered(
            lambda line: line.account_id.account_type == 'asset_receivable'
        )
        self.assertTrue(all(credit_receivable.mapped('reconciled')))
        self.assertEqual(
            credit.eh_recurring_proration_source_move_id, source,
        )

    def test_product_driven_line_uses_posted_resolved_account(self):
        product = self.env['product.product'].create({
            'name': 'Product-driven recurring service',
            'property_account_income_id': self.account_revenue.id,
        })
        tpl = self.Template.create({
            'name': 'Product-driven proration',
            'code': 'product_driven_proration',
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'interval': 1,
            'interval_unit': 'month',
            'start_date': fields.Date.from_string('2026-04-01'),
            'next_run_date': fields.Date.from_string('2026-04-01'),
            'line_ids': [(0, 0, {
                'name': 'Product service',
                'product_id': product.id,
                # Valid product-driven configuration: no explicit account.
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })
        self.assertFalse(tpl.line_ids.account_id)
        source = self._post_source(tpl)
        source_account = source.invoice_line_ids.account_id
        self.assertTrue(source_account)

        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        moves = self.env['account.move'].browse(
            wiz.action_apply()['domain'][0][2]
        )
        self.assertTrue(all(
            line.account_id == source_account
            for line in moves.mapped('invoice_line_ids')
        ))

    def test_multi_line_source_has_explicit_error_not_ensure_one_valueerror(self):
        tpl = self._make_template(price=100.0)
        tpl.line_ids.create({
            'template_id': tpl.id,
            'name': 'Second billed service',
            'account_id': self.account_revenue.id,
            'quantity': 1.0,
            'price_unit': 20.0,
        })
        source = self._post_source(tpl)
        wiz = self._open_wizard(tpl, '2026-04-16', 200.0)
        with self.assertRaisesRegex(UserError, 'commercial lines'):
            wiz._eh_single_source_line(source)
