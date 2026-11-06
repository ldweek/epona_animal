# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
OFX parser integration test using an embedded SGML OFX 1.0 sample.

Skipped when the `ofxparse` library is not installed, since OFX
parsing is a pluggable optional dependency. Verifies that the parser
returns the normalised dict shape declared by StatementParser.parse,
that signs are correct on debit and credit transactions, and that
the statement-level balances are pulled from the LEDGERBAL block.
"""

import datetime
import unittest

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_bank_statement_import.parsers.ofx_parser import (
    OfxStatementParser,
)
from odoo.addons.eh_account_bank_statement_import.parsers.base import (
    StatementParserError,
)


_SAMPLE_OFX = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1>
<SONRS>
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<DTSERVER>20260415120000
<LANGUAGE>ENG
</SONRS>
</SIGNONMSGSRSV1>
<BANKMSGSRSV1>
<STMTTRNRS>
<TRNUID>1
<STATUS>
<CODE>0
<SEVERITY>INFO
</STATUS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM>
<BANKID>123456789
<ACCTID>987654321
<ACCTTYPE>CHECKING
</BANKACCTFROM>
<BANKTRANLIST>
<DTSTART>20260401120000
<DTEND>20260415120000
<STMTTRN>
<TRNTYPE>CREDIT
<DTPOSTED>20260410120000
<TRNAMT>500.00
<FITID>TXN001
<NAME>Customer Payment
<MEMO>Invoice INV-100
</STMTTRN>
<STMTTRN>
<TRNTYPE>DEBIT
<DTPOSTED>20260412120000
<TRNAMT>-25.50
<FITID>TXN002
<NAME>Bank Fee
<MEMO>Service charge
</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>2000.00
<DTASOF>20260415120000
</LEDGERBAL>
<AVAILBAL>
<BALAMT>1950.00
<DTASOF>20260415120000
</AVAILBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""


def _ofxparse_available():
    try:
        import ofxparse  # noqa: F401
        return True
    except ImportError:
        return False


@tagged('eh_account_bank_statement_import', 'unit')
class TestOfxParser(TransactionCase):

    @unittest.skipUnless(
        _ofxparse_available(),
        "ofxparse not installed; install with: pip install ofxparse",
    )
    def test_parse_two_transactions(self):
        parsed = OfxStatementParser().parse(_SAMPLE_OFX)
        self.assertEqual(len(parsed['lines']), 2)
        amounts = sorted(line['amount'] for line in parsed['lines'])
        self.assertEqual(amounts, [-25.50, 500.00])

    @unittest.skipUnless(_ofxparse_available(), "ofxparse not installed")
    def test_parse_unique_refs_use_fitid(self):
        parsed = OfxStatementParser().parse(_SAMPLE_OFX)
        refs = sorted(line['unique_import_ref'] for line in parsed['lines'])
        self.assertEqual(refs, ['TXN001', 'TXN002'])

    @unittest.skipUnless(_ofxparse_available(), "ofxparse not installed")
    def test_parse_extracts_currency_from_curdef(self):
        parsed = OfxStatementParser().parse(_SAMPLE_OFX)
        self.assertEqual(parsed['currency_code'], 'USD')

    @unittest.skipUnless(_ofxparse_available(), "ofxparse not installed")
    def test_parse_dates_returned_as_python_dates(self):
        parsed = OfxStatementParser().parse(_SAMPLE_OFX)
        for line in parsed['lines']:
            self.assertIsInstance(line['date'], datetime.date)

    @unittest.skipUnless(_ofxparse_available(), "ofxparse not installed")
    def test_parse_pulls_closing_balance(self):
        parsed = OfxStatementParser().parse(_SAMPLE_OFX)
        self.assertEqual(parsed['closing_balance'], 2000.00)

    @unittest.skipUnless(_ofxparse_available(), "ofxparse not installed")
    def test_parse_payee_and_memo(self):
        parsed = OfxStatementParser().parse(_SAMPLE_OFX)
        credit_line = next(
            line for line in parsed['lines'] if line['amount'] > 0
        )
        self.assertEqual(credit_line['payment_ref'], 'Customer Payment')
        self.assertIn('Invoice', credit_line['narration'])

    def test_parse_garbage_input_raises(self):
        if not _ofxparse_available():
            with self.assertRaises(StatementParserError):
                OfxStatementParser().parse(b'<<not OFX>>')
            return
        with self.assertRaises(StatementParserError):
            OfxStatementParser().parse(b'<<not OFX>>')

    def test_parse_without_library_raises_helpful_error(self):
        """When ofxparse is missing, the parser must surface a clear
        instruction rather than a bare ImportError. We can only assert
        this when the library is actually missing, so the test is
        conditional on its absence.
        """
        if _ofxparse_available():
            self.skipTest("ofxparse is installed; cannot exercise missing-lib path")
        with self.assertRaises(StatementParserError) as cm:
            OfxStatementParser().parse(_SAMPLE_OFX)
        self.assertIn('ofxparse', str(cm.exception))
