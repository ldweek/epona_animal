# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
MT940 bank statement parser.

MT940 is the SWIFT customer-statement message format. Banks across
Europe, Asia and several US correspondents publish statements in this
format. The format is line-oriented with tag fields (`:20:`, `:25:`,
`:60F:`, `:61:`, `:86:`, `:62F:`) and is documented in the SWIFT
User Handbook chapter on Category 9 messages.

This implementation is a fresh read against the public specification.
It supports the core message body (one logical statement per file)
and reads:

* `:20:` -- transaction reference number (statement id).
* `:25:` -- account identification.
* `:60F:` / `:60M:` -- opening balance (final or intermediate).
* `:61:` -- statement line: value date, entry date, mark
  (C/D/RC/RD), amount, transaction code, customer reference, bank
  reference, supplementary information.
* `:86:` -- transaction information (free text describing the line
  above).
* `:62F:` / `:62M:` -- closing balance.

Multi-statement files (a single envelope carrying several account
statements) are out of scope: the parser raises StatementParserError
naming the offending tag rather than guessing which one to keep.

The maths conventions follow the SWIFT spec literally:
* Amounts use comma as the decimal separator. We accept either comma
  or dot but standardise to dot in the returned dict.
* The mark on `:61:` is the line's debit/credit indicator; we sign
  the amount accordingly so the dict reports positive=credit (money in)
  and negative=debit (money out), consistent with the rest of the
  bank-import dict shape.
