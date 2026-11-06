# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Factur-X (France) PEPPOL profile validation.

Factur-X is the French national e-invoicing standard. It is a hybrid
format: a PDF/A-3 document carrying an embedded XML payload (the
Cross-Industry Invoice schema, EN 16931 compliant). The mandatory
phased rollout starts September 2026:

  Phase 1 (Sep 2026): all businesses must be able to receive
                       Factur-X. Large + intermediate-size companies
                       must send.
  Phase 2 (Sep 2027): SMEs and micro-enterprises must send.

This validator targets the EN 16931-compliant XML body, which is
also what PEPPOL routes. Sites that need the PDF/A-3 hybrid carrier
generate the PDF separately and embed the validated XML.

Country-specific rules:

* Supplier country code MUST be 'FR'. Cross-border outbound from a
  non-FR supplier into a FR buyer should not use this profile; use
  the supplier-country profile instead.
* Supplier identifier MUST be a French VAT number (FR + 2 alphanum
  key + 9 digits SIREN) under PEPPOL scheme 9930. The 2-character
  key MUST verify against the SIREN per the DGFiP mod-97 algorithm.
* Customer identifier:
  - Domestic FR: French VAT under scheme 9930.
  - Cross-border EU: foreign VAT under scheme 9930; the country
    prefix MUST match the customer.country_code (anti-spoofing).
  - B2C: SIRET under scheme 0009 OR no endpoint id (consumer).
* Tax category codes restricted to S (Standard), Z (Zero rate /
  export), E (Exempt), AE (Reverse charge), G (Free export), K (EU
  intra-community). Other PEPPOL codes rejected for FR commerce.
* Per-line tax rate gated to the FR VAT regime: 0% (zero / exempt /
  reverse charge), 2.1% (super-reduced: press, medicines), 5.5%
  (reduced: food, books, energy renovation), 10% (intermediate:
  restaurants, hotels, public transport), 20% (standard).

References:
* AFNOR Z 25-001 Factur-X specification.
* PEPPOL BIS Billing 3.0 + EN 16931.
* DGFiP (Direction Generale des Finances Publiques) FR VAT format.
* Code general des impots, Article 256 ff. (FR VAT rates).

