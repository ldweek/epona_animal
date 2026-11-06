# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Peppol BIS Billing 3.0 (UBL 2.1) Invoice / Credit Note parser.

Inverse of ubl_generator.render_invoice_xml. Takes UBL 2.1 XML bytes
(or a parsed lxml etree) and returns a normalised payload dict matching
the shape that make_invoice_payload produces. Concrete callers (the
inbound model, batch jobs, test fixtures) consume the dict to create
vendor bills.

Originality
-----------

Built from the same spec sources as the generator (UBL 2.1, EN 16931,
Peppol BIS Billing 3.0). No code or comments derive from any third-
party Odoo eInvoicing implementation.

Tolerance
---------

The parser is lenient on optional fields (note, buyer_reference,
contract_reference, payment_terms) and strict on required ones
(invoice_number, issue_date, currency_code, supplier, customer,
lines). Required-field violations raise PeppolParserError naming the
offending element.

Out of scope
------------

* XAdES signature verification (transport-level concern; access point
  validates).
* Country profile extensions (FatturaPA, MyInvois, XRechnung). The
  parser reads the BIS 3.0 core; downstream callers can extract
  country-specific extensions from the rendered tree.
"""

import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from lxml import etree


MAX_UBL_BYTES = 10 * 1024 * 1024

# Peppol's validation rules intentionally allow bounded rounding slack in
# two places.  R120 uses 0.02 for line-net arithmetic.  The EN 16931 VAT
# breakdown rules (for example BR-S-08/09) use an open +/- 1 currency-unit
# interval.  Keep those rule-specific bounds separate: applying the broad
# VAT-breakdown slack to document totals would hide real payable errors.
PEPPOL_LINE_NET_SLACK = Decimal('0.02')
PEPPOL_VAT_BREAKDOWN_SLACK = Decimal('1.00')


# Match the namespaces in the generator so a round-trip test can
# verify symmetry.
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


_NS_MAP = {
    'cbc': NS_CBC,
    'cac': NS_CAC,
    'invoice': NS_INVOICE,
    'credit': NS_CREDIT,
}


class PeppolParserError(ValueError):
    """Raised on malformed or non-conformant inbound XML.

    The message names the offending element or attribute so the user
    can locate the failure in the source XML quickly.
    """


def parse_invoice_xml(source):
    """Parse UBL 2.1 invoice or credit-note XML and return a payload.

    :param source: bytes, str, file-like, or an lxml _Element.
    :return: dict with the same keys as make_invoice_payload's return
        value, i.e. invoice_number, issue_date, due_date,
        currency_code, invoice_type_code, note, buyer_reference,
        order_reference, contract_reference, supplier, customer,
        lines, tax_categories, payment_means, payment_terms,
        document_type.
    :raises PeppolParserError: when the XML is unparseable or a
        required field is missing.
    """
    root = _coerce_root(source)
    document_type = _detect_document_type(root)
    currency_code = _required_text(
        root, './cbc:DocumentCurrencyCode', 'currency_code',
    ).upper()
    _reject_unsupported_document_adjustments(root)
    lines = _parse_lines(root, document_type, currency_code)
    tax_categories, tax_amount = _parse_tax_categories(
        root, currency_code,
    )
    monetary_totals = _parse_monetary_totals(root, currency_code)
    _validate_financial_consistency(
        lines, tax_categories, tax_amount, monetary_totals,
    )
    return {
        'document_type': document_type,
        'invoice_number': _required_text(
            root, './cbc:ID', 'invoice_number',
        ),
        'issue_date': _required_date(
            root, './cbc:IssueDate', 'issue_date',
        ),
        'due_date': _optional_date(root, './cbc:DueDate'),
        'currency_code': currency_code,
        'invoice_type_code': _optional_text(
            root, './cbc:InvoiceTypeCode',
        ) or _optional_text(root, './cbc:CreditNoteTypeCode') or (
            '380' if document_type == 'invoice' else '381'
        ),
        'note': _optional_text(root, './cbc:Note') or '',
        'buyer_reference': _optional_text(
            root, './cbc:BuyerReference',
        ) or '',
        'order_reference': _optional_text(
            root, './cac:OrderReference/cbc:ID',
        ) or '',
        'contract_reference': _optional_text(
            root, './cac:ContractDocumentReference/cbc:ID',
        ) or '',
        'supplier': _parse_party(
            root, './cac:AccountingSupplierParty', 'supplier',
        ),
        'customer': _parse_party(
            root, './cac:AccountingCustomerParty', 'customer',
        ),
        'payment_means': _parse_payment_means(root),
        'payment_terms': _parse_payment_terms(root),
        'tax_categories': tax_categories,
        'tax_amount': float(tax_amount),
        'monetary_totals': {
            key: float(value) for key, value in monetary_totals.items()
        },
        'lines': lines,
    }


# ---- helpers ----


def _secure_parser():
    """lxml parser hardened against XXE and entity-expansion attacks.

    Inbound Peppol documents arrive from external, untrusted senders, so the
    parser must not resolve external entities, must not fetch over the
    network (SSRF via SYSTEM entities), and must reject DTDs and oversized
    trees. Every parse of untrusted UBL goes through here.
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )


