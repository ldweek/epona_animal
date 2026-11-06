# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Customer credit limit tests.

Sets up a company default policy with a $1000 limit, two test partners,
and walks through:

* Exposure calculation (open AR, optional drafts).
* Block on customer invoice post when over limit.
* Warn-only mode logs to chatter without blocking.
* Override flow records to the audit log and allows the post.
* Override log is append-only and undeletable.
* Refund reduces exposure rather than adding.
* Per-partner override takes precedence over the company default.
"""

from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_credit_limit', 'integration', 'post_install', '-at_install')
class TestCreditLimit(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Policy = cls.env['eh.credit.policy']
        cls.LogModel = cls.env['eh.credit.override.log']

        cls.policy = cls.Policy.create({
            'name': 'Default test policy',
            'company_id': cls.env.company.id,
            'is_company_default': True,
            'default_credit_limit': 1000.0,
            'enforcement_mode': 'block',
            'include_drafts': False,
        })

        # Test user gets manager so override paths can be exercised.
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )
        cls.plain_user = cls.env['res.users'].create({
            'name': 'Credit Limit Plain User',
            'login': 'credit_limit_plain_user',
            'email': 'credit_limit_plain_user@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.standard_account_user = cls.env['res.users'].create({
            'name': 'Credit Standard Accounting User',
            'login': 'credit_standard_account_user',
            'email': 'credit_standard_account_user@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('account.group_account_invoice').id,
            ])],
        })
        cls.non_account_user = cls.env['res.users'].create({
            'name': 'Credit Non-account User',
            'login': 'credit_non_account_user',
            'email': 'credit_non_account_user@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.portal_user = cls.env['res.users'].create({
            'name': 'Credit Portal User',
            'login': 'credit_portal_user',
            'email': 'credit_portal_user@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        cls.restricted_manager = cls.env['res.users'].create({
            'name': 'Credit Limit Restricted Manager',
            'login': 'credit_limit_restricted_manager',
            'email': 'credit_limit_restricted_manager@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, [
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def _customer_invoice(self, partner, amount, post=True):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Test sale',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_revenue.id,
            })],
        })
        if post:
            move.action_post()
        return move

    def _sale_order_linked_draft(
            self, order_amount=800.0, linked_amount=None,
            unlinked_amount=0.0):
        """Create confirmed SO plus draft invoice carrying source links."""
        linked_amount = (
            order_amount if linked_amount is None else linked_amount
        )
        sale_line_vals = {
            'name': 'Credit exposure source order',
            'product_id': self._eh_credit_test_product().id,
            'product_uom_qty': 1.0,
            'price_unit': order_amount,
        }
        tax_field = (
            'tax_ids' if 'tax_ids' in self.env['sale.order.line']._fields
            else 'tax_id'
        )
        sale_line_vals[tax_field] = [(6, 0, [])]
        order = self.env['sale.order'].create({
            'partner_id': self.partner_a.id,
            'order_line': [(0, 0, sale_line_vals)],
        })
        order.action_confirm()
        invoice_lines = [(0, 0, {
            'name': 'Sale-linked invoice amount',
            'quantity': 1.0,
            'price_unit': linked_amount,
            'account_id': self.account_revenue.id,
            'tax_ids': [(6, 0, [])],
            'sale_line_ids': [(6, 0, order.order_line.ids)],
        })]
        if unlinked_amount:
            invoice_lines.append((0, 0, {
                'name': 'New credit outside source order',
                'quantity': 1.0,
                'price_unit': unlinked_amount,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, [])],
            }))
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': invoice_lines,
        })
        return order, invoice

    # ---- exposure calculation ----

    def test_zero_exposure_for_clean_partner(self):
        self.assertEqual(self.partner_a.eh_credit_exposure, 0.0)
        self.assertEqual(self.partner_a.eh_credit_status, 'ok')

    def test_exposure_includes_posted_open_ar(self):
        self._customer_invoice(self.partner_a, 200.0)
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 200.0)

    def test_status_warn_above_80_percent(self):
        self._customer_invoice(self.partner_a, 850.0)  # 85% of 1000
        self.partner_a.invalidate_recordset(['eh_credit_status'])
        self.assertEqual(self.partner_a.eh_credit_status, 'warn')

    def test_status_over_above_100_percent(self):
        # Switch to warn mode so the over-limit invoice can post and
        # exposure becomes computable. The block-mode gate is exercised
        # in test_post_blocked_when_over_limit instead.
        self.policy.enforcement_mode = 'warn'
        self._customer_invoice(self.partner_a, 1100.0)
        self.partner_a.invalidate_recordset(['eh_credit_status'])
        self.assertEqual(self.partner_a.eh_credit_status, 'over')

    def test_drafts_count_when_policy_says_so(self):
        self.policy.include_drafts = True
        self._customer_invoice(self.partner_a, 200.0)
        self._customer_invoice(self.partner_a, 300.0, post=False)
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 500.0)

    def test_drafts_excluded_by_default(self):
        self._customer_invoice(self.partner_a, 200.0)
        self._customer_invoice(self.partner_a, 300.0, post=False)
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 200.0)

    def test_child_contact_draft_counts_against_commercial_parent(self):
        self.policy.include_drafts = True
        child = self.env['res.partner'].create({
            'name': 'Credit child contact',
            'parent_id': self.partner_a.id,
            'type': 'invoice',
        })
        self._customer_invoice(child, 800.0, post=False)
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 800.0)
        with self.assertRaisesRegex(UserError, 'Credit limit exceeded'):
            self._customer_invoice(self.partner_a, 300.0)

    def test_child_contact_sale_order_counts_against_commercial_parent(self):
        if 'sale.order' not in self.env:
            self.skipTest("sale module not installed in this run")
        self.policy.include_sale_orders = True
        child = self.env['res.partner'].create({
            'name': 'Credit child sale contact',
            'parent_id': self.partner_a.id,
            'type': 'contact',
        })
        line_vals = {
            'name': 'Child-contact order',
            'product_id': self._eh_credit_test_product().id,
            'product_uom_qty': 1.0,
            'price_unit': 800.0,
        }
        tax_field = (
            'tax_ids' if 'tax_ids' in self.env['sale.order.line']._fields
            else 'tax_id'
        )
        line_vals[tax_field] = [(6, 0, [])]
        order = self.env['sale.order'].create({
            'partner_id': child.id,
            'order_line': [(0, 0, line_vals)],
        })
        order.action_confirm()
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 800.0)
        with self.assertRaisesRegex(UserError, 'Credit limit exceeded'):
            self._customer_invoice(self.partner_a, 300.0)

    def test_child_contact_cannot_own_separate_credit_configuration(self):
        child = self.env['res.partner'].create({
            'name': 'Credit configuration child',
            'parent_id': self.partner_a.id,
            'type': 'contact',
        })
        with self.assertRaisesRegex(
                ValidationError, 'commercial customer'):
            child.write({'eh_credit_limit': 999999.0})
        with self.assertRaisesRegex(
                ValidationError, 'commercial customer'):
            self.env['res.partner'].create({
                'name': 'Configured child on create',
                'parent_id': self.partner_a.id,
                'eh_credit_policy_id': self.policy.id,
            })

        self.partner_a.eh_credit_limit = 400.0
        child.invalidate_recordset([
            'eh_effective_credit_limit', 'eh_credit_exposure',
        ])
        self.assertEqual(child.eh_effective_credit_limit, 400.0)
        self._customer_invoice(child, 300.0)
        child.invalidate_recordset(['eh_credit_exposure'])
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertEqual(child.eh_credit_exposure, 300.0)
        self.assertEqual(
            child.eh_credit_exposure, self.partner_a.eh_credit_exposure,
        )

    def test_native_branch_receivable_rolls_to_root_credit_limit(self):
        if 'root_id' not in self.env['res.company']._fields:
            self.skipTest('native accounting branches start in Odoo 17')
        branch = self._create_accounting_branch({
            'name': 'Credit exposure branch',
            'parent_id': self.company.id,
        })
        allowed_ids = [self.company.id, branch.id]
        partner = self.partner_a.with_context(
            allowed_company_ids=allowed_ids,
        )
        Move = self.env['account.move'].with_context(
            allowed_company_ids=allowed_ids,
        )
        branch_move = Move.with_company(branch).create({
            'move_type': 'out_invoice',
            'company_id': branch.id,
            'partner_id': partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Branch sale',
                'quantity': 1,
                'price_unit': 400.0,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, [])],
            })],
        })
        branch_move.action_post()
        root_partner = partner.with_company(self.company)
        root_partner.invalidate_recordset(['eh_credit_exposure'])
        self.assertEqual(root_partner.eh_credit_exposure, 400.0)

        root_move = Move.with_company(self.company).create({
            'move_type': 'out_invoice',
            'company_id': self.company.id,
            'partner_id': partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Root sale beyond group limit',
                'quantity': 1,
                'price_unit': 700.0,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, [])],
            })],
        })
        with self.assertRaisesRegex(UserError, 'Credit limit exceeded'):
            root_move.action_post()

    def test_sale_order_open_amount_tracks_partial_and_full_invoicing(self):
        if 'sale.order' not in self.env:
            self.skipTest("sale module not installed in this run")
        product = self._eh_credit_test_product()
        line_vals = {
            'name': 'Partially invoiced credit exposure',
            'product_id': product.id,
            'product_uom_qty': 2.0,
            'price_unit': 400.0,
        }
        tax_field = (
            'tax_ids' if 'tax_ids' in self.env['sale.order.line']._fields
            else 'tax_id'
        )
        line_vals[tax_field] = [(6, 0, [])]
        order = self.env['sale.order'].create({
            'partner_id': self.partner_a.id,
            'order_line': [(0, 0, line_vals)],
        })
        order.action_confirm()
        sale_line = order.order_line
        self.assertAlmostEqual(
            self.partner_a._eh_sale_order_uninvoiced_amount(order),
            800.0,
        )

        for expected in (400.0, 0.0):
            invoice = self.env['account.move'].create({
                'move_type': 'out_invoice',
                'partner_id': self.partner_a.id,
                'invoice_date': '2026-04-15',
                'invoice_line_ids': [(0, 0, {
                    'name': 'Linked partial invoice',
                    'quantity': 1.0,
                    'price_unit': 400.0,
                    'account_id': self.account_revenue.id,
                    'tax_ids': [(6, 0, [])],
                    'sale_line_ids': [(6, 0, sale_line.ids)],
                })],
            })
            invoice.action_post()
            order.invalidate_recordset()
            sale_line.invalidate_recordset()
            self.assertAlmostEqual(
                self.partner_a._eh_sale_order_uninvoiced_amount(order),
                expected,
            )

    def test_delivery_policy_order_is_credit_exposure_before_delivery(self):
        if 'sale.order' not in self.env:
            self.skipTest("sale module not installed in this run")
        product = self.env['product.product'].create({
            'name': 'EH Credit Delivery Policy Product',
            'type': 'service',
            'invoice_policy': 'delivery',
            'list_price': 800.0,
        })
        line_vals = {
            'name': 'Undelivered confirmed credit commitment',
            'product_id': product.id,
            'product_uom_qty': 1.0,
            'price_unit': 800.0,
        }
        tax_field = (
            'tax_ids' if 'tax_ids' in self.env['sale.order.line']._fields
            else 'tax_id'
        )
        line_vals[tax_field] = [(6, 0, [])]
        order = self.env['sale.order'].create({
            'partner_id': self.partner_a.id,
            'order_line': [(0, 0, line_vals)],
        })
        order.action_confirm()

        # Core's workflow measure is version-dependent here: Odoo 17 reports
        # the ordered amount, while Odoo 19 reports zero until delivery. The
        # credit commitment must remain the full confirmed amount on both.
        self.assertAlmostEqual(
            self.partner_a._eh_sale_order_uninvoiced_amount(order),
            800.0,
        )

    def test_sale_derived_invoice_replaces_order_exposure_at_gate(self):
        if 'sale.order' not in self.env:
            self.skipTest("sale module not installed in this run")
        self.policy.include_sale_orders = True
        order, invoice = self._sale_order_linked_draft()
        partner = self.partner_a.commercial_partner_id
        self.assertAlmostEqual(
            partner._eh_sale_order_uninvoiced_amount(order), 800.0,
        )
        exposure = invoice._eh_partner_exposure_excluding_self(
            partner,
            self.policy,
        )
        self.assertAlmostEqual(exposure, 0.0)
        invoice.action_post()
        partner.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(partner.eh_credit_exposure, 800.0)

    def test_linked_draft_counted_once_when_both_sources_enabled(self):
        if 'sale.order' not in self.env:
            self.skipTest("sale module not installed in this run")
        self.policy.write({
            'include_sale_orders': True,
            'include_drafts': True,
        })
        _order, invoice = self._sale_order_linked_draft()
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 800.0)
        invoice.action_post()

    def test_unlinked_invoice_amount_remains_new_exposure(self):
        if 'sale.order' not in self.env:
            self.skipTest("sale module not installed in this run")
        self.policy.include_sale_orders = True
        _order, invoice = self._sale_order_linked_draft(
            unlinked_amount=300.0,
        )
        with self.assertRaisesRegex(UserError, 'Credit limit exceeded'):
            invoice.action_post()

    # ---- inline credit warning banner ----

    def test_standard_account_user_is_governed_without_eh_policy_acl(self):
        """Posting gate works for Accounting users outside EH groups."""
        partner = self.env['res.partner'].create({
            'name': 'Standard-account credit customer',
            'eh_credit_policy_id': self.policy.id,
        })
        invoice = self._customer_invoice(partner, 100.0, post=False)
        actor_invoice = invoice.with_user(self.standard_account_user)
        actor_invoice.invalidate_recordset(['eh_credit_warning'])

        resolved = partner.with_user(
            self.standard_account_user,
        )._eh_resolve_credit_policy()
        self.assertEqual(resolved.id, self.policy.id)
        self.assertFalse(actor_invoice.eh_credit_warning)
        actor_invoice.action_post()
        self.assertEqual(invoice.state, 'posted')

    def test_policy_lookup_role_and_company_matrix(self):
        current = self.Policy.with_user(
            self.standard_account_user,
        ).find_for_company(self.company)
        self.assertEqual(current.id, self.policy.id)

        internal = self.Policy.with_user(
            self.non_account_user,
        ).find_for_company(self.company)
        self.assertEqual(internal.id, self.policy.id)
        internal_partner = self.partner_a.with_user(self.non_account_user)
        internal_partner.invalidate_recordset(['eh_credit_status'])
        with self.assertRaises(AccessError):
            internal_partner.eh_credit_status

        with self.assertRaises(AccessError):
            self.Policy.with_user(self.portal_user).find_for_company(
                self.company,
            )

        other_company = self.env['res.company'].create({
            'name': 'Credit Policy Hidden Company',
        })
        with self.assertRaises(AccessError):
            self.Policy.with_user(
                self.standard_account_user,
            ).find_for_company(other_company)

    def test_credit_warning_on_over_limit_draft(self):
        self.partner_a.eh_credit_limit = 500.0
        draft = self._customer_invoice(self.partner_a, 800.0, post=False)
        draft.invalidate_recordset(['eh_credit_warning'])
        self.assertTrue(draft.eh_credit_warning)
        self.assertIn('credit limit', draft.eh_credit_warning)

    def test_unsaved_move_never_leaks_newid_into_sql_domain(self):
        unsaved = self.env['account.move'].new({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'company_id': self.company.id,
        })
        self.assertNotIsInstance(unsaved.id, int)
        self.assertEqual(
            unsaved._eh_partner_exposure_excluding_self(
                self.partner_a.commercial_partner_id,
                self.policy,
            ),
            0.0,
        )

    def test_no_credit_warning_within_limit(self):
        self.partner_a.eh_credit_limit = 5000.0
        draft = self._customer_invoice(self.partner_a, 800.0, post=False)
        draft.invalidate_recordset(['eh_credit_warning'])
        self.assertFalse(draft.eh_credit_warning)

    def test_partner_credit_configuration_is_manager_only(self):
        # Reach the credit-specific guard rather than Odoo's independent
        # contact-creation ACL.  This group does not imply EH Manager.
        self.plain_user.group_ids |= self.env.ref(
            'base.group_partner_manager',
        )
        self.assertFalse(self.plain_user.has_group(
            'eh_account_base.group_eh_manager',
        ))
        partner = self.env['res.partner'].with_user(self.plain_user).create({
            'name': 'Explicit empty credit defaults',
            'eh_credit_limit': 0.0,
            'eh_credit_policy_id': False,
        })
        self.assertFalse(partner.eh_credit_limit)
        self.assertFalse(partner.eh_credit_policy_id)

        with self.assertRaises(AccessError):
            self.env['res.partner'].with_user(self.plain_user).with_context(
                default_eh_credit_limit=999999.0,
            ).create({'name': 'Forged context credit limit'})
        with self.assertRaises(AccessError):
            self.env['res.partner'].with_user(self.plain_user).with_context(
                default_eh_credit_policy_id=self.policy.id,
            ).create({'name': 'Forged context credit policy'})
        with self.assertRaises(ValidationError):
            self.env['res.partner'].with_user(self.plain_user).with_context(
                default_eh_credit_limit=-1.0,
            ).create({'name': 'Negative context credit limit'})

        for quarantine in ('forged', False):
            with self.subTest(quarantine=quarantine, mode='non_su'):
                with self.assertRaises(AccessError):
                    self.env['res.partner'].with_user(
                        self.plain_user,
                    ).with_context(
                        default_eh_credit_legacy_config_quarantine=quarantine,
                    ).create({'name': 'Forged context quarantine'})
            with self.subTest(quarantine=quarantine, mode='sudo'):
                with self.assertRaises(AccessError):
                    self.env['res.partner'].sudo().with_context(
                        default_eh_credit_legacy_config_quarantine=quarantine,
                    ).create({'name': 'Unprivileged sudo context quarantine'})

        quarantined_partner = self.env['res.partner'].sudo().with_context(
            default_eh_credit_legacy_config_quarantine='trusted evidence',
            eh_credit_quarantine_internal=True,
        ).create({'name': 'Privileged context quarantine'})
        self.assertEqual(
            quarantined_partner.eh_credit_legacy_config_quarantine,
            'trusted evidence',
        )

        partner = self.env['res.partner'].with_user(
            self.plain_user,
        ).with_context(
            default_eh_credit_limit=999999.0,
            default_eh_credit_policy_id=self.policy.id,
        ).create({
            'name': 'Explicit empty credit defaults beat context',
            'eh_credit_limit': 0.0,
            'eh_credit_policy_id': False,
        })
        self.assertFalse(partner.eh_credit_limit)
        self.assertFalse(partner.eh_credit_policy_id)

        with self.assertRaises(AccessError):
            self.env['res.partner'].with_user(self.plain_user).create({
                'name': 'Forged credit limit on create',
                'eh_credit_limit': 999999.0,
            })
        with self.assertRaises(AccessError):
            self.env['res.partner'].with_user(self.plain_user).create({
                'name': 'Forged credit policy on create',
                'eh_credit_policy_id': self.policy.id,
            })
        with self.assertRaises(AccessError):
            self.partner_a.with_user(self.plain_user).write({
                'eh_credit_limit': 999999.0,
            })
        with self.assertRaises(AccessError):
            self.partner_a.with_user(self.plain_user).write({
                'eh_credit_policy_id': self.policy.id,
            })
        with self.assertRaises(AccessError):
            self.partner_a.with_user(self.plain_user).write({
                'eh_credit_limit': 0.0,
                'eh_credit_policy_id': False,
            })
        with self.assertRaises(ValidationError):
            self.partner_a.write({'eh_credit_limit': -1.0})

    def test_company_owned_partner_configuration_requires_its_context(self):
        other_company = self.env['res.company'].create({
            'name': 'Credit Context Other Company',
        })
        owned_partner = self.env['res.partner'].create({
            'name': 'Company-owned credit customer',
            'company_id': self.company.id,
        })
        with self.assertRaises(AccessError):
            owned_partner.with_company(other_company).with_user(
                self.restricted_manager,
            ).write({'eh_credit_limit': 250.0})

    def test_shared_partner_credit_configuration_is_company_scoped(self):
        other_company = self.env['res.company'].create({
            'name': 'Credit Limit Other Company',
            'currency_id': self.env.ref('base.USD').id,
        })
        other_policy = self.Policy.with_company(other_company).create({
            'name': 'Other company credit policy',
            'company_id': other_company.id,
            'default_credit_limit': 2000.0,
            'enforcement_mode': 'block',
        })
        partner_a = self.partner_a.with_company(self.company)
        partner_b = self.partner_a.with_company(other_company)
        partner_a.write({
            'eh_credit_limit': 111.0,
            'eh_credit_policy_id': self.policy.id,
        })
        partner_b.write({
            'eh_credit_limit': 222.0,
            'eh_credit_policy_id': other_policy.id,
        })
        self.assertEqual(partner_a.eh_credit_limit, 111.0)
        self.assertEqual(partner_b.eh_credit_limit, 222.0)
        self.assertEqual(partner_a.eh_credit_policy_id, self.policy)
        self.assertEqual(partner_b.eh_credit_policy_id, other_policy)
        self.assertEqual(
            partner_a.eh_credit_policy_company_id, self.company,
        )
        self.assertEqual(
            partner_b.eh_credit_policy_company_id, other_company,
        )
        self.assertEqual(
            self.partner_a._fields['eh_credit_policy_id'].domain,
            "[('company_id', '=', eh_credit_policy_company_id)]",
        )
        with self.assertRaises(AccessError):
            self.policy.with_user(self.restricted_manager).write({
                'company_id': other_company.id,
            })

    def test_credit_fields_and_override_reason_are_role_restricted(self):
        Partner = self.env['res.partner']
        Move = self.env['account.move']
        for field_name in (
            'eh_credit_exposure', 'eh_effective_credit_limit',
            'eh_credit_status',
        ):
            groups = Partner._fields[field_name].groups
            self.assertIn('account.group_account_invoice', groups)
            self.assertIn('eh_account_base.group_eh_user', groups)
        self.assertIn(
            'eh_credit_can_override',
            self.env.ref(
                'eh_account_credit_limit.view_move_form_eh_credit',
            ).arch_db,
        )
        self.assertIn(
            'eh_account_base.group_eh_user',
            Move._fields['eh_credit_can_override'].groups,
        )
        partner_arch = self.env.ref(
            'eh_account_credit_limit.view_partner_form_eh_credit',
        ).arch_db
        self.assertIn('eh_account_base.group_eh_manager', partner_arch)
        self.assertIn('name="eh_credit_limit"', partner_arch)
        self.assertIn('name="eh_credit_policy_id"', partner_arch)

    def test_legacy_shared_configuration_is_quarantined_on_upgrade(self):
        owned_partner = self.env['res.partner'].create({
            'name': 'Legacy owned credit customer',
            'company_id': self.company.id,
        })
        shared_partner = self.env['res.partner'].create({
            'name': 'Legacy shared credit customer',
            'company_id': False,
        })
        # Recreate both pre-1.2.3 scalar columns. PostgreSQL DDL is
        # transactional, so the test-case rollback restores the registry's
        # normal JSONB schema even when an assertion fails.
        # Odoo 16/17 company-dependent fields live in ``ir.property`` and have
        # no model-table column, while 18/19 keep JSONB columns.  Seed the
        # deployed scalar shape explicitly on either storage series.
        self.env.cr.execute(
            "ALTER TABLE res_partner ADD COLUMN IF NOT EXISTS "
            "eh_credit_limit numeric"
        )
        self.env.cr.execute(
            "ALTER TABLE res_partner ADD COLUMN IF NOT EXISTS "
            "eh_credit_policy_id int4"
        )
        self.env.cr.execute(
            "UPDATE res_partner SET eh_credit_limit = NULL, "
            "eh_credit_policy_id = NULL"
        )
        self.env.cr.execute(
            "ALTER TABLE res_partner ALTER COLUMN eh_credit_limit "
            "TYPE numeric USING NULL"
        )
        self.env.cr.execute(
            "ALTER TABLE res_partner ALTER COLUMN eh_credit_policy_id "
            "TYPE int4 USING NULL"
        )
        legacy_fk = 'eh_credit_limit_test_policy_legacy_fkey'
        self.env.cr.execute(
            "ALTER TABLE res_partner ADD CONSTRAINT "
            "eh_credit_limit_test_policy_legacy_fkey "
            "FOREIGN KEY (eh_credit_policy_id) "
            "REFERENCES eh_credit_policy(id)"
        )
        self.env.cr.execute(
            "UPDATE res_partner SET eh_credit_limit = %s, "
            "eh_credit_policy_id = %s WHERE id = %s",
            [321.5, self.policy.id, owned_partner.id],
        )
        self.env.cr.execute(
            "UPDATE res_partner SET eh_credit_limit = %s, "
            "eh_credit_policy_id = %s WHERE id = %s",
            [654.25, self.policy.id, shared_partner.id],
        )
        migration_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.2.3' / 'pre-migration.py'
        )
        spec = spec_from_file_location(
            'eh_credit_limit_pre_migration', migration_path,
        )
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.2.2')

        self.env.cr.execute(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'res_partner'::regclass AND conname = %s",
            [legacy_fk],
        )
        self.assertEqual(self.env.cr.fetchone()[0], 0)

        self.env.cr.execute(
            "SELECT eh_credit_limit, eh_credit_policy_id, "
            "eh_credit_legacy_config_quarantine FROM res_partner "
            "WHERE id = %s",
            [owned_partner.id],
        )
        owned_limit, owned_policy, owned_quarantine = self.env.cr.fetchone()
        company_key = str(self.company.id)
        self.assertEqual(float(owned_limit[company_key]), 321.5)
        self.assertEqual(owned_policy[company_key], self.policy.id)
        self.assertFalse(owned_quarantine)

        self.env.cr.execute(
            "SELECT eh_credit_limit, eh_credit_policy_id, "
            "eh_credit_legacy_config_quarantine FROM res_partner "
            "WHERE id = %s",
            [shared_partner.id],
        )
        shared_limit, shared_policy, quarantine_text = self.env.cr.fetchone()
        self.assertIsNone(shared_limit)
        self.assertIsNone(shared_policy)
        quarantine = json.loads(quarantine_text)
        self.assertEqual(quarantine['credit_limit'], 654.25)
        self.assertEqual(quarantine['credit_policy_id'], self.policy.id)
        self.assertEqual(
            quarantine['reason'], 'shared-partner-company-ambiguous',
        )

    # ---- block mode ----

    def test_post_blocked_when_over_limit(self):
        self._customer_invoice(self.partner_a, 800.0)
        # Now try to post a 500 invoice; total would be 1300 vs 1000.
        with self.assertRaises(UserError) as cm:
            self._customer_invoice(self.partner_a, 500.0)
        self.assertIn('Credit limit exceeded', str(cm.exception))

    def test_post_succeeds_under_limit(self):
        self._customer_invoice(self.partner_a, 200.0)
        # 200 + 700 = 900 vs 1000 limit.
        invoice = self._customer_invoice(self.partner_a, 700.0)
        self.assertEqual(invoice.state, 'posted')

    # ---- warn mode ----

    def test_warn_mode_logs_but_does_not_block(self):
        self.policy.enforcement_mode = 'warn'
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self._customer_invoice(self.partner_a, 500.0)
        self.assertEqual(invoice.state, 'posted')
        # The chatter should mention the breach.
        bodies = [m.body or '' for m in invoice.message_ids]
        self.assertTrue(
            any('Credit warning' in b for b in bodies),
            "expected a warning chatter post on the over-limit move",
        )

    # ---- override flow ----

    def test_override_with_reason_succeeds(self):
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'eh_credit_override_reason': 'Long-term customer; deal pending',
            'invoice_line_ids': [(0, 0, {
                'name': 'Big sale',
                'quantity': 1,
                'price_unit': 500.0,
                'account_id': self.account_revenue.id,
            })],
        })
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')
        self.assertTrue(invoice.eh_credit_override_log_id)
        self.assertEqual(
            invoice.eh_credit_override_reason_author_id, self.env.user,
        )
        self.assertTrue(invoice.eh_credit_override_reason_at)
        log = invoice.eh_credit_override_log_id
        self.assertEqual(log.partner_id, self.partner_a)
        self.assertAlmostEqual(log.exposure_at_override, 800.0)
        self.assertAlmostEqual(log.limit_at_override, 1000.0)
        self.assertAlmostEqual(log.move_amount, 500.0)
        self.assertAlmostEqual(log.excess, 300.0)
        self.assertEqual(log.reason, 'Long-term customer; deal pending')
        self.assertEqual(log.reason_author_id, self.env.user)
        self.assertEqual(
            log.reason_authored_at, invoice.eh_credit_override_reason_at,
        )

    def test_partner_and_override_reason_can_be_saved_atomically(self):
        invoice = self._customer_invoice(
            self.partner_a, 100.0, post=False,
        )
        invoice.write({
            'partner_id': self.partner_b.id,
            'eh_credit_override_reason': 'Approved with customer change',
        })
        self.assertEqual(invoice.partner_id, self.partner_b)
        self.assertEqual(
            invoice.eh_credit_override_reason_author_id, self.env.user,
        )
        self.assertTrue(invoice.eh_credit_override_reason_at)
        self.assertEqual(
            invoice.eh_credit_override_basis,
            invoice._eh_credit_override_basis_digest(),
        )

    def test_override_reason_is_bound_to_reviewed_invoice_basis(self):
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self._customer_invoice(
            self.partner_a, 300.0, post=False,
        )
        invoice.eh_credit_override_reason = 'Approved at three hundred'
        approved_basis = invoice.eh_credit_override_basis
        self.assertTrue(approved_basis)

        invoice.invoice_line_ids.price_unit = 500.0
        invoice.invalidate_recordset()
        self.assertNotEqual(
            invoice._eh_credit_override_basis_digest(), approved_basis,
        )
        with self.assertRaisesRegex(UserError, 'changed after'):
            invoice.action_post()
        self.assertEqual(invoice.state, 'draft')
        self.assertFalse(invoice.eh_credit_override_log_id)

        # Re-saving even the same text is a fresh positive act and binds the
        # authorisation to the now-current amount/lines.
        invoice.write({
            'eh_credit_override_reason': 'Approved at three hundred',
        })
        self.assertNotEqual(invoice.eh_credit_override_basis, approved_basis)
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')
        self.assertAlmostEqual(
            invoice.eh_credit_override_log_id.move_amount, 500.0,
        )

    def test_reset_to_draft_consumes_override_but_preserves_log(self):
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self._customer_invoice(
            self.partner_a, 500.0, post=False,
        )
        invoice.eh_credit_override_reason = 'First posting approval'
        invoice.action_post()
        first_log = invoice.eh_credit_override_log_id
        self.assertTrue(first_log)

        invoice.button_draft()
        self.assertEqual(invoice.state, 'draft')
        self.assertFalse(invoice.eh_credit_override_reason)
        self.assertFalse(invoice.eh_credit_override_reason_author_id)
        self.assertFalse(invoice.eh_credit_override_reason_at)
        self.assertFalse(invoice.eh_credit_override_basis)
        self.assertFalse(invoice.eh_credit_override_log_id)
        self.assertTrue(first_log.exists())
        self.assertEqual(first_log.move_id, invoice)

        with self.assertRaisesRegex(UserError, 'manager must record'):
            invoice.action_post()
        invoice.eh_credit_override_reason = 'Second posting approval'
        invoice.action_post()
        self.assertNotEqual(invoice.eh_credit_override_log_id, first_log)
        self.assertTrue(first_log.exists())

    def test_late_post_failure_rolls_back_override_evidence(self):
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self._customer_invoice(
            self.partner_a, 500.0, post=False,
        )
        invoice.eh_credit_override_reason = 'Approved before late failure'
        log_count = self.LogModel.search_count([])
        Move = type(self.env['account.move'])

        def fail_after_gate(move_self, soft=True):
            raise UserError("forced downstream posting failure")

        self.patch(Move, '_eh_post_after_credit_gate', fail_after_gate)
        with self.assertRaisesRegex(UserError, 'forced downstream'):
            invoice.action_post()
        invoice.invalidate_recordset()
        self.assertEqual(invoice.state, 'draft')
        self.assertFalse(invoice.eh_credit_override_log_id)
        self.assertEqual(self.LogModel.search_count([]), log_count)
        self.assertFalse(any(
            'Credit override recorded' in (message.body or '')
            for message in invoice.message_ids
        ))

    def test_multi_move_gate_failure_rolls_back_earlier_override_log(self):
        self._customer_invoice(self.partner_a, 800.0)
        approved = self._customer_invoice(
            self.partner_a, 300.0, post=False,
        )
        approved.eh_credit_override_reason = 'Approved batch member'
        blocked = self._customer_invoice(
            self.partner_a, 300.0, post=False,
        )
        log_count = self.LogModel.search_count([])
        with self.assertRaisesRegex(UserError, 'Credit limit exceeded'):
            (approved | blocked).action_post()
        (approved | blocked).invalidate_recordset()
        self.assertEqual(approved.state, 'draft')
        self.assertEqual(blocked.state, 'draft')
        self.assertFalse(approved.eh_credit_override_log_id)
        self.assertEqual(self.LogModel.search_count([]), log_count)

    def test_override_blocked_when_user_not_in_override_group(self):
        self._customer_invoice(self.partner_a, 800.0)
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(self.plain_user).create({
                'move_type': 'out_invoice',
                'partner_id': self.partner_a.id,
                'invoice_date': '2026-04-15',
                'eh_credit_override_reason': 'Trying to bypass',
                'invoice_line_ids': [(0, 0, {
                    'name': 'Big sale',
                    'quantity': 1,
                    'price_unit': 500.0,
                    'account_id': self.account_revenue.id,
                })],
            })

    def test_cleared_override_group_still_blocks_non_manager(self):
        """Explicitly clearing the policy override group must not open a
        bypass: the override falls back to the manager group, so a
        non-manager who types a reason is still blocked. Previously an
        empty override group let any invoice writer override."""
        self._customer_invoice(self.partner_a, 800.0)
        self.policy.override_group_id = False
        with self.assertRaises(AccessError):
            self.env['account.move'].with_user(self.plain_user).create({
                'move_type': 'out_invoice',
                'partner_id': self.partner_a.id,
                'invoice_date': '2026-04-15',
                'eh_credit_override_reason': (
                    'Bypass attempt, no group configured'
                ),
                'invoice_line_ids': [(0, 0, {
                    'name': 'Big sale', 'quantity': 1, 'price_unit': 500.0,
                    'account_id': self.account_revenue.id,
                })],
            })

    def test_cleared_override_group_allows_manager_via_fallback(self):
        """With the policy override group cleared, a manager can still
        override through the manager-group fallback."""
        self._customer_invoice(self.partner_a, 800.0)
        self.policy.override_group_id = False
        # env.user remains a manager (granted in setUpClass).
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'eh_credit_override_reason': 'Manager override, no group set',
            'invoice_line_ids': [(0, 0, {
                'name': 'Big sale', 'quantity': 1, 'price_unit': 500.0,
                'account_id': self.account_revenue.id,
            })],
        })
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')
        self.assertTrue(invoice.eh_credit_override_log_id)

    # ---- audit log immutability ----

    def test_override_log_is_append_only(self):
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'eh_credit_override_reason': 'Bypass',
            'invoice_line_ids': [(0, 0, {
                'name': 'X', 'quantity': 1, 'price_unit': 500.0,
                'account_id': self.account_revenue.id,
            })],
        })
        invoice.action_post()
        log = invoice.eh_credit_override_log_id
        self.assertTrue(log)
        with self.assertRaises(UserError):
            log.write({'reason': 'changed'})
        with self.assertRaises(UserError):
            log.unlink()
        # Comment is the one writable field.
        log.write({'comment': 'discussion notes'})
        self.assertEqual(log.comment, 'discussion notes')
        self.assertEqual(log.user_id, self.env.user)
        self.assertEqual(log.reason_author_id, self.env.user)
        with self.assertRaises(AccessError):
            self.LogModel.with_user(self.plain_user).create({
                'move_id': invoice.id,
                'partner_id': self.partner_a.id,
                'company_id': self.company.id,
                'exposure_at_override': 0.0,
                'limit_at_override': 999999.0,
                'move_amount': invoice.amount_total,
                'reason': 'forged evidence',
            })
        with self.assertRaises(AccessError):
            invoice.with_user(self.plain_user).write({
                'eh_credit_override_log_id': False,
            })
        with self.assertRaises(AccessError):
            invoice.with_user(self.plain_user).write({
                'eh_credit_override_reason': 'rewritten',
            })

    def test_retained_override_evidence_blocks_move_and_partner_delete(self):
        self._customer_invoice(self.partner_a, 800.0)
        invoice = self._customer_invoice(
            self.partner_a, 500.0, post=False,
        )
        invoice.eh_credit_override_reason = 'Retain deletion evidence'
        invoice.action_post()
        invoice.button_draft()
        with self.assertRaisesRegex(UserError, 'retained credit-override'):
            invoice.unlink()
        with self.assertRaisesRegex(
                ValidationError, 'retained credit-override'):
            self.partner_a.unlink()

        child = self.env['res.partner'].create({
            'name': 'Deletable child without own evidence',
            'parent_id': self.partner_a.id,
        })
        child.unlink()
        self.assertFalse(child.exists())

    # ---- refund reduces exposure ----

    def test_refund_reduces_exposure(self):
        self._customer_invoice(self.partner_a, 800.0)
        # Refund 300; exposure should become 500 once refund posts.
        # We cannot use _customer_invoice helper because it has only
        # out_invoice; build manually.
        refund = self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-16',
            'invoice_line_ids': [(0, 0, {
                'name': 'Refund', 'quantity': 1, 'price_unit': 300.0,
                'account_id': self.account_revenue.id,
            })],
        })
        refund.action_post()
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 500.0)
        at_limit = self._customer_invoice(self.partner_a, 500.0)
        self.assertEqual(at_limit.state, 'posted')
        with self.assertRaisesRegex(UserError, 'Credit limit exceeded'):
            self._customer_invoice(self.partner_a, 0.01)

    def test_cash_settlement_removes_invoice_from_exposure_and_gate(self):
        invoice = self._customer_invoice(self.partner_a, 900.0)
        bank_journal = self._ensure_journal(
            self.env, self.company, 'bank', 'CRPAY',
            'Credit settlement bank', default_account=self.account_cash,
        )
        method_line = bank_journal.inbound_payment_method_line_ids[:1]
        if (
            method_line
            and 'payment_account_id' in method_line._fields
            and not method_line.payment_account_id
        ):
            method_line.payment_account_id = self.account_cash
        self.env['account.payment.register'].with_context(
            active_model='account.move',
            active_ids=invoice.ids,
        ).create({'journal_id': bank_journal.id})._create_payments()
        invoice.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertTrue(
            invoice.currency_id.is_zero(invoice.amount_residual),
        )
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        self.assertTrue(
            self.company.currency_id.is_zero(
                self.partner_a.eh_credit_exposure,
            ),
        )
        replacement = self._customer_invoice(self.partner_a, 1000.0)
        self.assertEqual(replacement.state, 'posted')

    # ---- per-partner override ----

    def test_per_partner_limit_override(self):
        self.partner_a.eh_credit_limit = 500.0
        # 500 is the per-partner limit, ignoring the 1000 default.
        self._customer_invoice(self.partner_a, 400.0)
        with self.assertRaises(UserError):
            self._customer_invoice(self.partner_a, 200.0)

    def test_per_partner_policy_override(self):
        strict_policy = self.Policy.create({
            'name': 'Strict policy',
            'company_id': self.env.company.id,
            'is_company_default': False,
            'default_credit_limit': 200.0,
            'enforcement_mode': 'block',
        })
        self.partner_b.eh_credit_policy_id = strict_policy.id
        # partner_b uses the 200 limit, not the 1000 default.
        self._customer_invoice(self.partner_b, 150.0)
        with self.assertRaises(UserError):
            self._customer_invoice(self.partner_b, 100.0)

    def test_archived_partner_policy_falls_back_to_active_default(self):
        strict_policy = self.Policy.create({
            'name': 'Archive-safe strict policy',
            'company_id': self.company.id,
            'default_credit_limit': 200.0,
            'enforcement_mode': 'block',
        })
        self.partner_b.eh_credit_policy_id = strict_policy.id
        self.assertEqual(
            self.partner_b._eh_resolve_credit_policy(), strict_policy,
        )
        strict_policy.active = False
        self.partner_b.invalidate_recordset([
            'eh_credit_policy_id', 'eh_effective_credit_limit',
        ])
        self.assertEqual(
            self.partner_b._eh_resolve_credit_policy(), self.policy,
        )
        self.assertEqual(
            self.partner_b.eh_effective_credit_limit,
            self.policy.default_credit_limit,
        )

    # ---- zero limit means no enforcement ----

    def test_zero_limit_disables_gate(self):
        self.policy.default_credit_limit = 0.0
        # Even a huge invoice posts because zero means disabled.
        invoice = self._customer_invoice(self.partner_a, 99999.0)
        self.assertEqual(invoice.state, 'posted')

    def test_missing_policy_skips_move_row_lock_and_double_refresh(self):
        self.policy.active = False
        Move = type(self.env['account.move'])
        locked = []
        original = Move._eh_lock_moves_for_credit_gate

        def spy(move_self):
            locked.extend(move_self.ids)
            return original(move_self)

        self.patch(Move, '_eh_lock_moves_for_credit_gate', spy)
        invoice = self._customer_invoice(self.partner_a, 99999.0)
        self.assertEqual(invoice.state, 'posted')
        self.assertFalse(locked)

    def test_breach_message_uses_company_currency_formatting(self):
        self.partner_a.eh_credit_limit = 100.0
        with self.assertRaises(UserError) as raised:
            self._customer_invoice(self.partner_a, 100.01)
        message = str(raised.exception)
        self.assertIn(self.company.currency_id.symbol, message)
        self.assertNotIn('100.010000', message)

    # ---- multi-currency exposure ----

    def test_exposure_currency_is_company_currency(self):
        self.assertEqual(
            self.partner_a.eh_credit_exposure_currency_id,
            self.env.company.currency_id,
        )

    def test_draft_exposure_uses_company_currency_signed(self):
        """A draft refund should reduce, not inflate, exposure."""
        self.policy.include_drafts = True
        self._customer_invoice(self.partner_a, 200.0, post=False)
        # Draft refund of 50 in same currency.
        self.env['account.move'].create({
            'move_type': 'out_refund',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'X', 'quantity': 1, 'price_unit': 50.0,
                'account_id': self.account_revenue.id,
            })],
        })
        self.partner_a.invalidate_recordset(['eh_credit_exposure'])
        # 200 invoice draft (signed +200) plus 50 refund draft
        # (signed -50) gives 150 exposure when drafts are counted.
        self.assertAlmostEqual(self.partner_a.eh_credit_exposure, 150.0)

    def test_foreign_so_enforced_at_converted_value(self):
        """A foreign-currency open sale order must be enforced against
        the limit at its company-currency-converted value, not its raw
        order-currency figure.

        The post-time gate (account_move._eh_partner_exposure_excluding_self)
        must convert the open-SO amount exactly as the partner-form
        compute does. Rate: 1 EUR = 2 USD (company currency is USD).
        A 600 EUR open order converts to 1200 USD. Raw (unconverted)
        exposure of 600 would stay under the 1000 limit and wrongly
        allow the post; the converted 1200 exceeds it and must block.
        """
        if 'sale.order' not in self.env:
            self.skipTest("sale module not installed in this run")

        company = self.env.company
        eur = self.env.ref('base.EUR')
        eur.active = True
        # Company currency is USD (base=1); an EUR rate of 0.5 means
        # 1 USD = 0.5 EUR, i.e. 1 EUR = 2 USD.
        self.env['res.currency.rate'].create({
            'name': '2026-01-01',
            'currency_id': eur.id,
            'rate': 0.5,
            'company_id': company.id,
        })
        # sale.order.currency_id is driven by the pricelist, so force the
        # order into EUR with an EUR pricelist rather than writing
        # currency_id directly (which does not stick across versions).
        pricelist = self.env['product.pricelist'].create({
            'name': 'EH Credit EUR',
            'currency_id': eur.id,
            'company_id': company.id,
        })
        self.policy.include_sale_orders = True

        order_line = {
            'name': 'FX line',
            'product_id': self._eh_credit_test_product().id,
            'product_uom_qty': 1,
            'price_unit': 600.0,
        }
        # sale.order.line renamed tax_id -> tax_ids in Odoo 17; clear the tax
        # (so the order is exactly 600 EUR = 1200 USD) on whichever exists.
        tax_field = 'tax_ids' \
            if 'tax_ids' in self.env['sale.order.line']._fields else 'tax_id'
        order_line[tax_field] = [(6, 0, [])]
        order = self.env['sale.order'].create({
            'partner_id': self.partner_a.id,
            'pricelist_id': pricelist.id,
            'date_order': '2026-04-15',
            'order_line': [(0, 0, order_line)],
        })
        order.action_confirm()

        # The order must genuinely be in EUR for the conversion to bite.
        self.assertEqual(order.currency_id, eur)
        self.assertIn(order.state, ('sale', 'done'))

        # 600 EUR converts to 1200 USD, already over the 1000 limit, so
        # even a small 100 USD invoice must block. If the gate used the
        # raw 600 figure, 600 + 100 = 700 would post cleanly. The block
        # therefore fires ONLY because the open SO was converted.
        with self.assertRaises(UserError) as cm:
            self._customer_invoice(self.partner_a, 100.0)
        self.assertIn('Credit limit exceeded', str(cm.exception))

    def test_foreign_invoice_gate_uses_converted_amount(self):
        """The move under post must contribute its COMPANY-currency value.

        The gated invoice is denominated in a foreign currency. Its
        amount_total lives in that currency, but the exposure and the
        limit are in company currency. _eh_credit_relevant_amount must
        return the converted (company-currency) figure so the gate
        compares like-for-like.

        Rate: 1 EUR = 2 USD (company currency USD). A 600 EUR invoice is
        1200 USD, already over the 1000 limit, so the post must block.
        If the gate used the raw 600 amount_total, 600 < 1000 would post
        cleanly; the block therefore fires ONLY because the move was
        converted to company currency.
        """
        company = self.env.company
        eur = self.env.ref('base.EUR')
        eur.active = True
        # Company currency is USD (base=1); an EUR rate of 0.5 means
        # 1 USD = 0.5 EUR, i.e. 1 EUR = 2 USD.
        self.env['res.currency.rate'].create({
            'name': '2026-01-01',
            'currency_id': eur.id,
            'rate': 0.5,
            'company_id': company.id,
        })
        self.assertNotEqual(
            eur, company.currency_id,
            "test presumes company currency is not EUR",
        )

        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'currency_id': eur.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'FX sale',
                'quantity': 1,
                'price_unit': 600.0,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, [])],
            })],
        })
        # The move must genuinely be in EUR for the conversion to bite.
        self.assertEqual(move.currency_id, eur)
        self.assertAlmostEqual(move.amount_total, 600.0)
        # amount_total_signed is company currency: 600 EUR -> 1200 USD.
        self.assertAlmostEqual(move.amount_total_signed, 1200.0)

        # The move's contribution to the gate must be the 1200 USD
        # converted figure, not the 600 EUR raw amount.
        self.assertAlmostEqual(move._eh_credit_relevant_amount(), 1200.0)

        # 1200 USD alone exceeds the 1000 USD limit, so the post blocks.
        # With the pre-fix raw-amount behaviour (600), it would post.
        with self.assertRaises(UserError) as cm:
            move.action_post()
        self.assertIn('Credit limit exceeded', str(cm.exception))
        self.assertNotEqual(move.state, 'posted')

    # ---- concurrency: block-mode gate serialises on the partner row ----

    def test_block_gate_locks_partner_before_reading_exposure(self):
        """The block-mode gate must take a FOR UPDATE row lock on the
        commercial partner before reading exposure, so two concurrent
        posts for the same customer serialise instead of both passing a
        stale check-then-act and both breaching the limit.

        A live race is not deterministically unit-testable; instead we
        assert the lock helper is invoked on the sequential post path
        (with the resolved commercial partner) whenever a block-mode
        gate runs.
        """
        Move = type(self.env['account.move'])
        original = Move._eh_lock_partner_for_gate
        locked = []

        def spy(move_self, partner):
            locked.append(partner.id)
            return original(move_self, partner)

        self.patch(Move, '_eh_lock_partner_for_gate', spy)
        # 200 against a 1000 block-mode default: gate runs, lock taken.
        self._customer_invoice(self.partner_a, 200.0)
        self.assertIn(
            self.partner_a.commercial_partner_id.id, locked,
            "block-mode gate did not lock the commercial partner row",
        )

    def test_warn_gate_does_not_lock_partner(self):
        """Warn-only enforcement is advisory, so it must not take the
        block-mode partner lock (there is no serialisation guarantee to
        uphold when posting is never refused)."""
        self.policy.enforcement_mode = 'warn'
        Move = type(self.env['account.move'])
        original = Move._eh_lock_partner_for_gate
        locked = []

        def spy(move_self, partner):
            locked.append(partner.id)
            return original(move_self, partner)

        self.patch(Move, '_eh_lock_partner_for_gate', spy)
        self._customer_invoice(self.partner_a, 200.0)
        self.assertFalse(
            locked, "warn-mode gate should not take the partner row lock",
        )

    # ---- single-default invariant survives unarchive ----

    def test_unarchive_stale_default_is_revalidated(self):
        """Restoring an archived default must re-run the single-default
        check. Archive A, create B as the new active default (allowed
        because archived A is excluded from the active_test search), then
        unarchiving A must raise rather than leave two active defaults
        that find_for_company resolves non-deterministically.
        """
        company = self.env.company
        # Neutralise the setUpClass default so this company starts with a
        # single, controllable default for the scenario.
        self.policy.is_company_default = False

        policy_a = self.Policy.create({
            'name': 'Stale default A',
            'company_id': company.id,
            'is_company_default': True,
            'default_credit_limit': 5000.0,
        })
        # Archive A: it keeps is_company_default in the row but drops out
        # of the active_test=True search, so a second default is allowed.
        policy_a.active = False
        policy_b = self.Policy.create({
            'name': 'New default B',
            'company_id': company.id,
            'is_company_default': True,
            'default_credit_limit': 500000.0,
        })
        # Unarchiving A (the standard Unarchive action writes only
        # {'active': True}) must re-validate and refuse, because B holds
        # the active default slot. Wrap in a savepoint so the rejected
        # write rolls back cleanly.
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                policy_a.active = True
                policy_a.flush_recordset()
        # The resolver still deterministically returns the single active
        # default, never the stale one.
        self.assertEqual(self.Policy.find_for_company(company), policy_b)

    def test_archive_default_does_not_raise(self):
        """Archiving a company default must not trip the single-default
        constraint: an archived default is not competing for the slot."""
        company = self.env.company
        self.policy.is_company_default = False
        policy_a = self.Policy.create({
            'name': 'Archivable default',
            'company_id': company.id,
            'is_company_default': True,
            'default_credit_limit': 1000.0,
        })
        policy_a.active = False
        policy_a.flush_recordset()
        self.assertFalse(policy_a.active)

    def _eh_credit_test_product(self):
        """A stored product for sale-order lines in the FX test."""
        Product = self.env['product.product']
        product = Product.search([('name', '=', 'EH Credit FX Product')], limit=1)
        if not product:
            product = Product.create({
                'name': 'EH Credit FX Product',
                'type': 'service',
                'invoice_policy': 'order',
                'list_price': 600.0,
            })
        return product
