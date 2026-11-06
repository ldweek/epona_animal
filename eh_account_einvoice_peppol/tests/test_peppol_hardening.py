# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression tests for the Peppol hardening pass.

Covers two of the confirmed findings:

* The access-point credential field (eh_peppol_ap_config) is restricted
  to system administrators, so a plain internal user cannot RPC-read the
  live AP key / signing-cert path, while the transmission path still
  resolves it via sudo.
* Inbound duplicate detection keys on the business identity (supplier +
  invoice number), so a re-serialised redelivery is caught even when the
  raw XML bytes differ, and a posted vendor bill for the same supplier +
  reference blocks a second payable.
"""

import base64
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged
from odoo.tools import file_open

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_einvoice_peppol', 'post_install', '-at_install')
class TestPeppolCredentialGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.sudo().eh_peppol_ap_config = (
            '{"api_key": "secret-key", "base_url": "https://ap.example"}'
        )
        # A plain internal user (no base.group_system) to attempt the read.
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'Peppol Field Reader',
                'login': 'eh_peppol_field_reader',
                'company_id': cls.env.company.id,
                'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
            })
        except Exception:  # noqa: BLE001
            cls.plain_user = cls.env['res.users'].browse()

    def test_plain_user_cannot_read_credentials(self):
        """The RPC read a non-admin would use is refused by the field guard."""
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        company_as_plain = self.env.company.with_user(self.plain_user)
        with self.assertRaises(AccessError):
            company_as_plain.read(['eh_peppol_ap_config'])
        with self.assertRaises(AccessError):
            # Attribute access on the single restricted field also raises.
            company_as_plain.eh_peppol_ap_config  # noqa: B018

    def test_admin_can_read_credentials(self):
        """The field stays readable for administrators."""
        self.assertIn('secret-key', self.env.company.eh_peppol_ap_config or '')

    def test_integration_resolves_credentials_via_sudo(self):
        """The transmission path resolves the secret via sudo even when a
        non-admin operator triggers it, so restricting the field does not
        break the send/poll capability.
        """
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        self.env.company.eh_peppol_inbox_email = 'inbox@example.test'
        company_as_plain = self.env.company.with_user(self.plain_user)
        config = self.env['eh.peppol.inbound'].with_user(
            self.plain_user,
        )._eh_company_config(company_as_plain)
        self.assertEqual(config['credentials'].get('api_key'), 'secret-key')
        self.assertEqual(config['inbox_email'], 'inbox@example.test')
        self.assertIn(
            'manual', dict(self.env.company._eh_peppol_available_adapters()),
        )


@tagged('eh_account_einvoice_peppol', 'post_install', '-at_install')
class TestPeppolInboundDuplicate(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendor = cls.env['res.partner'].create({
            'name': 'Peppol Vendor Co', 'is_company': True,
        })
        cls.Inbound = cls.env['eh.peppol.inbound']

    def test_business_key_flags_reserialised_redelivery(self):
        """Same supplier + invoice number with DIFFERENT bytes is flagged as a
        duplicate, which a raw-hash-only check would miss.
        """
        first = self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-777',
            'document_type': 'invoice',
            'file_hash': 'a' * 64,
        })
        second = self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-777',
            'document_type': 'invoice',
            'file_hash': 'b' * 64,  # re-serialised: different SHA-256
        })
        second._eh_check_duplicate()
        self.assertEqual(second.duplicate_inbound_id, first)

    def test_distinct_invoice_number_not_flagged(self):
        """A genuinely different invoice from the same supplier is not a
        duplicate (no false positive).
        """
        self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-900',
            'document_type': 'invoice',
            'file_hash': 'c' * 64,
        })
        other = self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-901',
            'document_type': 'invoice',
            'file_hash': 'd' * 64,
        })
        other._eh_check_duplicate()
        self.assertFalse(other.duplicate_inbound_id)
        self.assertFalse(other.duplicate_move_id)

    def test_posted_bill_flags_cross_channel_duplicate(self):
        """An already-posted vendor bill for the same supplier + reference
        flags the inbound and blocks posting a second payable.
        """
        bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.vendor.id,
            'ref': 'INV-888',
            'invoice_date': '2026-05-01',
            'journal_id': self.journal_purchase.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line',
                'quantity': 1.0,
                'price_unit': 100.0,
                'account_id': self.account_expense.id,
            })],
        })
        bill.action_post()
        self.assertTrue(bill.eh_peppol_supplier_reference_claim)
        self.assertIn('INV-888', bill.eh_peppol_supplier_reference_claim)
        inbound = self.Inbound.create({
            'company_id': self.env.company.id,
            'partner_id': self.vendor.id,
            'invoice_number': 'INV-888',
            'issue_date': '2026-05-01',
            'document_type': 'invoice',
        })
        inbound._eh_check_duplicate()
        self.assertEqual(inbound.duplicate_move_id, bill)

    def test_same_supplier_reference_on_different_invoice_date_is_allowed(self):
        def bill(invoice_date):
            move = self.env['account.move'].create({
                'move_type': 'in_invoice',
                'partner_id': self.vendor.id,
                'ref': 'RECURRING-REFERENCE',
                'invoice_date': invoice_date,
                'journal_id': self.journal_purchase.id,
                'invoice_line_ids': [(0, 0, {
                    'name': 'Line', 'quantity': 1.0, 'price_unit': 10.0,
                    'account_id': self.account_expense.id,
                })],
            })
            move.action_post()
            return move

        first = bill('2026-05-01')
        second = bill('2026-06-01')

        self.assertNotEqual(
            first.eh_peppol_supplier_reference_claim,
            second.eh_peppol_supplier_reference_claim,
        )

    def test_supplier_claim_is_stamped_only_after_core_posts(self):
        move = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.vendor.id,
            'ref': 'POST-ORDER-1',
            'invoice_date': '2026-05-01',
            'journal_id': self.journal_purchase.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Line', 'quantity': 1.0, 'price_unit': 10.0,
                'account_id': self.account_expense.id,
            })],
        })
        owners = [
            owner for owner in type(move).__mro__
            if 'action_post' in owner.__dict__
        ]
        peppol_index = next(
            index for index, owner in enumerate(owners)
            if 'eh_account_einvoice_peppol' in owner.__module__
        )
        downstream = owners[peppol_index + 1]

        with patch.object(
                downstream, 'action_post', autospec=True,
                return_value=True):
            move.action_post()

        self.assertEqual(move.state, 'draft')
        self.assertFalse(move.eh_peppol_supplier_reference_claim)

    def test_posted_bill_reference_edit_cannot_create_duplicate(self):
        def posted_bill(reference):
            bill = self.env['account.move'].create({
                'move_type': 'in_invoice',
                'partner_id': self.vendor.id,
                'ref': reference,
                'invoice_date': '2026-05-01',
                'journal_id': self.journal_purchase.id,
                'invoice_line_ids': [(0, 0, {
                    'name': 'Line',
                    'quantity': 1.0,
                    'price_unit': 25.0,
                    'account_id': self.account_expense.id,
                })],
            })
            bill.action_post()
            return bill

        first = posted_bill('EDIT-REF-1')
        second = posted_bill('EDIT-REF-2')
        with self.assertRaisesRegex(UserError, 'duplicate posted bill'):
            second.write({'ref': '  edit-ref-1  '})
        self.assertEqual(first.ref, 'EDIT-REF-1')
        self.assertEqual(second.ref, 'EDIT-REF-2')

    def test_raw_state_write_cannot_bypass_duplicate_claim(self):
        def draft(reference):
            return self.env['account.move'].create({
                'move_type': 'in_invoice',
                'partner_id': self.vendor.id,
                'ref': reference,
                'invoice_date': '2026-05-01',
                'journal_id': self.journal_purchase.id,
                'invoice_line_ids': [(0, 0, {
                    'name': 'Line',
                    'quantity': 1.0,
                    'price_unit': 25.0,
                    'account_id': self.account_expense.id,
                })],
            })

        first = draft('RAW-POST-1')
        second = draft(' raw-post-1 ')
        first.action_post()
        with self.assertRaisesRegex(UserError, 'duplicate posted bill'):
            second.write({'state': 'posted'})
        self.assertEqual(second.state, 'draft')

    def test_move_type_write_cannot_enter_duplicate_vendor_identity(self):
        vendor_bill = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.vendor.id,
            'ref': 'TYPE-SWITCH-1',
            'invoice_date': '2026-05-01',
            'journal_id': self.journal_purchase.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Vendor line',
                'quantity': 1.0,
                'price_unit': 25.0,
                'account_id': self.account_expense.id,
            })],
        })
        vendor_bill.action_post()
        customer_invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.vendor.id,
            'ref': 'TYPE-SWITCH-1',
            'invoice_date': '2026-05-01',
            'journal_id': self.journal_sale.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Customer line',
                'quantity': 1.0,
                'price_unit': 25.0,
                'account_id': self.account_revenue.id,
            })],
        })
        customer_invoice.action_post()
        with self.assertRaisesRegex(UserError, 'duplicate posted bill'):
            customer_invoice.write({'move_type': 'in_invoice'})

    def test_transmission_audit_fields_rejected_on_create(self):
        with self.assertRaises(AccessError):
            self.env['account.move'].sudo().create({
                'move_type': 'entry',
                'eh_peppol_transmission_status': 'sent',
            })

    def test_supplier_reference_claim_is_server_owned(self):
        move = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.vendor.id,
            'ref': 'CLAIM-GUARD-1',
        })
        with self.assertRaises(AccessError):
            move.write({
                'eh_peppol_supplier_reference_claim': 'forged',
            })
        with self.assertRaises(AccessError):
            self.env['account.move'].create({
                'move_type': 'entry',
                'eh_peppol_supplier_reference_claim': 'forged',
            })


@tagged('eh_account_einvoice_peppol', 'post_install', '-at_install')
class TestPeppolInboundPayloadIntegrity(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Inbound = cls.env['eh.peppol.inbound']
        cls.vendor = cls.env['res.partner'].create({
            'name': 'Heritage Books Pty Ltd',
            'is_company': True,
            'eh_peppol_endpoint_id': '83914571673',
            'eh_peppol_endpoint_scheme': '0151',
        })
        cls.company.partner_id.write({
            'eh_peppol_endpoint_id': '53004085616',
            'eh_peppol_endpoint_scheme': '0151',
        })
        cls.eh_user = cls.env['res.users'].create({
            'name': 'Peppol Intake Operator',
            'login': 'eh_peppol_intake_operator',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.manager = cls.env['res.users'].create({
            'name': 'Peppol Intake Manager',
            'login': 'eh_peppol_intake_manager',
            'email': 'eh_peppol_intake_manager@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })
        cls.outsider = cls.env['res.users'].create({
            'name': 'Peppol Outsider',
            'login': 'eh_peppol_outsider',
            'email': 'eh_peppol_outsider@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, [cls.company.id])],
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id])],
        })
        cls.sentinel_move = cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': cls.journal_misc.id,
        })
        Tax = cls.env['account.tax']
        tax_domain = [
            ('type_tax_use', '=', 'purchase'),
            ('amount_type', '=', 'percent'),
            ('amount', '=', 10.0),
            ('company_id', '=', cls.company.id),
        ]
        tax_country = False
        if 'country_id' in Tax._fields:
            tax_country = getattr(
                cls.company, 'account_fiscal_country_id', False,
            )
            if not tax_country:
                tax_country = (
                    cls.company.country_id or cls.env.ref('base.us')
                )
                cls.company.account_fiscal_country_id = tax_country
        # Localisations seed different numbers of ordinary 10% purchase
        # taxes across supported Odoo series.  Inbound posting deliberately
        # fails closed when more than one mapping matches, so give this test
        # class one explicit category-S mapping instead of depending on the
        # ambient chart template.
        cls._ambient_purchase_taxes = Tax.search(
            tax_domain + [('active', '=', True)],
        ).filtered(
            lambda tax: not tax.price_include
            and tax.eh_edi_resolve_category() == 'S'
        )
        cls._ambient_purchase_taxes.write({'active': False})
        tax_vals = {
            'name': 'Peppol input tax 10%',
            'type_tax_use': 'purchase',
            'amount_type': 'percent',
            'amount': 10.0,
            'company_id': cls.company.id,
            'price_include_override': 'tax_excluded',
            'eh_edi_tax_category': 'S',
        }
        if tax_country:
            tax_vals['country_id'] = tax_country.id
        cls.purchase_tax = Tax.create(tax_vals)
        with file_open(
                'eh_account_einvoice_peppol/tests/golden/ubl_invoice.xml',
                'rb') as source:
            # Keep this fixture in the seeded company's currency so posting
            # validation tests do not depend on an unrelated exchange rate.
            cls.valid_xml = source.read().replace(
                b'AUD', cls.company.currency_id.name.encode('ascii'),
            )

    @classmethod
    def tearDownClass(cls):
        try:
            # A full-suite registry reuses chart data after this class. Make
            # fixture isolation explicit instead of relying only on test-case
            # savepoint/cache cleanup.
            cls._ambient_purchase_taxes.sudo().write({'active': True})
        finally:
            super().tearDownClass()

    def _make_received(self, xml=None):
        attachment = self.env['ir.attachment'].with_user(
            self.eh_user,
        ).create({
            'name': 'reviewed-inbound.xml',
            'type': 'binary',
            'raw': xml if xml is not None else self.valid_xml,
            'mimetype': 'application/xml',
        })
        inbound = self.Inbound.with_user(self.eh_user).create({
            'company_id': self.company.id,
            'access_point_key': 'manual',
            'xml_attachment_id': attachment.id,
        })
        return inbound, attachment

    def _make_matched(self, xml=None):
        inbound, attachment = self._make_received(xml=xml)
        inbound.action_parse()
        inbound.action_match()
        self.assertEqual(inbound.state, 'matched')
        self.assertEqual(inbound.partner_id, self.vendor)
        self.assertTrue(inbound.file_hash)
        return inbound, attachment

    def _credit_note_xml(self):
        return self.valid_xml.replace(
            b'urn:oasis:names:specification:ubl:schema:xsd:Invoice-2',
            b'urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2',
        ).replace(b'<Invoice ', b'<CreditNote ', 1).replace(
            b'</Invoice>', b'</CreditNote>', 1,
        ).replace(
            b'<cbc:InvoiceTypeCode>380</cbc:InvoiceTypeCode>',
            b'<cbc:CreditNoteTypeCode>381</cbc:CreditNoteTypeCode>',
        ).replace(
            b'<cac:InvoiceLine>', b'<cac:CreditNoteLine>',
        ).replace(
            b'</cac:InvoiceLine>', b'</cac:CreditNoteLine>',
        ).replace(
            b'<cbc:InvoicedQuantity', b'<cbc:CreditedQuantity',
        ).replace(
            b'</cbc:InvoicedQuantity>', b'</cbc:CreditedQuantity>',
        )

    def test_currency_compute_batches_and_refuses_inactive_or_unknown(self):
        self.env['res.currency'].create({
            'name': 'ZQX',
            'symbol': 'ZQX',
            'rounding': 0.01,
            'active': False,
        })
        records = self.Inbound.create([
            {
                'company_id': self.company.id,
                'currency_code': self.company.currency_id.name.lower(),
            },
            {'company_id': self.company.id, 'currency_code': 'ZQX'},
            {'company_id': self.company.id, 'currency_code': 'ZZZ'},
        ])
        CurrencyClass = type(self.env['res.currency'])
        original_search = CurrencyClass.search
        searches = []

        def capture_search(recordset, domain, *args, **kwargs):
            searches.append(domain)
            return original_search(recordset, domain, *args, **kwargs)

        with patch.object(CurrencyClass, 'search', new=capture_search):
            records._compute_currency_id()

        self.assertEqual(len(searches), 1)
        self.assertEqual(records[0].currency_id, self.company.currency_id)
        self.assertFalse(records[1].currency_id)
        self.assertFalse(records[2].currency_id)

    def _purchase_tax_vals(self, name, amount=0.0, **extra):
        vals = {
            'name': name,
            'type_tax_use': 'purchase',
            'amount_type': 'percent',
            'amount': amount,
            'company_id': self.company.id,
            'price_include_override': 'tax_excluded',
        }
        if 'country_id' in self.env['account.tax']._fields:
            vals['country_id'] = (
                self.company.account_fiscal_country_id
                or self.company.country_id
                or self.env.ref('base.us')
            ).id
        vals.update(extra)
        return vals

    def _deactivate_explicit_purchase_category(self, category):
        self.env['account.tax'].search([
            ('type_tax_use', '=', 'purchase'),
            ('company_id', '=', self.company.id),
            ('active', '=', True),
            ('eh_edi_tax_category', '=', category),
        ]).write({'active': False})

    def _zero_tax_xml(self, category):
        return self.valid_xml.replace(
            b'<cbc:TaxAmount currencyID="'
            + self.company.currency_id.name.encode('ascii')
            + b'">100.00</cbc:TaxAmount>',
            b'<cbc:TaxAmount currencyID="'
            + self.company.currency_id.name.encode('ascii')
            + b'">0.00</cbc:TaxAmount>',
        ).replace(
            b'<cbc:ID>S</cbc:ID>',
            b'<cbc:ID>' + category.encode('ascii') + b'</cbc:ID>',
        ).replace(
            b'<cbc:Percent>10.00</cbc:Percent>',
            b'<cbc:Percent>0.00</cbc:Percent>',
        ).replace(
            b'<cbc:TaxInclusiveAmount currencyID="'
            + self.company.currency_id.name.encode('ascii')
            + b'">1100.00</cbc:TaxInclusiveAmount>',
            b'<cbc:TaxInclusiveAmount currencyID="'
            + self.company.currency_id.name.encode('ascii')
            + b'">1000.00</cbc:TaxInclusiveAmount>',
        ).replace(
            b'<cbc:PayableAmount currencyID="'
            + self.company.currency_id.name.encode('ascii')
            + b'">1100.00</cbc:PayableAmount>',
            b'<cbc:PayableAmount currencyID="'
            + self.company.currency_id.name.encode('ascii')
            + b'">1000.00</cbc:PayableAmount>',
        )

    def test_non_su_create_cannot_forge_reviewed_evidence(self):
        attachment = self.env['ir.attachment'].with_user(
            self.eh_user,
        ).create({
            'name': 'unreviewed.xml',
            'type': 'binary',
            'raw': self.valid_xml,
            'mimetype': 'application/xml',
        })
        inbound = self.Inbound.with_user(self.eh_user).create({
            'company_id': self.company.id,
            'xml_attachment_id': attachment.id,
            'state': 'matched',
            'file_hash': 'f' * 64,
            'invoice_number': 'FORGED-1',
            'partner_id': self.vendor.id,
            'duplicate_move_id': self.sentinel_move.id,
        })
        self.assertEqual(inbound.state, 'received')
        self.assertFalse(inbound.file_hash)
        self.assertFalse(inbound.invoice_number)
        self.assertFalse(inbound.partner_id)
        self.assertFalse(inbound.duplicate_move_id)

    def test_manual_upload_widget_creates_marked_source(self):
        wizard = self.env['eh.peppol.inbound.upload'].with_user(
            self.eh_user
        ).create({
            'xml_file': base64.b64encode(self.valid_xml),
            'filename': 'manual-inbound.xml',
            'company_id': self.company.id,
        })

        action = wizard.action_upload()
        inbound = self.Inbound.browse(action['res_id'])

        self.assertEqual(inbound.state, 'received')
        self.assertTrue(inbound.xml_attachment_id.eh_peppol_inbound_source)

    def test_ordinary_attachment_write_skips_peppol_lock_and_search(self):
        attachment = self.env['ir.attachment'].with_user(self.eh_user).create({
            'name': 'ordinary.txt', 'type': 'binary', 'raw': b'ordinary',
            'mimetype': 'text/plain',
        })
        AttachmentClass = type(self.env['ir.attachment'])
        with patch.object(
                AttachmentClass, '_eh_lock_peppol_review_guard',
                autospec=True) as lock_guard, patch.object(
                    AttachmentClass, '_eh_reviewed_peppol_sources',
                    autospec=True) as reviewed_search:
            attachment.with_user(self.eh_user).write({'name': 'renamed.txt'})
        lock_guard.assert_not_called()
        reviewed_search.assert_not_called()

    def test_non_su_cannot_bind_unreadable_attachment_for_sudo_parse(self):
        foreign_attachment = self.env['ir.attachment'].sudo().create({
            'name': 'other-users-private.xml',
            'type': 'binary',
            'raw': self.valid_xml,
            'mimetype': 'application/xml',
        })
        with self.assertRaises(AccessError):
            self.Inbound.with_user(self.eh_user).create({
                'company_id': self.company.id,
                'xml_attachment_id': foreign_attachment.id,
            })

    def test_reviewed_fields_and_source_link_are_rpc_immutable(self):
        inbound, attachment = self._make_matched()
        replacement = self.env['ir.attachment'].with_user(
            self.eh_user,
        ).create({
            'name': 'replacement.xml',
            'type': 'binary',
            'raw': self.valid_xml,
            'mimetype': 'application/xml',
        })
        attempts = (
            {'xml_attachment_id': replacement.id},
            {'invoice_number': 'FORGED-2'},
            {'partner_id': self.env.company.partner_id.id},
            {'duplicate_inbound_id': False},
            {'duplicate_move_id': False},
            {'move_id': self.sentinel_move.id},
        )
        for vals in attempts:
            with self.subTest(vals=vals), self.assertRaises(AccessError):
                inbound.write(vals)
        # Client-controlled context remains data, never authorization.
        with self.assertRaises(AccessError):
            inbound.with_context(eh_workflow_action=True).write({
                'file_hash': '0' * 64,
            })
        with self.assertRaises(AccessError):
            attachment.with_user(self.eh_user).unlink()

    def test_post_refuses_attachment_bytes_changed_after_review(self):
        inbound, attachment = self._make_matched()
        altered_xml = self.valid_xml.replace(
            b'<cbc:PriceAmount currencyID="USD">100.00</cbc:PriceAmount>',
            b'<cbc:PriceAmount currencyID="USD">999.00</cbc:PriceAmount>',
        )
        self.assertNotEqual(altered_xml, self.valid_xml)
        with self.assertRaises(AccessError):
            attachment.with_user(self.eh_user).write({'raw': altered_xml})
        # The posting check remains tamper-evident against privileged or
        # out-of-band storage changes, not only normal ORM writes.
        attachment.sudo().write({'raw': altered_xml})

        with self.assertRaisesRegex(
                UserError, 'source XML changed after it was parsed'):
            inbound.with_user(self.manager).action_post()
        self.assertFalse(inbound.move_id)
        self.assertEqual(inbound.state, 'matched')

    def test_post_rechecks_duplicate_created_after_matching(self):
        inbound, _attachment = self._make_matched()
        self.assertFalse(inbound.duplicate_move_id)
        duplicate = self.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': self.vendor.id,
            'ref': inbound.invoice_number,
            'invoice_date': inbound.issue_date,
            'journal_id': self.journal_purchase.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Already booked',
                'quantity': 1.0,
                'price_unit': 100.0,
                'account_id': self.account_expense.id,
            })],
        })
        duplicate.action_post()

        action = inbound.with_user(self.manager).action_post()
        inbound.invalidate_recordset(['duplicate_move_id', 'move_id'])
        self.assertEqual(action['tag'], 'display_notification')
        self.assertEqual(inbound.state, 'exception')
        self.assertEqual(inbound.duplicate_move_id, duplicate)
        self.assertFalse(inbound.move_id)

    def test_unchanged_reviewed_payload_validates(self):
        inbound, _attachment = self._make_matched()
        payload = inbound.sudo()._eh_validate_reviewed_payload()
        self.assertEqual(payload['invoice_number'], inbound.invoice_number)
        self.assertEqual(
            payload['currency_code'], self.company.currency_id.name,
        )

    def test_workflow_authorizes_before_sudo(self):
        inbound, _attachment = self._make_received()
        with self.assertRaises(AccessError):
            inbound.with_user(self.outsider).action_parse()
        self.assertEqual(inbound.state, 'received')

        inbound.with_user(self.eh_user).action_parse()
        with self.assertRaises(AccessError):
            inbound.with_user(self.outsider).action_match()
        with self.assertRaises(AccessError):
            inbound.with_user(self.eh_user).action_reject()
        self.assertEqual(inbound.state, 'parsed')

    def test_company_record_rule_checked_before_sudo(self):
        other_company = self.env['res.company'].sudo().create({
            'name': 'Other Peppol Company',
        })
        foreign = self.Inbound.sudo().create({
            'company_id': other_company.id,
            'access_point_key': 'manual',
        })
        with self.assertRaises(AccessError):
            foreign.with_user(self.manager).action_parse()
        self.assertEqual(foreign.state, 'received')

    def test_successful_parse_is_one_shot(self):
        inbound, attachment = self._make_received()
        inbound.action_parse()
        with self.assertRaisesRegex(UserError, 'only available once'):
            inbound.action_parse()
        with self.assertRaises(AccessError):
            attachment.with_user(self.eh_user).write({
                'raw': self.valid_xml,
            })
        self.assertEqual(inbound.state, 'parsed')

    def test_reject_requires_and_freezes_reason(self):
        inbound, _attachment = self._make_received()
        inbound.action_parse()
        with self.assertRaisesRegex(UserError, 'reason is required'):
            inbound.with_user(self.manager).action_reject()
        inbound.with_user(self.manager).write({
            'rejection_reason': 'Supplier confirmed duplicate delivery',
        })

        inbound.with_user(self.manager).action_reject()

        self.assertEqual(inbound.state, 'rejected')
        with self.assertRaisesRegex(AccessError, 'immutable'):
            inbound.with_user(self.manager).write({
                'rejection_reason': 'replacement',
            })

    def test_partner_resolution_uses_normalized_exact_identity(self):
        self.vendor.eh_peppol_endpoint_id = '83 914-571.673'
        inbound, _attachment = self._make_received()
        inbound.action_parse()

        inbound.action_match()

        self.assertEqual(inbound.partner_id, self.vendor)
        self.assertEqual(inbound.state, 'matched')

    def test_partner_resolution_rejects_partial_identifier(self):
        self.vendor.eh_peppol_endpoint_id = 'UNRELATED123'
        self.env['res.partner'].create({
            'name': 'Partial Identifier Vendor',
            'is_company': True,
            'eh_peppol_endpoint_id': '914571',
        })
        inbound, _attachment = self._make_received()
        inbound.action_parse()

        inbound.action_match()

        self.assertFalse(inbound.partner_id)
        self.assertEqual(inbound.state, 'exception')

    def test_partner_resolution_rejects_ambiguous_normalized_identity(self):
        self.env['res.partner'].create({
            'name': 'Duplicate Identifier Vendor',
            'is_company': True,
            'eh_peppol_endpoint_id': '83 914 571 673',
        })
        inbound, _attachment = self._make_received()
        inbound.action_parse()

        inbound.action_match()

        self.assertFalse(inbound.partner_id)
        self.assertEqual(inbound.state, 'exception')

    def test_parse_rejects_invoice_addressed_to_another_buyer(self):
        wrong_buyer_xml = self.valid_xml.replace(
            b'53004085616', b'00000000000',
        )
        attachment = self.env['ir.attachment'].with_user(
            self.eh_user,
        ).create({
            'name': 'wrong-buyer.xml',
            'type': 'binary',
            'raw': wrong_buyer_xml,
            'mimetype': 'application/xml',
        })
        inbound = self.Inbound.with_user(self.eh_user).create({
            'company_id': self.company.id,
            'xml_attachment_id': attachment.id,
        })
        with self.assertRaisesRegex(UserError, 'does not match receiving'):
            inbound.action_parse()
        self.assertEqual(inbound.state, 'received')
        self.assertFalse(inbound.file_hash)

    def test_post_revalidates_resolved_supplier_identity(self):
        inbound, _attachment = self._make_matched()
        self.vendor.sudo().write({
            'eh_peppol_endpoint_id': '00000000000',
        })
        with self.assertRaisesRegex(UserError, 'does not carry'):
            inbound.with_user(self.manager).action_post()
        self.assertEqual(inbound.state, 'matched')
        self.assertFalse(inbound.move_id)

    def test_manager_can_confirm_valid_manual_supplier_resolution(self):
        manual_xml = self.valid_xml.replace(
            b'83914571673', b'51824753556',
        )
        attachment = self.env['ir.attachment'].with_user(
            self.eh_user,
        ).create({
            'name': 'manual-supplier.xml',
            'type': 'binary',
            'raw': manual_xml,
            'mimetype': 'application/xml',
        })
        inbound = self.Inbound.with_user(self.eh_user).create({
            'company_id': self.company.id,
            'xml_attachment_id': attachment.id,
        })
        inbound.action_parse()
        inbound.action_match()
        self.assertEqual(inbound.state, 'exception')
        self.assertFalse(inbound.partner_id)

        corrected_vendor = self.env['res.partner'].create({
            'name': 'Corrected Peppol Vendor',
            'is_company': True,
            'eh_peppol_endpoint_id': '51824753556',
            'eh_peppol_endpoint_scheme': '0151',
        })
        inbound.with_user(self.manager).write({
            'partner_id': corrected_vendor.id,
        })
        inbound.with_user(self.manager).action_match()
        self.assertEqual(inbound.state, 'matched')
        self.assertEqual(inbound.partner_id, corrected_vendor)

    def test_valid_reviewed_payload_posts_exact_bill(self):
        inbound, _attachment = self._make_matched()

        result = inbound.with_user(self.manager).action_post()

        inbound.invalidate_recordset([
            'state', 'move_id', 'posted_at', 'posted_by_id',
        ])
        self.assertTrue(result)
        self.assertEqual(inbound.state, 'posted')
        self.assertTrue(inbound.posted_at)
        self.assertEqual(inbound.posted_by_id, self.manager)
        move = inbound.move_id
        self.assertEqual(move.state, 'posted')
        self.assertTrue(move.eh_sealed)
        with self.assertRaises(UserError):
            move.with_user(self.manager).button_draft()
        self.assertEqual(move.move_type, 'in_invoice')
        self.assertEqual(move.company_id, self.company)
        self.assertEqual(move.partner_id, self.vendor)
        self.assertEqual(move.ref, inbound.invoice_number)
        self.assertEqual(move.currency_id, self.company.currency_id)
        self.assertEqual(move.invoice_date, inbound.issue_date)
        self.assertEqual(move.invoice_date_due, inbound.due_date)
        self.assertEqual(len(move.invoice_line_ids), 1)
        line = move.invoice_line_ids
        self.assertEqual(line.quantity, 10.0)
        self.assertEqual(line.price_unit, 100.0)
        self.assertIn(self.purchase_tax, line.tax_ids)
        self.assertAlmostEqual(move.amount_untaxed, 1000.0, places=2)
        self.assertAlmostEqual(move.amount_tax, 100.0, places=2)
        self.assertAlmostEqual(move.amount_total, 1100.0, places=2)

    def test_inbound_credit_note_posts_vendor_refund(self):
        inbound, _attachment = self._make_matched(
            xml=self._credit_note_xml(),
        )

        inbound.with_user(self.manager).action_post()

        self.assertEqual(inbound.move_id.move_type, 'in_refund')
        self.assertEqual(inbound.move_id.state, 'posted')
        self.assertAlmostEqual(inbound.move_id.amount_total, 1100.0, places=2)

    def test_resolved_purchase_order_links_exact_bill_lines(self):
        hour_uom = self.env.ref('uom.product_uom_hour')
        product_vals = {
            'name': 'Peppol PO Consulting',
            'type': 'service',
            'uom_id': hour_uom.id,
            'property_account_expense_id': self.account_expense.id,
        }
        if 'uom_po_id' in self.env['product.product']._fields:
            product_vals['uom_po_id'] = hour_uom.id
        product = self.env['product.product'].create(product_vals)
        po_line_vals = {
            'name': 'Consulting',
            'product_id': product.id,
            'product_qty': 10.0,
            'product_uom_id': hour_uom.id,
            'price_unit': 100.0,
        }
        tax_field = (
            'tax_ids'
            if 'tax_ids' in self.env['purchase.order.line']._fields
            else 'taxes_id'
        )
        po_line_vals[tax_field] = [(6, 0, self.purchase_tax.ids)]
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'company_id': self.company.id,
            'currency_id': self.company.currency_id.id,
            'order_line': [(0, 0, po_line_vals)],
        })
        po.button_confirm()
        order_xml = self.valid_xml.replace(
            b'<cac:AccountingSupplierParty>',
            b'<cac:OrderReference><cbc:ID>'
            + po.name.encode('utf-8')
            + b'</cbc:ID></cac:OrderReference>'
            b'<cac:AccountingSupplierParty>',
            1,
        )
        inbound, _attachment = self._make_received(xml=order_xml)
        inbound.action_parse()

        inbound.action_match()
        inbound.with_user(self.manager).action_post()

        self.assertEqual(inbound.purchase_order_id, po)
        self.assertEqual(inbound.move_id.invoice_origin, po.name)
        self.assertEqual(
            inbound.move_id.invoice_line_ids.purchase_line_id,
            po.order_line,
        )
        self.assertIn(inbound.move_id, po.invoice_ids)

    def test_zero_tax_categories_require_and_keep_explicit_tax_mapping(self):
        inbound, _attachment = self._make_received()
        for category in ('Z', 'E', 'G', 'O'):
            self._deactivate_explicit_purchase_category(category)
            extra = {'eh_edi_tax_category': category}
            if category in ('E', 'G', 'O'):
                extra['eh_edi_tax_exemption_reason'] = (
                    'Inbound Peppol %s category' % category
                )
            tax = self.env['account.tax'].create(self._purchase_tax_vals(
                'Inbound Peppol %s 0%%' % category,
                **extra,
            ))

            resolved = inbound._eh_resolve_purchase_tax(0.0, category)

            self.assertEqual(resolved, tax)

    def test_zero_rated_line_posts_with_reporting_tax_attached(self):
        self._deactivate_explicit_purchase_category('Z')
        zero_tax = self.env['account.tax'].create(self._purchase_tax_vals(
            'Inbound Peppol zero rated',
            eh_edi_tax_category='Z',
        ))
        inbound, _attachment = self._make_matched(
            xml=self._zero_tax_xml('Z'),
        )

        inbound.with_user(self.manager).action_post()

        line = inbound.move_id.invoice_line_ids
        self.assertIn(zero_tax, line.tax_ids)
        self.assertEqual(inbound.move_id.amount_tax, 0.0)
        self.assertEqual(inbound.move_id.amount_total, 1000.0)

    def test_reverse_charge_line_posts_balanced_self_assessment_group(self):
        self._deactivate_explicit_purchase_category('AE')
        debit_tax = self.env['account.tax'].create(self._purchase_tax_vals(
            'Inbound reverse charge input', 10.0,
            type_tax_use='none',
        ))
        credit_tax = self.env['account.tax'].create(self._purchase_tax_vals(
            'Inbound reverse charge output', -10.0,
            type_tax_use='none',
        ))
        reverse_group = self.env['account.tax'].create(
            self._purchase_tax_vals(
                'Inbound Peppol reverse charge',
                amount_type='group',
                eh_edi_tax_category='AE',
                eh_edi_tax_exemption_reason=(
                    'Reverse charge: buyer accounts for VAT'
                ),
                children_tax_ids=[(6, 0, (debit_tax + credit_tax).ids)],
            ),
        )
        inbound, _attachment = self._make_matched(
            xml=self._zero_tax_xml('AE'),
        )

        inbound.with_user(self.manager).action_post()

        move = inbound.move_id
        self.assertIn(reverse_group, move.invoice_line_ids.tax_ids)
        booked_tax_lines = move.line_ids.filtered('tax_line_id')
        self.assertEqual(
            set(booked_tax_lines.tax_line_id.ids),
            set((debit_tax + credit_tax).ids),
        )
        self.assertAlmostEqual(sum(booked_tax_lines.mapped('balance')), 0.0)
        self.assertEqual(move.amount_tax, 0.0)
        self.assertEqual(move.amount_total, 1000.0)

    def test_zero_rate_without_explicit_category_tax_fails_closed(self):
        self._deactivate_explicit_purchase_category('E')
        inbound, _attachment = self._make_received()

        with self.assertRaisesRegex(UserError, 'exactly one active'):
            inbound._eh_resolve_purchase_tax(0.0, 'E')

    def test_unit_price_precision_drift_books_exact_ubl_line_total(self):
        currency = self.company.currency_id.name.encode('ascii')
        xml = self.valid_xml.replace(
            b'<cbc:PriceAmount currencyID="' + currency
            + b'">100.00</cbc:PriceAmount>',
            b'<cbc:PriceAmount currencyID="' + currency
            + b'">100.0004</cbc:PriceAmount>',
            1,
        )
        self.assertNotEqual(xml, self.valid_xml)
        inbound, _attachment = self._make_matched(xml=xml)

        inbound.with_user(self.manager).action_post()

        line = inbound.move_id.invoice_line_ids
        self.assertEqual(line.quantity, 10.0)
        self.assertEqual(line.price_unit, 100.0)
        self.assertEqual(line.price_subtotal, 1000.0)
        self.assertEqual(inbound.move_id.amount_total, 1100.0)

    def test_parse_rejects_manipulated_line_total(self):
        currency = self.company.currency_id.name.encode('ascii')
        original = (
            b'<cbc:LineExtensionAmount currencyID="' + currency
            + b'">1000.00</cbc:LineExtensionAmount>'
        )
        replacement = (
            b'<cbc:LineExtensionAmount currencyID="' + currency
            + b'">1.00</cbc:LineExtensionAmount>'
        )
        prefix, marker, suffix = self.valid_xml.rpartition(original)
        self.assertTrue(marker)
        inbound, _attachment = self._make_received(
            xml=prefix + replacement + suffix,
        )

        with self.assertRaisesRegex(UserError, 'LineExtensionAmount'):
            inbound.action_parse()

        self.assertEqual(inbound.state, 'received')
        self.assertFalse(inbound.file_hash)

    def test_parse_rejects_document_allowance(self):
        currency = self.company.currency_id.name.encode('ascii')
        allowance = (
            b'<cac:AllowanceCharge>'
            b'<cbc:ChargeIndicator>false</cbc:ChargeIndicator>'
            b'<cbc:Amount currencyID="' + currency + b'">1.00</cbc:Amount>'
            b'</cac:AllowanceCharge>'
        )
        xml = self.valid_xml.replace(
            b'<cac:TaxTotal>', allowance + b'<cac:TaxTotal>', 1,
        )
        inbound, _attachment = self._make_received(xml=xml)

        with self.assertRaisesRegex(UserError, 'Unsupported document-level'):
            inbound.action_parse()

        self.assertEqual(inbound.state, 'received')

    def test_nonzero_tax_requires_configured_mapping(self):
        inbound, _attachment = self._make_matched()
        candidate_taxes = self.env['account.tax'].search([
            ('type_tax_use', '=', 'purchase'),
            ('amount_type', '=', 'percent'),
            ('amount', '=', 10.0),
            ('company_id', '=', self.company.id),
            ('active', '=', True),
        ]).filtered(lambda tax: not tax.price_include)
        candidate_taxes.write({'active': False})
        before = self.env['account.move'].search_count([
            ('ref', '=', inbound.invoice_number),
        ])

        with self.assertRaisesRegex(UserError, 'exactly one active'):
            inbound.with_user(self.manager).action_post()

        self.assertEqual(inbound.state, 'matched')
        self.assertFalse(inbound.move_id)
        self.assertEqual(self.env['account.move'].search_count([
            ('ref', '=', inbound.invoice_number),
        ]), before)

    def test_nonzero_tax_rejects_ambiguous_mapping(self):
        inbound, _attachment = self._make_matched()
        self.env['account.tax'].create({
            'name': 'Duplicate Peppol input tax 10%',
            'type_tax_use': 'purchase',
            'amount_type': 'percent',
            'amount': 10.0,
            'company_id': self.company.id,
            'price_include_override': 'tax_excluded',
        })

        with self.assertRaisesRegex(UserError, 'found 2'):
            inbound.with_user(self.manager).action_post()

        self.assertEqual(inbound.state, 'matched')
        self.assertFalse(inbound.move_id)

    def test_caught_post_failure_leaves_no_draft_bill(self):
        inbound, _attachment = self._make_matched()
        before = self.env['account.move'].search_count([
            ('ref', '=', inbound.invoice_number),
        ])
        MoveClass = type(self.env['account.move'])

        with patch.object(
                MoveClass, 'action_post', side_effect=UserError('probe')):
            with self.assertRaisesRegex(UserError, 'probe'):
                inbound.with_user(self.manager).action_post()

        self.assertEqual(inbound.state, 'matched')
        self.assertFalse(inbound.move_id)
        self.assertEqual(self.env['account.move'].search_count([
            ('ref', '=', inbound.invoice_number),
        ]), before)

    def test_manual_vendor_post_uses_same_normalized_duplicate_claim(self):
        def _draft(reference):
            return self.env['account.move'].create({
                'move_type': 'in_invoice',
                'partner_id': self.vendor.id,
                'ref': reference,
                'invoice_date': '2026-05-01',
                'journal_id': self.journal_purchase.id,
                'invoice_line_ids': [(0, 0, {
                    'name': 'Manual duplicate probe',
                    'quantity': 1.0,
                    'price_unit': 25.0,
                    'account_id': self.account_expense.id,
                })],
            })

        first = _draft('SUP-REF-900')
        second = _draft('  sup-ref-900  ')
        first.action_post()
        with self.assertRaisesRegex(UserError, 'duplicates posted bill'):
            second.action_post()
        self.assertEqual(first.state, 'posted')
        self.assertEqual(second.state, 'draft')
