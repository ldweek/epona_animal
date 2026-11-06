# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
QIF (Quicken Interchange Format) statement parser.

QIF is a line-oriented legacy export format still emitted by Quicken,
GnuCash, and a long tail of bank and credit-union portals. A file is a
sequence of sections introduced by a `!` directive (`!Type:Bank`,
`!Type:CCard`, `!Account`, `!Type:Cat`, ...). Inside a transaction
section each transaction is a run of single-letter field lines
terminated by a line containing only `^`:

    !Type:Bank
    D04/15/2026
    T-50.00
    PCoffee shop
    MCard payment
    N1234
    ^

Field codes we read: `D` date, `T`/`U` amount (signed; negative = money
out), `P` payee, `M` memo, `N` cheque/reference number. Investment
sections (`!Type:Invst`) and the non-transaction list sections
(`!Type:Cat`, `!Type:Class`, `!Type:Memorized`, `!Account`, ...) are
skipped, so a category list bundled into the same file never pollutes
the imported lines.

QIF carries no per-line unique identifier and no balance, so we mint a
stable idempotency key the same way the CSV parser does: a SHA-1
fingerprint over the normalised content of the line plus an occurrence
counter scoped per identical-content group. Re-importing the same file
therefore skips the lines already seen instead of doubling them, and two
genuinely identical transactions in one file still receive distinct
keys.

