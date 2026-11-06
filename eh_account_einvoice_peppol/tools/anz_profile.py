# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
A-NZ PEPPOL profile validation.

The Australia and New Zealand digital business council operates a
country profile on top of PEPPOL BIS Billing 3.0 with the following
country-specific rules:

* The supplier's participant identifier MUST be an ABN (scheme 0151)
  for AU senders, an NZBN (scheme 0088 GLN) for NZ senders. Other
  schemes are technically valid PEPPOL but A-NZ recipients reject
  them.

* The buyer's participant identifier follows the same rule on the
  receiving end.

* Tax category codes constrain to the AU GST / NZ GST regime:
  - 'S' (Standard rate): GST 10% (AU) or GST 15% (NZ).
  - 'Z' (Zero rate): GST-free supplies (AU exports, basic food, etc.).
  - 'E' (Exempt): input-taxed supplies (financial services,
    residential rent).
  - 'G' (Export) and 'O' (outside scope), as published by PINT A-NZ.
  Other codes ('AE' reverse charge, 'K' EU intra-community, etc.) are
  rejected for A-NZ profile invoices because they do not exist in
  AU / NZ tax law.

* The country code on supplier and customer addresses MUST be 'AU'
  or 'NZ'. Mixed AU sender / NZ buyer is permitted (cross-Tasman
  transactions); senders outside A-NZ should not pick this profile.

* Tax rate percentages are gated to the rates the AU/NZ GST regimes
  publish: 0.0, 10.0 (AU GST), 15.0 (NZ GST). Other rates raise.

This module provides a single entry point: validate_anz_payload(payload)
that reads the same dict shape as ubl_generator.make_invoice_payload
and raises AnzProfileError naming the offending field. It is
deliberately decoupled from the generator so callers can validate
without rendering, and so the generator can stay profile-agnostic
(BIS Billing 3.0 vendor-neutral by default; profile validation is
opt-in).

