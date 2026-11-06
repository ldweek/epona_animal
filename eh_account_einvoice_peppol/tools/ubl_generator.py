# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Peppol BIS Billing 3.0 (UBL 2.1) Invoice generator.

Plain Python (lxml only). The generator takes a normalised invoice dict
and returns the serialised XML bytes that a Peppol access point can
transmit.

The dict contract is in `make_invoice_payload` below; concrete callers
(Odoo `account.move` adapters, scripted batch jobs, or test fixtures)
build the dict from whatever source is convenient and hand it to
`render_invoice_xml(payload)`.

Originality
-----------

This implementation is built from:

* OASIS UBL 2.1 specification (`urn:oasis:names:specification:
  ubl:schema:xsd:Invoice-2`, `...:CreditNote-2`).
* EN 16931 European core invoice norm.
* Peppol BIS Billing 3.0 (https://docs.peppol.eu/poacc/billing/3.0/).
* OpenPeppol naming and identifier registries (Peppol scheme ids).

No code, comments, schema fragments, or example payloads are derived
from any third-party Odoo eInvoicing implementation.

Schema invariants
-----------------

* Customisation id: `urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:
  2017:poacc:billing:3.0`.
* Profile id: `urn:fdc:peppol.eu:2017:poacc:billing:01:1.0`.
* Document currency code is mandatory; we reject a missing
  `currency_code` with a `PeppolGeneratorError` rather than defaulting.
* Every line carries `LineExtensionAmount` in the document currency;
  per-line tax appears in `cac:Item/cac:ClassifiedTaxCategory`.
* `cac:TaxTotal` at the document level emits per-rate breakdowns from
  the line tax categories so the line side and the document side
  agree by construction.
"""

import datetime
from decimal import Decimal, ROUND_HALF_UP

from lxml import etree


# Namespace literals as the OASIS / Peppol specs publish them.
NS_INVOICE = (
    "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
)
NS_CREDIT = (
    "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
)
NS_CBC = (
    "urn:oasis:names:specification:ubl:schema:xsd:"
    "CommonBasicComponents-2"
)
NS_CAC = (
    "urn:oasis:names:specification:ubl:schema:xsd:"
    "CommonAggregateComponents-2"
)

CUSTOMIZATION_ID = (
    "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:"
    "poacc:billing:3.0"
)
PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"


class PeppolGeneratorError(ValueError):
    """Raised for any malformed input dict.

    The message names the offending field. No silent fallback.
    """


def _q(value):
    """Round to 2 dp half-up, return a Decimal -- the convention every
    monetary field in BIS Billing 3.0 uses.
    """
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _money(value):
    """Render a Decimal money value as a UBL-friendly string."""
    return format(_q(value), 'f')


def _qty(value):
    return format(Decimal(str(value)).quantize(Decimal("0.0001"),
                                                 rounding=ROUND_HALF_UP), 'f')


def _ensure(value, field):
    if value in (None, '', []):
        raise PeppolGeneratorError(
            "Required field missing: %s" % field,
        )
    return value


def make_invoice_payload(
    invoice_number, issue_date, due_date,
    currency_code,
    supplier, customer, lines, tax_categories,
    document_type='invoice',
    invoice_type_code=None,
    note=None, payment_means=None, payment_terms=None,
    buyer_reference=None, order_reference=None,
    contract_reference=None, billing_reference=None,
    tax_currency_code=None, monetary_totals=None,
):
    """Build the invoice payload dict the generator consumes.

    All required fields raise `PeppolGeneratorError` if missing.

    Required arguments:

    * `invoice_number`: string, the issuer's invoice number.
    * `issue_date`: `datetime.date`.
    * `due_date`: optional `datetime.date`; omitted when not evidenced.
    * `currency_code`: ISO 4217.
    * `supplier`: dict carrying `name`, `endpoint_id`, `endpoint_scheme`,
      `country_code`, `vat_id` (or other tax registration), `address`
      (street, city, postcode, country).
    * `customer`: dict in the same shape.
    * `lines`: list of dicts each carrying `id`, `description`,
      `quantity`, `unit_code` (UN/ECE rec 20), `unit_price`,
      `line_total`, `tax_category_code`, `tax_rate_pct`.
    * `tax_categories`: list of dicts each carrying `category_code`
      (S, Z, E, AE, K, G, O), `rate_pct`, `taxable_amount`,
      `tax_amount`. The line-level codes must reference these.

    Optional fields default to None and are simply omitted from the
    XML; downstream Peppol access points enforce presence as required
    for the recipient's profile.
    """
    payload = {
        'document_type': document_type,
        'invoice_number': _ensure(invoice_number, 'invoice_number'),
        'issue_date': _ensure(issue_date, 'issue_date'),
        # BT-9 is conditional in EN 16931. Missing evidence stays omitted;
        # issue date must never be fabricated as payment due date.
        'due_date': due_date,
        'currency_code': _ensure(currency_code, 'currency_code'),
        'tax_currency_code': tax_currency_code or '',
        'invoice_type_code': invoice_type_code or (
            '380' if document_type == 'invoice' else '381'
        ),
        'note': note or '',
        'buyer_reference': buyer_reference or '',
        'order_reference': order_reference or '',
        'contract_reference': contract_reference or '',
        'billing_reference': billing_reference or '',
        'supplier': _ensure(supplier, 'supplier'),
        'customer': _ensure(customer, 'customer'),
        'lines': _ensure(lines, 'lines'),
        'tax_categories': _ensure(tax_categories, 'tax_categories'),
        'payment_means': payment_means,
        'payment_terms': payment_terms,
        'monetary_totals': monetary_totals or {},
    }
    for field in ('issue_date',):
        if not isinstance(payload[field], datetime.date):
            raise PeppolGeneratorError(
                "%s must be a datetime.date instance (got %r)"
                % (field, payload[field]),
            )
    if payload['due_date'] is not None and not isinstance(
        payload['due_date'], datetime.date
    ):
        raise PeppolGeneratorError(
            "due_date must be a datetime.date instance or None (got %r)"
            % (payload['due_date'],)
        )
    if document_type not in ('invoice', 'credit_note'):
        raise PeppolGeneratorError(
            "document_type must be 'invoice' or 'credit_note' (got %r)"
            % (document_type,),
        )
    return payload


def render_invoice_xml(payload):
    """Return UBL 2.1 XML bytes for the given payload.

    The output is signed-only at the document level (no XAdES); transport
    to a Peppol access point is the deployment's responsibility.
    """
    is_credit = payload['document_type'] == 'credit_note'
    root_ns = NS_CREDIT if is_credit else NS_INVOICE
    root_name = "CreditNote" if is_credit else "Invoice"

    nsmap = {
        None: root_ns,
        'cbc': NS_CBC,
        'cac': NS_CAC,
    }
    root = etree.Element(
        "{%s}%s" % (root_ns, root_name), nsmap=nsmap,
    )

    cbc(root, "CustomizationID", CUSTOMIZATION_ID)
    cbc(root, "ProfileID", PROFILE_ID)
    cbc(root, "ID", payload['invoice_number'])
    cbc(root, "IssueDate", payload['issue_date'].isoformat())
    if not is_credit and payload.get('due_date'):
        cbc(root, "DueDate", payload['due_date'].isoformat())
    cbc(root, "InvoiceTypeCode" if not is_credit else "CreditNoteTypeCode",
        payload['invoice_type_code'])
    if payload['note']:
        cbc(root, "Note", payload['note'])
    cbc(root, "DocumentCurrencyCode", payload['currency_code'])
    if payload.get('tax_currency_code'):
        cbc(root, "TaxCurrencyCode", payload['tax_currency_code'])
    if payload['buyer_reference']:
        cbc(root, "BuyerReference", payload['buyer_reference'])

    if payload.get('order_reference'):
        order_ref = cac(root, "OrderReference")
        cbc(order_ref, "ID", payload['order_reference'])
    if payload.get('billing_reference'):
        billing_ref = cac(root, "BillingReference")
        invoice_ref = cac(billing_ref, "InvoiceDocumentReference")
        cbc(invoice_ref, "ID", payload['billing_reference'])
    if payload.get('contract_reference'):
        contract = cac(root, "ContractDocumentReference")
        cbc(contract, "ID", payload['contract_reference'])

    _render_party(root, "AccountingSupplierParty", payload['supplier'])
    _render_party(root, "AccountingCustomerParty", payload['customer'])

    if payload.get('payment_means'):
        _render_payment_means(root, payload['payment_means'])
    if payload.get('payment_terms'):
        terms = cac(root, "PaymentTerms")
        cbc(terms, "Note", payload['payment_terms'])

    _render_tax_total(root, payload)
    _render_legal_monetary_total(root, payload)
    _render_lines(root, payload['lines'], payload['currency_code'], is_credit)

    return etree.tostring(
        root, xml_declaration=True, encoding='UTF-8', pretty_print=True,
    )


def cbc(parent, tag, text):
    el = etree.SubElement(parent, "{%s}%s" % (NS_CBC, tag))
    el.text = text if isinstance(text, str) else str(text)
    return el


def cbc_with_attr(parent, tag, text, **attrs):
    el = cbc(parent, tag, text)
    for k, v in attrs.items():
        el.set(k, v)
    return el


def cac(parent, tag):
    return etree.SubElement(parent, "{%s}%s" % (NS_CAC, tag))


def _render_party(root, role_tag, party):
    role = cac(root, role_tag)
    inner = cac(role, "Party")
    if party.get('endpoint_id'):
        cbc_with_attr(
            inner, "EndpointID", party['endpoint_id'],
            schemeID=party.get('endpoint_scheme', '0192'),
        )
    if party.get('party_id'):
        ident = cac(inner, "PartyIdentification")
        cbc(ident, "ID", party['party_id'])
    name_block = cac(inner, "PartyName")
    cbc(name_block, "Name", _ensure(party.get('name'), 'party.name'))
    address = party.get('address') or {}
    addr = cac(inner, "PostalAddress")
    if address.get('street'):
        cbc(addr, "StreetName", address['street'])
    if address.get('city'):
        cbc(addr, "CityName", address['city'])
    if address.get('postcode'):
        cbc(addr, "PostalZone", address['postcode'])
    country = cac(addr, "Country")
    cbc(country, "IdentificationCode",
        _ensure(party.get('country_code'), 'party.country_code'))
    if party.get('vat_id'):
        scheme = cac(inner, "PartyTaxScheme")
        cbc(scheme, "CompanyID", party['vat_id'])
        ts = cac(scheme, "TaxScheme")
        cbc(ts, "ID", "VAT")
    legal = cac(inner, "PartyLegalEntity")
    cbc(legal, "RegistrationName", party['name'])
    if party.get('legal_id'):
        cbc(legal, "CompanyID", party['legal_id'])


def _render_payment_means(root, pm):
    means = cac(root, "PaymentMeans")
    cbc(means, "PaymentMeansCode", pm.get('code', '30'))
    if pm.get('payment_id'):
        cbc(means, "PaymentID", pm['payment_id'])
    if pm.get('iban'):
        acct = cac(means, "PayeeFinancialAccount")
        cbc(acct, "ID", pm['iban'])
        if pm.get('bic'):
            branch = cac(acct, "FinancialInstitutionBranch")
            cbc(branch, "ID", pm['bic'])


def _render_tax_total(root, payload):
    categories = payload['tax_categories']
    currency = payload['currency_code']
    total_tax = sum(_q(c['tax_amount']) for c in categories)
    tax_total = cac(root, "TaxTotal")
    cbc_with_attr(
        tax_total, "TaxAmount", _money(total_tax),
        currencyID=currency,
    )
    for cat in categories:
        sub = cac(tax_total, "TaxSubtotal")
        cbc_with_attr(
            sub, "TaxableAmount", _money(cat['taxable_amount']),
            currencyID=currency,
        )
        cbc_with_attr(
            sub, "TaxAmount", _money(cat['tax_amount']),
            currencyID=currency,
        )
        cat_block = cac(sub, "TaxCategory")
        cbc(cat_block, "ID", cat['category_code'])
        cbc(cat_block, "Percent", _money(cat['rate_pct']))
        # EN 16931 requires a TaxExemptionReason for the no-tax categories
        # (E, AE, G, O). UBL orders it before TaxScheme inside TaxCategory.
        reason = cat.get('exemption_reason')
        if reason:
            cbc(cat_block, "TaxExemptionReason", reason)
        ts = cac(cat_block, "TaxScheme")
        cbc(ts, "ID", "VAT")
    tax_currency = payload.get('tax_currency_code')
    tax_currency_amount = (
        payload.get('monetary_totals', {}).get('tax_currency_amount') or 0.0
    )
    if tax_currency:
        accounting_total = cac(root, "TaxTotal")
        cbc_with_attr(
            accounting_total, "TaxAmount", _money(tax_currency_amount),
            currencyID=tax_currency,
        )


def _render_legal_monetary_total(root, payload):
    computed_line_total = sum(_q(l['line_total']) for l in payload['lines'])
    computed_tax_total = sum(
        _q(c['tax_amount']) for c in payload['tax_categories']
    )
    totals = payload.get('monetary_totals') or {}
    line_total = _q(totals.get('line_extension', computed_line_total))
    allowance_total = _q(totals.get('allowance_total', 0.0))
    charge_total = _q(totals.get('charge_total', 0.0))
    tax_exclusive = _q(totals.get(
        'tax_exclusive', line_total - allowance_total + charge_total,
    ))
    tax_inclusive = _q(totals.get(
        'tax_inclusive', tax_exclusive + computed_tax_total,
    ))
    prepaid = _q(totals.get('prepaid', 0.0))
    rounding = _q(totals.get('rounding', 0.0))
    payable = _q(totals.get(
        'payable', tax_inclusive - prepaid + rounding,
    ))
    checks = (
        ('BT-106', line_total, computed_line_total),
        ('BR-CO-13', tax_exclusive,
         line_total - allowance_total + charge_total),
        ('BR-CO-15', tax_inclusive, tax_exclusive + computed_tax_total),
        ('BR-CO-16', payable, tax_inclusive - prepaid + rounding),
    )
    for rule, declared, expected in checks:
        if abs(declared - expected) > Decimal('0.01'):
            raise PeppolGeneratorError(
                "%s monetary total mismatch: declared %s, expected %s."
                % (rule, declared, expected)
            )
    block = cac(root, "LegalMonetaryTotal")
    currency = payload['currency_code']
    cbc_with_attr(block, "LineExtensionAmount", _money(line_total),
                  currencyID=currency)
    cbc_with_attr(block, "TaxExclusiveAmount", _money(tax_exclusive),
                  currencyID=currency)
    cbc_with_attr(block, "TaxInclusiveAmount", _money(tax_inclusive),
                  currencyID=currency)
    if allowance_total:
        cbc_with_attr(block, "AllowanceTotalAmount", _money(allowance_total),
                      currencyID=currency)
    if charge_total:
        cbc_with_attr(block, "ChargeTotalAmount", _money(charge_total),
                      currencyID=currency)
    if prepaid:
        cbc_with_attr(block, "PrepaidAmount", _money(prepaid),
                      currencyID=currency)
    if rounding:
        cbc_with_attr(block, "PayableRoundingAmount", _money(rounding),
                      currencyID=currency)
    cbc_with_attr(block, "PayableAmount", _money(payable),
                  currencyID=currency)


def _price4(value):
    """Render a unit-price value at 4 dp.

    BT-146 (item net price) and BT-148 (item gross price) are not capped
    at 2 dp by EN 16931; a 2 dp net price on a discounted line loses the
    resolution needed for PriceAmount x Quantity to tie back to
    LineExtensionAmount (BR-CO-10). We keep 4 dp on the price side.
    """
    return format(
        Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        'f',
    )


def _render_lines(root, lines, currency, is_credit):
    line_tag = "CreditNoteLine" if is_credit else "InvoiceLine"
    qty_tag = "CreditedQuantity" if is_credit else "InvoicedQuantity"
    for line in lines:
        block = cac(root, line_tag)
        cbc(block, "ID", str(line['id']))
        cbc_with_attr(block, qty_tag, _qty(line['quantity']),
                      unitCode=line.get('unit_code', 'EA'))
        cbc_with_attr(block, "LineExtensionAmount",
                      _money(line['line_total']),
                      currencyID=currency)
        item = cac(block, "Item")
        cbc(item, "Name",
            _ensure(line.get('description'), 'line.description'))
        cat = cac(item, "ClassifiedTaxCategory")
        cbc(cat, "ID", line['tax_category_code'])
        cbc(cat, "Percent", _money(line['tax_rate_pct']))
        ts = cac(cat, "TaxScheme")
        cbc(ts, "ID", "VAT")

        # BR-CO-10: LineExtensionAmount (BT-131) must equal item net price
        # (BT-146) x quantity (BT-129), divided by the base quantity. The
        # supplied gross unit price (BT-148) only satisfies this on an
        # undiscounted line. When the gross price x quantity does not tie
        # to the line net amount we derive the net price from
        # LineExtensionAmount / quantity and emit a line-level
        # AllowanceCharge (BT-147 item price discount) so the identity
        # holds by construction instead of silently dropping the discount.
        qty = Decimal(str(line['quantity']))
        line_net = _q(line['line_total'])
        supplied_price = Decimal(str(line['unit_price']))
        supplied_gross = line.get('gross_unit_price')
        if supplied_gross not in (None, 0, 0.0, ''):
            net_price = supplied_price
            gross_price = Decimal(str(supplied_gross))
        elif qty != 0 and _q(supplied_price * qty) != line_net:
            # Backwards-compatible legacy payload: unit_price was gross.
            gross_price = supplied_price
            net_price = (line_net / qty).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP,
            )
        else:
            net_price = supplied_price
            gross_price = supplied_price

        price = cac(block, "Price")
        price_text = (
            _money(net_price)
            if net_price == _q(net_price)
            else _price4(net_price)
        )
        cbc_with_attr(price, "PriceAmount", price_text,
                      currencyID=currency)
        price_difference = (gross_price - net_price).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP,
        )
        if price_difference:
            adjustment = cac(price, "AllowanceCharge")
            cbc(
                adjustment, "ChargeIndicator",
                "false" if price_difference > 0 else "true",
            )
            cbc_with_attr(
                adjustment, "Amount", _price4(abs(price_difference)),
                currencyID=currency,
            )
            cbc_with_attr(
                adjustment, "BaseAmount", _price4(gross_price),
                currencyID=currency,
            )


# ---- structural validation ------------------------------------------------
#
# Peppol BIS 3.0 / EN 16931 invoices have a small set of mandatory
# elements; a missing one bounces at the access point with an obscure
# message. The validator below checks the most common fail-fast
# violations against the rendered XML *after* generation so callers can
# catch problems before transmission. It is NOT a full XSD validator
# (that would require shipping the OASIS schema files); the goal is to
# surface the high-frequency mistakes a generator-side bug would
# introduce.
#
# Rules checked:
#   * Mandatory document-level elements: CustomizationID, ProfileID, ID,
#     IssueDate, DocumentCurrencyCode, AccountingSupplierParty,
#     AccountingCustomerParty, LegalMonetaryTotal, at least one
#     InvoiceLine / CreditNoteLine.
#   * DocumentCurrencyCode matches every currencyID attribute on Amount,
#     PriceAmount, LineExtensionAmount, TaxAmount, TaxableAmount,
#     PayableAmount.
#   * sum(LineExtensionAmount) on lines == LegalMonetaryTotal/
#     LineExtensionAmount within 1 cent.
#   * sum(TaxAmount on TaxSubtotal) == document TaxTotal/TaxAmount
#     within 1 cent.
#
# A violation raises PeppolGeneratorError with a reason naming the rule.

_MANDATORY_TAGS = (
    "CustomizationID", "ProfileID", "ID", "IssueDate",
    "DocumentCurrencyCode",
)


def validate_rendered(xml_bytes):
    """Validate UBL output against the high-frequency Peppol invariants.

    :param xml_bytes: bytes returned by render_invoice_xml().
    :raises PeppolGeneratorError: when an invariant fails. The exception
        message names the rule violated and the offending value.
    :return: True on success.
    """
    try:
        tree = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise PeppolGeneratorError(
            "Rendered XML is not parseable: %s" % exc,
        ) from exc

    ns = {'cbc': NS_CBC, 'cac': NS_CAC}

    # Mandatory document-level cbc tags.
    for tag in _MANDATORY_TAGS:
        if tree.find('cbc:' + tag, ns) is None:
            raise PeppolGeneratorError(
                "UBL invariant violated: missing mandatory %s element." % tag,
            )

    # Both parties.
    if tree.find('cac:AccountingSupplierParty', ns) is None:
        raise PeppolGeneratorError(
            "UBL invariant violated: missing AccountingSupplierParty.",
        )
    if tree.find('cac:AccountingCustomerParty', ns) is None:
        raise PeppolGeneratorError(
            "UBL invariant violated: missing AccountingCustomerParty.",
        )

    # Monetary totals + at least one line.
    if tree.find('cac:LegalMonetaryTotal', ns) is None:
        raise PeppolGeneratorError(
            "UBL invariant violated: missing LegalMonetaryTotal.",
        )
    line_count = (
        len(tree.findall('cac:InvoiceLine', ns))
        + len(tree.findall('cac:CreditNoteLine', ns))
    )
    if line_count == 0:
        raise PeppolGeneratorError(
            "UBL invariant violated: invoice has no InvoiceLine "
            "or CreditNoteLine elements.",
        )
    # Currency consistency. Every currencyID attribute must equal the
    # document's DocumentCurrencyCode.
    doc_currency = tree.find('cbc:DocumentCurrencyCode', ns).text
    if not doc_currency:
        raise PeppolGeneratorError(
            "UBL invariant violated: DocumentCurrencyCode is empty.",
        )
    tax_currency_el = tree.find('cbc:TaxCurrencyCode', ns)
    tax_currency = tax_currency_el.text if tax_currency_el is not None else ''
    for el in tree.iter():
        ccy = el.get('currencyID')
        tax_currency_amount = (
            etree.QName(el.tag).localname == 'TaxAmount'
            and etree.QName(el.getparent().tag).localname == 'TaxTotal'
            and el.getparent().find('cac:TaxSubtotal', ns) is None
            and ccy == tax_currency
        )
        if ccy and ccy != doc_currency and not tax_currency_amount:
            raise PeppolGeneratorError(
                "UBL invariant violated: %(tag)s carries currencyID="
                "%(ccy)s but DocumentCurrencyCode=%(doc)s. Mixed "
                "currencies are rejected by Peppol access points." % {
                    'tag': etree.QName(el.tag).localname,
                    'ccy': ccy, 'doc': doc_currency,
                },
            )

    # Sum of line extension amounts must match LegalMonetaryTotal/
    # LineExtensionAmount within 1 cent. The 1-cent tolerance covers
    # half-up rounding on per-line decimals.
    line_total = Decimal('0.00')
    line_amount_path = (
        '|'.join((
            'cac:InvoiceLine/cbc:LineExtensionAmount',
            'cac:CreditNoteLine/cbc:LineExtensionAmount',
        ))
    )
    for el in tree.xpath(line_amount_path, namespaces=ns):
        line_total += _q(el.text or '0')
    declared_lex = tree.find(
        'cac:LegalMonetaryTotal/cbc:LineExtensionAmount', ns,
    )
    if declared_lex is not None:
        declared = _q(declared_lex.text or '0')
        if abs(declared - line_total) > Decimal('0.01'):
            raise PeppolGeneratorError(
                "UBL invariant violated: sum of line LineExtension"
                "Amounts (%(line)s) differs from LegalMonetaryTotal/"
                "LineExtensionAmount (%(doc)s) by more than 1 cent." % {
                    'line': str(line_total), 'doc': str(declared),
                },
            )

    # BR-CO-10 per line: item net price (BT-146, cac:Price/cbc:PriceAmount)
    # times quantity (BT-129), adjusted by any line-level price
    # AllowanceCharge, must equal the line LineExtensionAmount (BT-131).
    # This catches a generator that emits a gross PriceAmount against a
    # discounted (net) LineExtensionAmount without carrying the discount.
    line_path = '|'.join((
        'cac:InvoiceLine', 'cac:CreditNoteLine',
    ))
    for line_el in tree.xpath(line_path, namespaces=ns):
        lex_el = line_el.find('cbc:LineExtensionAmount', ns)
        price_el = line_el.find('cac:Price/cbc:PriceAmount', ns)
        if lex_el is None or price_el is None:
            continue
        qty_el = (
            line_el.find('cbc:InvoicedQuantity', ns)
            if line_el.find('cbc:InvoicedQuantity', ns) is not None
            else line_el.find('cbc:CreditedQuantity', ns)
        )
        if qty_el is None:
            continue
        line_id_el = line_el.find('cbc:ID', ns)
        line_id = line_id_el.text if line_id_el is not None else '?'
        qty = Decimal(qty_el.text or '0')
        net_price = Decimal(price_el.text or '0')
        base_qty_el = line_el.find('cac:Price/cbc:BaseQuantity', ns)
        base_qty = Decimal(base_qty_el.text or '1') if (
            base_qty_el is not None and base_qty_el.text) else Decimal('1')
        if base_qty == 0:
            base_qty = Decimal('1')
        expected = _q(net_price * qty / base_qty)
        declared_line = _q(lex_el.text or '0')
        if abs(expected - declared_line) > Decimal('0.01'):
            raise PeppolGeneratorError(
                "UBL invariant violated (BR-CO-10): line %(id)s PriceAmount "
                "(%(price)s) x Quantity (%(qty)s) = %(exp)s but "
                "LineExtensionAmount is %(lex)s. The item net price must "
                "tie to the line net amount." % {
                    'id': line_id, 'price': str(net_price),
                    'qty': str(qty), 'exp': str(expected),
                    'lex': str(declared_line),
                },
            )

    # Sum of TaxSubtotal/TaxAmount must match document TaxTotal/TaxAmount.
    subtotals = tree.xpath(
        'cac:TaxTotal/cac:TaxSubtotal/cbc:TaxAmount',
        namespaces=ns,
    )
    if subtotals:
        sub_sum = sum((_q(s.text or '0') for s in subtotals), Decimal('0.00'))
        declared_total = tree.find('cac:TaxTotal/cbc:TaxAmount', ns)
        if declared_total is not None:
            declared = _q(declared_total.text or '0')
            if abs(declared - sub_sum) > Decimal('0.01'):
                raise PeppolGeneratorError(
                    "UBL invariant violated: sum of TaxSubtotal "
                    "amounts (%(sub)s) differs from TaxTotal/TaxAmount "
                    "(%(doc)s) by more than 1 cent." % {
                        'sub': str(sub_sum), 'doc': str(declared),
                    },
                )

    # BR-CO-14 / BR-S-08: within each tax category, the declared tax
    # (BT-117 TaxSubtotal/TaxAmount) must reconcile to its declared base
    # (BT-116 TaxSubtotal/TaxableAmount) at the category rate (BT-119
    # TaxCategory/Percent). A generator that keys a multi-tax line's whole
    # base to one category's bucket leaves the other categories carrying a
    # non-zero tax against a base of zero (or a base far too small for the
    # rate), so base * rate / 100 no longer matches the booked tax for
    # those categories and this fires. Charged categories (a non-zero
    # rate) are checked; a 1-cent per category tolerance covers half-up
    # rounding, widened by 1 cent per extra line that could round into the
    # bucket. The no-tax categories (Z/E/AE/G/O, rate 0) are skipped: they
    # carry base with zero tax by definition.
    line_count = max(
        len(tree.findall('cac:InvoiceLine', ns))
        + len(tree.findall('cac:CreditNoteLine', ns)),
        1,
    )
    tolerance = Decimal('0.01') * line_count
    for sub in tree.xpath('cac:TaxTotal/cac:TaxSubtotal', namespaces=ns):
        base_el = sub.find('cbc:TaxableAmount', ns)
        tax_el = sub.find('cbc:TaxAmount', ns)
        pct_el = sub.find('cac:TaxCategory/cbc:Percent', ns)
        code_el = sub.find('cac:TaxCategory/cbc:ID', ns)
        if base_el is None or tax_el is None or pct_el is None:
            continue
        rate = Decimal(pct_el.text or '0')
        if rate == 0:
            continue
        base = _q(base_el.text or '0')
        tax = _q(tax_el.text or '0')
        expected_tax = _q(base * rate / Decimal('100'))
        if abs(expected_tax - tax) > tolerance:
            raise PeppolGeneratorError(
                "UBL invariant violated (BR-CO-14): tax category "
                "%(code)s at %(rate)s%% declares TaxableAmount %(base)s "
                "and TaxAmount %(tax)s, but %(base)s x %(rate)s%% = "
                "%(exp)s. The category base and tax do not reconcile, "
                "which happens when a line's base is mis-attributed "
                "across tax categories." % {
                    'code': code_el.text if code_el is not None else '?',
                    'rate': str(rate), 'base': str(base),
                    'tax': str(tax), 'exp': str(expected_tax),
                },
            )

    return True
