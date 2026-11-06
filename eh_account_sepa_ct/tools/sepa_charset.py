# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
SEPA character-set sanitisation.

ISO 20022 SEPA payment messages restrict free-text fields (names,
remittance information, identifiers) to the Latin character set:

    a-z A-Z 0-9 space and / - ? : ( ) . , ' +

Characters outside this set (accented Latin, non-Latin scripts, other
punctuation) are rejected by many banks' validation gateways. This
helper transliterates accented Latin characters to their ASCII base
(for example e-acute becomes e) and replaces anything still outside the
allowed set with a space, so generated XML always clears SEPA character
validation.

This lives in eh_account_sepa_ct because eh_account_sepa_dd already
depends on it; both modules import sanitize_sepa_text from here.
"""

import unicodedata


_SEPA_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "/-?:().,'+ "
)

_LATIN_TRANSLITERATION = str.maketrans({
    'Æ': 'AE', 'æ': 'ae', 'Œ': 'OE', 'œ': 'oe',
    'Ø': 'O', 'ø': 'o', 'Ł': 'L', 'ł': 'l',
    'Đ': 'D', 'đ': 'd', 'Ð': 'D', 'ð': 'd',
    'Þ': 'TH', 'þ': 'th', 'ß': 'ss', 'ı': 'i',
})


def sanitize_sepa_text(value):
    """Return ``value`` reduced to the SEPA Latin character set.

    None and empty strings pass through unchanged. Accented Latin
    characters are transliterated to their ASCII base; any remaining
    disallowed character becomes a space.
    """
    if not value:
        return value
    decomposed = unicodedata.normalize(
        'NFKD', value.translate(_LATIN_TRANSLITERATION),
    )
    out = []
    for ch in decomposed:
        if unicodedata.combining(ch):
            # Drop combining diacritical marks left by NFKD; the base
            # ASCII letter has already been emitted.
            continue
        out.append(ch if ch in _SEPA_ALLOWED else ' ')
    cleaned = ' '.join(''.join(out).split())
    while '//' in cleaned:
        cleaned = cleaned.replace('//', '/')
    # EPC permits slash only inside text, never first/last or doubled.
    return cleaned.strip('/').strip()
