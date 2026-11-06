# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Australian Business Number (ABN) validator.

Pure-Python; no Odoo imports so the helper is unit-testable in
isolation. The algorithm is published by the ATO at the Australian
Business Register; the implementation here is independent.

Usage:

    from odoo.addons.eh_account_l10n_au_bas.tools.abn_validator import (
        validate_abn, normalise_abn, AbnValidationError,
    )

    canonical = validate_abn("83 914 571 673")  # returns "83914571673"
"""

import re


_ABN_DIGITS_RE = re.compile(r'\D+')
_WEIGHTS = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)


class AbnValidationError(ValueError):
    """Raised when a value does not validate as an ABN."""


def normalise_abn(value):
    """Strip whitespace, hyphens, and any non-digit characters.

    Does not validate; returns the bare 11-digit string (or shorter
    when the input is malformed). The caller passes the result to
    validate_abn for the format + checksum check.
    """
    if not value:
        return ''
    return _ABN_DIGITS_RE.sub('', str(value))


def validate_abn(value):
    """Validate an Australian Business Number.

    Returns the canonical 11-digit form on success. Raises
    AbnValidationError on any failure: wrong length, non-digit
    characters after normalisation, or failed weighted checksum.

    Algorithm (per ATO):
      1. Subtract 1 from the leading digit.
      2. Multiply each of the 11 digits by its position weight
         (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19).
      3. Sum the products. Sum mod 89 must equal 0.
    """
    canonical = normalise_abn(value)
    if len(canonical) != 11:
        raise AbnValidationError(
            "ABN must contain exactly 11 digits (got %d after "
            "stripping non-digits)." % len(canonical),
        )
    if not canonical.isdigit():
        raise AbnValidationError(
            "ABN must contain only digits after stripping whitespace "
            "and punctuation.",
        )
    digits = [int(d) for d in canonical]
    digits[0] -= 1
    total = sum(d * w for d, w in zip(digits, _WEIGHTS))
    if total % 89 != 0:
        raise AbnValidationError(
            "ABN %s failed the mod-89 weighted checksum and is not a "
            "valid Australian Business Number." % canonical,
        )
    return canonical


def is_valid_abn(value):
    """Return True when value validates as an ABN, False otherwise.

    Convenience wrapper for callers that prefer a boolean check over
    exception handling. Returns False for None, empty string, or any
    input that fails validate_abn.
    """
    try:
        validate_abn(value)
        return True
    except AbnValidationError:
        return False