This implementation is original work; no code or comments derive from
any third-party Factur-X implementation.
"""

import re


_FR_VAT_RATES = {0.0, 2.1, 5.5, 10.0, 20.0}
_PERMITTED_TAX_CATEGORIES = {'S', 'Z', 'E', 'AE', 'G', 'K'}
_FR_COUNTRY = 'FR'

_FR_VAT_RE = re.compile(r'^FR[A-Z0-9]{2}\d{9}$')
_SIRET_RE = re.compile(r'^\d{14}$')


class FrProfileError(ValueError):
    """Raised when a payload fails Factur-X profile validation."""


def validate_fr_payload(payload):
    """Validate a UBL invoice payload against the Factur-X profile."""
    if not isinstance(payload, dict):
        raise FrProfileError("payload must be a dict")
    supplier = payload.get('supplier') or {}
    customer = payload.get('customer') or {}
    _check_supplier(supplier)
    _check_customer(customer)
    _check_tax_categories(payload.get('tax_categories') or [])
    _check_lines_tax_rates(payload.get('lines') or [])
    return True


def _check_supplier(supplier):
    country = (supplier.get('country_code') or '').upper()
    if country != _FR_COUNTRY:
        raise FrProfileError(
            "supplier.country_code must be FR for the Factur-X "
            "profile (got %r)." % supplier.get('country_code'),
        )
    scheme = (supplier.get('endpoint_scheme') or '').strip()
    if scheme != '9930':
        raise FrProfileError(
            "supplier.endpoint_scheme must be 9930 (VAT) for a FR "
            "supplier (got %r)." % scheme,
        )
    endpoint = (supplier.get('endpoint_id') or '').strip()
    try:
        validate_fr_vat(endpoint)
    except FrProfileError as exc:
        raise FrProfileError(
            "supplier.endpoint_id failed French VAT validation: %s"
            % exc,
        )


def _check_customer(customer):
    country = (customer.get('country_code') or '').upper()
    scheme = (customer.get('endpoint_scheme') or '').strip()
    endpoint = (customer.get('endpoint_id') or '').strip()
    if not country:
        raise FrProfileError("customer.country_code is required")
    # B2C consumer: no endpoint required when scheme is empty AND
    # the country is FR (the consumer is a private individual).
    if not endpoint and not scheme:
        if country == _FR_COUNTRY:
            return
        # Cross-border B2C still needs at least country code.
        if len(country) == 2 and country.isalpha():
            return
        raise FrProfileError(
            "customer.country_code must be a 2-letter ISO code for "
            "B2C customer (got %r)." % country,
        )
    if not endpoint:
        raise FrProfileError(
            "customer.endpoint_id is required when "
            "endpoint_scheme is set",
        )
    # Domestic SIRET (B2C with business identifier).
    if scheme == '0009':
        try:
            validate_fr_siret(endpoint)
        except FrProfileError as exc:
            raise FrProfileError(
                "customer.endpoint_id failed French SIRET "
                "validation: %s" % exc,
            )
        return
    # Domestic FR VAT.
    if country == _FR_COUNTRY and scheme == '9930':
        try:
            validate_fr_vat(endpoint)
        except FrProfileError as exc:
            raise FrProfileError(
                "customer.endpoint_id failed French VAT "
                "validation: %s" % exc,
            )
        return
    # Cross-border EU: any 9930 VAT, country prefix matches the
    # customer.country_code.
    if scheme == '9930':
        normalised = endpoint.upper().replace(' ', '').replace('-', '')
        if not normalised.startswith(country):
            raise FrProfileError(
                "customer.endpoint_id %r does not start with the "
                "customer country prefix %s; cross-border VAT must "
                "carry the recipient's ISO country prefix." % (
                    endpoint, country,
                ),
            )
        return
    raise FrProfileError(
        "customer.endpoint_scheme %r is not supported for the "
        "Factur-X profile. Use 9930 (VAT) or 0009 (SIRET); "
        "leave both endpoint fields blank for B2C consumers." % scheme,
    )


def _check_tax_categories(tax_categories):
    if not tax_categories:
        return
    for idx, cat in enumerate(tax_categories):
        code = (cat.get('category_code') or '').upper()
        if code not in _PERMITTED_TAX_CATEGORIES:
            raise FrProfileError(
                "tax_categories[%d].category_code %r is not "
                "permitted by the Factur-X profile. Allowed: %s." % (
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
            raise FrProfileError(
                "lines[%d].tax_rate_pct must be numeric (got %r)"
                % (idx, rate_value),
            )
        if not any(abs(rate_float - r) < 0.01 for r in _FR_VAT_RATES):
            raise FrProfileError(
                "lines[%d].tax_rate_pct %.2f is not permitted under "
                "the French VAT regime. Allowed: %s." % (
                    idx, rate_float,
                    ', '.join(
                        '%.1f%%' % r for r in sorted(_FR_VAT_RATES)
                    ),
                ),
            )


# ---- FR VAT -----------------------------------------------------------------


def normalise_fr_vat(value):
    """Strip whitespace, hyphens, dots; uppercase. Result includes the
    'FR' prefix.
    """
    if not value:
        return ''
    return re.sub(r'[\s\-_.]+', '', str(value)).upper()


def validate_fr_vat(value):
    """Validate a French VAT identifier.

    Format: 'FR' + 2 alphanumeric key + 9 digit SIREN. Total 13 chars.
    The key verifies via the DGFiP mod-97 algorithm:

        if key is purely numeric:
            expected_key = (12 + 3 * (SIREN mod 97)) mod 97
            assert int(key) == expected_key

    For alphanumeric keys (the more recent format used on overseas
    territories and certain entity types), the structural check
    passes and the registry verifies actual existence at filing time.
    Pure-numeric keys are checksum-verified inline.

    Returns the canonical 'FRXXnnnnnnnnn' form on success.
    """
    canonical = normalise_fr_vat(value)
    if not canonical:
        raise FrProfileError("VAT identifier cannot be empty")
    if not _FR_VAT_RE.fullmatch(canonical):
        raise FrProfileError(
            "FR VAT must be 'FR' + 2 alphanumeric + 9 digits "
            "(got %r after normalisation)." % canonical,
        )
    key = canonical[2:4]
    siren = canonical[4:]
    # Numeric key: verify via mod-97 algorithm.
    if key.isdigit():
        expected = (12 + 3 * (int(siren) % 97)) % 97
        if int(key) != expected:
            raise FrProfileError(
                "FR VAT %s failed the DGFiP mod-97 checksum "
                "(expected key %02d, got %s)."
                % (canonical, expected, key),
            )
    # Alphanumeric key: structural check only; DGFiP registry verifies
    # at filing time.
    return canonical


def is_valid_fr_vat(value):
    try:
        validate_fr_vat(value)
        return True
    except FrProfileError:
        return False


# ---- SIRET ------------------------------------------------------------------


def normalise_fr_siret(value):
    if not value:
        return ''
    return re.sub(r'[\s\-_.]+', '', str(value))


def validate_fr_siret(value):
    """Validate a French SIRET (establishment identifier).

    Format: 14 digits. Checksum via Luhn algorithm on the 14 digits.
    The first 9 digits are the SIREN (legal entity); the next 5 are
    the establishment number (NIC). Returns canonical 14-digit form.
    """
    canonical = normalise_fr_siret(value)
    if not canonical:
        raise FrProfileError("SIRET cannot be empty")
    if not _SIRET_RE.fullmatch(canonical):
        raise FrProfileError(
            "SIRET must be exactly 14 digits (got %d after "
            "stripping separators)." % len(canonical),
        )
    if not _luhn_valid(canonical):
        raise FrProfileError(
            "SIRET %s failed the Luhn checksum." % canonical,
        )
    return canonical


def is_valid_fr_siret(value):
    try:
        validate_fr_siret(value)
        return True
    except FrProfileError:
        return False


def _luhn_valid(s):
    """Luhn check: doubles every second digit from the right; valid
    when the sum is divisible by 10.
    """
    total = 0
    for pos, ch in enumerate(reversed(s)):
        d = int(ch)
        if pos % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0
