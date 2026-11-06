# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Base parser interface for bank statement files.

Every concrete parser (CSV, OFX, CAMT.053) returns a normalised dict of
the form:

    {
        'statement_date': date,        # statement period end
        'opening_balance': float,      # account opening balance, may be None
        'closing_balance': float,      # account closing balance, may be None
        'currency_code': str,          # ISO currency, may be None
        'account_id': str,             # source account, may be None
        'lines': [
            {
                'date': date,
                'amount': float,        # signed; positive = money in
                'payment_ref': str,     # primary reference / counter party
                'narration': str,       # secondary memo
                'partner_name': str,    # may be None
                'unique_import_ref': str,  # idempotency key per line
            },
            ...
        ],
    }

Parsers are plain Python classes so they can be unit-tested without the
ORM. The orchestrator (the import wizard) wraps the parser output into
account.bank.statement / account.bank.statement.line records.
"""


class StatementParserError(ValueError):
    """Raised when a parser cannot understand the input bytes."""


class StatementParser:
    """Subclass and override parse(). Pure Python, no Odoo env needed."""

    FORMAT_KEY = ''
    FORMAT_LABEL = ''

    def parse(self, content_bytes, profile=None):
        """Return the normalised statement dict. Raise StatementParserError
        on malformed input.

        :param content_bytes: bytes of the statement file as uploaded.
        :param profile: optional eh.account.bank.statement.import.profile
            record carrying CSV column mapping for parsers that need it.
        """
        raise NotImplementedError(
            "%s.parse must be implemented by the concrete parser."
            % type(self).__name__
        )
