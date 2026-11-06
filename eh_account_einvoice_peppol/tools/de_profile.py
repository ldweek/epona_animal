# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
XRechnung (Germany) PEPPOL profile validation.

XRechnung is the German national electronic-invoice standard,
mandatory for B2G since 2020 (federal level) and phased for B2B
(receiving from 1 January 2025; sending phased through 2027 / 2028
by turnover band). Format is UBL 2.1 (or CII for the alternative
syntax); this validator targets the UBL flavour, which matches the
existing generator output.

Country-specific rules:

* Supplier country code MUST be 'DE'.
* Supplier identifier MUST be a German VAT number under PEPPOL
  scheme 9930 (DE prefix + 9 digits + ISO 7064 mod-11/10
  checksum).
* Customer identifier:
  - For B2G (federal / state government): MUST be a Leitweg-ID
    under scheme 0204. The Leitweg-ID encodes the receiving
    authority and routes the invoice through the federal /
    state portal (ZRE / OZG-RE).
  - For B2B: MUST be a German VAT (scheme 9930) for domestic, or
    a foreign-country VAT for cross-border EU invoices.
* Tax category codes restricted to the German VAT regime: S
  (Standard rate), Z (Zero rate / export), E (Exempt), AE (Reverse
  charge), K (EU intra-community), G (Free export). Other PEPPOL
  codes (O Out of scope, L Canary Islands, M Ceuta / Melilla) are
  rejected because they do not appear in German VAT law for
  ordinary B2B / B2G commerce.
* Per-line tax rate gated to the DE VAT regime: 0%, 7% (reduced),
  19% (standard). Reverse-charge lines are at 0% on the supplier
  side regardless of underlying rate (the buyer accounts for the
  VAT under the reverse-charge mechanism).

