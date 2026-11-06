# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move -> canonical EN 16931 InvoiceModel.

The single place that reads Odoo fields for outbound e-invoicing. Lives
in tools so it can be unit-tested with stub records; the Odoo-facing
adapters pass a real recordset.

Optional, module-specific fields (the French SIRET, the Peppol endpoint
id and scheme, the stock procurement references) are read defensively
with getattr so the mapper works whether or not those modules are
installed. Standard account.move / res.partner fields are read directly.

Line unit codes use Odoo's stable UN/ECE resolver. Translated display names
are never parsed as identifiers.
"""

import inspect

from .model import (
    Address,
    InvoiceModel,
    Line,
    Party,
    TaxSubtotal,
    Totals,
)


# EN 16931 type codes are resolved by the serializers from move_type; the
# mapper carries move_type unchanged.

_PRODUCT_DISPLAY_TYPES = (False, None, '', 'product')
_PERCENTAGE_TAX_TYPES = frozenset({'percent', 'division'})

# Same stable external identifiers used by Odoo's UBL/CII formatter across
# 16-19. Keep this local: eh_edi_core must not depend on account_edi_ubl_cii.
_UOM_XMLID_TO_UNECE = {
    'uom.product_uom_unit': 'C62',
    'uom.product_uom_dozen': 'DZN',
    'uom.product_uom_kgm': 'KGM',
    'uom.product_uom_gram': 'GRM',
    'uom.product_uom_day': 'DAY',
    'uom.product_uom_hour': 'HUR',
    'uom.product_uom_minute': 'MIN',
    'uom.product_uom_ton': 'TNE',
    'uom.product_uom_meter': 'MTR',
    'uom.product_uom_km': 'KMT',
    'uom.product_uom_cm': 'CMT',
    'uom.product_uom_litre': 'LTR',
    'uom.product_uom_cubic_meter': 'MTQ',
    'uom.product_uom_lb': 'LBR',
    'uom.product_uom_oz': 'ONZ',
    'uom.product_uom_inch': 'INH',
    'uom.product_uom_foot': 'FOT',
    'uom.product_uom_mile': 'SMI',
    'uom.product_uom_floz': 'OZA',
    'uom.product_uom_qt': 'QTL',
    'uom.product_uom_gal': 'GLL',
    'uom.product_uom_cubic_inch': 'INQ',
    'uom.product_uom_cubic_foot': 'FTQ',
    'uom.product_uom_square_meter': 'MTK',
    'uom.product_uom_square_foot': 'FTK',
    'uom.product_uom_yard': 'YRD',
    'uom.product_uom_millimeter': 'MMT',
    'uom.product_uom_kwh': 'KWH',
}


class En16931MappingError(ValueError):
    """Raised when posted tax semantics cannot be represented truthfully."""


def _safe_attr(record, attr, default=None):
    """Read an attribute that might not exist on a stub or when the
    declaring module is not installed."""
    return getattr(record, attr, default)


def _is_product_line(line):
    """True for an actual product line across supported Odoo versions.

    Odoo 16-19 registries can expose either ``'product'`` or a false value
    depending on series and record origin. Section and note values stay
    excluded.
    """
    return _safe_attr(line, 'display_type', False) in _PRODUCT_DISPLAY_TYPES


def _tax_category(tax):
    """UNTDID 5305 category for an account.tax.

    Prefers the explicit EN 16931 category set on the tax
    (eh_edi_tax_category, covering S/Z/E/AE/G/O), then the French
    e-reporting override (eh_fr_einv_category), then falls back to the
    historical rate rule (zero rate -> 'Z', otherwise 'S'). Read
    defensively with getattr so this works whether or not the Peppol /
    FR modules are installed and with stub records in unit tests."""
    if not tax:
        return 'S'
    explicit = _safe_attr(tax, 'eh_edi_tax_category')
    if explicit:
        return explicit
    override = _safe_attr(tax, 'eh_fr_einv_category')
    if override:
        return override
    if (tax.amount or 0.0) == 0:
        return 'Z'
    return 'S'


def _leaf_taxes(taxes):
    """Yield effective taxes, recursively expanding Odoo group taxes."""
    for tax in taxes or ():
        amount_type = _safe_attr(tax, 'amount_type', 'percent') or 'percent'
        if amount_type == 'group':
            children = _safe_attr(tax, 'children_tax_ids', ()) or ()
            if not children:
                raise En16931MappingError(
                    "An empty group tax cannot be mapped to EN 16931."
                )
            yield from _leaf_taxes(children)
        else:
            yield tax


def _percentage_tax_profile(tax):
    """Return (category, rate, reason), or refuse non-VAT semantics.

    EN 16931 BT-151/BT-152 describes one VAT category and percentage per
    invoice line. Odoo fixed taxes and negative withholding taxes are not a
    percentage VAT rate; silently serialising their ``amount`` as BT-152 makes
    a formally plausible but false invoice. The canonical model has no
    allowance/charge or withholding structure yet, so those cases fail closed.
    """
    amount_type = _safe_attr(tax, 'amount_type', 'percent') or 'percent'
    if amount_type not in _PERCENTAGE_TAX_TYPES:
        raise En16931MappingError(
            "Tax %r uses amount type %r, which is not an EN 16931 VAT "
            "percentage. Configure a percentage/division VAT tax or add a "
            "format-specific allowance/withholding mapping."
            % (_safe_attr(tax, 'name', ''), amount_type)
        )
    rate = float(_safe_attr(tax, 'amount', 0.0) or 0.0)
    if rate < 0:
        raise En16931MappingError(
            "Negative tax %r is a withholding/retention, not an EN 16931 "
            "VAT category rate; it cannot be emitted as category S."
            % (_safe_attr(tax, 'name', ''),)
        )
    category = _tax_category(tax)
    reason = _tax_exemption_reason(tax)
    if category in ('E', 'AE', 'G', 'O') and not reason:
        raise En16931MappingError(
            "Tax %r uses EN 16931 category %s but has no exemption reason."
            % (_safe_attr(tax, 'name', ''), category)
        )
    return category, rate, reason


def _line_tax_profile(line):
    leaves = tuple(_leaf_taxes(line.tax_ids or ()))
    if not leaves:
        raise En16931MappingError(
            "Invoice line %r has no tax classification. EN 16931 cannot "
            "infer whether an untaxed line is exempt (E), zero-rated (Z), "
            "outside scope (O), or reverse charged (AE); attach an explicit "
            "configured tax."
            % (line.name or '',)
        )
    if len(leaves) != 1:
        raise En16931MappingError(
            "Invoice line %r carries %d effective taxes. EN 16931 BT-151/"
            "BT-152 permits one VAT category and rate per line; equal-rate "
            "tax components cannot be collapsed without changing the "
            "effective rate. Split the line or correct its tax setup."
            % (line.name or '', len(leaves))
        )
    profiles = {_percentage_tax_profile(tax) for tax in leaves}
    if len(profiles) != 1:
        raise En16931MappingError(
            "Invoice line %r carries multiple EN 16931 VAT category/rate "
            "profiles. BT-151/BT-152 permits one profile per line; split the "
            "line or correct its tax configuration."
            % (line.name or '',)
        )
    return next(iter(profiles))


def _tax_exemption_reason(tax):
    """EN 16931 exemption reason (BT-120) for an account.tax, or ''.

    Only categories that charge no tax (E, AE, G, O) carry a reason; the
    field is read defensively so stubs and installs without the Peppol
    module still work."""
    if not tax:
        return ''
    return _safe_attr(tax, 'eh_edi_tax_exemption_reason') or ''


def _party(partner, siret_field=None, fallback_company=None):
    """Build a Party from a res.partner. siret_field + fallback_company
    let the seller read its SIRET off res.company when that is where it
    lives."""
    if not partner:
        raise En16931MappingError("EN 16931 party is missing.")
    country_record = _safe_attr(partner, 'country_id')
    country = _safe_attr(country_record, 'code', '') or ''
    if not country:
        raise En16931MappingError(
            "EN 16931 party %r has no country. Configure the legal party "
            "country instead of fabricating one in the export."
            % (_safe_attr(partner, 'name', '') or '',)
        )
    legal_id = ''
    if siret_field and fallback_company is not None:
        legal_id = _safe_attr(fallback_company, siret_field) or ''
    if not legal_id:
        legal_id = (
            _safe_attr(partner, 'fr_siret')
            or _safe_attr(partner, 'siret')
            or ''
        )
    return Party(
        name=partner.name or '',
        vat=partner.vat or '',
        legal_id=legal_id,
        electronic_id=_safe_attr(partner, 'eh_peppol_endpoint_id') or '',
        electronic_scheme=(
            _safe_attr(partner, 'eh_peppol_endpoint_scheme') or ''
        ),
        email=partner.email or '',
        country_code=country or '',
        address=Address(
            street=partner.street or '',
            street2=partner.street2 or '',
            city=partner.city or '',
            zip=partner.zip or '',
            country_code=country or '',
        ),
    )


def _unit_code(line):
    """UN/ECE Recommendation 20 unit code from the line unit of measure.

    Odoo's standard formatter uses the UoM external identifier. That evidence
    is stable across supported series and independent of language. A line with
    no UoM maps to ``C62`` (one/piece); an unmapped real UoM fails closed.
    """
    uom = _safe_attr(line, 'product_uom_id')
    if not uom:
        return 'C62'
    explicit = _safe_attr(uom, 'unece_code')
    if explicit:
        return explicit
    external_id_resolver = _safe_attr(uom, 'get_external_id')
    if callable(external_id_resolver):
        external_ids = external_id_resolver() or {}
        record_id = _safe_attr(uom, 'id')
        xmlid = external_ids.get(record_id)
        code = _UOM_XMLID_TO_UNECE.get(xmlid)
        if code:
            return code
    raise En16931MappingError(
        "Unit of measure %r has no deterministic UN/ECE Recommendation 20 "
        "code. Configure a supported UoM; translated names are not parsed."
        % (_safe_attr(uom, 'display_name', _safe_attr(uom, 'name', '')),)
    )


def _line_prices(line):
    """Return truthful (net BT-146, gross BT-148) unit prices.

    Odoo ``price_unit`` is before discount and may include VAT. The posted
    document-currency ``price_subtotal`` is tax-exclusive and after discount,
    so its per-quantity value is the only stable BT-146 source. Gross BT-148
    is retained only when taxes are price-excluded; otherwise Odoo's gross
    value includes VAT and cannot truthfully be labelled item gross price.
    """
    quantity = float(_safe_attr(line, 'quantity', 0.0) or 0.0)
    line_net = float(_safe_attr(line, 'price_subtotal', 0.0) or 0.0)
    if not quantity:
        if line_net:
            raise En16931MappingError(
                "Invoice line %r has a non-zero net amount with zero "
                "quantity; BT-146 cannot be derived."
                % (_safe_attr(line, 'name', '') or '',)
            )
        return 0.0, 0.0
    net_price = line_net / quantity
    taxes = tuple(_leaf_taxes(_safe_attr(line, 'tax_ids', ()) or ()))
    price_included = any(
        bool(_safe_attr(tax, 'price_include', False)) for tax in taxes
    )
    gross_price = 0.0 if price_included else float(
        _safe_attr(line, 'price_unit', 0.0) or 0.0
    )
    return net_price, gross_price


def _line(line):
    category, rate, reason = _line_tax_profile(line)
    net_price, gross_price = _line_prices(line)
    return Line(
        name=line.name or (line.product_id.name if line.product_id else ''),
        quantity=line.quantity or 0.0,
        unit_code=_unit_code(line),
        price_unit=net_price,
        gross_price_unit=gross_price,
        line_net=line.price_subtotal or 0.0,
        tax_rate=rate,
        tax_category=category,
        tax_exemption_reason=reason,
    )


def _tax_breakdown(product_lines):
    buckets = {}
    order = []
    for line in product_lines:
        for tax in _leaf_taxes(line.tax_ids or ()):
            category, rate, reason = _percentage_tax_profile(tax)
            key = (rate, category)
            if key not in buckets:
                buckets[key] = {
                    'basis': 0.0, 'amount': 0.0,
                    'reason': reason,
                }
                order.append(key)
            buckets[key]['basis'] += line.price_subtotal or 0.0
            buckets[key]['amount'] += (
                (line.price_subtotal or 0.0) * rate / 100.0
            )
    return tuple(
        TaxSubtotal(
            category=key[1], rate=key[0],
            taxable_amount=buckets[key]['basis'],
            tax_amount=buckets[key]['amount'],
            exemption_reason=buckets[key]['reason'],
        )
        for key in order
    )


def _booked_tax_breakdown(move, product_lines):
    """Return document-currency tax buckets tied to Odoo tax details.

    ``account.move.line.tax_base_amount`` is company currency while
    ``amount_currency`` is document currency.  Mixing those fields produces
    an invalid EN 16931 bucket on every foreign-currency invoice.  Odoo's
    canonical aggregator exposes both ``base_amount_currency`` and
    ``tax_amount_currency`` and reconciles manual/per-line rounding back to
    posted tax lines.  Pure duck-typed unit-test stubs do not expose that ORM
    API, so they retain the deterministic arithmetic fallback.
    """
    aggregate = _safe_attr(move, '_prepare_invoice_aggregated_taxes')
    if not callable(aggregate):
        return _tax_breakdown(product_lines)

    def grouping_key(_base_line, tax_data):
        tax = tax_data.get('tax')
        if not tax:
            repartition = tax_data.get('tax_repartition_line')
            tax = repartition and repartition.tax_id
        if not tax:
            raise En16931MappingError(
                "Odoo returned booked tax detail without its source tax."
            )
        category, rate, reason = _percentage_tax_profile(tax)
        return {
            'eh_category': category,
            'eh_rate': rate,
            'eh_reason': reason,
        }

    kwargs = {'grouping_key_generator': grouping_key}
    if 'round_from_tax_lines' in inspect.signature(aggregate).parameters:
        kwargs['round_from_tax_lines'] = True
    details = aggregate(**kwargs)
    return tuple(
        TaxSubtotal(
            category=key['eh_category'],
            rate=key['eh_rate'],
            taxable_amount=abs(values.get('base_amount_currency') or 0.0),
            tax_amount=abs(values.get('tax_amount_currency') or 0.0),
            exemption_reason=key.get('eh_reason') or '',
        )
        for key, values in details.get('tax_details', {}).items()
    )


def map_move(move):
    """Convert an account.move record to a canonical InvoiceModel."""
    company = move.company_id
    partner = move.partner_id
    buyer = _safe_attr(partner, 'commercial_partner_id') or partner
    product_lines = [
        line for line in move.invoice_line_ids if _is_product_line(line)
    ]
    buyer_reference = _safe_attr(move, 'buyer_reference') or ''
    currency_record = _safe_attr(move, 'currency_id')
    currency_code = _safe_attr(currency_record, 'name', '') or ''
    if not currency_code:
        raise En16931MappingError(
            "Invoice %r has no document currency."
            % (_safe_attr(move, 'name', '') or '',)
        )
    sum_line_net = sum(
        float(_safe_attr(line, 'price_subtotal', 0.0) or 0.0)
        for line in product_lines
    )
    tax_basis = float(_safe_attr(move, 'amount_untaxed', 0.0) or 0.0)
    round_currency = _safe_attr(currency_record, 'round')
    difference = tax_basis - sum_line_net
    difference_is_zero = (
        bool(_safe_attr(currency_record, 'is_zero')(difference))
        if callable(_safe_attr(currency_record, 'is_zero'))
        else abs(difference) < 0.005
    )
    if not difference_is_zero:
        raise En16931MappingError(
            "Invoice %r has a document-level allowance/charge difference "
            "of %.6f between BT-106 and BT-109, but no reviewed EN 16931 "
            "allowance/charge evidence. Refusing to guess BT-92/BT-99."
            % (_safe_attr(move, 'name', '') or '', difference)
        )
    tax_amount = float(_safe_attr(move, 'amount_tax', 0.0) or 0.0)
    tax_inclusive = tax_basis + tax_amount
    amount_total = float(_safe_attr(move, 'amount_total', 0.0) or 0.0)
    rounding_amount = amount_total - tax_inclusive
    amount_residual = _safe_attr(move, 'amount_residual', None)
    payable_amount = (
        amount_total if amount_residual is None else float(amount_residual)
    )
    prepaid_amount = amount_total - payable_amount
    if callable(round_currency):
        sum_line_net = round_currency(sum_line_net)
        tax_basis = round_currency(tax_basis)
        tax_amount = round_currency(tax_amount)
        tax_inclusive = round_currency(tax_inclusive)
        rounding_amount = round_currency(rounding_amount)
        prepaid_amount = round_currency(prepaid_amount)
        payable_amount = round_currency(payable_amount)
    company_currency = _safe_attr(company, 'currency_id')
    company_currency_code = _safe_attr(company_currency, 'name', '') or ''
    tax_currency = (
        company_currency_code
        if company_currency_code and company_currency_code != currency_code
        else ''
    )
    tax_currency_amount = (
        abs(float(_safe_attr(move, 'amount_tax_signed', 0.0) or 0.0))
        if tax_currency else 0.0
    )
    return InvoiceModel(
        invoice_number=move.name or '',
        invoice_date=move.invoice_date,
        due_date=_safe_attr(move, 'invoice_date_due'),
        move_type=move.move_type,
        currency=currency_code,
        tax_currency=tax_currency,
        note=_safe_attr(move, 'narration') or '',
        buyer_reference=buyer_reference,
        contract_reference=_safe_attr(move, 'contract_reference') or '',
        # Purchase order reference only, to reproduce the CII mapping
        # exactly. The Peppol layer maps its order reference from
        # invoice_origin when that format adopts the model.
        order_reference=_safe_attr(move, 'purchase_order_reference') or '',
        seller=_party(
            company.partner_id, siret_field='fr_siret',
            fallback_company=company,
        ),
        buyer=_party(buyer),
        lines=tuple(_line(line) for line in product_lines),
        tax_breakdown=_booked_tax_breakdown(move, product_lines),
        totals=Totals(
            sum_line_net=sum_line_net,
            allowance_total=0.0,
            charge_total=0.0,
            tax_basis=tax_basis,
            tax_amount=tax_amount,
            tax_currency_amount=tax_currency_amount,
            grand_total=tax_inclusive,
            prepaid_amount=prepaid_amount,
            rounding_amount=rounding_amount,
            payable_amount=payable_amount,
        ),
    )
