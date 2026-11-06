# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Bridge tests: recurring invoice templates -> IFRS 15 revenue contracts.

A linked template must route generated invoice lines to the contract
liability account (deferred revenue) instead of income, register the billed
amount on the contract on posting (with the Record Billing asset-first
split replicated on the ledger), and leave unlinked templates byte
identical to before. Golden numbers are hand-derived from the inputs
stated in each test.
"""

import ast
from unittest.mock import patch

from dateutil.relativedelta import relativedelta
from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_revenue_recurring', 'post_install',
        '-at_install')
class TestRevenueRecurringBridge(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.contract_asset_acc = cls._ensure_account(
            cls.env, '1350', 'Contract Asset', 'asset_current')
        cls.contract_liab_acc = cls._ensure_account(
            cls.env, '2350', 'Contract Liability', 'liability_current')
        # Make the invoice receivable account deterministic for the exact
        # journal-entry assertions.
        cls.partner_a.property_account_receivable_id = cls.account_receivable
        cls.bridge_user = new_test_user(
            cls.env,
            login='revenue_recurring_bridge_user',
            groups='eh_account_base.group_eh_user',
        )
        cls.bridge_manager = new_test_user(
            cls.env,
            login='revenue_recurring_bridge_manager',
            groups='eh_account_base.group_eh_manager',
        )
        cls.plain_invoice_user = new_test_user(
            cls.env,
            login='revenue_recurring_plain_invoice_user',
            groups='account.group_account_invoice',
        )

    def _contract(self, price=1200.0):
        c = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': price,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'obligation_ids': [(0, 0, {
                'name': 'Support year', 'standalone_price': price,
                'satisfaction': 'over_time',
                'progress_method': 'output_milestones',
                'method_basis': 'Monthly service milestones depict the '
                                'stand-ready transfer (IFRS 15.B15).',
            })],
        })
        return c

    def _template(self, code, **overrides):
        vals = {
            'name': 'Monthly support %s' % code,
            'code': code,
            'partner_id': self.partner_a.id,
            'journal_id': self.journal_sale.id,
            'interval': 1,
            'interval_unit': 'month',
            'start_date': fields.Date.context_today(self.env.user),
            'next_run_date': fields.Date.context_today(self.env.user),
            'auto_post': True,
            'line_ids': [(0, 0, {
                'name': 'Support fee',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        }
        vals.update(overrides)
        return self.env['eh.recurring.invoice.template'].create(vals)

    def _financed_contract(self, satisfied=True):
        interest = self._ensure_account(
            self.env, '4298', 'Bridge Financing Income', 'income_other',
        )
        today = fields.Date.context_today(self.env.user)
        contract = self.env['eh.revenue.contract'].create({
            'partner_id': self.partner_a.id,
            'transaction_price': 1000.0,
            'contract_date': today,
            'revenue_account_id': self.account_revenue.id,
            'contract_asset_account_id': self.contract_asset_acc.id,
            'contract_liability_account_id': self.contract_liab_acc.id,
            'receivable_account_id': self.account_receivable.id,
            'journal_id': self.journal_misc.id,
            'financing_component': True,
            'financing_direction': 'arrears',
            'financing_rate': 0.10,
            'financing_period_months': 24,
            'financing_payment_date': today + relativedelta(months=24),
            'financing_account_id': interest.id,
            'obligation_ids': [(0, 0, {
                'name': 'Financed service',
                'standalone_price': 1000.0,
                'satisfaction': 'point_in_time',
                'satisfied': satisfied,
            })],
        })
        return contract, interest

    def test_golden_linked_template_credits_contract_liability(self):
        """Linked template, 100 per month, no tax.

        The generated invoice must post
        Dr trade receivable 100 / Cr contract liability 100
        (NOT income: the line account override routes the 100 to the
        contract's billing flow), and the contract registers 100 billed,
        so the position shows a 100 contract liability.
        """
        contract = self._contract()
        contract.action_activate()
        tpl = self._template('rev_linked', revenue_contract_id=contract.id)
        tpl.action_generate_now()
        inv = tpl.last_generated_move_id
        self.assertEqual(inv.state, 'posted')
        self.assertEqual(inv.move_type, 'out_invoice')
        self.assertEqual(inv.eh_revenue_contract_id, contract)
        self.assertEqual(inv.invoice_line_ids.account_id,
                         self.contract_liab_acc)
        self.assertMoveLines(inv, [
            (self.account_receivable, 100.0, 0.0),
            (self.contract_liab_acc, 0.0, 100.0),
        ])
        self.assertTrue(inv.eh_revenue_billing_registered)
        self.assertTrue(inv.eh_sealed)
        self.assertFalse(inv.eh_legacy_unverified_seal)
        with self.assertRaises(UserError):
            inv.invoice_line_ids.write({'price_unit': 1.0})
        with self.assertRaises(UserError):
            inv.with_context(force_delete=True).unlink()
        with self.assertRaisesRegex(UserError, 'frozen'):
            tpl.write({'revenue_contract_id': False})
        with self.assertRaisesRegex(UserError, 'cannot be deleted'):
            tpl.unlink()
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 100.0, places=2)
        # No income was touched; the ledger carries the deferred credit.
        self.assertAlmostEqual(
            self.posted_balance(self.account_revenue), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contract_liab_acc), -100.0, places=2)

    def test_unlinked_template_still_posts_to_income(self):
        # Regression: without a contract link the behaviour is byte
        # identical to the recurring module alone.
        tpl = self._template('rev_free')
        tpl.action_generate_now()
        inv = tpl.last_generated_move_id
        self.assertEqual(inv.state, 'posted')
        self.assertEqual(inv.invoice_line_ids.account_id,
                         self.account_revenue)
        self.assertFalse(inv.eh_revenue_contract_id)
        self.assertFalse(inv.eh_revenue_billing_registered)
        self.assertAlmostEqual(
            self.posted_balance(self.account_revenue), -100.0, places=2)

    def test_unlinked_generation_does_not_receive_revenue_capability(self):
        template = self._template('rev_unlinked_context')
        self.assertNotIn(
            'eh_revenue_move_link_capability',
            template._eh_move_create_context(),
        )
        contract = self._contract()
        contract.action_activate()
        linked = self._template(
            'rev_linked_context', revenue_contract_id=contract.id,
        )
        self.assertIn(
            'eh_revenue_move_link_capability',
            linked._eh_move_create_context(),
        )

    def test_tax_stays_on_invoice_and_billing_registers_untaxed_amount(self):
        tax_payable = self._ensure_account(
            self.env, '2259', 'Bridge output tax payable', 'liability_current',
        )
        country = (
            getattr(self.company, 'account_fiscal_country_id', False)
            or self.company.country_id
            or self.env.ref('base.us')
        )
        # A demo-less Odoo 16 company has neither a country nor a fiscal
        # country.  Configure the fiscal country explicitly before creating
        # the tax, just as production tax configuration requires.
        if 'account_fiscal_country_id' in self.company._fields:
            self.company.account_fiscal_country_id = country
        tax_vals = {
            'name': 'Bridge output tax 10%',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
        }
        if 'country_id' in self.env['account.tax']._fields:
            tax_vals['country_id'] = country.id
        tax = self.env['account.tax'].create(tax_vals)
        tax.invoice_repartition_line_ids.filtered(
            lambda line: line.repartition_type == 'tax'
        ).write({'account_id': tax_payable.id})
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_taxed',
            revenue_contract_id=contract.id,
            line_ids=[(0, 0, {
                'name': 'Taxed support fee',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 100.0,
                'tax_ids': [(6, 0, tax.ids)],
            })],
        )

        template.action_generate_now()

        invoice = template.last_generated_move_id
        self.assertAlmostEqual(invoice.amount_untaxed, 100.0, places=2)
        self.assertAlmostEqual(invoice.amount_tax, 10.0, places=2)
        self.assertAlmostEqual(invoice.amount_total, 110.0, places=2)
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 100.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(tax_payable), -10.0, places=2,
        )

    def test_generation_prevalidates_contract_asset_and_journal(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_missing_targets', revenue_contract_id=contract.id,
        )

        contract.write({'contract_asset_account_id': False})
        with self.assertRaisesRegex(UserError, 'contract asset account'):
            template.action_generate_now()
        self.assertFalse(template.generated_move_ids)

        contract.write({
            'contract_asset_account_id': self.contract_asset_acc.id,
            'journal_id': False,
        })
        with self.assertRaisesRegex(UserError, 'journal'):
            template.action_generate_now()
        self.assertFalse(template.generated_move_ids)

    def test_bridge_cron_pauses_and_preserves_misconfigured_period(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_cron_missing_asset',
            revenue_contract_id=contract.id,
            auto_post=True,
        )
        template.action_activate()
        failed_period = template.next_run_date
        contract.write({'contract_asset_account_id': False})

        self.env['eh.recurring.invoice.template']._cron_generate_due()

        self.assertEqual(template.state, 'paused')
        self.assertEqual(template.next_run_date, failed_period)
        self.assertIn('contract asset account', template.last_error)
        self.assertFalse(template.generated_move_ids)

    def test_generated_move_currency_is_checked_not_related_field_aliases(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_move_currency', revenue_contract_id=contract.id,
        )
        other_currency = self.env['res.currency'].with_context(
            active_test=False,
        ).search([
            ('id', '!=', self.company.currency_id.id),
        ], limit=1)
        self.assertTrue(other_currency)
        other_currency.sudo().active = True
        with self.assertRaisesRegex(ValidationError, "contract's currency"):
            template._eh_apply_revenue_contract_to_move_vals({
                'currency_id': other_currency.id,
                'invoice_line_ids': [(0, 0, {
                    'name': 'Foreign currency attempt',
                    'account_id': self.account_revenue.id,
                    'quantity': 1.0,
                    'price_unit': 100.0,
                })],
            })

    def test_reviewed_draft_is_sealed_before_revenue_registration(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_reviewed_draft',
            revenue_contract_id=contract.id,
            auto_post=False,
        )
        template.action_generate_now()
        invoice = template.last_generated_move_id
        self.assertEqual(invoice.state, 'draft')
        self.assertFalse(invoice.eh_sealed)
        invoice.invoice_line_ids.write({'price_unit': 125.0})

        invoice.action_post()

        self.assertEqual(invoice.state, 'posted')
        self.assertTrue(invoice.eh_sealed)
        self.assertTrue(invoice.eh_revenue_billing_registered)
        self.assertAlmostEqual(contract.amount_billed, 125.0, places=2)

    def test_plain_invoicer_posts_manager_approved_linked_draft(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_plain_invoicer',
            revenue_contract_id=contract.id,
            auto_post=False,
        )
        template.action_generate_now()
        invoice = template.last_generated_move_id
        self.assertFalse(
            self.plain_invoice_user.has_group(
                'eh_account_base.group_eh_user'
            )
        )
        with self.assertRaises(AccessError):
            contract.with_user(self.plain_invoice_user)._eh_check_access('read')

        invoice.with_user(self.plain_invoice_user).action_post()

        self.assertEqual(invoice.state, 'posted')
        self.assertTrue(invoice.eh_revenue_billing_registered)
        self.assertTrue(invoice.eh_sealed)
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)

    def test_golden_recognition_releases_bridged_billing(self):
        """End to end: bridge billing 100, then 50% progress on the 1,200
        obligation targets 600 cumulative revenue. The recognition run
        releases the 100 liability first and books the 500 remainder as a
        contract asset:
        Dr contract liability 100 / Dr contract asset 500 / Cr revenue 600.
        """
        contract = self._contract()
        contract.action_activate()
        tpl = self._template('rev_release', revenue_contract_id=contract.id)
        tpl.action_generate_now()
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)
        contract.obligation_ids.percent_complete = 50.0
        contract.action_recognise()
        move = contract.move_ids.sorted('id')[-1]
        self.assertMoveLines(move, [
            (self.contract_liab_acc, 100.0, 0.0),
            (self.contract_asset_acc, 500.0, 0.0),
            (self.account_revenue, 0.0, 600.0),
        ])
        self.assertAlmostEqual(contract.contract_asset, 500.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)

    def test_golden_asset_cleared_before_liability(self):
        """Recognition first (600 contract asset), then a bridged invoice
        of 100 must replicate the Record Billing split: the invoice
        credits the liability, and the registration reclassifies the
        asset-clearing portion:
        reclass entry Dr contract liability 100 / Cr contract asset 100,
        leaving asset 500, liability 0 on both the ledger and the stored
        position.
        """
        contract = self._contract()
        contract.action_activate()
        contract.obligation_ids.percent_complete = 50.0
        contract.action_recognise()
        self.assertAlmostEqual(contract.contract_asset, 600.0, places=2)
        tpl = self._template('rev_reclass', revenue_contract_id=contract.id)
        tpl.action_generate_now()
        reclass = contract.move_ids.sorted('id')[-1]
        self.assertMoveLines(reclass, [
            (self.contract_liab_acc, 100.0, 0.0),
            (self.contract_asset_acc, 0.0, 100.0),
        ])
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)
        self.assertAlmostEqual(contract.contract_asset, 500.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contract_liab_acc), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contract_asset_acc), 500.0, places=2)

    def test_backdated_invoice_reclasses_on_latest_recognition_date(self):
        recognition_date = fields.Date.from_string('2026-03-31')
        invoice_date = fields.Date.from_string('2026-02-01')
        contract = self._contract()
        contract.write({
            'contract_date': fields.Date.from_string('2026-01-01'),
            'accounting_date': recognition_date,
        })
        contract.action_activate()
        contract.obligation_ids.percent_complete = 50.0
        contract.action_recognise(accounting_date=recognition_date)
        template = self._template(
            'rev_backdated_reclass',
            revenue_contract_id=contract.id,
            start_date=invoice_date,
            next_run_date=invoice_date,
        )

        template.action_generate_now()

        invoice = template.last_generated_move_id
        reclass = contract.move_ids.filtered(
            lambda move: (move.ref or '').startswith(
                'Recurring billing reclass:'
            )
        ).ensure_one()
        self.assertEqual(invoice.date, invoice_date)
        self.assertEqual(reclass.date, recognition_date)
        self.assertMoveLines(reclass, [
            (self.contract_liab_acc, 100.0, 0.0),
            (self.contract_asset_acc, 0.0, 100.0),
        ])
        self.assertAlmostEqual(contract.contract_asset, 500.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)

    def test_registered_invoice_cannot_reset_and_desynchronise_contract(self):
        contract = self._contract()
        contract.action_activate()
        tpl = self._template('rev_repost', revenue_contract_id=contract.id)
        tpl.action_generate_now()
        inv = tpl.last_generated_move_id
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)
        with self.assertRaises(UserError):
            inv.button_draft()
        # Reset/edit/repost would leave amount_billed stale.  The registered
        # invoice is therefore frozen at the accounting workflow boundary.
        self.assertEqual(inv.state, 'posted')
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)

    def test_registered_invoice_has_exact_sealed_correction(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_billing_correction', revenue_contract_id=contract.id,
        )
        template.action_generate_now()
        source = template.last_generated_move_id
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)

        correction_date = source.date + relativedelta(days=1)
        source.action_reverse_revenue_billing(
            accounting_date=correction_date,
        )
        correction = self.env['account.move'].search([
            ('eh_revenue_billing_correction_of_id', '=', source.id),
        ])
        self.assertEqual(len(correction), 1)
        self.assertEqual(correction.state, 'posted')
        self.assertEqual(correction.move_type, 'out_refund')
        self.assertEqual(correction.reversed_entry_id, source)
        self.assertEqual(correction.date, correction_date)
        self.assertTrue(correction.eh_sealed)
        self.assertTrue(correction.eh_revenue_billing_registered)
        self.assertAlmostEqual(contract.amount_billed, 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.contract_liab_acc), 0.0, places=2,
        )
        with self.assertRaises(UserError):
            source.action_reverse_revenue_billing(
                accounting_date=correction_date,
            )
        contract.reversal_reason = (
            'Customer cancellation approved after the exact billing credit.'
        )
        contract.action_reverse_and_cancel(
            accounting_date=correction_date,
        )
        self.assertEqual(contract.state, 'cancelled')
        self.assertEqual(len(source._eh_reversal_moves()), 1)

    def test_bridge_review_releases_template_and_contract_together(self):
        contract = self._contract()
        template = self._template(
            'rev_bridge_review', revenue_contract_id=contract.id,
        )
        contract.sudo().write({
            'eh_integrity_review_required': True,
            'eh_integrity_review_reason': 'Synthetic migration quarantine',
        })
        self.env.cr.execute(
            "UPDATE eh_recurring_invoice_template "
            "SET eh_legacy_integrity_unverified = TRUE, "
            "eh_legacy_integrity_reason = %s WHERE id = %s",
            ('Synthetic bridge quarantine', template.id),
        )
        template.invalidate_recordset([
            'eh_legacy_integrity_unverified', 'eh_legacy_integrity_reason',
        ])
        template.eh_legacy_review_note = (
            'Reviewed contract, schedule, lines, and zero opening balances.'
        )
        template.action_review_legacy_and_resume_setup()
        self.assertFalse(template.eh_legacy_integrity_unverified)
        self.assertFalse(contract.eh_integrity_review_required)
        self.assertTrue(template.eh_legacy_reviewed_at)
        self.assertTrue(contract.eh_integrity_reviewed_at)

    def test_inactive_contract_blocks_generation(self):
        contract = self._contract()  # left in draft
        tpl = self._template('rev_draft', revenue_contract_id=contract.id)
        with self.assertRaises(UserError):
            tpl.action_generate_now()

    def test_non_manager_cannot_link_contract_or_guess_foreign_id(self):
        contract = self._contract()
        template = self._template('rev_link_guard')
        with self.assertRaises(AccessError):
            template.with_user(self.bridge_user).write({
                'revenue_contract_id': contract.id,
            })

        foreign_company = self.env['res.company'].create({
            'name': 'Hidden Revenue Bridge Company',
        })
        foreign_contract = self.env['eh.revenue.contract'].sudo().with_company(
            foreign_company,
        ).create({
            'company_id': foreign_company.id,
            'partner_id': self.partner_a.id,
            'transaction_price': 100.0,
            'obligation_ids': [(0, 0, {
                'name': 'Hidden obligation',
                'standalone_price': 100.0,
            })],
        })
        with self.assertRaises(AccessError):
            template.with_user(self.bridge_manager).write({
                'revenue_contract_id': foreign_contract.id,
            })

    def test_registration_output_rejects_forgeable_context(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_registration_guard', revenue_contract_id=contract.id,
        )
        template.action_generate_now()
        invoice = template.last_generated_move_id
        with self.assertRaises(AccessError):
            invoice.with_context(
                eh_recurring_revenue_billing_capability=True,
            ).write({'eh_revenue_billing_registered': False})
        self.assertTrue(invoice.eh_revenue_billing_registered)

    def test_registration_status_is_visible_on_invoice_form(self):
        view = self.env.ref(
            'eh_account_revenue_recurring.'
            'view_move_form_revenue_recurring_correction'
        )
        arch = etree.fromstring(view.arch_db.encode())
        visible_status = arch.xpath(
            ".//field[@name='eh_revenue_billing_registered' "
            "and not(@invisible='1')]"
        )
        self.assertEqual(len(visible_status), 1)
        status_field = visible_status[0]
        if status_field.get('invisible') is not None:
            self.assertEqual(
                status_field.get('invisible'),
                'not eh_revenue_contract_id',
            )
        else:
            self.assertEqual(
                ast.literal_eval(status_field.get('attrs', '{}')),
                {
                    'invisible': [
                        ('eh_revenue_contract_id', '=', False),
                    ],
                },
            )

    def test_billing_registration_recomputes_under_both_row_locks(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_registration_lock', revenue_contract_id=contract.id,
        )
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            template.action_generate_now()
        self.assertTrue(any(
            'eh_revenue_contract' in query and 'FOR UPDATE' in query
            for query in queries
        ))
        self.assertTrue(any(
            'account_move' in query and 'FOR UPDATE' in query
            for query in queries
        ))

    def test_linked_proration_registers_one_signed_billing_delta(self):
        contract = self._contract()
        contract.action_activate()
        template = self._template(
            'rev_proration',
            revenue_contract_id=contract.id,
            start_date=fields.Date.from_string('2026-04-01'),
            next_run_date=fields.Date.from_string('2026-04-01'),
        )
        template.action_generate_now()
        source = template.last_generated_move_id
        self.assertAlmostEqual(contract.amount_billed, 100.0, places=2)

        wizard = self.env[
            'eh.recurring.invoice.proration.wizard'
        ].create({
            'template_id': template.id,
            'change_date': fields.Date.from_string('2026-04-16'),
            'new_amount': 200.0,
        })
        pair = self.env['account.move'].browse(
            wizard.action_apply()['domain'][0][2]
        )
        self.assertEqual(len(pair), 2)
        self.assertTrue(all(pair.mapped('eh_revenue_billing_registered')))
        self.assertEqual(pair.mapped('eh_revenue_contract_id'), contract)
        self.assertFalse(pair.mapped('eh_recurring_template_id'))
        self.assertEqual(
            pair.mapped('eh_recurring_proration_template_id'), template,
        )
        template.invalidate_recordset(['generated_count'])
        self.assertEqual(template.generated_count, 1)
        self.assertEqual(
            pair.mapped('eh_recurring_proration_source_move_id'), source,
        )
        self.assertTrue(all(
            line.account_id == self.contract_liab_acc
            for line in pair.invoice_line_ids.filtered('account_id')
        ))
        # Original billing 100, then credit 50 + charge 100 => net +50.
        self.assertAlmostEqual(contract.amount_billed, 150.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 150.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.account_revenue), 0.0, places=2,
        )

    def test_financed_billing_reclass_uses_actual_gl_position(self):
        contract, interest = self._financed_contract(satisfied=True)
        contract.action_activate()
        contract.action_recognise()
        self.assertAlmostEqual(
            sum(contract.obligation_ids.mapped('recognised_amount')),
            1000.0,
            places=2,
        )
        self.assertAlmostEqual(contract.amount_recognised, 826.45, places=2)
        self.assertAlmostEqual(contract.contract_asset, 826.45, places=2)
        template = self._template(
            'rev_financed_bridge',
            revenue_contract_id=contract.id,
            line_ids=[(0, 0, {
                'name': 'Financed service billing',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 1000.0,
            })],
        )
        template.action_generate_now()
        contract.invalidate_recordset(['contract_asset', 'contract_liability'])
        self.assertAlmostEqual(contract.amount_billed, 1000.0, places=2)
        self.assertAlmostEqual(contract.contract_asset, 0.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)
        self.assertAlmostEqual(
            contract.eh_recurring_financing_receivable_adjustment,
            173.55,
            places=2,
        )
        self.assertAlmostEqual(
            self.posted_balance(self.contract_asset_acc), 0.0, places=2,
        )
        self.assertAlmostEqual(
            self.posted_balance(self.contract_liab_acc), 0.0, places=2,
        )
        self.assertAlmostEqual(
            self.posted_balance(self.account_receivable), 826.45, places=2,
        )

        contract.action_accrue_financing(
            accounting_date=contract.financing_payment_date,
        )

        self.assertAlmostEqual(
            contract.eh_recurring_financing_receivable_adjustment,
            0.0,
            places=2,
        )
        self.assertAlmostEqual(
            contract.financing_interest_recognised, -173.55, places=2,
        )
        self.assertAlmostEqual(
            self.posted_balance(self.account_receivable), 1000.0, places=2,
        )
        self.assertAlmostEqual(
            self.posted_balance(interest), -173.55, places=2,
        )
        self.assertAlmostEqual(contract.contract_asset, 0.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)

    def test_financed_billing_before_satisfaction_reclassifies_on_recognition(self):
        contract, _interest = self._financed_contract(satisfied=False)
        contract.action_activate()
        template = self._template(
            'rev_financed_billing_first',
            revenue_contract_id=contract.id,
            line_ids=[(0, 0, {
                'name': 'Financed service billing first',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 1000.0,
            })],
        )
        template.action_generate_now()
        self.assertAlmostEqual(contract.contract_liability, 1000.0, places=2)

        contract.obligation_ids.satisfied = True
        contract.action_recognise()

        self.assertAlmostEqual(
            sum(contract.obligation_ids.mapped('recognised_amount')),
            1000.0,
            places=2,
        )
        self.assertAlmostEqual(contract.amount_recognised, 826.45, places=2)
        self.assertAlmostEqual(contract.amount_billed, 1000.0, places=2)
        self.assertAlmostEqual(contract.contract_asset, 0.0, places=2)
        self.assertAlmostEqual(contract.contract_liability, 0.0, places=2)
        self.assertAlmostEqual(
            contract.eh_recurring_financing_receivable_adjustment,
            173.55,
            places=2,
        )

    def test_quarantined_contract_blocks_bridge_before_invoice(self):
        contract = self._contract()
        contract.action_activate()
        contract.sudo().write({
            'eh_integrity_review_required': True,
            'eh_integrity_review_reason': 'synthetic legacy conflict',
        })
        template = self._template(
            'rev_quarantine', revenue_contract_id=contract.id,
        )
        with self.assertRaises(UserError):
            template.action_generate_now()
        self.assertFalse(template.last_generated_move_id)
        self.assertEqual(contract.amount_billed, 0.0)