References:
* PEPPOL BIS Billing 3.0 (en_16931 compliance).
* A-NZ PEPPOL Authority operational rules (https://peppol.org).
* ATO GST guidance.
* IRD New Zealand GST guidance.

This implementation is original work; no code or comments derive from
any third-party PEPPOL implementation.
"""

import re


_AU_GST_RATES = {0.0, 10.0}
_NZ_GST_RATES = {0.0, 15.0}
_PERMITTED_TAX_CATEGORIES = {'S', 'Z', 'E', 'G', 'O'}
_AU_NZ_COUNTRIES = {'AU', 'NZ'}


class AnzProfileError(ValueError):
    """Raised when a payload fails A-NZ profile validation."""


def validate_anz_payload(payload):
    """Validate a UBL invoice payload against the A-NZ PEPPOL profile.

    :param payload: dict in the shape returned by
        ubl_generator.make_invoice_payload.
    :raises AnzProfileError: with a message naming the offending field.
    """
    if not isinstance(payload, dict):
        raise AnzProfileError("payload must be a dict")
    supplier = payload.get('supplier') or {}
    customer = payload.get('customer') or {}
    _check_party(supplier, role='supplier')
    _check_party(customer, role='customer')
    _check_tax_categories(payload.get('tax_categories') or [])
    _check_lines_tax_rates(
        payload.get('lines') or [],
        supplier_country=supplier.get('country_code', '').upper(),
    )
    return True


def _check_party(party, role):
    country = (party.get('country_code') or '').upper()
    if country not in _AU_NZ_COUNTRIES:
        raise AnzProfileError(
            "%s.country_code must be AU or NZ for the A-NZ profile "
            "(got %r)" % (role, party.get('country_code')),
        )
    scheme = (party.get('endpoint_scheme') or '').strip()
    endpoint = (party.get('endpoint_id') or '').strip()
    if not endpoint:
        raise AnzProfileError(
            "%s.endpoint_id is required" % role,
        )
    expected = '0151' if country == 'AU' else '0088'
    if scheme != expected:
        raise AnzProfileError(
            "%s.endpoint_scheme must be %s for an %s party "
            "(got %r). 0151 = ABN for AU; 0088 = GLN/NZBN for NZ."
            % (role, expected, country, scheme),
        )


def _check_tax_categories(tax_categories):
    if not tax_categories:
        return
    for idx, cat in enumerate(tax_categories):
        code = (cat.get('category_code') or '').upper()
        if code not in _PERMITTED_TAX_CATEGORIES:
            raise AnzProfileError(
                "tax_categories[%d].category_code %r is not permitted "
                "by the A-NZ profile. Allowed: %s." % (
                    idx, cat.get('category_code'),
                    ', '.join(sorted(_PERMITTED_TAX_CATEGORIES)),
                ),
            )


def _check_lines_tax_rates(lines, supplier_country):
    """Per-line tax rate must match the supplier-country GST regime.

    AU senders: 0% or 10%.
    NZ senders: 0% or 15%.
    Cross-Tasman invoices follow the supplier's GST regime; the buyer
    on the other country reads the supplier's GST and books their own
    side via reverse-charge or import-GST treatment.
    """
    if not lines:
        return
    if supplier_country == 'AU':
        permitted = _AU_GST_RATES
    elif supplier_country == 'NZ':
        permitted = _NZ_GST_RATES
    else:
        # Already rejected upstream by _check_party.
        return
    for idx, line in enumerate(lines):
        rate_value = line.get('tax_rate_pct', 0.0) or 0.0
        try:
            rate_float = float(rate_value)
        except (TypeError, ValueError):
            raise AnzProfileError(
                "lines[%d].tax_rate_pct must be numeric (got %r)"
                % (idx, rate_value),
            )
        # Tolerate small float noise.
        if not any(abs(rate_float - r) < 0.01 for r in permitted):
            raise AnzProfileError(
                "lines[%d].tax_rate_pct %.2f is not permitted for "
                "%s GST. Allowed: %s." % (
                    idx, rate_float, supplier_country,
                    ', '.join('%.0f%%' % r for r in sorted(permitted)),
                ),
            )


# ---- NZBN helpers ------------------------------------------------------


_NZBN_RE = re.compile(r'\d{13}')


def normalise_nzbn(value):
    """Strip whitespace + punctuation; return the bare 13-digit string."""
    if not value:
        return ''
    return re.sub(r'\D+', '', str(value))


def validate_nzbn(value):
    """Validate a New Zealand Business Number.

    NZBN is 13 digits. Same length and structure as a GS1 GLN; mod-10
    check digit per the GS1 algorithm. Returns the canonical form on
    success, raises AnzProfileError on any failure.
    """
    canonical = normalise_nzbn(value)
    if not _NZBN_RE.fullmatch(canonical):
        raise AnzProfileError(
            "NZBN must be exactly 13 digits (got %d after stripping "
            "non-digits)." % len(canonical),
        )
    digits = [int(d) for d in canonical]
    # GS1 mod-10: weights alternate 1, 3 from the right; check digit
    # is the smallest non-negative number that brings the weighted
    # sum to a multiple of 10.
    body = digits[:-1]
    check = digits[-1]
    weighted = 0
    # Position from the right of the body: rightmost body digit gets
    # weight 3, next gets 1, alternating.
    for pos, d in enumerate(reversed(body)):
        weight = 3 if pos % 2 == 0 else 1
        weighted += d * weight
    expected_check = (10 - (weighted % 10)) % 10
    if check != expected_check:
        raise AnzProfileError(
            "NZBN %s failed the GS1 mod-10 checksum (expected check "
            "digit %d, got %d)." % (canonical, expected_check, check),
        )
    return canonical


def is_valid_nzbn(value):
    """Convenience wrapper. Returns True or False."""
    try:
        validate_nzbn(value)
        return True
    except AnzProfileError:
        return False
