# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Peppol participant identifier validation.

OpenPeppol identifies participants by a (scheme, identifier) pair. The
scheme is a 4-digit code drawn from the OpenPeppol code list (a subset
of ISO 6523 ICD codes plus a handful of Peppol-only allocations). The
identifier shape depends on the scheme:

* 0151 (Australian Business Number) is 11 numeric digits and carries a
  weighted-checksum digit that must verify.
* 0184 (DK CVR) and 0007 (SE Organisationsnummer) are numeric strings of
  fixed length. 0192 (NO Organisasjonsnummer) is 9 digits with mod-11.
* 0088 (GLN / EAN) is 13 digits with mod-10 GS1 checksum.
* 9930 (DE VAT) is the German VAT identifier with a country-prefixed
  numeric body.
* The remaining schemes are passed through with only basic length and
  charset checks; per-scheme rules can be added on demand.

Public references:

* OpenPeppol Code Lists v8.x (2024)
* ISO 6523 ICD registry
* Australian Business Register ABN format spec (ATO publication)
* GS1 GLN / mod-10 algorithm

This module is original work; the validators implement the public
algorithms above and do not derive from any third-party library.
"""

import re


class PeppolIdentifierError(ValueError):
    """Raised when a participant id fails scheme-specific validation."""


# Source of truth for scheme codes. Subset that the suite actively
# supports with per-scheme rules; other schemes pass the generic
# format check (4 digits, numeric).
_SCHEMES_WITH_RULES = {
    '0007', '0088', '0151', '0184', '0192', '0196', '9930',
}

# Full code list used to reject made-up schemes. Trimmed to the codes
# present in OpenPeppol code list v8.7 (2024-09); add more as Peppol
# allocates them.
_KNOWN_SCHEMES = {
    '0002', '0007', '0009', '0037', '0060', '0088', '0090', '0096',
    '0097', '0106', '0130', '0135', '0142', '0147', '0151', '0152',
    '0154', '0183', '0184', '0188', '0190', '0191', '0192', '0193',
    '0195', '0196', '0198', '0199', '0200', '0201', '0202', '0204',
    '0208', '0209', '0210', '0211', '0212', '0213', '0215', '0216',
    '0217', '0218', '0219', '0220', '0221', '0222', '0223', '0224',
    '0225', '0226', '0227', '0228', '0229', '0230', '0231',
    '9913', '9914', '9915', '9918', '9919', '9920', '9922', '9923',
    '9924', '9925', '9926', '9927', '9928', '9929', '9930', '9931',
    '9932', '9933', '9934', '9935', '9936', '9937', '9938', '9939',
    '9940', '9941', '9942', '9943', '9944', '9945', '9946', '9947',
    '9948', '9949', '9950', '9951', '9952', '9953', '9955', '9956',
    '9957', '9958', '9959',
}


def normalise_id(value):
    """Strip whitespace and inner separators from a participant id.

    OpenPeppol participant ids are exchanged without spacing on the
    wire, but humans paste them with spaces, dashes, or dots. The
    transport-layer id is the alphanumeric sequence; we drop those
    separators so the validator and the eventual XML render agree.
    """
    if value is None:
        return ''
    return re.sub(r'[\s\-_.:]', '', str(value)).strip()


def validate_scheme(scheme):
    """Return the canonical 4-digit scheme or raise."""
    s = (scheme or '').strip()
    if not s:
        raise PeppolIdentifierError("scheme is required")
    if not re.fullmatch(r'\d{4}', s):
        raise PeppolIdentifierError(
            "scheme must be 4 numeric digits (got %r)" % scheme,
        )
    if s not in _KNOWN_SCHEMES:
        raise PeppolIdentifierError(
            "scheme %s is not in the OpenPeppol code list" % s,
        )
    return s


def validate_participant(scheme, identifier):
    """Validate a (scheme, identifier) pair.

    Returns the canonicalised (scheme, identifier) tuple on success.
    Raises PeppolIdentifierError on any failure with a message naming
    the rule that was violated; the caller surfaces this as a UserError
    so the operator sees a precise reason.
    """
    scheme = validate_scheme(scheme)
    raw = normalise_id(identifier)
    if not raw:
        raise PeppolIdentifierError("identifier is required")

    if scheme == '0151':
        return scheme, _validate_abn(raw)
    if scheme == '0088':
        return scheme, _validate_gln(raw)
    if scheme == '0007':
        return scheme, _validate_numeric_fixed(raw, length=10, scheme=scheme)
    if scheme == '0192':
        return scheme, _validate_norwegian_org_number(raw)
    if scheme == '0184':
        return scheme, _validate_numeric_fixed(raw, length=8, scheme=scheme)
    if scheme == '0196':
        # Iceland Kennitala: 10 digits, ddmmyyXXXX form.
        return scheme, _validate_numeric_fixed(raw, length=10, scheme=scheme)
    if scheme == '9930':
        # Country-prefixed VAT id. Generic check: 2 letters + 1..14
        # alphanumerics. Per-country VAT validation lives in res.partner
        # already; we only assert the shape here.
        if not re.fullmatch(r'[A-Z]{2}[0-9A-Z]{1,14}', raw.upper()):
            raise PeppolIdentifierError(
                "scheme 9930 expects a 2-letter country prefix followed "
                "by an alphanumeric VAT body (got %r)" % identifier,
            )
        return scheme, raw.upper()

    # Generic: alphanumeric, 1..50 chars. Tighter per-scheme rules
    # can be added without churning the call sites.
    if not re.fullmatch(r'[0-9A-Za-z]{1,50}', raw):
        raise PeppolIdentifierError(
            "identifier must be alphanumeric and at most 50 chars "
            "(scheme %s, got %r)" % (scheme, identifier),
        )
    return scheme, raw


def _validate_abn(value):
    """Australian Business Number: 11 digits, weighted checksum.

    Algorithm (ATO):
      1. Subtract 1 from the leading digit.
      2. Multiply each of the 11 digits by the weight at its position
         (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19).
      3. Sum the products. Sum mod 89 must equal 0.
    """
    if not re.fullmatch(r'\d{11}', value):
        raise PeppolIdentifierError(
            "ABN (scheme 0151) must be exactly 11 digits (got %r)" % value,
        )
    digits = [int(d) for d in value]
    digits[0] -= 1
    weights = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    total = sum(d * w for d, w in zip(digits, weights))
    if total % 89 != 0:
        raise PeppolIdentifierError(
            "ABN %s failed the weighted checksum; not a valid ABN" % value,
        )
    return value


def _validate_gln(value):
    """GS1 GLN: 13 digits, mod-10 (Luhn-like) check.

    Algorithm: weight every odd-positioned digit by 1 and every
    even-positioned digit by 3 (1-based from the left, excluding the
    final check digit). Sum, take mod 10, subtract from 10, take mod 10
    again. The result must equal the final digit.
    """
    if not re.fullmatch(r'\d{13}', value):
        raise PeppolIdentifierError(
            "GLN (scheme 0088) must be exactly 13 digits (got %r)" % value,
        )
    body = value[:12]
    expected = int(value[12])
    total = 0
    for i, ch in enumerate(body, start=1):
        weight = 1 if i % 2 == 1 else 3
        total += int(ch) * weight
    check = (10 - (total % 10)) % 10
    if check != expected:
        raise PeppolIdentifierError(
            "GLN %s failed the GS1 mod-10 check; not a valid GLN" % value,
        )
    return value


def _validate_norwegian_org_number(value):
    """Norwegian organisation number: 9 digits with mod-11 check.

    Peppol rule PEPPOL-COMMON-R041 applies weights 3,2,7,6,5,4,3,2 to
    the first eight digits.  Remainder 10 is invalid; remainder 11 maps to
    zero.  The result must equal digit nine.
    """
    if not re.fullmatch(r'\d{9}', value):
        raise PeppolIdentifierError(
            "Norwegian organisation number (scheme 0192) must be exactly "
            "9 digits (got %r)" % value,
        )
    weights = (3, 2, 7, 6, 5, 4, 3, 2)
    remainder = 11 - (
        sum(int(digit) * weight
            for digit, weight in zip(value[:8], weights)) % 11
    )
    if remainder == 11:
        remainder = 0
    if remainder == 10 or remainder != int(value[-1]):
        raise PeppolIdentifierError(
            "Norwegian organisation number %s failed the mod-11 checksum"
            % value,
        )
    return value


def _validate_numeric_fixed(value, length, scheme):
    if not re.fullmatch(r'\d{%d}' % length, value):
        raise PeppolIdentifierError(
            "scheme %s expects exactly %d digits (got %r)" % (
                scheme, length, value,
            ),
        )
    return value
