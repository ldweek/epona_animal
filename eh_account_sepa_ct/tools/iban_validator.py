# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
IBAN validation built from the public ISO 13616 / ECBS specification.

ISO 13616 defines an IBAN as: 2-letter country code, 2-digit check
digits, basic bank account number (BBAN). The check digits are
calculated such that, after rearranging the IBAN with the country
code and check digits moved to the end and converting letters to two-
digit numbers (A=10, B=11, ..., Z=35), the resulting integer modulo 97
must equal 1.

The per-country BBAN length comes from the IBAN registry. We embed the
length table for the SEPA zone here because that is the only set the
PAIN.001 generator needs to validate. Adding more is mechanical.

Public references:

* https://www.iso.org/standard/41031.html (ISO 13616-1)
* https://www.swift.com/standards/data-standards/iban-international-bank-account-number
* https://www.ecbs.org/iban.htm

This implementation is original work. No code derives from any
proprietary or open-source IBAN library; the algorithm comes
directly from the published rules above.
"""

import re


# IBAN total length per country, restricted to the SEPA zone plus a few
# common adjacent. Source: SWIFT IBAN registry, public.
_IBAN_LENGTHS = {
    'AD': 24, 'AT': 20, 'BE': 16, 'BG': 22, 'CH': 21, 'CY': 28,
    'CZ': 24, 'DE': 22, 'DK': 18, 'EE': 20, 'ES': 24, 'FI': 18,
    'FO': 18, 'FR': 27, 'GB': 22, 'GI': 23, 'GL': 18, 'GR': 27,
    'HR': 21, 'HU': 28, 'IE': 22, 'IS': 26, 'IT': 27, 'LI': 21,
    'LT': 20, 'LU': 20, 'LV': 21, 'MC': 27, 'MT': 31, 'NL': 18,
    'NO': 15, 'PL': 28, 'PT': 25, 'RO': 24, 'SE': 24, 'SI': 19,
    'SK': 24, 'SM': 27, 'VA': 22,
}


_VALID_CHARS = re.compile(r'^[A-Z0-9]+$')


class IbanValidationError(ValueError):
    """Raised when an IBAN string fails validation."""


def normalise_iban(value):
    """Strip spaces and uppercase the input. Returns the cleaned string.

    Does not validate; pair with validate_iban for the full check.
    """
    if not value:
        return ''
    return value.replace(' ', '').upper()


def validate_iban(value):
    """Validate an IBAN and return the normalised form on success.

    Raises IbanValidationError on any failure with a message that
    names the specific check that broke. Tests rely on the message
    naming so users get actionable errors not a bare 'invalid'.
    """
    cleaned = normalise_iban(value)
    if not cleaned:
        raise IbanValidationError("IBAN is empty")
    if len(cleaned) < 4:
        raise IbanValidationError(
            "IBAN must contain at least the country code and check digits",
        )
    if not _VALID_CHARS.match(cleaned):
        raise IbanValidationError(
            "IBAN contains characters outside [A-Z0-9]",
        )
    country = cleaned[:2]
    if country not in _IBAN_LENGTHS:
        raise IbanValidationError(
            "IBAN country %r is not in the SEPA zone IBAN registry"
            % country,
        )
    expected = _IBAN_LENGTHS[country]
    if len(cleaned) != expected:
        raise IbanValidationError(
            "IBAN for %s must be %d characters; got %d"
            % (country, expected, len(cleaned)),
        )
    if not cleaned[2:4].isdigit():
        raise IbanValidationError("IBAN check digits must be numeric")

    # Mod-97 check. Move the first four characters to the end and
    # convert letters to two-digit numbers (A=10..Z=35).
    rearranged = cleaned[4:] + cleaned[:4]
    numeric = ''
    for ch in rearranged:
        if ch.isdigit():
            numeric += ch
        else:
            numeric += str(ord(ch) - 55)
    if int(numeric) % 97 != 1:
        raise IbanValidationError(
            "IBAN check digits are not consistent with the BBAN "
            "(mod-97 verification failed)",
        )
    return cleaned


def is_iban(value):
    """Convenience wrapper: True if validate_iban does not raise."""
    try:
        validate_iban(value)
        return True
    except IbanValidationError:
        return False