def _coerce_root(source):
    """Accept bytes / str / file-like / etree._Element and return the
    root Element. Strict, XXE-hardened parser: an XML error is converted to
    PeppolParserError with the location preserved.
    """
    if hasattr(source, 'tag'):
        _reject_doctype(source)
        return source
    if hasattr(source, 'read'):
        source = source.read(MAX_UBL_BYTES + 1)
    if isinstance(source, str):
        source = source.encode('utf-8')
    if isinstance(source, bytearray):
        source = bytes(source)
    if isinstance(source, bytes) and len(source) > MAX_UBL_BYTES:
        raise PeppolParserError(
            "UBL XML exceeds the maximum allowed size of %d bytes."
            % MAX_UBL_BYTES,
        )
    try:
        parser = _secure_parser()
        root = etree.fromstring(source, parser)
    except etree.XMLSyntaxError as exc:
        raise PeppolParserError(
            "UBL XML failed to parse: %s" % exc,
        ) from exc
    _reject_doctype(root)
    return root


def _reject_doctype(root):
    """Reject DTD-bearing trees, including caller-supplied parsed trees."""
    # Defence in depth: reject any DOCTYPE/entity declarations outright, even
    # though resolve_entities=False already neutralises expansion for bytes.
    doctype = getattr(getattr(root, 'getroottree', lambda: None)(),
                      'docinfo', None)
    if doctype is not None and doctype.doctype:
        raise PeppolParserError(
            "UBL XML with a DOCTYPE declaration is rejected (XXE guard).",
        )


def _detect_document_type(root):
    """Return 'invoice' or 'credit_note' based on the root element."""
    tag = etree.QName(root.tag)
    if tag.localname == 'Invoice' and tag.namespace == NS_INVOICE:
        return 'invoice'
    if tag.localname == 'CreditNote' and tag.namespace == NS_CREDIT:
        return 'credit_note'
    raise PeppolParserError(
        "Unrecognised root element %r in namespace %r; expected UBL "
        "Invoice or CreditNote" % (tag.localname, tag.namespace),
    )


def _required_text(node, xpath, field):
    elt = node.find(xpath, namespaces=_NS_MAP)
    if elt is None or not (elt.text or '').strip():
        raise PeppolParserError(
            "Required field %s missing (xpath %s)" % (field, xpath),
        )
    return elt.text.strip()


def _optional_text(node, xpath):
    elt = node.find(xpath, namespaces=_NS_MAP)
    if elt is None or elt.text is None:
        return None
    return elt.text.strip() or None


def _required_date(node, xpath, field):
    raw = _required_text(node, xpath, field)
    return _parse_date(raw, field)


def _optional_date(node, xpath):
    raw = _optional_text(node, xpath)
    if not raw:
        return None
    return _parse_date(raw, xpath)


def _parse_date(raw, field):
    try:
        return datetime.date.fromisoformat(raw[:10])
    except (TypeError, ValueError):
        raise PeppolParserError(
            "Field %s carries unparseable date %r (expected "
            "ISO 8601 YYYY-MM-DD)" % (field, raw),
        )


def _required_decimal(node, xpath, field):
    raw = _required_text(node, xpath, field)
    return _parse_decimal(raw, field)


def _optional_decimal(node, xpath, default=Decimal('0')):
    raw = _optional_text(node, xpath)
    if raw is None:
        return default
    return _parse_decimal(raw, xpath)


def _parse_decimal(raw, field):
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise PeppolParserError(
            "Field %s carries unparseable decimal %r" % (field, raw),
        )
    if not value.is_finite():
        raise PeppolParserError(
            "Field %s carries non-finite decimal %r" % (field, raw),
        )
    return value