Date and decimal conventions are profile-driven when a profile is
supplied (QIF is wildly inconsistent on both across locales); without a
profile the parser falls back to the common US/EU date layouts and a dot
decimal separator.
"""

import datetime
import hashlib

from .base import StatementParser, StatementParserError


# Section types that carry transactions we want. Anything else
# (category/class/memorized/security lists, account definition blocks)
# is skipped so its entries never reach the statement.
_TXN_TYPES = frozenset({'bank', 'ccard', 'cash', 'oth a', 'oth l'})

# Date formats tried, in order, after separators are normalised to '/'.
# A profile.date_format, when supplied, is tried first against the raw
# value so an explicit format with any separator is honoured.
_DATE_FORMATS = (
    '%m/%d/%Y', '%d/%m/%Y', '%m/%d/%y', '%d/%m/%y',
    '%Y/%m/%d', '%Y/%d/%m',
)


class QifStatementParser(StatementParser):
    FORMAT_KEY = 'qif'
    FORMAT_LABEL = "QIF (Quicken Interchange Format)"

    def parse(self, content_bytes, profile=None):
        text = self._decode(content_bytes)
        date_format = getattr(profile, 'date_format', None) if profile else None
        decimal_sep = (
            getattr(profile, 'decimal_separator', None) if profile else None
        ) or '.'

        in_txn_section = False
        saw_txn_section = False
        current = {}
        lines = []
        statement_date = None
        seen_counts = {}
        lineno = 0

        def _commit(entry, at_line):
            emitted = self._emit(
                entry, at_line, date_format, decimal_sep, seen_counts,
            )
            if emitted is None:
                return
            lines.append(emitted)
            nonlocal statement_date
            if statement_date is None or emitted['date'] > statement_date:
                statement_date = emitted['date']

        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith('!'):
                # Section directive. Switch context and drop any
                # half-built entry from the previous section.
                current = {}
                directive = line[1:].strip().lower()
                if directive.startswith('type:'):
                    section = directive.split(':', 1)[1].strip()
                    in_txn_section = section in _TXN_TYPES
                    saw_txn_section = saw_txn_section or in_txn_section
                else:
                    # !Account, !Option, !Clear, ... -- not a transaction
                    # section. !Option/!Clear toggle flags rather than
                    # start a list, but treating them as non-transaction
                    # is safe: a real transaction is always preceded by
                    # its own !Type: header.
                    in_txn_section = False
                continue

            if not in_txn_section:
                continue

            code = line[0]
            value = line[1:].strip()
            if code == '^':
                _commit(current, lineno)
                current = {}
            elif code == 'D':
                current['date'] = value
            elif code in ('T', 'U'):
                # T is the canonical amount; U is a duplicate some
                # writers emit. Keep the first T seen, else fall back
                # to U so a U-only line still parses.
                if code == 'T' or 'amount' not in current:
                    current['amount'] = value
            elif code == 'P':
                current['payee'] = value
            elif code == 'M':
                current['memo'] = value
            elif code == 'N':
                current['number'] = value
            # All other codes (L category, C cleared, A address, S/E/$
            # split lines, investment fields) are ignored for statement
            # import.

        # A well-formed QIF terminates every record with '^', but some
        # exporters omit it on the final record. Flush a pending in-section
        # entry so the last transaction is never silently dropped.
        if in_txn_section and current:
            _commit(current, lineno)

        if not saw_txn_section:
            raise StatementParserError(
                "No bank transaction section found in the QIF file. A bank "
                "statement export must contain a !Type:Bank, !Type:CCard, "
                "!Type:Cash, !Type:Oth A or !Type:Oth L header."
            )

        return {
            'statement_date': statement_date,
            'opening_balance': None,
            'closing_balance': None,
            'currency_code': (
                getattr(profile, 'currency_code', None) if profile else None
            ) or None,
            'lines': lines,
        }

    def _emit(self, current, lineno, date_format, decimal_sep, seen_counts):
        """Turn one ^-terminated entry into a normalised line dict.

        Returns None for an empty entry (two consecutive ^), raises
        StatementParserError for an entry that has fields but is missing
        or malformed on date or amount.
        """
        if not current:
            return None
        if 'date' not in current or 'amount' not in current:
            raise StatementParserError(
                "QIF transaction ending at line %d is missing a date (D) or "
                "amount (T) field." % lineno
            )
        try:
            date_value = self._parse_date(current['date'], date_format)
            amount = self._parse_amount(current['amount'], decimal_sep)
        except ValueError as exc:
            raise StatementParserError(
                "QIF transaction ending at line %d failed to parse: %s."
                % (lineno, exc)
            )

        payee = (current.get('payee') or '').strip()
        memo = (current.get('memo') or '').strip()
        number = (current.get('number') or '').strip()
        payment_ref = payee or number or ''
        content_key = self._content_key(date_value, amount, payment_ref, memo)
        occurrence = seen_counts.get(content_key, 0)
        seen_counts[content_key] = occurrence + 1
        return {
            'date': date_value,
            'amount': amount,
            'payment_ref': payment_ref,
            'narration': memo,
            'partner_name': payee or None,
            'unique_import_ref': self._row_fingerprint(content_key, occurrence),
        }

    @staticmethod
    def _decode(content_bytes):
        for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
            try:
                return content_bytes.decode(encoding)
            except UnicodeDecodeError:
                continue
        return content_bytes.decode('utf-8', errors='replace')

    @staticmethod
    def _parse_date(value, date_format):
        raw = value.strip()
        if not raw:
            raise ValueError("date field is empty")
        # Honour an explicit profile format first, against the raw value
        # so its own separators line up.
        if date_format:
            try:
                return datetime.datetime.strptime(raw, date_format).date()
            except ValueError:
                pass
        # Quicken uses "'" as a century marker between day and year
        # (8/15'09 -> 2009) and tolerates '.' or '-' separators and
        # stray spaces. Normalise all of those to '/' and try the
        # common layouts.
        normalised = (
            raw.replace("'", '/').replace('.', '/').replace('-', '/')
        )
        normalised = normalised.replace(' ', '')
        for fmt in _DATE_FORMATS:
            try:
                return datetime.datetime.strptime(normalised, fmt).date()
            except ValueError:
                continue
        raise ValueError("date %r matches no known QIF date layout" % value)

    @staticmethod
    def _parse_amount(value, decimal_sep):
        text = value.strip().replace(' ', '').replace('\xa0', '')
        if not text:
            raise ValueError("amount field is empty")
        if decimal_sep == ',':
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
        try:
            return float(text)
        except ValueError:
            raise ValueError("amount %r is not a number" % value)

    @staticmethod
    def _content_key(date_value, amount, payment_ref, narration):
        return (
            date_value.isoformat(),
            "%.4f" % amount,
            payment_ref or '',
            narration or '',
        )

    @staticmethod
    def _row_fingerprint(content_key, occurrence):
        h = hashlib.sha1()
        for part in content_key:
            h.update(part.encode('utf-8'))
            h.update(b'|')
        h.update(str(occurrence).encode('utf-8'))
        return h.hexdigest()
