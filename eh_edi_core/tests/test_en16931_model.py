# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Canonical EN 16931 model + mapper unit tests.

The mapper is pure: it reads duck-typed attributes off a stub record, so
no ORM is needed. These tests pin two things:

* map_move() extracts the right EN 16931 semantics from a move.
* model.to_cii_input() reproduces the historical Factur-X / CII mapper
  output shape exactly. That equality is what lets France swap its
  hand-rolled map_move() for this shared model without shifting a single
  byte of CII output (proven end to end by the golden harness once the
  swap lands).
"""

import datetime
import types

from odoo.tests import BaseCase, tagged

from odoo.addons.eh_edi_core.tools.en16931.mapper import (
    En16931MappingError,
    map_move,
)
from odoo.addons.eh_edi_core.tools.en16931.model import InvoiceModel


def _tax(
    amount,
    category=None,
    *,
    amount_type='percent',
    children=(),
    reason='',
    name='VAT',
    price_include=False,
):
    return types.SimpleNamespace(
        amount=amount,
        amount_type=amount_type,
        children_tax_ids=children,
        eh_edi_tax_category=category,
        eh_fr_einv_category=None,
        eh_edi_tax_exemption_reason=reason,
        name=name,
        price_include=price_include,
    )


class _HashableDict(dict):
    """Tiny stand-in for Odoo's hashable tax-grouping key."""

    def __hash__(self):
        return hash(tuple(sorted(self.items())))


def _country(code):
    return types.SimpleNamespace(code=code)