References:
* XRechnung 3.x specification (https://xeinkauf.de/xrechnung).
* PEPPOL BIS Billing 3.0 + EN 16931 base spec.
* Bundeszentralamt fuer Steuern (BZSt) DE VAT format spec.
* Leitweg-ID specification (https://leitweg-id.de).
* Umsatzsteuergesetz (UStG) for DE VAT rates.

This implementation is original work; no code or comments derive from
any third-party XRechnung implementation.
"""

import re


_DE_VAT_RATES = {0.0, 7.0, 19.0}
_PERMITTED_TAX_CATEGORIES = {'S', 'Z', 'E', 'AE', 'K', 'G'}
_DE_COUNTRY = 'DE'


# Leitweg-ID structure per OZG / federal spec:
#   Grobadressierung    (coarse identifier): 1-12 chars [A-Z0-9]
#   Feinadressierung    (optional fine):     1-30 chars [A-Z0-9]
#   Pruefziffer         (check digits):      2 digits
# Each segment separated by '-'. Examples:
#   04011000-1234512345-06  (coarse + fine + check)
#   991-04011000-44         (coarse + fine + check, both numeric)
#   04-99                   (coarse + check only, no fine)
# Checksum is mod-97 over the alphanumeric body; the federal spec
# documents the exact algorithm and we implement a pragmatic
# structural check + length check here. Full verification happens at
# the receiving authority.
_LEITWEG_RE = re.compile(
    r'^[A-Z0-9]{1,12}'        # coarse identifier
    r'(?:-[A-Z0-9]{1,30})?'   # optional fine identifier
    r'-\d{2}$'                # 2-digit check tail
)


class DeProfileError(ValueError):
    """Raised when a payload fails XRechnung profile validation."""


def validate_de_payload(payload):
    """Validate a UBL invoice payload against the XRechnung profile.

    :param payload: dict in the shape returned by
        ubl_generator.make_invoice_payload.
    :raises DeProfileError: with a message naming the offending field.
    """
    if not isinstance(payload, dict):
        raise DeProfileError("payload must be a dict")
    supplier = payload.get('supplier') or {}
    customer = payload.get('customer') or {}
    _check_supplier(supplier)
    _check_customer(customer)
    _check_tax_categories(payload.get('tax_categories') or [])
    _check_lines_tax_rates(payload.get('lines') or [])
    return True


def _check_supplier(supplier):
    country = (supplier.get('country_code') or '').upper()
    if country != _DE_COUNTRY:
        raise DeProfileError(
            "supplier.country_code must be DE for the XRechnung "
            "profile (got %r)." % supplier.get('country_code'),
        )
    scheme = (supplier.get('endpoint_scheme') or '').strip()
    if scheme != '9930':
        raise DeProfileError(
            "supplier.endpoint_scheme must be 9930 (VAT) for a DE "
            "supplier (got %r)." % scheme,
        )
    endpoint = (supplier.get('endpoint_id') or '').strip()
    try:
        validate_de_vat(endpoint)
    except DeProfileError as exc:
        raise DeProfileError(
            "supplier.endpoint_id failed German VAT validation: %s"
            % exc,
        )


def _check_customer(customer):
    country = (customer.get('country_code') or '').upper()
    scheme = (customer.get('endpoint_scheme') or '').strip()
    endpoint = (customer.get('endpoint_id') or '').strip()
    if not country:
        raise DeProfileError("customer.country_code is required")
    if not endpoint:
        raise DeProfileError("customer.endpoint_id is required")
    # B2G: Leitweg-ID under scheme 0204.
    if scheme == '0204':
        try:
            validate_leitweg_id(endpoint)
        except DeProfileError as exc:
            raise DeProfileError(
                "customer.endpoint_id failed Leitweg-ID validation: %s"
                % exc,
            )
        return
    # B2B domestic: DE VAT.
    if country == _DE_COUNTRY:
        if scheme != '9930':
            raise DeProfileError(
                "customer.endpoint_scheme must be 9930 (VAT) for a DE "
                "B2B customer (got %r). Use 0204 for B2G with a "
                "Leitweg-ID." % scheme,
            )
        try:
            validate_de_vat(endpoint)
        except DeProfileError as exc:
            raise DeProfileError(
                "customer.endpoint_id failed German VAT validation: %s"
                % exc,
            )
        return
    # B2B cross-border EU: any 9930 VAT, country prefix matches the
    # customer.country_code.
    if scheme == '9930':
        normalised = endpoint.upper().replace(' ', '').replace('-', '')
        if not normalised.startswith(country):
            raise DeProfileError(
                "customer.endpoint_id %r does not start with the "
                "customer country prefix %s; cross-border VAT must "
                "carry the recipient's ISO country prefix." % (
                    endpoint, country,
                ),
            )
        return
    # Anything else rejected.
    raise DeProfileError(
        "customer.endpoint_scheme %r is not supported for the "
        "XRechnung profile. Use 9930 (VAT) for B2B or 0204 "
        "(Leitweg-ID) for B2G." % scheme,
    )


def _check_tax_categories(tax_categories):
    if not tax_categories:
        return
    for idx, cat in enumerate(tax_categories):
        code = (cat.get('category_code') or '').upper()
        if code not in _PERMITTED_TAX_CATEGORIES:
            raise DeProfileError(
                "tax_categories[%d].category_code %r is not "
                "permitted by the XRechnung profile. Allowed: %s." % (
                    idx, cat.get('category_code'),
                    ', '.join(sorted(_PERMITTED_TAX_CATEGORIES)),
                ),
            )


def _check_lines_tax_rates(lines):
    if not lines:
        return
    for idx, line in enumerate(lines):
        rate_value = line.get('tax_rate_pct', 0.0) or 0.0
        try:
            rate_float = float(rate_value)
        except (TypeError, ValueError):
            raise DeProfileError(
                "lines[%d].tax_rate_pct must be numeric (got %r)"
                % (idx, rate_value),
            )
        if not any(abs(rate_float - r) < 0.01 for r in _DE_VAT_RATES):
            raise DeProfileError(
                "lines[%d].tax_rate_pct %.2f is not permitted under "
                "the German VAT regime. Allowed: %s." % (
                    idx, rate_float,
                    ', '.join(
                        '%.0f%%' % r for r in sorted(_DE_VAT_RATES)
                    ),
                ),
            )


# ---- DE VAT -----------------------------------------------------------------


def normalise_de_vat(value):
    """Strip whitespace, hyphens, dots; uppercase. Result includes the
    'DE' prefix.
    """
    if not value:
        return ''
    return re.sub(r'[\s\-_.]+', '', str(value)).upper()


def validate_de_vat(value):
    """Validate a German VAT identifier.

    Format: 'DE' + 9 digits. The 9-digit body carries an ISO 7064
    mod-11/10 checksum (the BZSt algorithm); the implementation is
    pure Python.

    Returns the canonical 'DEnnnnnnnnn' form on success, raises
    DeProfileError otherwise.
    """
    canonical = normalise_de_vat(value)
    if not canonical:
        raise DeProfileError("VAT identifier cannot be empty")
    if not re.fullmatch(r'DE\d{9}', canonical):
        raise DeProfileError(
            "DE VAT must be 'DE' followed by exactly 9 digits "
            "(got %r after normalisation)." % canonical,
        )
    body = canonical[2:]
    expected = _de_vat_check_digit(body[:8])
    if int(body[-1]) != expected:
        raise DeProfileError(
            "DE VAT %s failed the BZSt mod-11/10 checksum (expected "
            "check digit %d, got %s)." % (canonical, expected, body[-1]),
        )
    return canonical


def _de_vat_check_digit(eight):
    """ISO 7064 mod-11/10 over an 8-digit string; returns the check digit.

    Algorithm per BZSt / DE finance ministry spec:
      product = 10
      for each digit d in the body:
        sum = (d + product) mod 10; if sum == 0 set sum = 10
        product = (sum * 2) mod 11
      check_digit = (11 - product) mod 10
    """
    product = 10
    for ch in eight:
        s = (int(ch) + product) % 10
        if s == 0:
            s = 10
        product = (s * 2) % 11
    return (11 - product) % 10


def is_valid_de_vat(value):
    try:
        validate_de_vat(value)
        return True
    except DeProfileError:
        return False


# ---- Leitweg-ID -------------------------------------------------------------


def normalise_leitweg(value):
    """Strip whitespace; uppercase. Hyphens are part of the structure
    and preserved.
    """
    if not value:
        return ''
    return re.sub(r'\s+', '', str(value)).upper()


def validate_leitweg_id(value):
    """Validate a Leitweg-ID structurally.

    Format: <2-3 digit federal/authority prefix>-<1-30 char [A-Z0-9]
    coarse identifier>(-<subsidiary segments>)*-<2 digit check>.
    Examples:
      04011000-1234512345-06
      991-04011000-44

    The checksum is verified by the receiving portal (ZRE / OZG-RE)
    via a published mod-97 algorithm; this validator does the
    structural check + length checks only. Sites that need full
    checksum verification override this with the full algorithm in a
    deployment-specific extension.
    """
    canonical = normalise_leitweg(value)
    if not canonical:
        raise DeProfileError("Leitweg-ID cannot be empty")
    if not _LEITWEG_RE.match(canonical):
        raise DeProfileError(
            "Leitweg-ID structure invalid (got %r). Expected "
            "<coarse 1-12 alphanumerics>(-<fine 1-30 alphanumerics>)?"
            "-<2 digits>." % canonical,
        )
    if len(canonical) > 46:
        raise DeProfileError(
            "Leitweg-ID exceeds 46-character limit (got %d)."
            % len(canonical),
        )
    return canonical


def is_valid_leitweg_id(value):
    try:
        validate_leitweg_id(value)
        return True
    except DeProfileError:
        return False
