# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
CAMT.053 (ISO 20022) statement parser.

CAMT.053 is the standard XML format EU banks export for end-of-day
statements. The structure has nested namespaces and bank-specific
extensions; this parser implements the common subset that handles the
overwhelming majority of EU bank exports. For edge cases, customers can
inherit and override the methods that need adjusting.

References:

* ISO 20022 BankToCustomerStatementV02 (and later) schemas.
* Iso 20022 message reference manual at iso20022.org.

The implementation here uses lxml for XML parsing because Odoo already
ships with it.
"""

import datetime
import hashlib
from decimal import Decimal, InvalidOperation

from .base import StatementParser, StatementParserError


_CAMT_NS = {
    'urn:iso:std:iso:20022:tech:xsd:camt.053.001.02',
    'urn:iso:std:iso:20022:tech:xsd:camt.053.001.06',
    'urn:iso:std:iso:20022:tech:xsd:camt.053.001.08',
}


class Camt053StatementParser(StatementParser):
    FORMAT_KEY = 'camt053'
    FORMAT_LABEL = "CAMT.053 (ISO 20022)"

    def parse(self, content_bytes, profile=None):
        try:
            from lxml import etree
        except ImportError as exc:
            raise StatementParserError(
                "CAMT.053 import requires lxml, which Odoo bundles by "
                "default. If you see this error, your Python environment "
                "is missing it: pip install lxml"
            ) from exc

        # Uploaded statement files are untrusted external input. Parse
        # through an explicitly hardened parser so an internal-subset
        # SYSTEM entity cannot read local files (XXE) or fetch over the
        # network (SSRF), and so entity amplification cannot exhaust the
        # worker. This does not rely on version-dependent libxml2 defaults,
        # closing the gap across the whole 16-19 support matrix.
        secure_parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            dtd_validation=False,
            huge_tree=False,
        )
        try:
            root = etree.fromstring(content_bytes, secure_parser)
        except etree.XMLSyntaxError as exc:
            raise StatementParserError(
                "CAMT.053 file could not be parsed as XML: %s" % exc,
            ) from exc

        # Defence in depth: reject any DOCTYPE/entity declaration outright,
        # even though resolve_entities=False already neutralises expansion.
        roottree = getattr(root, 'getroottree', lambda: None)()
        docinfo = getattr(roottree, 'docinfo', None)
        if docinfo is not None and docinfo.doctype:
            raise StatementParserError(
                "CAMT.053 file with a DOCTYPE declaration is rejected "
                "(XXE guard).",
            )

        ns = self._detect_namespace(root)
        statements = (
            root.findall('.//{%s}Stmt' % ns)
            if ns else root.findall('.//Stmt')
        )
        if not statements:
            raise StatementParserError(
                "CAMT.053 file contains no <Stmt> section.",
            )
        parsed_statements = [
            self._parse_statement(stmt, ns) for stmt in statements
        ]
        if len(parsed_statements) == 1:
            return parsed_statements[0]
        return {'statements': parsed_statements}

    def _parse_statement(self, stmt, ns):
        currency = self._first_text(stmt, ns, './ns:Acct/ns:Ccy')
        account_id = (
            self._first_text(stmt, ns, './ns:Acct/ns:Id/ns:IBAN')
            or self._first_text(stmt, ns, './ns:Acct/ns:Id/ns:Othr/ns:Id')
        )
        statement_date = self._parse_iso_date(
            self._first_text(stmt, ns, './ns:CreDtTm')
            or self._first_text(stmt, ns, './ns:FrToDt/ns:ToDtTm'),
        )
        opening_balance, closing_balance = self._extract_balances(stmt, ns)
        lines = []
        for entry_index, ntry in enumerate(
                self._findall(stmt, ns, './ns:Ntry'), start=1):
            lines.extend(self._parse_entry(
                ntry, ns, currency, entry_index,
            ))
        return {
            'source_statement_id': self._first_text(stmt, ns, './ns:Id'),
            'statement_date': statement_date,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'currency_code': currency,
            'account_id': account_id,
            'lines': lines,
        }

    def _parse_entry(self, ntry, ns, statement_currency, entry_index):
        status = (
            self._first_text(ntry, ns, './ns:Sts/ns:Cd')
            or self._first_text(ntry, ns, './ns:Sts')
        )
        if status and status != 'BOOK':
            raise StatementParserError(
                "CAMT.053 contains non-booked entry status %r; import a "
                "final booked statement instead." % status
            )
        cdt_dbt = self._first_text(ntry, ns, './ns:CdtDbtInd')
        if cdt_dbt not in ('CRDT', 'DBIT'):
            raise StatementParserError(
                "CAMT.053 entry has invalid credit/debit indicator %r."
                % cdt_dbt
            )
        reversal_text = self._first_text(ntry, ns, './ns:RvslInd').lower()
        if reversal_text not in ('', 'true', 'false'):
            raise StatementParserError(
                "CAMT.053 entry has invalid reversal indicator %r."
                % reversal_text
            )
        reversed_entry = reversal_text == 'true'
        direction = Decimal('-1') if cdt_dbt == 'DBIT' else Decimal('1')
        if reversed_entry:
            direction = -direction

        booking_date_text = (
            self._first_text(ntry, ns, './ns:BookgDt/ns:Dt')
            or self._first_text(ntry, ns, './ns:BookgDt/ns:DtTm')
            or self._first_text(ntry, ns, './ns:ValDt/ns:Dt')
        )
        try:
            line_date = self._parse_iso_date(booking_date_text)
        except ValueError as exc:
            raise StatementParserError(
                "CAMT.053 entry has invalid booking date %r: %s"
                % (booking_date_text, exc),
            )
        entry_amount = self._parse_decimal(
            self._first_text(ntry, ns, './ns:Amt'),
            "entry amount",
        )
        transaction_details = self._findall(
            ntry, ns, './ns:NtryDtls/ns:TxDtls')
        entry_ref = self._first_text(ntry, ns, './ns:AcctSvcrRef')
        if len(transaction_details) <= 1:
            detail = transaction_details[0] if transaction_details else None
            return [self._build_line(
                ntry, detail, ns, statement_currency, line_date,
                entry_amount, direction, entry_ref, entry_index, 1,
            )]

        detail_amounts = [
            self._detail_statement_amount(
                detail, ns, statement_currency, entry_index, detail_index,
            )
            for detail_index, detail in enumerate(
                transaction_details, start=1)
        ]
        if sum(detail_amounts, Decimal('0')) != entry_amount:
            raise StatementParserError(
                "CAMT.053 entry %s transaction-detail amounts %s do not "
                "equal entry amount %s %s."
                % (
                    entry_index,
                    sum(detail_amounts, Decimal('0')),
                    entry_amount,
                    statement_currency or '',
                )
            )
        return [
            self._build_line(
                ntry, detail, ns, statement_currency, line_date,
                detail_amount, direction, entry_ref, entry_index,
                detail_index, split_entry=True,
            )
            for detail_index, (detail, detail_amount) in enumerate(
                zip(transaction_details, detail_amounts), start=1)
        ]

    def _detail_statement_amount(
            self, detail, ns, statement_currency, entry_index, detail_index):
        candidates = []
        for xpath in (
            './ns:Amt',
            './ns:AmtDtls/ns:CntrValAmt/ns:Amt',
            './ns:AmtDtls/ns:TxAmt/ns:Amt',
        ):
            element = self._first_element(detail, ns, xpath)
            if element is None:
                continue
            currency = (element.get('Ccy') or '').strip().upper()
            if statement_currency and currency != statement_currency.upper():
                continue
            candidates.append(self._parse_decimal(
                element.text, "transaction-detail amount",
            ))
        unique = set(candidates)
        if len(unique) != 1:
            raise StatementParserError(
                "CAMT.053 entry %s detail %s does not prove exactly one "
                "amount in statement currency %s."
                % (entry_index, detail_index, statement_currency or '')
            )
        return unique.pop()

    def _build_line(
            self, ntry, tx_detail, ns, statement_currency, line_date,
            amount, direction, entry_ref, entry_index, detail_index,
            split_entry=False):
        signed_amount = amount * direction
        unstructured = self._all_text(
            tx_detail, ns, './ns:RmtInf/ns:Ustrd')
        payment_ref = (
            ' / '.join(unstructured)
            or self._first_text(
                tx_detail, ns,
                './ns:RmtInf/ns:Strd/ns:CdtrRefInf/ns:Ref',
            )
            or entry_ref
            or ''
        )
        cdt_dbt = self._first_text(ntry, ns, './ns:CdtDbtInd')
        counterparty_path = (
            './ns:RltdPties/ns:Dbtr/ns:Nm'
            if cdt_dbt == 'CRDT'
            else './ns:RltdPties/ns:Cdtr/ns:Nm'
        )
        narration = self._first_text(tx_detail, ns, counterparty_path) or ''
        unique_ref = (
            self._first_text(tx_detail, ns, './ns:Refs/ns:AcctSvcrRef')
            or self._first_text(tx_detail, ns, './ns:Refs/ns:TxId')
            or self._first_text(tx_detail, ns, './ns:Refs/ns:EndToEndId')
            or (
                '%s:tx:%s' % (entry_ref, detail_index)
                if entry_ref and split_entry else entry_ref
            )
            or self._fingerprint(
                line_date, float(signed_amount), payment_ref,
            )
        )
        line = {
            'date': line_date,
            'amount': float(signed_amount),
            'payment_ref': payment_ref.strip(),
            'narration': narration.strip(),
            'partner_name': narration.strip() or None,
            'unique_import_ref': unique_ref,
        }
        foreign_amount_element = self._first_element(
            tx_detail, ns, './ns:AmtDtls/ns:TxAmt/ns:Amt')
        if foreign_amount_element is None:
            foreign_amount_element = self._first_element(
                tx_detail, ns, './ns:AmtDtls/ns:InstdAmt/ns:Amt')
        if foreign_amount_element is not None:
            foreign_currency = (
                foreign_amount_element.get('Ccy') or ''
            ).strip().upper()
            if (foreign_currency and statement_currency
                    and foreign_currency != statement_currency.upper()):
                foreign_amount = self._parse_decimal(
                    foreign_amount_element.text, "transaction amount",
                ) * direction
                line.update({
                    'foreign_currency_code': foreign_currency,
                    'foreign_amount': float(foreign_amount),
                })
        return line

    # ---- helpers ----

    @staticmethod
    def _detect_namespace(root):
        for ns in _CAMT_NS:
            if root.tag.startswith('{%s}' % ns) or any(
                child.tag.startswith('{%s}' % ns) for child in root
            ):
                return ns
        # Fallback: extract from root tag if it has any namespace.
        if root.tag.startswith('{'):
            return root.tag.split('}', 1)[0][1:]
        return ''

    @staticmethod
    def _findall(element, ns, xpath):
        if ns:
            return element.findall(xpath, namespaces={'ns': ns})
        return element.findall(xpath.replace('ns:', ''))

    @staticmethod
    def _first_text(element, ns, xpath):
        if element is None:
            return ''
        if ns:
            found = element.find(xpath, namespaces={'ns': ns})
        else:
            found = element.find(xpath.replace('ns:', ''))
        if found is None or found.text is None:
            return ''
        return found.text.strip()

    @staticmethod
    def _all_text(element, ns, xpath):
        if element is None:
            return []
        if ns:
            found = element.findall(xpath, namespaces={'ns': ns})
        else:
            found = element.findall(xpath.replace('ns:', ''))
        return [
            node.text.strip() for node in found
            if node.text and node.text.strip()
        ]

    @staticmethod
    def _first_element(element, ns, xpath):
        if element is None:
            return None
        if ns:
            return element.find(xpath, namespaces={'ns': ns})
        return element.find(xpath.replace('ns:', ''))

    @staticmethod
    def _parse_iso_date(text):
        if not text:
            return None
        # Allow date or datetime with timezone.
        for fmt in ('%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
            try:
                return datetime.datetime.strptime(text[:len(fmt) + 4], fmt).date()
            except ValueError:
                continue
        if 'T' in text:
            return datetime.datetime.fromisoformat(
                text.replace('Z', '+00:00'),
            ).date()
        raise ValueError("unrecognised ISO date %r" % text)

    @staticmethod
    def _parse_decimal(text, label):
        try:
            amount = Decimal(str(text or ''))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise StatementParserError(
                "CAMT.053 has invalid %s %r: %s" % (label, text, exc)
            ) from exc
        if not amount.is_finite() or amount < 0:
            raise StatementParserError(
                "CAMT.053 has invalid %s %r; expected a finite, non-negative "
                "amount." % (label, text)
            )
        return amount

    def _extract_balances(self, stmt, ns):
        """Pull opening and closing balances. CAMT codes are OPBD or
        ITBD for opening, CLBD for closing."""
        opening = None
        closing = None
        for bal in self._findall(stmt, ns, './ns:Bal'):
            code = self._first_text(bal, ns, './ns:Tp/ns:CdOrPrtry/ns:Cd')
            amount_text = self._first_text(bal, ns, './ns:Amt')
            cdt_dbt = self._first_text(bal, ns, './ns:CdtDbtInd')
            try:
                amount = float(amount_text or '0')
            except ValueError:
                continue
            if cdt_dbt == 'DBIT':
                amount = -amount
            if code in ('OPBD', 'ITBD') and opening is None:
                opening = amount
            elif code == 'CLBD':
                closing = amount
        return opening, closing

    @staticmethod
    def _fingerprint(date_value, amount, ref):
        h = hashlib.sha1()
        if date_value:
            h.update(date_value.isoformat().encode('utf-8'))
        h.update(b'|')
        h.update(("%.4f" % amount).encode('utf-8'))
        h.update(b'|')
        h.update((ref or '').encode('utf-8'))
        return h.hexdigest()
