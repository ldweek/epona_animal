# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Canonical EN 16931 semantic invoice model.

One format-neutral representation of an invoice's EN 16931 content, plus
adapters that render it into the input shapes the existing serializers
already consume:

* to_cii_input()  -> the dict the Factur-X / CII builder consumes
  (eh_l10n_fr_einvoicing.tools.factur_x.builder.build_cii_xml). It
  reproduces the historical map_move() output shape exactly, so the CII
  output stays byte for byte identical when France adopts this model.

* to_ubl_inputs() -> the kwargs the Peppol UBL generator consumes
  (eh_account_einvoice_peppol.tools.ubl_generator.make_invoice_payload).
  This is the format-neutral base. The Peppol layer keeps its own
  participant-id validation, country-to-scheme fallback, and unit-of-
  measure mapping and overlays them on the party / line data; those are
  Peppol concerns, not shared semantics.

The model is pure data: no Odoo imports, unit-testable with stub records.
Monetary values are carried as plain floats here to match the inputs the
current serializers expect; the serializers own their rounding.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Address:
    street: str = ''
    street2: str = ''
    city: str = ''
    zip: str = ''
    country_code: str = ''


@dataclass(frozen=True)
class Party:
    name: str = ''
    vat: str = ''                 # BT-31 / BT-48
    legal_id: str = ''            # SIRET (FR), company registry, BT-30 / BT-47
    electronic_id: str = ''       # Peppol endpoint id, BT-34 / BT-49
    electronic_scheme: str = ''   # Peppol endpoint scheme id
    email: str = ''
    country_code: str = ''
    address: Address = field(default_factory=Address)


@dataclass(frozen=True)
class Line:
    name: str = ''
    quantity: float = 0.0
    unit_code: str = 'C62'        # UN/ECE Rec 20, C62 = one / piece
    price_unit: float = 0.0         # BT-146 net item price
    gross_price_unit: float = 0.0   # BT-148 before item-price discount
    line_net: float = 0.0         # BT-131
    tax_rate: float = 0.0         # BT-152
    tax_category: str = 'S'       # UNTDID 5305
    tax_exemption_reason: str = ''  # BT-120, required for E/AE/G/O


@dataclass(frozen=True)
class TaxSubtotal:
    category: str = 'S'
    rate: float = 0.0
    taxable_amount: float = 0.0   # BT-116
    tax_amount: float = 0.0       # BT-117
    exemption_reason: str = ''    # BT-120, required for E/AE/G/O


@dataclass(frozen=True)
class Totals:
    sum_line_net: float = 0.0     # BT-106
    allowance_total: float = 0.0  # BT-107
    charge_total: float = 0.0     # BT-108
    tax_basis: float = 0.0        # BT-109
    tax_amount: float = 0.0       # BT-110
    tax_currency_amount: float = 0.0  # BT-111
    grand_total: float = 0.0      # BT-112
    prepaid_amount: float = 0.0   # BT-113
    rounding_amount: float = 0.0  # BT-114
    payable_amount: float = 0.0   # BT-115


