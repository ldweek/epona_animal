# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
OFX (Open Financial Exchange) statement parser.

OFX files come in two dialects, the legacy SGML form and the current
XML form. Rather than carry a hand-rolled reader for both, this parser
delegates to the `ofxparse` library, which handles the dialect
detection and the balance/transaction extraction, and maps its result
onto the normalised StatementParser dict.

`ofxparse` is the one non-stdlib dependency in this addon and is only
needed when an OFX file is actually imported, so it is imported lazily
inside parse(): sites that import only CSV, QIF, CAMT.053 or MT940 do
not need it installed, and a site that does import OFX without the
package gets a clear instruction to run `pip install ofxparse` instead
of an opaque ImportError.
"""

import datetime
import hashlib

from .base import StatementParser, StatementParserError


class OfxStatementParser(StatementParser):
    FORMAT_KEY = 'ofx'
    FORMAT_LABEL = "OFX (Open Financial Exchange)"

    def parse(self, content_bytes, profile=None):
        # Lazy import so the dependency is only required when OFX is used.
        try:
            from ofxparse import OfxParser  # type: ignore
        except ImportError as exc:
            raise StatementParserError(
                "OFX import requires the 'ofxparse' Python package. "
                "Install it with: pip install ofxparse"
            ) from exc

        try:
            from io import BytesIO
            ofx = OfxParser.parse(BytesIO(content_bytes))
        except Exception as exc:
            raise StatementParserError(
                "OFX file could not be parsed: %s" % exc,
            ) from exc

        accounts = list(ofx.accounts)
        if not accounts:
            raise StatementParserError(
                "OFX file contains no account section.",
            )
        if len(accounts) != 1:
            raise StatementParserError(
                "OFX files containing multiple accounts are not supported; "
                "export and import one bank account at a time."
            )
        account = accounts[0]
        statement = account.statement

        lines = []
        for tx in statement.transactions:
            tx_date = tx.date.date() if isinstance(tx.date, datetime.datetime) else tx.date
            unique_ref = (
                tx.id
                or self._fingerprint(tx_date, float(tx.amount), tx.memo or '')
            )
            lines.append({
                'date': tx_date,
                'amount': float(tx.amount),
                'payment_ref': (tx.payee or '').strip(),
                'narration': (tx.memo or '').strip(),
                'partner_name': (tx.payee or '').strip() or None,
                'unique_import_ref': unique_ref,
            })

        # ofxparse field names changed across versions. Map every
        # known balance attribute against OFX semantics:
        #   LEDGERBAL.BALAMT -> closing balance (the "ledger as of"
        #     value the bank reports). Newer ofxparse exposes this as
        #     `Statement.balance`; older as `end_balance`.
        #   AVAILBAL.BALAMT  -> available_balance (cleared funds; not
        #     used as opening or closing).
        #   Opening / start_balance is not represented in OFX 1.x at
        #     the statement level; older ofxparse synthesised it via
        #     start_balance, newer drops the attribute entirely.
        closing = (
            getattr(statement, 'end_balance', None)
            if getattr(statement, 'end_balance', None) is not None
            else getattr(statement, 'balance', None)
        )
        opening = getattr(statement, 'start_balance', None)
        currency_code = (
            getattr(statement, 'currency', None)
            or getattr(account, 'curdef', None)
        )
        if currency_code:
            currency_code = str(currency_code).upper()
        return {
            'statement_date': (
                statement.end_date.date()
                if isinstance(statement.end_date, datetime.datetime)
                else statement.end_date
            ),
            'opening_balance': float(opening) if opening is not None else None,
            'closing_balance': float(closing) if closing is not None else None,
            'currency_code': currency_code,
            'account_id': (
                getattr(account, 'account_id', None)
                or getattr(account, 'number', None)
            ),
            'lines': lines,
        }

    @staticmethod
    def _fingerprint(date_value, amount, memo):
        h = hashlib.sha1()
        h.update(date_value.isoformat().encode('utf-8'))
        h.update(b'|')
        h.update(("%.4f" % amount).encode('utf-8'))
        h.update(b'|')
        h.update(memo.encode('utf-8'))
        return h.hexdigest()
