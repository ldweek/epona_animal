# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
CSV statement parser unit tests.

The parser is a plain Python class so we test it without spinning up an
ORM. We mock the profile object with a SimpleNamespace because all the
parser cares about is duck-typed attributes.
"""

import datetime
import types

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_bank_statement_import.parsers.csv_parser import (
    CsvStatementParser,
)
from odoo.addons.eh_account_bank_statement_import.parsers.base import (
    StatementParserError,
)


def _profile(**overrides):
    base = {
        'csv_delimiter': ',',
        'csv_quotechar': '"',
        'csv_encoding': 'utf-8',
        'csv_header_rows': 1,
        'decimal_separator': '.',
        'date_format': '%Y-%m-%d',
        'col_date': 0,
        'col_amount': 1,
        'col_debit': -1,
        'col_credit': -1,
        'col_ref': 2,
        'col_narration': 3,
        'col_partner': -1,
        'currency_code': 'AUD',
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


@tagged('eh_account_bank_statement_import', 'unit')
class TestCsvParser(TransactionCase):

    def test_parse_simple_three_lines(self):
        content = (
            "Date,Amount,Reference,Memo\n"
            "2026-04-15,150.00,INV-100,Sale to A\n"
            "2026-04-16,-50.50,FEE,Bank fee\n"
            "2026-04-17,300.00,INV-101,Sale to B\n"
        ).encode('utf-8')
        parsed = CsvStatementParser().parse(content, profile=_profile())
        self.assertEqual(len(parsed['lines']), 3)
        self.assertEqual(parsed['lines'][0]['amount'], 150.00)
        self.assertEqual(parsed['lines'][1]['amount'], -50.50)
        self.assertEqual(parsed['lines'][1]['payment_ref'], 'FEE')
        self.assertEqual(parsed['statement_date'], datetime.date(2026, 4, 17))
        self.assertEqual(parsed['currency_code'], 'AUD')

    def test_unique_import_ref_is_stable(self):
        content = (
            "Date,Amount,Reference,Memo\n"
            "2026-04-15,150.00,INV-100,Sale to A\n"
        ).encode('utf-8')
        a = CsvStatementParser().parse(content, profile=_profile())
        b = CsvStatementParser().parse(content, profile=_profile())
        self.assertEqual(
            a['lines'][0]['unique_import_ref'],
            b['lines'][0]['unique_import_ref'],
        )

    def test_european_decimal_with_comma(self):
        content = (
            "Date;Amount;Ref;Memo\n"
            "15/04/2026;1.234,56;INV-100;Sale\n"
        ).encode('utf-8')
        parsed = CsvStatementParser().parse(
            content,
            profile=_profile(
                csv_delimiter=';',
                decimal_separator=',',
                date_format='%d/%m/%Y',
            ),
        )
        self.assertEqual(parsed['lines'][0]['amount'], 1234.56)
        self.assertEqual(parsed['lines'][0]['date'], datetime.date(2026, 4, 15))

    def test_separate_debit_credit_columns(self):
        content = (
            "Date,Debit,Credit,Ref,Memo\n"
            "2026-04-15,,150.00,INV-100,Sale\n"
            "2026-04-16,50.00,,FEE,Bank fee\n"
        ).encode('utf-8')
        parsed = CsvStatementParser().parse(
            content,
            profile=_profile(
                col_amount=-1,
                col_debit=1,
                col_credit=2,
                col_ref=3,
                col_narration=4,
            ),
        )
        self.assertEqual(parsed['lines'][0]['amount'], 150.00)
        self.assertEqual(parsed['lines'][1]['amount'], -50.00)

    def test_missing_profile_raises(self):
        with self.assertRaises(StatementParserError):
            CsvStatementParser().parse(b"a,b,c\n", profile=None)

    def test_header_rows_zero(self):
        content = (
            "2026-04-15,150.00,INV-100,Sale\n"
        ).encode('utf-8')
        parsed = CsvStatementParser().parse(
            content,
            profile=_profile(csv_header_rows=0),
        )
        self.assertEqual(len(parsed['lines']), 1)

    def test_garbage_row_raises_with_index(self):
        content = (
            "Date,Amount,Ref,Memo\n"
            "2026-04-15,150.00,OK,fine\n"
            "not-a-date,not-a-number,X,Y\n"
        ).encode('utf-8')
        with self.assertRaises(StatementParserError) as cm:
            CsvStatementParser().parse(content, profile=_profile())
        self.assertIn("row 2", str(cm.exception))

    def test_fingerprint_is_position_independent(self):
        # The headline regression. A later re-export of the same
        # statement inserts a brand-new row at the top. The three
        # original rows must keep the exact fingerprints they had in
        # the first import; otherwise the wizard's idempotency check
        # misses them and a partial re-upload duplicates every row
        # below the insertion point.
        first = (
            "Date,Amount,Reference,Memo\n"
            "2026-04-15,150.00,INV-100,Sale to A\n"
            "2026-04-16,-50.50,FEE,Bank fee\n"
            "2026-04-17,300.00,INV-101,Sale to B\n"
        ).encode('utf-8')
        second = (
            "Date,Amount,Reference,Memo\n"
            "2026-04-14,99.00,INV-099,Earlier sale\n"
            "2026-04-15,150.00,INV-100,Sale to A\n"
            "2026-04-16,-50.50,FEE,Bank fee\n"
            "2026-04-17,300.00,INV-101,Sale to B\n"
        ).encode('utf-8')
        a = CsvStatementParser().parse(first, profile=_profile())
        b = CsvStatementParser().parse(second, profile=_profile())
        a_refs = {line['unique_import_ref'] for line in a['lines']}
        b_refs = {line['unique_import_ref'] for line in b['lines']}
        # Every original fingerprint survives unchanged in the re-export
        # and only the one new row contributes a fingerprint not seen
        # before.
        self.assertTrue(a_refs.issubset(b_refs))
        self.assertEqual(len(b_refs - a_refs), 1)

    def test_identical_rows_get_distinct_fingerprints(self):
        # Two byte-identical transactions in one file are legitimate
        # (e.g., two equal card payments on the same day). They must
        # receive distinct fingerprints: both so neither is dropped as a
        # self-duplicate, and so the unique(journal, ref) constraint on
        # the statement line holds when both are inserted.
        content = (
            "Date,Amount,Reference,Memo\n"
            "2026-04-15,20.00,COFFEE,Cafe\n"
            "2026-04-15,20.00,COFFEE,Cafe\n"
        ).encode('utf-8')
        parsed = CsvStatementParser().parse(content, profile=_profile())
        refs = [line['unique_import_ref'] for line in parsed['lines']]
        self.assertEqual(len(refs), 2)
        self.assertNotEqual(refs[0], refs[1])

    def test_occurrence_counter_survives_unrelated_insert(self):
        # The two identical COFFEE rows must keep their fingerprints
        # even when an unrelated row is later inserted between them,
        # because the occurrence counter is scoped per content group,
        # not by absolute file position.
        before = (
            "Date,Amount,Reference,Memo\n"
            "2026-04-15,20.00,COFFEE,Cafe\n"
            "2026-04-15,20.00,COFFEE,Cafe\n"
        ).encode('utf-8')
        after = (
            "Date,Amount,Reference,Memo\n"
            "2026-04-15,20.00,COFFEE,Cafe\n"
            "2026-04-15,-5.00,FEE,Card fee\n"
            "2026-04-15,20.00,COFFEE,Cafe\n"
        ).encode('utf-8')
        a = CsvStatementParser().parse(before, profile=_profile())
        b = CsvStatementParser().parse(after, profile=_profile())
        a_coffee = {
            line['unique_import_ref']
            for line in a['lines']
            if line['payment_ref'] == 'COFFEE'
        }
        b_coffee = {
            line['unique_import_ref']
            for line in b['lines']
            if line['payment_ref'] == 'COFFEE'
        }
        self.assertEqual(a_coffee, b_coffee)