def _required_single_node(node, xpath, field):
    matches = node.findall(xpath, namespaces=_NS_MAP)
    if len(matches) != 1:
        raise PeppolParserError(
            "Field %s must occur exactly once (xpath %s; found %d)"
            % (field, xpath, len(matches)),
        )
    return matches[0]


def _required_money(node, xpath, field, currency_code):
    """Read one monetary amount and bind it to document currency."""
    amount_node = _required_single_node(node, xpath, field)
    raw = (amount_node.text or '').strip()
    if not raw:
        raise PeppolParserError("Required monetary field %s is empty" % field)
    currency_id = (amount_node.get('currencyID') or '').strip().upper()
    if currency_id != currency_code:
        raise PeppolParserError(
            "Field %s currencyID %r does not match document currency %r"
            % (field, currency_id or None, currency_code),
        )
    return _parse_decimal(raw, field)


def _optional_money(node, xpath, field, currency_code):
    matches = node.findall(xpath, namespaces=_NS_MAP)
    if not matches:
        return None
    if len(matches) != 1:
        raise PeppolParserError(
            "Field %s must occur at most once (found %d)"
            % (field, len(matches)),
        )
    amount_node = matches[0]
    currency_id = (amount_node.get('currencyID') or '').strip().upper()
    if currency_id != currency_code:
        raise PeppolParserError(
            "Field %s currencyID %r does not match document currency %r"
            % (field, currency_id or None, currency_code),
        )
    return _parse_decimal((amount_node.text or '').strip(), field)


def _money_round(value):
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _within_slack(actual, expected, slack, *, inclusive=True):
    """Return whether two Decimals meet a specific Peppol rule's slack."""
    difference = abs(actual - expected)
    return difference <= slack if inclusive else difference < slack


# ---- party ----


def _parse_party(root, base_xpath, role):
    party_node = root.find(base_xpath, namespaces=_NS_MAP)
    if party_node is None:
        raise PeppolParserError(
            "Required party block %s missing for role %s"
            % (base_xpath, role),
        )
    inner = party_node.find('./cac:Party', namespaces=_NS_MAP)
    if inner is None:
        raise PeppolParserError(
            "Required cac:Party child missing under %s" % base_xpath,
        )
    name = (
        _optional_text(
            inner,
            './cac:PartyName/cbc:Name',
        )
        or _optional_text(
            inner, './cac:PartyLegalEntity/cbc:RegistrationName',
        )
    )
    if not name:
        raise PeppolParserError(
            "Party %s has no name (cac:PartyName/cbc:Name nor "
            "cac:PartyLegalEntity/cbc:RegistrationName)" % role,
        )
    endpoint_node = inner.find(
        './cbc:EndpointID', namespaces=_NS_MAP,
    )
    endpoint_id = (endpoint_node.text or '').strip() if endpoint_node is not None else ''
    endpoint_scheme = (
        endpoint_node.get('schemeID') if endpoint_node is not None else ''
    )
    address_node = inner.find(
        './cac:PostalAddress', namespaces=_NS_MAP,
    )
    address = {}
    if address_node is not None:
        address = {
            'street': _optional_text(
                address_node, './cbc:StreetName',
            ) or '',
            'city': _optional_text(
                address_node, './cbc:CityName',
            ) or '',
            'postcode': _optional_text(
                address_node, './cbc:PostalZone',
            ) or '',
            'country': _optional_text(
                address_node,
                './cac:Country/cbc:IdentificationCode',
            ) or '',
        }
    country_code = address.get('country', '')
    vat_id = _optional_text(
        inner,
        './cac:PartyTaxScheme/cbc:CompanyID',
    ) or ''
    legal_id = _optional_text(
        inner,
        './cac:PartyLegalEntity/cbc:CompanyID',
    ) or ''
    return {
        'name': name,
        'endpoint_id': endpoint_id,
        'endpoint_scheme': endpoint_scheme or '',
        'country_code': country_code,
        'vat_id': vat_id,
        'legal_id': legal_id,
        'address': address,
    }


# ---- payment ----


def _parse_payment_means(root):
    pm = root.find('./cac:PaymentMeans', namespaces=_NS_MAP)
    if pm is None:
        return None
    payee_account = pm.find(
        './cac:PayeeFinancialAccount', namespaces=_NS_MAP,
    )
    iban = _optional_text(payee_account, './cbc:ID') if payee_account is not None else None
    return {
        'payment_means_code': _optional_text(
            pm, './cbc:PaymentMeansCode',
        ) or '',
        'payment_id': _optional_text(pm, './cbc:PaymentID') or '',
        'payee_account_id': iban or '',
    }