* Reversed entries (RC / RD) negate the sign one extra time.
"""

import datetime
import re

from .base import StatementParser, StatementParserError


_TAG_LINE_RE = re.compile(r'^:(\d{2}[A-Z]?):(.*)$')
# :61: value date YYMMDD, optional entry date MMDD, mark, amount,
# transaction code, customer ref //bank ref optional supplementary.
_LINE61_RE = re.compile(
    r'^(?P<vd>\d{6})'
    r'(?P<ed>\d{4})?'
    r'(?P<mark>RC|RD|C|D)'
    r'(?P<curr_letter>[A-Z])?'
    r'(?P<amount>\d+[,\.]\d{0,2})'
    r'(?P<txcode>[A-Z][A-Z0-9]{3})'
    r'(?P<rest>.*)$',
)
# :60F: opening final balance, :62F: closing final.
_BALANCE_RE = re.compile(
    r'^(?P<dc>[CD])(?P<date>\d{6})(?P<curr>[A-Z]{3})(?P<amount>\d+[,\.]\d{0,2})$',
)


class Mt940StatementParser(StatementParser):
    FORMAT_KEY = 'mt940'
    FORMAT_LABEL = 'MT940 (SWIFT)'

    def parse(self, content_bytes, profile=None):
        if isinstance(content_bytes, bytes):
            try:
                text = content_bytes.decode('utf-8')
            except UnicodeDecodeError:
                # MT940 from many European banks is latin-1.
                text = content_bytes.decode('latin-1')
        else:
            text = content_bytes
        # Normalise CRLF and collapse blank-line separators inside the
        # message body. Multi-line tags (continuation lines after a
        # tag without a leading colon) are joined to the prior tag
        # value before parsing.
        raw_lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        joined = []
        for line in raw_lines:
            if not line:
                continue
            if line.startswith(':') or line.startswith('{') or line.startswith('-'):
                joined.append(line)
            else:
                if joined:
                    joined[-1] += '\n' + line
                else:
                    joined.append(line)

        result = {
            'statement_date': None,
            'opening_balance': None,
            'closing_balance': None,
            'currency_code': None,
            'lines': [],
        }
        current_line = None
        statement_count = 0
        for raw in joined:
            match = _TAG_LINE_RE.match(raw)
            if not match:
                # Block 1/2/3/4/5 envelope markers (`{1:...}`) and the
                # message terminator (`-`) are skipped silently; they
                # contain no statement data we report on.
                continue
            tag, value = match.group(1), match.group(2).strip()
            if tag == '20':
                statement_count += 1
                if statement_count > 1:
                    raise StatementParserError(
                        "MT940 multi-statement files are not supported; "
                        "found a second :20: tag at line %r" % raw,
                    )
            elif tag == '25':
                # Account id. Stored as part of the result for callers
                # that want to verify the statement matches the journal.
                result.setdefault('account_id', value)
            elif tag in ('60F', '60M'):
                bal = self._parse_balance(value)
                # 60M is an intermediate opening carried between pages.
                # Never let it overwrite the statement's final 60F anchor.
                if tag == '60F' or result['opening_balance'] is None:
                    result['opening_balance'] = bal['amount']
                if result['currency_code'] not in (None, bal['currency']):
                    raise StatementParserError(
                        "MT940 balance currencies disagree within one "
                        "statement."
                    )
                result['currency_code'] = bal['currency']
            elif tag == '61':
                if current_line:
                    result['lines'].append(current_line)
                current_line = self._parse_line_61(value)
            elif tag == '86':
                if current_line:
                    self._merge_narration(current_line, value)
            elif tag in ('62F', '62M'):
                bal = self._parse_balance(value)
                if result['currency_code'] not in (None, bal['currency']):
                    raise StatementParserError(
                        "MT940 balance currencies disagree within one "
                        "statement."
                    )
                result['currency_code'] = bal['currency']
                # 62F is final closing evidence. 62M is retained only until
                # a final closing arrives and never replaces it afterwards.
                if tag == '62F' or result['closing_balance'] is None:
                    result['closing_balance'] = bal['amount']
                    result['statement_date'] = bal['date']
        if current_line:
            result['lines'].append(current_line)

        if not result['lines'] and result['opening_balance'] is None:
            raise StatementParserError(
                "MT940 file did not contain any recognisable statement "
                "tags (no :60:, :61:, or :62: encountered).",
            )
        if not result['statement_date'] and result['lines']:
            result['statement_date'] = max(
                line['date'] for line in result['lines'])
        return result

    def _parse_balance(self, value):
        m = _BALANCE_RE.match(value)
        if not m:
            raise StatementParserError(
                "Malformed MT940 balance tag value: %r" % value,
            )
        sign = -1.0 if m.group('dc') == 'D' else 1.0
        amount = sign * float(m.group('amount').replace(',', '.'))
        return {
            'date': self._parse_date(m.group('date')),
            'currency': m.group('curr'),
            'amount': amount,
        }

    def _parse_line_61(self, value):
        # The tag value can be very long; the supplementary section
        # after the customer reference is optional. Support the common
        # form where the customer reference uses `//` to separate the
        # bank reference.
        m = _LINE61_RE.match(value)
        if not m:
            raise StatementParserError(
                "Malformed MT940 :61: line: %r" % value,
            )
        sign = -1.0 if m.group('mark') in ('D', 'RD') else 1.0
        # Reversed entries (RC, RD) flip again so a reversal of a
        # credit is a debit and vice-versa, matching the bank's
        # accounting convention.
        if m.group('mark') in ('RC', 'RD'):
            sign = -sign
        amount = sign * float(m.group('amount').replace(',', '.'))
        rest = (m.group('rest') or '').strip()
        customer_ref, bank_ref = self._split_refs(rest)
        return {
            'date': self._parse_date(m.group('vd')),
            'amount': round(amount, 2),
            'payment_ref': customer_ref or bank_ref or '',
            'narration': '',
            'partner_name': None,
            'unique_import_ref': bank_ref or customer_ref,
        }

    def _merge_narration(self, line, value):
        if line.get('narration'):
            line['narration'] = line['narration'] + ' ' + value
        else:
            line['narration'] = value
        # Many banks place the counterparty name in subfield 32 of the
        # 86 tag; fall back to subfield 33 (city). The subfield format
        # is `?NN`; we extract the first occurrence as a best-effort
        # partner_name without claiming completeness.
        if not line.get('partner_name'):
            for code in ('?32', '?33', '?60'):
                idx = value.find(code)
                if idx >= 0:
                    end = value.find('?', idx + 3)
                    name = value[idx + 3:end if end > 0 else None].strip()
                    if name:
                        line['partner_name'] = name
                        break
        if not line.get('unique_import_ref'):
            # Some banks expose the EREF / NSRef in subfield 20 of
            # the 86 tag; treat that as the canonical unique import
            # reference when present.
            idx = value.find('?20')
            if idx >= 0:
                end = value.find('?', idx + 3)
                ref = value[idx + 3:end if end > 0 else None].strip()
                if ref:
                    line['unique_import_ref'] = ref

    @staticmethod
    def _split_refs(rest):
        # Form: `customer_ref//bank_ref<remainder>` where the remainder
        # is supplementary info we ignore for now.
        if '//' in rest:
            cust, _, after = rest.partition('//')
            bank = after.split('\n', 1)[0]
            return cust.strip(), bank.strip()
        return rest.split('\n', 1)[0].strip(), ''

    @staticmethod
    def _parse_date(value6):
        # YYMMDD; SWIFT does not include a century. We pivot on year 80
        # the way most banks do: 80-99 -> 1980-1999, 00-79 -> 2000-2079.
        yy = int(value6[0:2])
        mm = int(value6[2:4])
        dd = int(value6[4:6])
        year = 1900 + yy if yy >= 80 else 2000 + yy
        return datetime.date(year, mm, dd)
