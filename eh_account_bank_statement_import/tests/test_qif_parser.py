# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
QIF statement parser unit tests.

Plain-Python parser, tested without the ORM. The profile is duck-typed
with a SimpleNamespace because the parser only reads date_format,
decimal_separator and currency_code off it.
"""

import datetime
import types

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_bank_statement_import.parsers.qif_parser import (
    QifStatementParser,
)
from odoo.addons.eh_account_bank_statement_import.parsers.base import (
    StatementParserError,
)


def _profile(**overrides):
    base = {
        'date_format': None,
        'decimal_separator': '.',
        'currency_code': 'AUD',
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


@tagged('eh_account_bank_statement_import', 'unit')
class TestQifParser(TransactionCase):

    def test_parse_bank_section(self):
        content = (
            "!Type:Bank\n"
            "D04/15/2026\n"
            "T150.00\n"
            "PSale to A\n"
            "MInvoice INV-100\n"
            "N100\n"
            "^\n"
            "D04/16/2026\n"
            "T-50.50\n"
            "PBank\n"
            "MMonthly fee\n"
            "^\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(content, profile=_profile())
        self.assertEqual(len(parsed['lines']), 2)
        self.assertEqual(parsed['lines'][0]['amount'], 150.00)
        self.assertEqual(parsed['lines'][0]['payment_ref'], 'Sale to A')
        self.assertEqual(parsed['lines'][0]['partner_name'], 'Sale to A')
        self.assertEqual(parsed['lines'][0]['narration'], 'Invoice INV-100')
        self.assertEqual(parsed['lines'][1]['amount'], -50.50)
        self.assertEqual(parsed['statement_date'], datetime.date(2026, 4, 16))
        self.assertEqual(parsed['currency_code'], 'AUD')

    def test_no_profile_defaults(self):
        content = (
            "!Type:Bank\n"
            "D04/15/2026\nT10.00\nPx\n^\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(content, profile=None)
        self.assertEqual(len(parsed['lines']), 1)
        self.assertIsNone(parsed['currency_code'])

    def test_payee_falls_back_to_number(self):
        content = (
            "!Type:Bank\n"
            "D04/15/2026\nT-20.00\nN5567\nMATM withdrawal\n^\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(content, profile=_profile())
        self.assertEqual(parsed['lines'][0]['payment_ref'], '5567')
        self.assertIsNone(parsed['lines'][0]['partner_name'])

    def test_category_section_is_skipped(self):
        # A !Type:Cat list bundled in the same file must not produce
        # statement lines even though its entries carry D (description)
        # and ^ terminators.
        content = (
            "!Type:Cat\n"
            "NGroceries\nDFood and household\nE\n^\n"
            "!Type:Bank\n"
            "D04/15/2026\nT99.00\nPShop\n^\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(content, profile=_profile())
        self.assertEqual(len(parsed['lines']), 1)
        self.assertEqual(parsed['lines'][0]['amount'], 99.00)

    def test_account_block_is_skipped(self):
        content = (
            "!Account\n"
            "NMy Checking\nTBank\n^\n"
            "!Type:Bank\n"
            "D04/15/2026\nT5.00\nPa\n^\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(content, profile=_profile())
        self.assertEqual(len(parsed['lines']), 1)

    def test_european_decimal_and_date(self):
        content = (
            "!Type:Bank\n"
            "D15/04/2026\nT1.234,56\nPSale\n^\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(
            content,
            profile=_profile(decimal_separator=',', date_format='%d/%m/%Y'),
        )
        self.assertEqual(parsed['lines'][0]['amount'], 1234.56)
        self.assertEqual(parsed['lines'][0]['date'], datetime.date(2026, 4, 15))

    def test_apostrophe_century_marker(self):
        content = (
            "!Type:Bank\n"
            "D8/15'09\nT1.00\nPx\n^\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(content, profile=_profile())
        self.assertEqual(parsed['lines'][0]['date'], datetime.date(2009, 8, 15))

    def test_u_amount_fallback(self):
        content = (
            "!Type:Bank\n"
            "D04/15/2026\nU42.00\nPx\n^\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(content, profile=_profile())
        self.assertEqual(parsed['lines'][0]['amount'], 42.00)

    def test_unique_import_ref_is_stable(self):
        content = (
            "!Type:Bank\n"
            "D04/15/2026\nT150.00\nPSale\nMref\n^\n"
        ).encode('utf-8')
        a = QifStatementParser().parse(content, profile=_profile())
        b = QifStatementParser().parse(content, profile=_profile())
        self.assertEqual(
            a['lines'][0]['unique_import_ref'],
            b['lines'][0]['unique_import_ref'],
        )

    def test_identical_entries_get_distinct_refs(self):
        content = (
            "!Type:Bank\n"
            "D04/15/2026\nT-4.50\nPCoffee\n^\n"
            "D04/15/2026\nT-4.50\nPCoffee\n^\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(content, profile=_profile())
        refs = [line['unique_import_ref'] for line in parsed['lines']]
        self.assertEqual(len(refs), 2)
        self.assertNotEqual(refs[0], refs[1])

    def test_final_record_without_caret(self):
        # Some exporters omit the trailing '^' on the last record. It must
        # still be imported, not silently dropped.
        content = (
            "!Type:Bank\n"
            "D04/15/2026\nT10.00\nPFirst\n^\n"
            "D04/16/2026\nT20.00\nPLast no caret\n"
        ).encode('utf-8')
        parsed = QifStatementParser().parse(content, profile=_profile())
        self.assertEqual(len(parsed['lines']), 2)
        self.assertEqual(parsed['lines'][1]['amount'], 20.00)
        self.assertEqual(parsed['lines'][1]['payment_ref'], 'Last no caret')

    def test_no_transaction_section_raises(self):
        content = (
            "!Type:Cat\n"
            "NGroceries\nDFood\n^\n"
        ).encode('utf-8')
        with self.assertRaises(StatementParserError):
            QifStatementParser().parse(content, profile=_profile())

    def test_incomplete_transaction_raises(self):
        content = (
            "!Type:Bank\n"
            "D04/15/2026\nPMissing amount\n^\n"
        ).encode('utf-8')
        with self.assertRaises(StatementParserError) as cm:
            QifStatementParser().parse(content, profile=_profile())
        self.assertIn("amount", str(cm.exception))

    def test_bad_date_raises_with_line(self):
        content = (
            "!Type:Bank\n"
            "Dnot-a-date\nT10.00\nPx\n^\n"
        ).encode('utf-8')
        with self.assertRaises(StatementParserError) as cm:
            QifStatementParser().parse(content, profile=_profile())
        self.assertIn("line", str(cm.exception))
