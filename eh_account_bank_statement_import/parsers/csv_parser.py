# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
CSV statement parser driven by a per-bank profile.

The profile tells the parser which column carries the date, which the
amount, which the payment reference. Decimal and date conventions are
profile-driven too, since EU-style ',' decimal versus US-style '.'
decimal is a common cause of silent corruption.
"""

import csv
import datetime
import hashlib
import io

from .base import StatementParser, StatementParserError


class CsvStatementParser(StatementParser):
    FORMAT_KEY = 'csv'
    FORMAT_LABEL = "CSV (per-bank profile)"

    def parse(self, content_bytes, profile=None):
        if profile is None:
            raise StatementParserError(
                "CSV parser requires a profile that maps the columns. "
                "Configure one under Configuration > Bank Statement "
                "Import Profiles before importing CSV files."
            )
        text = self._decode(content_bytes, profile)
        delimiter = profile.csv_delimiter or ','
        reader = csv.reader(
            io.StringIO(text),
            delimiter=delimiter,
            quotechar=profile.csv_quotechar or '"',
        )
        rows = list(reader)
        if profile.csv_header_rows:
            rows = rows[profile.csv_header_rows:]

        lines = []
        statement_date = None
        # Occurrence counter keyed by row content. The fingerprint must
        # not depend on a row's absolute position: a later re-export
        # with one row added or removed elsewhere would otherwise shift
        # every row below it, change their fingerprints, and defeat the
        # idempotency check so a partial re-import duplicates lines. Two
        # rows with genuinely identical content (same date, amount,
        # reference, narration) are a legitimate case a bank can
        # produce, so we disambiguate them by their order of appearance
        # among identical rows only, never by file position.
        seen_counts = {}
        for idx, row in enumerate(rows):
            if not any(cell.strip() for cell in row):
                continue
            try:
                date_value = self._parse_date(
                    self._cell(row, profile.col_date), profile,
                )
                amount = self._parse_amount(row, profile)
            except (IndexError, ValueError) as exc:
                raise StatementParserError(
                    "CSV row %d failed to parse: %s. Check the profile "
                    "column mapping and decimal convention." % (idx + 1, exc)
                )
            payment_ref = self._cell(row, profile.col_ref) or ''
            narration = self._cell(row, profile.col_narration) or ''
            partner_name = self._cell(row, profile.col_partner) or ''
            content_key = self._content_key(
                date_value, amount, payment_ref, narration,
            )
            occurrence = seen_counts.get(content_key, 0)
            seen_counts[content_key] = occurrence + 1
            unique_ref = self._row_fingerprint(content_key, occurrence)
            lines.append({
                'date': date_value,
                'amount': amount,
                'payment_ref': payment_ref.strip(),
                'narration': narration.strip(),
                'partner_name': partner_name.strip() or None,
                'unique_import_ref': unique_ref,
            })
            if statement_date is None or date_value > statement_date:
                statement_date = date_value

        return {
            'statement_date': statement_date,
            'opening_balance': None,
            'closing_balance': None,
            'currency_code': profile.currency_code or None,
            'lines': lines,
        }

    @staticmethod
    def _decode(content_bytes, profile):
        encoding = profile.csv_encoding or 'utf-8-sig'
        try:
            return content_bytes.decode(encoding)
        except UnicodeDecodeError:
            return content_bytes.decode('utf-8', errors='replace')

    @staticmethod
    def _cell(row, index):
        if index is None or index < 0 or index >= len(row):
            return ''
        return row[index]

    def _parse_date(self, value, profile):
        if not value:
            raise ValueError("date cell is empty")
        formats = (
            profile.date_format and [profile.date_format]
            or ['%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y']
        )
        for fmt in formats:
            try:
                return datetime.datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
        raise ValueError("date %r matches no configured format" % value)

    def _parse_amount(self, row, profile):
        if profile.col_amount is not None and profile.col_amount >= 0:
            raw = self._cell(row, profile.col_amount).strip()
            if not raw:
                return 0.0
            return self._normalise_amount(raw, profile)
        debit = self._cell(row, profile.col_debit).strip() if profile.col_debit is not None else ''
        credit = self._cell(row, profile.col_credit).strip() if profile.col_credit is not None else ''
        d_val = self._normalise_amount(debit, profile) if debit else 0.0
        c_val = self._normalise_amount(credit, profile) if credit else 0.0
        # Money in is positive on the bank account (credit on the bank),
        # money out is negative.
        return c_val - d_val

    @staticmethod
    def _normalise_amount(text, profile):
        if not text:
            return 0.0
        text = text.replace(' ', '').replace(' ', '')
        if profile.decimal_separator == ',':
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
        return float(text)

    @staticmethod
    def _content_key(date_value, amount, payment_ref, narration):
        """Normalised content tuple for a row.

        Used both to group identical rows for the occurrence counter and
        as the body of the fingerprint, so the grouping key and the hash
        can never drift apart.
        """
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