def _parse_payment_terms(root):
    pt = root.find('./cac:PaymentTerms', namespaces=_NS_MAP)
    if pt is None:
        return None
    return {
        'note': _optional_text(pt, './cbc:Note') or '',
    }


# ---- tax categories ----


def _parse_tax_categories(root, currency_code):
    """Return [{category_code, rate_pct, taxable_amount, tax_amount}].

    Reads cac:TaxTotal/cac:TaxSubtotal entries at the document level.
    BIS Billing 3.0 requires the per-rate breakdown here; the per-line
    classified-tax-category references this table by code.
    """
    tax_totals = root.findall('./cac:TaxTotal', namespaces=_NS_MAP)
    if len(tax_totals) != 1:
        raise PeppolParserError(
            "Document must carry exactly one cac:TaxTotal (found %d)"
            % len(tax_totals),
        )
    tax_total = tax_totals[0]
    declared_total = _required_money(
        tax_total, './cbc:TaxAmount', 'tax_total.tax_amount', currency_code,
    )
    subtotals = tax_total.findall(
        './cac:TaxSubtotal', namespaces=_NS_MAP,
    )
    if not subtotals:
        raise PeppolParserError(
            "TaxTotal must carry at least one TaxSubtotal",
        )
    out = []
    seen_keys = set()
    for index, subtotal in enumerate(subtotals, start=1):
        categories = subtotal.findall(
            './cac:TaxCategory', namespaces=_NS_MAP,
        )
        if len(categories) != 1:
            raise PeppolParserError(
                "TaxSubtotal %d must carry exactly one TaxCategory"
                % index,
            )
        category = categories[0]
        category_code = _required_text(
            category, './cbc:ID', 'tax_subtotal.category_code',
        ).upper()
        rate_pct = _required_decimal(
            category, './cbc:Percent', 'tax_subtotal.rate_pct',
        )
        if rate_pct < 0:
            raise PeppolParserError(
                "TaxSubtotal rate cannot be negative",
            )
        key = (category_code, rate_pct)
        if key in seen_keys:
            raise PeppolParserError(
                "Duplicate TaxSubtotal for category %s at rate %s"
                % key,
            )
        seen_keys.add(key)
        taxable_amount = _required_money(
            subtotal, './cbc:TaxableAmount',
            'tax_subtotal.taxable_amount', currency_code,
        )
        tax_amount = _required_money(
            subtotal, './cbc:TaxAmount',
            'tax_subtotal.tax_amount', currency_code,
        )
        out.append({
            'category_code': category_code,
            'rate_pct': float(rate_pct),
            'taxable_amount': float(taxable_amount),
            'tax_amount': float(tax_amount),
            '_rate_decimal': rate_pct,
            '_taxable_decimal': taxable_amount,
            '_tax_decimal': tax_amount,
        })
    if _money_round(sum(
            cat['_tax_decimal'] for cat in out
    )) != declared_total:
        raise PeppolParserError(
            "TaxTotal TaxAmount does not equal sum of TaxSubtotal amounts",
        )
    return out, declared_total


def _parse_monetary_totals(root, currency_code):
    totals = root.findall('./cac:LegalMonetaryTotal', namespaces=_NS_MAP)
    if len(totals) != 1:
        raise PeppolParserError(
            "Document must carry exactly one LegalMonetaryTotal (found %d)"
            % len(totals),
        )
    total = totals[0]
    return {
        'line_extension_amount': _required_money(
            total, './cbc:LineExtensionAmount',
            'monetary_total.line_extension_amount', currency_code,
        ),
        'tax_exclusive_amount': _required_money(
            total, './cbc:TaxExclusiveAmount',
            'monetary_total.tax_exclusive_amount', currency_code,
        ),
        'tax_inclusive_amount': _required_money(
            total, './cbc:TaxInclusiveAmount',
            'monetary_total.tax_inclusive_amount', currency_code,
        ),
        'payable_amount': _required_money(
            total, './cbc:PayableAmount',
            'monetary_total.payable_amount', currency_code,
        ),
    }


