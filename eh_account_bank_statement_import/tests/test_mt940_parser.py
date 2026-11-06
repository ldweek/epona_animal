# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""MT940 parser contract and malformed-input coverage."""

from datetime import date

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_bank_statement_import.parsers.base import (
    StatementParserError,
)
from odoo.addons.eh_account_bank_statement_import.parsers.mt940_parser import (
    Mt940StatementParser,
)


@tagged('eh_account_bank_statement_import', 'post_install', '-at_install')
class TestMt940Parser(TransactionCase):

    def test_account_balances_refs_and_narration(self):
        parsed = Mt940StatementParser().parse(b"\n".join((
            b":20:STATEMENT-202608",
            b":25:AU1234567890",
            b":60F:C260801AUD100,00",
            b":61:2608020802D12,34NTRFCUST-1//BANK-1",
            b":86:?20EREF-1?32Example Shop?33Melbourne",
            b":61:2608030803C20,00NTRF",
            b":62F:C260803AUD107,66",
        )))
        self.assertEqual(parsed['account_id'], 'AU1234567890')
        self.assertEqual(parsed['currency_code'], 'AUD')
        self.assertEqual(parsed['statement_date'], date(2026, 8, 3))
        self.assertEqual(parsed['opening_balance'], 100.0)
        self.assertEqual(parsed['closing_balance'], 107.66)
        self.assertEqual(len(parsed['lines']), 2)
        self.assertEqual(parsed['lines'][0]['amount'], -12.34)
        self.assertEqual(parsed['lines'][0]['unique_import_ref'], 'BANK-1')
        self.assertEqual(parsed['lines'][0]['partner_name'], 'Example Shop')
        self.assertEqual(parsed['lines'][1]['amount'], 20.0)
        self.assertFalse(parsed['lines'][1]['unique_import_ref'])

    def test_reversal_marks_flip_sign(self):
        parser = Mt940StatementParser()
        reversed_credit = parser._parse_line_61(
            '2608040804RC5,00NTRFRC-1')
        reversed_debit = parser._parse_line_61(
            '2608040804RD7,00NTRFRD-1')
        self.assertEqual(reversed_credit['amount'], -5.0)
        self.assertEqual(reversed_debit['amount'], 7.0)

    def test_multi_statement_and_malformed_input_fail_closed(self):
        parser = Mt940StatementParser()
        with self.assertRaises(StatementParserError):
            parser.parse(b":20:ONE\n:20:TWO\n:60F:C260801AUD1,00")
        with self.assertRaises(StatementParserError):
            parser.parse(b":20:ONE\n:60F:not-a-balance")

    def test_intermediate_balances_do_not_overwrite_final_anchors(self):
        parsed = Mt940StatementParser().parse(b"\n".join((
            b":20:PAGED-ONE",
            b":25:AU1234567890",
            b":60F:C260801AUD100,00",
            b":60M:C260802AUD150,00",
            b":61:2608020802C50,00NTRFREF-1",
            b":62M:C260802AUD150,00",
            b":62F:C260803AUD155,00",
        )))
        self.assertEqual(parsed['opening_balance'], 100.0)
        self.assertEqual(parsed['closing_balance'], 155.0)
        self.assertEqual(parsed['statement_date'], date(2026, 8, 3))
