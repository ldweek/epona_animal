# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Integration test for the account.move adapter in eh_account_einvoice_peppol.
"""

import base64

from lxml import etree

from odoo.exceptions import UserError
from odoo.tests.common import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_einvoice_peppol', 'post_install', '-at_install')
class PeppolMoveExportTest(EhAccountIntegrationTestCase):

    def setUp(self):
        super().setUp()
        # The Peppol export validates BOTH parties' participant ids.
        # Configure the company (sender) with a valid Australian
        # Business Number so the seller party passes the ABN
        # mod-89 checksum, and the buyer with a complementary one.
        au = self.env.ref('base.au')
        self.env.company.write({
            'country_id': au.id,
            # Bare 11 digit ABN, no country prefix: this is the format
            # base_vat enforces for AU when it is installed (l10n_fr,
            # co-installed with fr e-invoicing, pulls it in). Passes the
            # ABN mod-89 checksum.
            'vat': '83914571673',
            'street': '1 Seller Street',
            'city': 'Sydney',
            'zip': '2000',
        })
        self.env.company.partner_id.write({
            'eh_peppol_endpoint_scheme': '0151',
            'eh_peppol_endpoint_id': '83914571673',
        })
        self.partner = self.env['res.partner'].create({
            'name': 'Buyer Co',
            'is_company': True,
            'country_id': au.id,
            'vat': '53004085616',  # bare ABN, base_vat valid, passes mod-89
            'eh_peppol_endpoint_scheme': '0151',
            'eh_peppol_endpoint_id': '53004085616',
            'street': '2 Buyer Street',
            'city': 'Sydney',
            'zip': '2000',
        })
        # Ensure a sale journal exists so account.move auto-resolution
        # works in fresh databases that ship neither demo data nor a
        # chart of accounts.
        self.sale_journal = self.env['account.journal'].search(
            [('type', '=', 'sale'),
             ('company_id', '=', self.env.company.id)], limit=1,
        )
        if not self.sale_journal:
            self.sale_journal = self.env['account.journal'].create({
                'name': 'Sales',
                'code': 'INV',
                'type': 'sale',
                'company_id': self.env.company.id,
            })

    def _post_invoice(self, partner=None):
        currency = self.env.company.currency_id
        product = self.env['product.product'].create({
            'name': 'Test Service', 'type': 'service',
        })
        tax = self._make_tax('EH Export Standard 10', 10.0)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': (partner or self.partner).id,
            'currency_id': currency.id,
            'invoice_date': '2026-05-01',
            'invoice_origin': 'PO-TEST-001',
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': 'Consulting',
                'quantity': 10.0,
                'price_unit': 100.0,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, tax.ids)],
            })],
        })
        move.action_post()
        return move

    def test_xml_export_action_returns_attachment(self):
        move = self._post_invoice()
        result = move.action_eh_export_peppol_xml()
        self.assertIsInstance(result, dict)
        self.assertIn('url', result)
        attachment_id = int(result['url'].split('/web/content/')[1].split('?')[0])
        attachment = self.env['ir.attachment'].browse(attachment_id)
        self.assertEqual(attachment.mimetype, 'application/xml')
        root = etree.fromstring(base64.b64decode(attachment.datas))
        self.assertEqual(root.xpath('string(//*[local-name()="ID"][1])'), move.name)

    def test_r003_requires_buyer_or_order_reference(self):
        move = self._post_invoice()
        move.invoice_origin = False

        with self.assertRaisesRegex(UserError, 'R003'):
            move.action_eh_export_peppol_xml()

    def test_payment_means_and_plain_text_note_render(self):
        from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
            render_invoice_xml,
        )

        move = self._post_invoice()
        move.narration = '<p>Pay <strong>within 30 days</strong>.</p>'

        payload = move._eh_build_peppol_payload()
        root = etree.fromstring(render_invoice_xml(payload))

        self.assertEqual(payload['payment_means']['code'], '1')
        self.assertEqual(payload['payment_means']['payment_id'], move.name)
        self.assertNotIn('<', payload['note'])
        self.assertIn('within 30 days', payload['note'])
        self.assertEqual(
            root.xpath('string(//*[local-name()="PaymentMeansCode"])'),
            '1',
        )
        self.assertNotIn(
            '<strong>', root.xpath('string(//*[local-name()="Note"])'),
        )

    def test_credit_transfer_requires_and_renders_payee_account(self):
        from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
            PeppolGeneratorError,
            render_invoice_xml,
        )

        move = self._post_invoice()
        self.env.company.eh_peppol_payment_means_code = '30'
        with self.assertRaisesRegex(PeppolGeneratorError, 'payee bank'):
            move._eh_build_peppol_payload()

        bank = self.env['res.partner.bank'].create({
            'acc_number': '123456789',
            'partner_id': self.env.company.partner_id.id,
        })
        move.partner_bank_id = bank
        root = etree.fromstring(
            render_invoice_xml(move._eh_build_peppol_payload())
        )
        self.assertEqual(
            root.xpath('string(//*[local-name()="PaymentMeansCode"])'),
            '30',
        )
        self.assertEqual(
            root.xpath(
                'string(//*[local-name()="PayeeFinancialAccount"]'
                '/*[local-name()="ID"])'
            ),
            '123456789',
        )

    def test_lines_and_tax_come_from_canonical_model(self):
        inv = self._post_invoice()
        lines, tax = inv._eh_build_peppol_lines_and_tax()
        source = inv.invoice_line_ids.filtered(
            lambda line: line.display_type not in (
                'line_section', 'line_note',
            )
        )[0]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]['line_total'], source.price_subtotal)
        self.assertEqual(
            lines[0]['unit_price'],
            source.price_subtotal / source.quantity,
        )
        self.assertEqual(lines[0]['gross_unit_price'], source.price_unit)
        self.assertEqual(
            lines[0]['unit_code'], source.product_uom_id._get_unece_code(),
        )
        self.assertEqual(tax[0]['taxable_amount'], inv.amount_untaxed)
        self.assertEqual(tax[0]['tax_amount'], inv.amount_tax)

        parity_tax = self._make_tax('EH Parity 10', 10.0)
        multi = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_line_ids': [
                (0, 0, {'name': 'A', 'quantity': 2.0, 'price_unit': 33.33,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, parity_tax.ids)]}),
                (0, 0, {'name': 'B', 'quantity': 1.0, 'price_unit': 50.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, parity_tax.ids)]}),
            ],
        })
        multi.action_post()
        lines, tax = multi._eh_build_peppol_lines_and_tax()
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            multi.currency_id.round(sum(item['line_total'] for item in lines)),
            multi.currency_id.round(multi.amount_untaxed),
        )
        self.assertEqual(
            multi.currency_id.round(sum(item['tax_amount'] for item in tax)),
            multi.currency_id.round(multi.amount_tax),
        )

    def test_payload_does_not_guess_buyer_reference_from_partner_ref(self):
        move = self._post_invoice()
        self.partner.ref = 'INTERNAL-PAYMENT-REFERENCE'

        payload = move._eh_build_peppol_payload()

        self.assertEqual(payload['buyer_reference'], '')

    def test_payload_buyer_is_commercial_legal_party(self):
        contact = self.env['res.partner'].create({
            'name': 'Buyer Billing Contact',
            'parent_id': self.partner.id,
            'type': 'invoice',
            'email': 'billing@example.test',
        })
        move = self._post_invoice(contact)

        payload = move._eh_build_peppol_payload()

        self.assertEqual(payload['customer']['name'], self.partner.name)
        self.assertEqual(
            payload['customer']['endpoint_id'],
            self.partner.eh_peppol_endpoint_id,
        )

    def _tax_group(self):
        company = self.env.company
        country = company.account_fiscal_country_id or self.env.ref('base.us')
        # account.tax.group carries company_id/country_id from Odoo 17;
        # on Odoo 16 it is global with neither field.
        Group = self.env['account.tax.group'].sudo()
        if 'company_id' in Group._fields:
            return Group.search(
                [('company_id', '=', company.id)], limit=1,
            ) or Group.create({
                'name': 'EH EDI Test Tax Group',
                'company_id': company.id,
                'country_id': country.id,
            })
        return Group.search([], limit=1) or Group.create({
            'name': 'EH EDI Test Tax Group',
        })

    def _make_tax(self, name, amount, category=None, reason=None):
        company = self.env.company
        country = company.account_fiscal_country_id or self.env.ref('base.us')
        vals = {
            'name': name,
            'amount': amount,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': company.id,
            'country_id': country.id,
            'tax_group_id': self._tax_group().id,
        }
        if category is not None:
            vals['eh_edi_tax_category'] = category
        if reason is not None:
            vals['eh_edi_tax_exemption_reason'] = reason
        return self.env['account.tax'].sudo().create(vals)

    def test_en16931_tax_categories_are_distinct(self):
        # EN 16931 categories must not collapse to Z/S by rate: a standard,
        # zero-rated, exempt and reverse-charge line must each carry their
        # own UNTDID 5305 category, and exempt / reverse charge must carry
        # the exemption reason.
        std = self._make_tax('EH Std 20', 20.0)  # defaults to S by rate
        zero = self._make_tax('EH Zero', 0.0, category='Z')
        exempt = self._make_tax(
            'EH Exempt', 0.0, category='E',
            reason='Exempt under Division 38',
        )
        reverse = self._make_tax(
            'EH Reverse', 0.0, category='AE',
            reason='Reverse charge, buyer accounts for VAT',
        )
        product = self.env['product.product'].create({
            'name': 'Cat Service', 'type': 'service',
        })
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_origin': 'PO-TAX-CATEGORY-001',
            'invoice_line_ids': [
                (0, 0, {'product_id': product.id, 'name': 'Standard',
                        'quantity': 1.0, 'price_unit': 100.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, std.ids)]}),
                (0, 0, {'product_id': product.id, 'name': 'Zero',
                        'quantity': 1.0, 'price_unit': 100.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, zero.ids)]}),
                (0, 0, {'product_id': product.id, 'name': 'Exempt',
                        'quantity': 1.0, 'price_unit': 100.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, exempt.ids)]}),
                (0, 0, {'product_id': product.id, 'name': 'Reverse',
                        'quantity': 1.0, 'price_unit': 100.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, reverse.ids)]}),
            ],
        })
        move.action_post()

        lines, tax_cats = move._eh_build_peppol_lines_and_tax()
        by_desc = {l['description']: l['tax_category_code'] for l in lines}
        self.assertEqual(by_desc['Standard'], 'S')
        self.assertEqual(by_desc['Zero'], 'Z')
        self.assertEqual(by_desc['Exempt'], 'E')
        self.assertEqual(by_desc['Reverse'], 'AE')
        # All four are distinct: the rate-based collapse would have made
        # Zero, Exempt and Reverse all 'Z'.
        self.assertEqual(
            len({by_desc['Zero'], by_desc['Exempt'], by_desc['Reverse']}),
            3,
        )
        # Exemption reason survives into the document tax breakdown.
        cats_by_code = {c['category_code']: c for c in tax_cats}
        self.assertEqual(
            cats_by_code['E']['exemption_reason'],
            'Exempt under Division 38',
        )
        self.assertEqual(
            cats_by_code['AE']['exemption_reason'],
            'Reverse charge, buyer accounts for VAT',
        )

        # And it renders into the UBL as TaxExemptionReason.
        from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
            render_invoice_xml,
        )
        xml = render_invoice_xml(move._eh_build_peppol_payload())
        self.assertIn(b'Exempt under Division 38', xml)
        self.assertIn(b'>AE<', xml)
        self.assertIn(b'>E<', xml)

    def test_ubl_tax_ties_to_booked_move_tax_on_rounded_line(self):
        # EN 16931 BR-CO-14: the category tax on the e-invoice must equal
        # the tax actually booked on the move. Build a line whose base is
        # constructed so a naive round(base * rate / 100, 2) disagrees with
        # what the tax engine books, then assert the rendered UBL carries
        # the booked figure, not the recomputed one.
        from odoo.addons.eh_account_einvoice_peppol.tools.ubl_generator import (
            render_invoice_xml,
        )
        std = self._make_tax('EH Std 10 Round', 10.0)  # 10 percent, cat S
        product = self.env['product.product'].create({
            'name': 'Rounded Service', 'type': 'service',
        })
        # Two lines of 5.55 at 10 percent. Odoo rounds tax per line
        # (round_per_line is the default), so each line books 0.56 and the
        # move's booked tax is 1.12. A naive per-bucket recompute of
        # round(sum_base * rate / 100, 2) = round(11.10 * 0.10, 2) = 1.11,
        # which disagrees with the ledger by one cent. The UBL must carry
        # the booked 1.12, not the recomputed 1.11.
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_origin': 'PO-ROUNDING-001',
            'invoice_line_ids': [
                (0, 0, {'product_id': product.id, 'name': 'R1',
                        'quantity': 1.0, 'price_unit': 5.55,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, std.ids)]}),
                (0, 0, {'product_id': product.id, 'name': 'R2',
                        'quantity': 1.0, 'price_unit': 5.55,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, std.ids)]}),
            ],
        })
        move.action_post()
        currency = move.currency_id

        _lines, tax_cats = move._eh_build_peppol_lines_and_tax()
        std_bucket = next(c for c in tax_cats if c['category_code'] == 'S')

        # The bucket tax must equal the move's booked amount_tax exactly.
        # Without the fix, tax_amount is round(11.10 * 0.10, 2) and this
        # fails whenever the ledger rounded per line to a different cent.
        self.assertEqual(
            currency.round(std_bucket['tax_amount']),
            currency.round(move.amount_tax),
        )

        # And the rendered UBL serializes the booked figure.
        booked = currency.round(move.amount_tax)
        xml = render_invoice_xml(move._eh_build_peppol_payload())
        self.assertIn(('%.2f' % booked).encode(), xml)

    def test_multi_tax_line_fails_closed_before_ubl(self):
        # BT-151/BT-152 permit one VAT category and percentage per invoice
        # line. Emitting the first of two effective taxes or duplicating the
        # line base across both buckets creates a formally false invoice, so
        # the shared EN 16931 mapper must refuse this Odoo tax setup.
        from odoo.addons.eh_edi_core.tools.en16931.mapper import (
            En16931MappingError,
        )
        ten = self._make_tax('EH Split 10', 10.0)
        five = self._make_tax('EH Split 5', 5.0)
        product = self.env['product.product'].create({
            'name': 'Split Service', 'type': 'service',
        })
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': self.env.company.currency_id.id,
            'invoice_date': '2026-05-01',
            'invoice_line_ids': [
                (0, 0, {'product_id': product.id, 'name': 'Dual',
                        'quantity': 1.0, 'price_unit': 200.0,
                        'account_id': self.account_revenue.id,
                        'tax_ids': [(6, 0, (ten + five).ids)]}),
            ],
        })
        move.action_post()

        with self.assertRaisesRegex(
                En16931MappingError, 'one VAT category and rate'):
            move._eh_build_peppol_payload()

    def test_foreign_currency_tax_base_and_tax_use_document_currency(self):
        """BT-116 and BT-117 must use one currency on foreign invoices."""
        eur = self.env.ref('base.EUR')
        eur.active = True
        self.env['res.currency.rate'].create({
            'currency_id': eur.id,
            'company_id': self.env.company.id,
            'name': '2026-05-01',
            # Non-unit rate makes company and document amounts visibly
            # different; exact direction is irrelevant to this assertion.
            'rate': 0.625,
        })
        tax = self._make_tax('EH Foreign 10', 10.0)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': eur.id,
            'invoice_date': '2026-05-01',
            'invoice_line_ids': [(0, 0, {
                'name': 'Foreign service',
                'quantity': 1.0,
                'price_unit': 100.0,
                'account_id': self.account_revenue.id,
                'tax_ids': [(6, 0, tax.ids)],
            })],
        })
        move.action_post()

        _lines, tax_categories = move._eh_build_peppol_lines_and_tax()
        bucket = next(
            item for item in tax_categories
            if item['category_code'] == 'S'
            and item['rate_pct'] == 10.0
        )
        self.assertEqual(eur.round(bucket['taxable_amount']), 100.0)
        self.assertEqual(eur.round(bucket['tax_amount']), 10.0)
        self.assertNotEqual(
            self.env.company.currency_id.round(bucket['taxable_amount']),
            self.env.company.currency_id.round(abs(
                sum(move.line_ids.filtered('tax_line_id').mapped(
                    'tax_base_amount'))
            )),
        )

    def test_exempt_tax_requires_exemption_reason(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self._make_tax('EH Bad Exempt', 0.0, category='E')

    def test_export_blocked_on_draft_move(self):
        currency = self.env.company.currency_id
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'currency_id': currency.id,
            'invoice_date': '2026-05-01',
        })
        # draft state, the action should fail clean
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            move.action_eh_export_peppol_xml()