def _partner(**kw):
    base = {
        'name': '', 'vat': '', 'email': '',
        'street': '', 'street2': '', 'city': '', 'zip': '',
        'country_id': None, 'fr_siret': None, 'siret': None,
        'eh_peppol_endpoint_id': None, 'eh_peppol_endpoint_scheme': None,
        'commercial_partner_id': None,
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


def _line(**kw):
    base = {
        'display_type': False, 'name': '', 'product_id': None,
        'quantity': 0.0, 'price_unit': 0.0, 'price_subtotal': 0.0,
        'tax_ids': [], 'product_uom_id': None,
    }
    base.update(kw)
    return types.SimpleNamespace(**base)


def _sample_move():
    seller = _partner(
        name='Demo SARL', vat='FR12345678900',
        country_id=_country('FR'),
        street='10 Rue de la Paix', zip='75001', city='Paris',
    )
    buyer = _partner(
        name='Customer SAS', vat='FR98765432100',
        country_id=_country('FR'), fr_siret='98765432100013',
        street='20 Avenue des Champs', zip='69001', city='Lyon',
    )
    company = types.SimpleNamespace(
        partner_id=seller, fr_siret='12345678900014',
        currency_id=types.SimpleNamespace(name='EUR'),
    )
    line = _line(
        name='Consulting day', quantity=5.0, price_unit=800.0,
        price_subtotal=4000.0, tax_ids=[_tax(20.0)],
    )
    return types.SimpleNamespace(
        name='INV-2026-00001',
        invoice_date=datetime.date(2026, 5, 27),
        invoice_date_due=datetime.date(2026, 6, 30),
        move_type='out_invoice',
        currency_id=types.SimpleNamespace(name='EUR'),
        narration='',
        buyer_reference='PO-12345',
        contract_reference='',
        purchase_order_reference='',
        ref=None,
        company_id=company,
        partner_id=buyer,
        invoice_line_ids=[line],
        amount_untaxed=4000.0,
        amount_tax=800.0,
        amount_tax_signed=-800.0,
        amount_total=4800.0,
        amount_residual=4800.0,
    )


def _with_booked_tax_details(move, *, basis=4000.0, tax_amount=800.0):
    """Attach the relevant account.move aggregation API to a test stub."""
    tax = move.invoice_line_ids[0].tax_ids[0]

    def aggregate(*, grouping_key_generator=None, round_from_tax_lines=False):
        assert round_from_tax_lines
        key = _HashableDict(grouping_key_generator(None, {'tax': tax}))
        return {
            'tax_details': {
                key: {
                    'base_amount_currency': basis,
                    'tax_amount_currency': tax_amount,
                },
            },
        }

    move._prepare_invoice_aggregated_taxes = aggregate
    return move


@tagged('eh_edi_core', 'post_install', '-at_install')
class TestEn16931Model(BaseCase):

    def test_map_move_extracts_semantics(self):
        model = map_move(_sample_move())
        self.assertIsInstance(model, InvoiceModel)
        self.assertEqual(model.invoice_number, 'INV-2026-00001')
        self.assertEqual(model.currency, 'EUR')
        self.assertEqual(model.buyer_reference, 'PO-12345')
        self.assertEqual(model.seller.legal_id, '12345678900014')
        self.assertEqual(model.buyer.legal_id, '98765432100013')
        self.assertEqual(len(model.lines), 1)
        self.assertEqual(model.lines[0].tax_category, 'S')
        self.assertEqual(model.lines[0].tax_rate, 20.0)
        self.assertEqual(len(model.tax_breakdown), 1)
        self.assertAlmostEqual(model.tax_breakdown[0].tax_amount, 800.0)
        self.assertAlmostEqual(model.totals.payable_amount, 4800.0)

    def test_to_cii_input_reproduces_legacy_shape(self):
        model = map_move(_sample_move())
        expected = {
            'invoice_number': 'INV-2026-00001',
            'invoice_date': datetime.date(2026, 5, 27),
            'invoice_date_due': datetime.date(2026, 6, 30),
            'move_type': 'out_invoice',
            'currency': 'EUR',
            'buyer_reference': 'PO-12345',
            'contract_reference': '',
            'purchase_order_reference': '',
            'seller': {
                'name': 'Demo SARL',
                'siret': '12345678900014',
                'vat': 'FR12345678900',
                'contact_email': '',
                'address': {
                    'street': '10 Rue de la Paix',
                    'street2': '',
                    'zip': '75001',
                    'city': 'Paris',
                    'country_code': 'FR',
                },
            },
            'buyer': {
                'name': 'Customer SAS',
                'siret': '98765432100013',
                'vat': 'FR98765432100',
                'contact_email': '',
                'address': {
                    'street': '20 Avenue des Champs',
                    'street2': '',
                    'zip': '69001',
                    'city': 'Lyon',
                    'country_code': 'FR',
                },
            },
            'lines': [
                {
                    'name': 'Consulting day',
                    'quantity': 5.0,
                    'unit_code': 'C62',
                    'price_unit': 800.0,
                    'gross_price_unit': 800.0,
                    'tax_rate': 20.0,
                    'tax_category': 'S',
                    'line_total': 4000.0,
                },
            ],
            'totals': {
                'sum_line_net': 4000.0,
                'allowance_total': 0.0,
                'charge_total': 0.0,
                'tax_basis': 4000.0,
                'tax_amount': 800.0,
                'tax_currency_amount': 0.0,
                'invoice_total': 4800.0,
                'prepaid_amount': 0.0,
                'rounding_amount': 0.0,
                'payable_amount': 4800.0,
            },
            'tax_breakdown': [
                {'rate': 20.0, 'category': 'S',
                 'basis': 4000.0, 'amount': 800.0},
            ],
        }
        self.assertEqual(model.to_cii_input(), expected)

    def test_credit_note_maps_to_credit_note_doc_type(self):
        move = _sample_move()
        move.move_type = 'out_refund'
        ubl = map_move(move).to_ubl_inputs()
        self.assertEqual(ubl['document_type'], 'credit_note')

    def test_to_ubl_inputs_base_shape(self):
        ubl = map_move(_sample_move()).to_ubl_inputs()
        self.assertEqual(ubl['document_type'], 'invoice')
        self.assertEqual(ubl['currency_code'], 'EUR')
        self.assertEqual(ubl['supplier']['vat_id'], 'FR12345678900')
        self.assertEqual(len(ubl['lines']), 1)
        self.assertEqual(ubl['lines'][0]['id'], 1)
        # No unit of measure on the stub line, so the UBL unit code is
        # the EA default; the CII side stays C62 regardless.
        self.assertEqual(ubl['lines'][0]['unit_code'], 'C62')
        self.assertEqual(ubl['lines'][0]['tax_category_code'], 'S')
        self.assertEqual(len(ubl['tax_categories']), 1)
        self.assertAlmostEqual(
            ubl['tax_categories'][0]['tax_amount'], 800.0,
        )
        self.assertEqual(ubl['supplier']['endpoint_id'], '')

    def test_discounted_line_uses_net_bt146_and_retains_gross_bt148(self):
        move = _sample_move()
        line = move.invoice_line_ids[0]
        line.quantity = 5.0
        line.price_unit = 100.0
        line.price_subtotal = 450.0
        move.amount_untaxed = 450.0
        move.amount_tax = 90.0
        move.amount_tax_signed = -90.0
        move.amount_total = 540.0
        move.amount_residual = 540.0

        model = map_move(move)

        self.assertEqual(model.lines[0].price_unit, 90.0)
        self.assertEqual(model.lines[0].gross_price_unit, 100.0)
        self.assertEqual(model.lines[0].line_net, 450.0)
        self.assertEqual(model.to_ubl_inputs()['lines'][0]['unit_price'], 90.0)

    def test_price_included_tax_uses_tax_exclusive_bt146(self):
        move = _sample_move()
        line = move.invoice_line_ids[0]
        line.quantity = 1.0
        line.price_unit = 120.0
        line.price_subtotal = 100.0
        line.tax_ids = [_tax(20.0, price_include=True)]
        move.amount_untaxed = 100.0
        move.amount_tax = 20.0
        move.amount_tax_signed = -20.0
        move.amount_total = 120.0
        move.amount_residual = 120.0

        mapped = map_move(move).lines[0]

        self.assertEqual(mapped.price_unit, 100.0)
        self.assertEqual(mapped.gross_price_unit, 0.0)

    def test_buyer_is_legal_commercial_partner(self):
        move = _sample_move()
        legal = _partner(
            name='Legal Buyer SAS', vat='FR11111111111',
            country_id=_country('FR'),
        )
        move.partner_id.name = 'Billing Contact'
        move.partner_id.commercial_partner_id = legal

        self.assertEqual(map_move(move).buyer.name, 'Legal Buyer SAS')

    def test_buyer_reference_never_falls_back_to_move_ref(self):
        move = _sample_move()
        move.buyer_reference = ''
        move.ref = 'PAYMENT-MEMO-DO-NOT-EMIT-AS-BT10'

        self.assertEqual(map_move(move).buyer_reference, '')

    def test_uom_uses_stable_unece_resolver_not_translated_name(self):
        move = _sample_move()
        move.invoice_line_ids[0].product_uom_id = types.SimpleNamespace(
            id=42,
            name='Heures traduites', display_name='Heures traduites',
            get_external_id=lambda: {42: 'uom.product_uom_hour'},
        )

        self.assertEqual(map_move(move).lines[0].unit_code, 'HUR')

    def test_uom_without_stable_code_fails_closed(self):
        move = _sample_move()
        move.invoice_line_ids[0].product_uom_id = types.SimpleNamespace(
            name='hour translated', display_name='hour translated',
        )

        with self.assertRaisesRegex(En16931MappingError, 'deterministic'):
            map_move(move)

    def test_section_and_note_rows_are_excluded(self):
        move = _sample_move()
        move.invoice_line_ids = [
            _line(display_type='line_section', name='Section'),
            move.invoice_line_ids[0],
            _line(display_type='line_note', name='Note'),
        ]

        self.assertEqual([line.name for line in map_move(move).lines], [
            'Consulting day',
        ])

    def test_missing_party_country_fails_instead_of_fabricating_fr(self):
        move = _sample_move()
        move.partner_id.country_id = None

        with self.assertRaisesRegex(En16931MappingError, 'no country'):
            map_move(move)

    def test_missing_due_date_stays_missing(self):
        move = _sample_move()
        move.invoice_date_due = None

        self.assertIsNone(map_move(move).to_ubl_inputs()['due_date'])

    def test_rounding_prepayment_and_tax_currency_totals(self):
        move = _sample_move()
        move.company_id.currency_id = types.SimpleNamespace(name='AUD')
        move.amount_tax_signed = -1240.0
        move.amount_total = 4800.05
        move.amount_residual = 3800.05

        model = map_move(move)

        self.assertEqual(model.tax_currency, 'AUD')
        self.assertEqual(model.totals.grand_total, 4800.0)
        self.assertAlmostEqual(model.totals.rounding_amount, 0.05)
        self.assertEqual(model.totals.prepaid_amount, 1000.0)
        self.assertEqual(model.totals.payable_amount, 3800.05)
        self.assertEqual(model.totals.tax_currency_amount, 1240.0)

    def test_unproved_document_allowance_or_charge_fails_closed(self):
        move = _sample_move()
        move.amount_untaxed = 3990.0

        with self.assertRaisesRegex(En16931MappingError, 'BT-92/BT-99'):
            map_move(move)

    def test_zero_rate_maps_to_z_category(self):
        move = _sample_move()
        move.invoice_line_ids[0].tax_ids = [_tax(0.0)]
        model = map_move(move)
        self.assertEqual(model.lines[0].tax_category, 'Z')

    def test_tax_breakdown_uses_booked_document_currency_amounts(self):
        move = _with_booked_tax_details(
            _sample_move(), basis=3999.97, tax_amount=799.99,
        )

        bucket = map_move(move).tax_breakdown[0]

        self.assertEqual(bucket.taxable_amount, 3999.97)
        self.assertEqual(bucket.tax_amount, 799.99)

    def test_group_tax_uses_effective_child_profile(self):
        move = _sample_move()
        child = _tax(20.0, name='VAT 20%')
        move.invoice_line_ids[0].tax_ids = [
            _tax(0.0, amount_type='group', children=(child,), name='VAT group'),
        ]

        model = map_move(move)

        self.assertEqual(model.lines[0].tax_rate, 20.0)
        self.assertEqual(model.tax_breakdown[0].tax_amount, 800.0)

    def test_division_tax_is_a_percentage_profile(self):
        move = _sample_move()
        move.invoice_line_ids[0].tax_ids = [
            _tax(20.0, amount_type='division'),
        ]

        self.assertEqual(map_move(move).lines[0].tax_rate, 20.0)

    def test_fixed_tax_is_not_misreported_as_vat_percentage(self):
        move = _sample_move()
        move.invoice_line_ids[0].tax_ids = [
            _tax(5.0, amount_type='fixed', name='Eco fee'),
        ]

        with self.assertRaisesRegex(En16931MappingError, 'fixed'):
            map_move(move)

    def test_withholding_is_not_misreported_as_standard_vat(self):
        move = _sample_move()
        move.invoice_line_ids[0].tax_ids = [
            _tax(-3.0, name='Retention 3%'),
        ]

        with self.assertRaisesRegex(En16931MappingError, 'withholding'):
            map_move(move)

    def test_multiple_vat_profiles_on_one_line_fail_closed(self):
        move = _sample_move()
        move.invoice_line_ids[0].tax_ids = [_tax(20.0), _tax(10.0)]

        with self.assertRaisesRegex(En16931MappingError, 'effective taxes'):
            map_move(move)

    def test_equal_rate_tax_components_are_not_silently_collapsed(self):
        move = _sample_move()
        move.invoice_line_ids[0].tax_ids = [
            _tax(5.0, name='Federal VAT'),
            _tax(5.0, name='Provincial VAT'),
        ]

        with self.assertRaisesRegex(En16931MappingError, '2 effective taxes'):
            map_move(move)

    def test_unclassified_untaxed_line_is_not_guessed_as_zero_rated(self):
        move = _sample_move()
        move.invoice_line_ids[0].tax_ids = []

        with self.assertRaisesRegex(En16931MappingError, 'cannot infer'):
            map_move(move)

    def test_explicit_exempt_tax_and_reason_are_preserved(self):
        move = _sample_move()
        move.invoice_line_ids[0].tax_ids = [
            _tax(
                0.0,
                category='E',
                reason='Article 132 VAT Directive',
                name='Exempt supply',
            ),
        ]

        line = map_move(move).lines[0]

        self.assertEqual(line.tax_category, 'E')
        self.assertEqual(line.tax_exemption_reason, 'Article 132 VAT Directive')