def _reject_unsupported_document_adjustments(root):
    """Fail closed for financial constructs inbound booking cannot retain."""
    unsupported = []
    if root.findall('./cac:AllowanceCharge', namespaces=_NS_MAP):
        unsupported.append('document AllowanceCharge')
    totals = root.findall('./cac:LegalMonetaryTotal', namespaces=_NS_MAP)
    if totals:
        for xpath, label in (
            ('./cbc:AllowanceTotalAmount', 'AllowanceTotalAmount'),
            ('./cbc:ChargeTotalAmount', 'ChargeTotalAmount'),
            ('./cbc:PrepaidAmount', 'PrepaidAmount'),
            ('./cbc:PayableRoundingAmount', 'PayableRoundingAmount'),
        ):
            if totals[0].findall(xpath, namespaces=_NS_MAP):
                unsupported.append(label)
    if unsupported:
        raise PeppolParserError(
            "Unsupported document-level financial adjustment(s): %s"
            % ', '.join(unsupported),
        )


def _validate_financial_consistency(
        lines, tax_categories, tax_amount, monetary_totals):
    """Cross-check every declared net, tax and payable identity."""
    line_extension = sum(
        line['_line_total_decimal'] for line in lines
    )
    if line_extension != monetary_totals['line_extension_amount']:
        raise PeppolParserError(
            "LegalMonetaryTotal LineExtensionAmount does not equal sum of "
            "invoice line extension amounts",
        )
    if monetary_totals['tax_exclusive_amount'] != line_extension:
        raise PeppolParserError(
            "TaxExclusiveAmount must equal LineExtensionAmount when "
            "document allowances and charges are unsupported",
        )
    if (monetary_totals['tax_inclusive_amount']
            != monetary_totals['tax_exclusive_amount'] + tax_amount):
        raise PeppolParserError(
            "TaxInclusiveAmount does not equal TaxExclusiveAmount plus tax",
        )
    if (monetary_totals['payable_amount']
            != monetary_totals['tax_inclusive_amount']):
        raise PeppolParserError(
            "PayableAmount must equal TaxInclusiveAmount when prepayments "
            "and payable rounding are unsupported",
        )

    line_bases = {}
    for line in lines:
        key = (line['tax_category_code'], line['_tax_rate_decimal'])
        line_bases[key] = (
            line_bases.get(key, Decimal('0'))
            + line['_line_total_decimal']
        )
    category_by_key = {
        (cat['category_code'], cat['_rate_decimal']): cat
        for cat in tax_categories
    }
    if set(line_bases) != set(category_by_key):
        missing = sorted(set(line_bases) - set(category_by_key))
        orphaned = sorted(set(category_by_key) - set(line_bases))
        raise PeppolParserError(
            "Line tax categories and TaxSubtotal categories disagree "
            "(missing=%r, orphaned=%r)" % (missing, orphaned),
        )
    for key, taxable_amount in line_bases.items():
        category = category_by_key[key]
        if not _within_slack(
                category['_taxable_decimal'], taxable_amount,
                PEPPOL_VAT_BREAKDOWN_SLACK, inclusive=False):
            raise PeppolParserError(
                "TaxableAmount for category %s at rate %s does not equal "
                "sum of matching line amounts" % key,
            )
        expected_tax = _money_round(
            taxable_amount * key[1] / Decimal('100'),
        )
        if not _within_slack(
                category['_tax_decimal'], expected_tax,
                PEPPOL_VAT_BREAKDOWN_SLACK, inclusive=False):
            raise PeppolParserError(
                "TaxAmount for category %s at rate %s is inconsistent with "
                "its taxable amount" % key,
            )

    # Keep only stable public payload keys. Decimal shadow fields above avoid
    # float arithmetic during validation but must not leak to downstream APIs.
    for line in lines:
        line.pop('_line_total_decimal', None)
        line.pop('_tax_rate_decimal', None)
    for category in tax_categories:
        category.pop('_rate_decimal', None)
        category.pop('_taxable_decimal', None)
        category.pop('_tax_decimal', None)


# ---- lines ----