@dataclass(frozen=True)
class InvoiceModel:
    invoice_number: str = ''
    invoice_date: object = None
    due_date: object = None
    move_type: str = 'out_invoice'
    currency: str = 'EUR'
    tax_currency: str = ''
    note: str = ''
    # References. buyer_reference is explicit BT-10 evidence only;
    # order_reference carries a purchase order / origin reference;
    # contract_reference is BT-12.
    buyer_reference: str = ''
    contract_reference: str = ''
    order_reference: str = ''
    seller: Party = field(default_factory=Party)
    buyer: Party = field(default_factory=Party)
    lines: tuple = ()
    tax_breakdown: tuple = ()
    totals: Totals = field(default_factory=Totals)

    # ---- adapters ----

    def to_cii_input(self):
        """Dict for the Factur-X / CII builder. Reproduces the legacy
        map_move() output shape exactly."""
        def _party(p):
            if not (p.country_code or p.address.country_code):
                raise ValueError(
                    "EN 16931 party country is required; refusing to "
                    "fabricate a country code."
                )
            return {
                'name': p.name,
                'siret': p.legal_id,
                'vat': p.vat,
                'contact_email': p.email,
                'address': {
                    'street': p.address.street,
                    'street2': p.address.street2,
                    'zip': p.address.zip,
                    'city': p.address.city,
                    'country_code': p.address.country_code,
                },
            }
        return {
            'invoice_number': self.invoice_number,
            'invoice_date': self.invoice_date,
            'invoice_date_due': self.due_date,
            'move_type': self.move_type,
            'currency': self.currency,
            'buyer_reference': self.buyer_reference,
            'contract_reference': self.contract_reference,
            'purchase_order_reference': self.order_reference,
            'seller': _party(self.seller),
            'buyer': _party(self.buyer),
            'lines': [
                {
                    'name': ln.name,
                    'quantity': ln.quantity,
                    # The historical CII mapping hardcodes C62; the UBL
                    # adapter uses the unit-of-measure derived code.
                    'unit_code': 'C62',
                    'price_unit': ln.price_unit,
                    'gross_price_unit': ln.gross_price_unit,
                    'tax_rate': ln.tax_rate,
                    'tax_category': ln.tax_category,
                    'line_total': ln.line_net,
                }
                for ln in self.lines
            ],
            'totals': {
                'sum_line_net': self.totals.sum_line_net,
                'allowance_total': self.totals.allowance_total,
                'charge_total': self.totals.charge_total,
                'tax_basis': self.totals.tax_basis,
                'tax_amount': self.totals.tax_amount,
                'tax_currency_amount': self.totals.tax_currency_amount,
                'invoice_total': self.totals.grand_total,
                'prepaid_amount': self.totals.prepaid_amount,
                'rounding_amount': self.totals.rounding_amount,
                'payable_amount': self.totals.payable_amount,
            },
            'tax_breakdown': [
                {'rate': t.rate, 'category': t.category,
                 'basis': t.taxable_amount, 'amount': t.tax_amount}
                for t in self.tax_breakdown
            ],
        }

    def to_ubl_inputs(self):
        """Kwargs for the Peppol UBL generator make_invoice_payload().

        Format-neutral base. The Peppol module overlays participant-id
        validation and country-to-scheme fallback on the party data
        before serialising; that logic stays in the Peppol layer.
        """
        def _party(p):
            return {
                'name': p.name,
                # Participant identifiers are transport-profile evidence.
                # Never fabricate one from VAT, email or display name.
                'endpoint_id': p.electronic_id,
                'endpoint_scheme': p.electronic_scheme,
                'country_code': p.country_code or p.address.country_code,
                'vat_id': p.vat,
                'legal_id': p.legal_id or p.vat,
                'address': {
                    'street': p.address.street,
                    'city': p.address.city,
                    'postcode': p.address.zip,
                    'country': p.country_code or p.address.country_code,
                },
            }
        document_type = (
            'credit_note' if self.move_type in ('out_refund', 'in_refund')
            else 'invoice'
        )
        lines = [
            {
                'id': i,
                'description': ln.name,
                'quantity': ln.quantity,
                'unit_code': ln.unit_code,
                'unit_price': ln.price_unit,
                'gross_unit_price': ln.gross_price_unit,
                'line_total': ln.line_net,
                'tax_category_code': ln.tax_category,
                'tax_rate_pct': ln.tax_rate,
            }
            for i, ln in enumerate(self.lines, start=1)
        ]
        tax_categories = [
            {
                'category_code': t.category,
                'rate_pct': t.rate,
                'taxable_amount': t.taxable_amount,
                'tax_amount': t.tax_amount,
                'exemption_reason': t.exemption_reason,
            }
            for t in self.tax_breakdown
        ]
        return {
            'invoice_number': self.invoice_number,
            'issue_date': self.invoice_date,
            'due_date': self.due_date,
            'currency_code': self.currency,
            'tax_currency_code': self.tax_currency,
            'supplier': _party(self.seller),
            'customer': _party(self.buyer),
            'lines': lines,
            'tax_categories': tax_categories,
            'document_type': document_type,
            'note': self.note,
            'buyer_reference': self.buyer_reference,
            'order_reference': self.order_reference,
            'contract_reference': self.contract_reference,
            'monetary_totals': {
                'line_extension': self.totals.sum_line_net,
                'allowance_total': self.totals.allowance_total,
                'charge_total': self.totals.charge_total,
                'tax_exclusive': self.totals.tax_basis,
                'tax_inclusive': self.totals.grand_total,
                'prepaid': self.totals.prepaid_amount,
                'rounding': self.totals.rounding_amount,
                'payable': self.totals.payable_amount,
                'tax_currency_amount': self.totals.tax_currency_amount,
            },
        }
