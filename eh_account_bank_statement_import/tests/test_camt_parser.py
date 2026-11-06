# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
CAMT.053 (ISO 20022) parser tests using an embedded sample document.

Verifies the parser's namespace detection, sign handling for CRDT vs
DBIT entries, opening / closing balance extraction from the OPBD /
CLBD codes, and fallbacks for unique import references.
"""

import datetime

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_bank_statement_import.parsers.camt_parser import (
    Camt053StatementParser,
)
from odoo.addons.eh_account_bank_statement_import.parsers.base import (
    StatementParserError,
)


_SAMPLE_CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <GrpHdr>
      <MsgId>MSG001</MsgId>
      <CreDtTm>2026-04-15T10:00:00</CreDtTm>
    </GrpHdr>
    <Stmt>
      <Id>STMT001</Id>
      <CreDtTm>2026-04-15T10:00:00</CreDtTm>
      <FrToDt>
        <FrDtTm>2026-04-01T00:00:00</FrDtTm>
        <ToDtTm>2026-04-15T23:59:59</ToDtTm>
      </FrToDt>
      <Acct>
        <Id>
          <IBAN>DE12345678901234567890</IBAN>
        </Id>
        <Ccy>EUR</Ccy>
      </Acct>
      <Bal>
        <Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="EUR">1000.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <Dt><Dt>2026-04-01</Dt></Dt>
      </Bal>
      <Bal>
        <Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
        <Amt Ccy="EUR">1474.50</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <Dt><Dt>2026-04-15</Dt></Dt>
      </Bal>
      <Ntry>
        <Amt Ccy="EUR">500.00</Amt>
        <CdtDbtInd>CRDT</CdtDbtInd>
        <BookgDt><Dt>2026-04-10</Dt></BookgDt>
        <ValDt><Dt>2026-04-10</Dt></ValDt>
        <AcctSvcrRef>SRV001</AcctSvcrRef>
        <NtryDtls>
          <TxDtls>
            <Refs>
              <AcctSvcrRef>SRV001</AcctSvcrRef>
            </Refs>
            <RltdPties>
              <Dbtr>
                <Nm>Customer Inc</Nm>
              </Dbtr>
            </RltdPties>
            <RmtInf>
              <Ustrd>Invoice INV-100</Ustrd>
            </RmtInf>
          </TxDtls>
        </NtryDtls>
      </Ntry>
      <Ntry>
        <Amt Ccy="EUR">25.50</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <BookgDt><Dt>2026-04-12</Dt></BookgDt>
        <ValDt><Dt>2026-04-12</Dt></ValDt>
        <AcctSvcrRef>SRV002</AcctSvcrRef>
        <NtryDtls>
          <TxDtls>
            <Refs>
              <AcctSvcrRef>SRV002</AcctSvcrRef>
            </Refs>
            <RmtInf>
              <Ustrd>Bank service charge</Ustrd>
            </RmtInf>
          </TxDtls>
        </NtryDtls>
      </Ntry>
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


@tagged('eh_account_bank_statement_import', 'unit')
class TestCamt053Parser(TransactionCase):

    def test_parse_two_entries(self):
        parsed = Camt053StatementParser().parse(_SAMPLE_CAMT)
        self.assertEqual(len(parsed['lines']), 2)

    def test_signs_credit_positive_debit_negative(self):
        parsed = Camt053StatementParser().parse(_SAMPLE_CAMT)
        amounts = sorted(line['amount'] for line in parsed['lines'])
        self.assertEqual(amounts, [-25.50, 500.00])

    def test_extracts_currency(self):
        parsed = Camt053StatementParser().parse(_SAMPLE_CAMT)
        self.assertEqual(parsed['currency_code'], 'EUR')

    def test_extracts_balances(self):
        parsed = Camt053StatementParser().parse(_SAMPLE_CAMT)
        self.assertEqual(parsed['opening_balance'], 1000.00)
        self.assertEqual(parsed['closing_balance'], 1474.50)

    def test_unique_ref_uses_acct_svcr_ref(self):
        parsed = Camt053StatementParser().parse(_SAMPLE_CAMT)
        refs = sorted(line['unique_import_ref'] for line in parsed['lines'])
        self.assertEqual(refs, ['SRV001', 'SRV002'])

    def test_payment_ref_pulls_remittance_info(self):
        parsed = Camt053StatementParser().parse(_SAMPLE_CAMT)
        credit_line = next(
            line for line in parsed['lines'] if line['amount'] > 0
        )
        self.assertEqual(credit_line['payment_ref'], 'Invoice INV-100')

    def test_partner_name_from_related_parties(self):
        parsed = Camt053StatementParser().parse(_SAMPLE_CAMT)
        credit_line = next(
            line for line in parsed['lines'] if line['amount'] > 0
        )
        self.assertEqual(credit_line['partner_name'], 'Customer Inc')

    def test_dates_returned_as_python_dates(self):
        parsed = Camt053StatementParser().parse(_SAMPLE_CAMT)
        for line in parsed['lines']:
            self.assertIsInstance(line['date'], datetime.date)
        self.assertIsInstance(parsed['statement_date'], datetime.date)

    def test_garbage_input_raises(self):
        with self.assertRaises(StatementParserError):
            Camt053StatementParser().parse(b'<<not XML>>')

    def test_xml_without_stmt_raises(self):
        empty = (
            b'<?xml version="1.0"?>\n'
            b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"/>\n'
        )
        with self.assertRaises(StatementParserError):
            Camt053StatementParser().parse(empty)

    def test_doctype_declaration_rejected(self):
        """A DOCTYPE-bearing document is rejected outright (XXE guard),
        regardless of the underlying libxml2 version's defaults."""
        with_doctype = (
            b'<?xml version="1.0"?>\n'
            b'<!DOCTYPE Document [<!ENTITY foo "bar">]>\n'
            b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">'
            b'<BkToCstmrStmt><Stmt><Id>S</Id></Stmt></BkToCstmrStmt>'
            b'</Document>\n'
        )
        with self.assertRaises(StatementParserError):
            Camt053StatementParser().parse(with_doctype)

    def test_external_entity_not_resolved(self):
        """An internal-subset SYSTEM entity must never be expanded into the
        parsed payload. The hardened parser rejects the DOCTYPE before any
        local file can be read (XXE / local-file disclosure)."""
        xxe = (
            b'<?xml version="1.0"?>\n'
            b'<!DOCTYPE Document [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>\n'
            b'<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">'
            b'<BkToCstmrStmt><Stmt><Id>S</Id>'
            b'<Ntry><Amt Ccy="EUR">1.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>'
            b'<BookgDt><Dt>2026-04-10</Dt></BookgDt>'
            b'<NtryDtls><TxDtls><RmtInf><Ustrd>&xxe;</Ustrd></RmtInf>'
            b'</TxDtls></NtryDtls></Ntry>'
            b'</Stmt></BkToCstmrStmt></Document>\n'
        )
        with self.assertRaises(StatementParserError):
            Camt053StatementParser().parse(xxe)

    def test_multiple_statements_are_returned_separately(self):
        multiple_statements = _SAMPLE_CAMT.replace(
            b'</BkToCstmrStmt>',
            b'<Stmt><Id>SECOND</Id></Stmt></BkToCstmrStmt>',
        )
        parsed = Camt053StatementParser().parse(multiple_statements)
        self.assertEqual(len(parsed['statements']), 2)
        self.assertEqual(
            [item['source_statement_id'] for item in parsed['statements']],
            ['STMT001', 'SECOND'],
        )

    def test_batched_details_split_when_amounts_prove_entry_total(self):
        multiple_details = _SAMPLE_CAMT.replace(
            b'<TxDtls>',
            b'<TxDtls><AmtDtls><TxAmt><Amt Ccy="EUR">200.00</Amt>'
            b'</TxAmt></AmtDtls>',
            1,
        ).replace(
            b'</NtryDtls>',
            b'<TxDtls><AmtDtls><TxAmt><Amt Ccy="EUR">300.00</Amt>'
            b'</TxAmt></AmtDtls><Refs><TxId>TX-SECOND</TxId></Refs>'
            b'<RltdPties><Dbtr><Nm>Second Customer</Nm></Dbtr></RltdPties>'
            b'<RmtInf><Ustrd>SECOND</Ustrd></RmtInf>'
            b'</TxDtls></NtryDtls>',
            1,
        )
        parsed = Camt053StatementParser().parse(multiple_details)
        self.assertEqual(len(parsed['lines']), 3)
        self.assertEqual(
            [line['amount'] for line in parsed['lines'][:2]],
            [200.0, 300.0],
        )
        self.assertEqual(parsed['lines'][1]['partner_name'], 'Second Customer')
        self.assertEqual(parsed['lines'][1]['unique_import_ref'], 'TX-SECOND')

    def test_batched_details_without_exact_footing_fail_closed(self):
        multiple_details = _SAMPLE_CAMT.replace(
            b'<TxDtls>',
            b'<TxDtls><AmtDtls><TxAmt><Amt Ccy="EUR">200.00</Amt>'
            b'</TxAmt></AmtDtls>',
            1,
        ).replace(
            b'</NtryDtls>',
            b'<TxDtls><AmtDtls><TxAmt><Amt Ccy="EUR">299.99</Amt>'
            b'</TxAmt></AmtDtls></TxDtls></NtryDtls>',
            1,
        )
        with self.assertRaisesRegex(StatementParserError, 'do not equal'):
            Camt053StatementParser().parse(multiple_details)

    def test_nonbooked_entry_fails_closed(self):
        pending = _SAMPLE_CAMT.replace(
            b'<Ntry>', b'<Ntry><Sts><Cd>PDNG</Cd></Sts>', 1,
        )
        with self.assertRaisesRegex(StatementParserError, 'non-booked'):
            Camt053StatementParser().parse(pending)

    def test_reversal_direction_and_foreign_amount_are_preserved(self):
        sample = _SAMPLE_CAMT.replace(
            b'<Ntry>', b'<Ntry><Sts><Cd>BOOK</Cd></Sts>'
            b'<RvslInd>true</RvslInd>', 1,
        ).replace(
            b'<TxDtls>',
            b'<TxDtls><AmtDtls><TxAmt><Amt Ccy="USD">600.00</Amt>'
            b'</TxAmt></AmtDtls>',
            1,
        )
        parsed = Camt053StatementParser().parse(sample)
        reversed_credit = parsed['lines'][0]
        self.assertEqual(reversed_credit['amount'], -500.0)
        self.assertEqual(reversed_credit['partner_name'], 'Customer Inc')
        self.assertEqual(reversed_credit['foreign_currency_code'], 'USD')
        self.assertEqual(reversed_credit['foreign_amount'], -600.0)