def _parse_lines(root, document_type, currency_code):
    """Read InvoiceLine or CreditNoteLine entries.

    Returns a list of dicts: {id, description, quantity, unit_code,
    unit_price, line_total, tax_category_code, tax_rate_pct}.
    """
    line_xpath = (
        './cac:InvoiceLine'
        if document_type == 'invoice'
        else './cac:CreditNoteLine'
    )
    qty_xpath = (
        './cbc:InvoicedQuantity'
        if document_type == 'invoice'
        else './cbc:CreditedQuantity'
    )
    out = []
    for line_index, line in enumerate(
            root.findall(line_xpath, namespaces=_NS_MAP), start=1):
        qty_node = line.find(qty_xpath, namespaces=_NS_MAP)
        if qty_node is None or qty_node.text is None:
            raise PeppolParserError(
                "Line missing quantity element %s" % qty_xpath,
            )
        unit_code = qty_node.get('unitCode') or 'EA'
        quantity = _parse_decimal(qty_node.text, 'line.quantity')
        if quantity <= 0:
            raise PeppolParserError(
                "Line %d quantity must be greater than zero" % line_index,
            )
        line_total = _required_money(
            line, './cbc:LineExtensionAmount', 'line.line_total',
            currency_code,
        )
        price_blocks = line.findall('./cac:Price', namespaces=_NS_MAP)
        if len(price_blocks) != 1:
            raise PeppolParserError(
                "Line %d must carry exactly one Price block" % line_index,
            )
        price_block = price_blocks[0]
        unit_price = _required_money(
            price_block, './cbc:PriceAmount', 'line.unit_price',
            currency_code,
        )
        if unit_price < 0:
            raise PeppolParserError(
                "Line %d unit price cannot be negative" % line_index,
            )
        base_quantity = _optional_decimal(
            price_block, './cbc:BaseQuantity', Decimal('1'),
        )
        if base_quantity <= 0:
            raise PeppolParserError(
                "Line %d Price BaseQuantity must be greater than zero"
                % line_index,
            )
        item_node = line.find('./cac:Item', namespaces=_NS_MAP)
        description = ''
        tax_category_code = ''
        tax_rate_pct = Decimal('0')
        if item_node is not None:
            description = _optional_text(
                item_node, './cbc:Name',
            ) or _optional_text(
                item_node, './cbc:Description',
            ) or ''
            cat = item_node.find(
                './cac:ClassifiedTaxCategory', namespaces=_NS_MAP,
            )
            if cat is not None:
                tax_category_code = _required_text(
                    cat, './cbc:ID', 'line.tax_category_code',
                ).upper()
                tax_rate_pct = _required_decimal(
                    cat, './cbc:Percent', 'line.tax_rate_pct',
                )
                if tax_rate_pct < 0:
                    raise PeppolParserError(
                        "Line %d tax rate cannot be negative" % line_index,
                    )
        if not tax_category_code:
            raise PeppolParserError(
                "Line %d has no ClassifiedTaxCategory" % line_index,
            )

        adjustment = Decimal('0')
        for allowance_index, allowance in enumerate(line.findall(
                './cac:AllowanceCharge', namespaces=_NS_MAP), start=1):
            indicator = _required_text(
                allowance, './cbc:ChargeIndicator',
                'line.allowance_charge.charge_indicator',
            ).casefold()
            if indicator not in ('true', 'false', '1', '0'):
                raise PeppolParserError(
                    "Line %d AllowanceCharge %d has invalid ChargeIndicator"
                    % (line_index, allowance_index),
                )
            amount = _required_money(
                allowance, './cbc:Amount',
                'line.allowance_charge.amount', currency_code,
            )
            if amount < 0:
                raise PeppolParserError(
                    "Line %d AllowanceCharge amount cannot be negative"
                    % line_index,
                )
            adjustment += amount if indicator in ('true', '1') else -amount
        expected_line_total = (
            quantity * unit_price / base_quantity + adjustment
        )
        if not _within_slack(
                line_total, expected_line_total, PEPPOL_LINE_NET_SLACK):
            raise PeppolParserError(
                "Line %d LineExtensionAmount %s is inconsistent with "
                "quantity, price, BaseQuantity and line allowances/charges "
                "(expected %s)"
                % (line_index, line_total, _money_round(expected_line_total)),
            )
        out.append({
            'id': _optional_text(line, './cbc:ID') or '',
            'description': description,
            'quantity': float(quantity),
            'unit_code': unit_code,
            'unit_price': float(unit_price),
            'line_total': float(line_total),
            'tax_category_code': tax_category_code,
            'tax_rate_pct': float(tax_rate_pct),
            '_line_total_decimal': line_total,
            '_tax_rate_decimal': tax_rate_pct,
        })
    if not out:
        raise PeppolParserError(
            "Document carries no %s lines" % (
                'invoice' if document_type == 'invoice'
                else 'credit-note',
            ),
        )
    return out
