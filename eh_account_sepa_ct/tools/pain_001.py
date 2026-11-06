# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
ISO 20022 PAIN.001 SEPA Credit Transfer XML generator.

Supports both PAIN.001.001.03 (legacy SEPA scheme rulebook) and
PAIN.001.001.09 (current EU Implementation Guidelines, mandatory for
many banks from 2024 onwards).

Built from the public ISO 20022 message reference manual at
https://www.iso20022.org/ . No code or comments derive from any
proprietary or third-party implementation.

The generator takes a normalised input dict that the caller assembles
from the batch payment record. The dict shape:

    {
        'message_id': str,             # max 35 chars, unique per file
        'creation_datetime': datetime, # UTC
        'initiating_party': {
            'name': str,               # max 70 chars
            'identifier': str | None,  # optional, max 35 chars
        },
        'payments': [                  # one entry per PmtInf block
            {
                'payment_info_id': str,         # max 35 chars
                'requested_execution_date': date,
                'debtor': {
                    'name': str,
                    'iban': str,                # validated
                    'bic': str | None,
                },
                'transactions': [
                    {
                        'end_to_end_id': str,   # max 35 chars
                        'amount': Decimal,
                        'creditor': {
                            'name': str,
                            'iban': str,
                            'bic': str | None,
                        },
                        'remittance_info': str | None,  # unstructured, max 140
                    },
                    ...
                ],
            },
            ...
        ],
    }

Returns bytes containing the rendered XML, encoded UTF-8 with XML
declaration. Lines are emitted in the spec order.

All amounts are formatted with exactly two decimal places (the SEPA
scheme rule). Currency is always EUR per scheme. Charge bearer is SLEV.

Version differences applied automatically by the renderer:
  * Namespace switches from pain.001.001.03 to pain.001.001.09.
  * The financial-institution BIC element is renamed from BIC to BICFI.
  * SchemaLocation hint switches to the .09 file name.
All other element names and structural rules carry over unchanged.
"""

from decimal import Decimal, ROUND_HALF_UP


_SUPPORTED_VERSIONS = ('03', '09')
_DEFAULT_VERSION = '03'

_NAMESPACE_TEMPLATE = 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.{version}'
_SCHEMA_LOCATION_TEMPLATE = (
    'urn:iso:std:iso:20022:tech:xsd:pain.001.001.{version} '
    'pain.001.001.{version}.xsd'
)
_XSI_NS = 'http://www.w3.org/2001/XMLSchema-instance'


def _ns_for(version):
    return _NAMESPACE_TEMPLATE.format(version=version)


def _bic_tag_for(version):
    """Return the financial-institution BIC element name for the version.

    .03 uses <BIC>; .09 renamed it to <BICFI> as part of the broader
    ISO 20022 alignment with corporate-action and securities messages.
    """
    return 'BICFI' if version == '09' else 'BIC'


# Legacy constants kept for callers that import them directly. Both
# default to the .03 spec so existing import sites do not break.
_NAMESPACE = _ns_for(_DEFAULT_VERSION)
_SCHEMA_LOCATION = _SCHEMA_LOCATION_TEMPLATE.format(version=_DEFAULT_VERSION)


# Field length caps come from the SEPA Credit Transfer scheme rulebook.
_MAX_MSG_ID = 35
_MAX_PMT_INFO_ID = 35
_MAX_END_TO_END_ID = 35
_MAX_NAME = 70
_MAX_REMIT_INFO = 140
_MAX_INITGPTY_ID = 35
_MAX_STRD_REF = 25
# ISO 20022 ChargeBearerType1Code values permitted under the SEPA rulebook.
_CHARGE_BEARERS = frozenset({'SLEV', 'SHAR', 'CRED', 'DEBT'})
# ISO 20022 Priority2Code values.
_INSTRUCTION_PRIORITIES = frozenset({'NORM', 'HIGH'})


class Pain001GenerationError(ValueError):
    """Raised when the input dict cannot produce a compliant PAIN.001."""


def normalize_structured_reference(value):
    """Return a valid ISO 11649 creditor reference or raise.

    Spaces are presentation-only. Validation uses the same rearrange/letter
    expansion/mod-97 rule as IBAN, with the ISO 11649 ``RF`` prefix and
    25-character maximum.
    """
    reference = ''.join((value or '').split()).upper()
    if not (4 <= len(reference) <= _MAX_STRD_REF):
        raise Pain001GenerationError(
            "structured_reference must be 4..%d characters" % _MAX_STRD_REF,
        )
    if not reference.startswith('RF') or not reference[2:4].isdigit():
        raise Pain001GenerationError(
            "structured_reference must start with RF and two check digits",
        )
    if not reference.isalnum():
        raise Pain001GenerationError(
            "structured_reference must contain only letters and digits",
        )
    rearranged = reference[4:] + reference[:4]
    numeric = ''.join(
        char if char.isdigit() else str(ord(char) - 55)
        for char in rearranged
    )
    remainder = 0
    for char in numeric:
        remainder = (remainder * 10 + int(char)) % 97
    if remainder != 1:
        raise Pain001GenerationError(
            "structured_reference has invalid ISO 11649 check digits",
        )
    return reference


def render(payload, version=_DEFAULT_VERSION):
    """Render the PAIN.001 XML and return bytes.

    Performs structural validation only (presence of mandatory fields,
    length caps, amount formatting). IBAN and BIC validation is done
    by the iban_validator and bic_validator modules and must be applied
    by the caller BEFORE calling render. Doing the validation in the
    caller keeps user-facing error messages closer to the data source.

    :param payload: normalised input dict (see module docstring).
    :param version: '03' (legacy) or '09' (current EU IG). Defaults to
        '03' so existing callers keep their behaviour. New deployments
        should target '09'.
    """
    if version not in _SUPPORTED_VERSIONS:
        raise Pain001GenerationError(
            "Unsupported PAIN.001 version %r; supported: %s"
            % (version, ", ".join(_SUPPORTED_VERSIONS)),
        )
    _validate_payload(payload)
    namespace = _ns_for(version)
    schema_location = _SCHEMA_LOCATION_TEMPLATE.format(version=version)
    # Import lazily so structural validation remains independently testable.
    from lxml import etree as _et
    root = _et.Element(
        '{%s}Document' % namespace,
        nsmap={None: namespace, 'xsi': _XSI_NS},
        attrib={
            '{%s}schemaLocation' % _XSI_NS: schema_location,
        },
    )
    cstmr = _et.SubElement(root, '{%s}CstmrCdtTrfInitn' % namespace)
    _render_grphdr(cstmr, payload, namespace)
    for pmt_info in payload['payments']:
        _render_pmtinf(
            cstmr, pmt_info, payload['initiating_party'],
            namespace=namespace, version=version,
        )
    return _et.tostring(
        root,
        xml_declaration=True,
        encoding='UTF-8',
        standalone=True,
    )


# ---- structural validation ----


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise Pain001GenerationError("payload must be a dict")
    for field in ('message_id', 'creation_datetime',
                  'initiating_party', 'payments'):
        if field not in payload:
            raise Pain001GenerationError(
                "payload missing required field %r" % field,
            )
    msg_id = payload['message_id']
    if not msg_id or len(msg_id) > _MAX_MSG_ID:
        raise Pain001GenerationError(
            "message_id must be 1..%d characters" % _MAX_MSG_ID,
        )
    init = payload['initiating_party']
    if not isinstance(init, dict) or not init.get('name'):
        raise Pain001GenerationError(
            "initiating_party.name is required",
        )
    if len(init['name']) > _MAX_NAME:
        raise Pain001GenerationError(
            "initiating_party.name exceeds %d characters" % _MAX_NAME,
        )
    if init.get('identifier') and len(init['identifier']) > _MAX_INITGPTY_ID:
        raise Pain001GenerationError(
            "initiating_party.identifier exceeds %d characters"
            % _MAX_INITGPTY_ID,
        )
    if not payload['payments']:
        raise Pain001GenerationError(
            "payload must contain at least one payment block",
        )
    for idx, pmt in enumerate(payload['payments']):
        _validate_payment(idx, pmt)


def _validate_payment(idx, pmt):
    for field in ('payment_info_id', 'requested_execution_date',
                  'debtor', 'transactions'):
        if field not in pmt:
            raise Pain001GenerationError(
                "payments[%d] missing %r" % (idx, field),
            )
    if not pmt['payment_info_id'] or len(pmt['payment_info_id']) > _MAX_PMT_INFO_ID:
        raise Pain001GenerationError(
            "payments[%d].payment_info_id must be 1..%d characters"
            % (idx, _MAX_PMT_INFO_ID),
        )
    debtor = pmt['debtor']
    for field in ('name', 'iban'):
        if not debtor.get(field):
            raise Pain001GenerationError(
                "payments[%d].debtor.%s is required" % (idx, field),
            )
    if len(debtor['name']) > _MAX_NAME:
        raise Pain001GenerationError(
            "payments[%d].debtor.name exceeds %d characters"
            % (idx, _MAX_NAME),
        )
    charge_bearer = pmt.get('charge_bearer')
    if charge_bearer is not None and charge_bearer not in _CHARGE_BEARERS:
        raise Pain001GenerationError(
            "payments[%d].charge_bearer must be one of %s"
            % (idx, ", ".join(sorted(_CHARGE_BEARERS))),
        )
    instruction_priority = pmt.get('instruction_priority')
    if instruction_priority is not None and (
            instruction_priority not in _INSTRUCTION_PRIORITIES):
        raise Pain001GenerationError(
            "payments[%d].instruction_priority must be one of %s"
            % (idx, ", ".join(sorted(_INSTRUCTION_PRIORITIES))),
        )
    if not pmt['transactions']:
        raise Pain001GenerationError(
            "payments[%d] has no transactions" % idx,
        )
    for tidx, tx in enumerate(pmt['transactions']):
        _validate_transaction(idx, tidx, tx)


def _validate_transaction(idx, tidx, tx):
    for field in ('end_to_end_id', 'amount', 'creditor'):
        if field not in tx:
            raise Pain001GenerationError(
                "payments[%d].transactions[%d] missing %r"
                % (idx, tidx, field),
            )
    e2e = tx['end_to_end_id']
    if not e2e or len(e2e) > _MAX_END_TO_END_ID:
        raise Pain001GenerationError(
            "payments[%d].transactions[%d].end_to_end_id must be "
            "1..%d characters" % (idx, tidx, _MAX_END_TO_END_ID),
        )
    amt = tx['amount']
    if not isinstance(amt, (int, float, Decimal)):
        raise Pain001GenerationError(
            "payments[%d].transactions[%d].amount must be numeric"
            % (idx, tidx),
        )
    if Decimal(str(amt)) <= 0:
        raise Pain001GenerationError(
            "payments[%d].transactions[%d].amount must be > 0"
            % (idx, tidx),
        )
    cdtr = tx['creditor']
    for field in ('name', 'iban'):
        if not cdtr.get(field):
            raise Pain001GenerationError(
                "payments[%d].transactions[%d].creditor.%s is required"
                % (idx, tidx, field),
            )
    if len(cdtr['name']) > _MAX_NAME:
        raise Pain001GenerationError(
            "payments[%d].transactions[%d].creditor.name exceeds "
            "%d characters" % (idx, tidx, _MAX_NAME),
        )
    if tx.get('remittance_info') and len(tx['remittance_info']) > _MAX_REMIT_INFO:
        raise Pain001GenerationError(
            "payments[%d].transactions[%d].remittance_info exceeds "
            "%d characters" % (idx, tidx, _MAX_REMIT_INFO),
        )
    structured = tx.get('structured_reference')
    if structured is not None:
        if not isinstance(structured, str) or not structured:
            raise Pain001GenerationError(
                "payments[%d].transactions[%d].structured_reference must "
                "be a non-empty string" % (idx, tidx),
            )
        normalize_structured_reference(structured)


# ---- XML rendering ----


def quantize_amount(value):
    """Return amount exactly as each InstdAmt is represented."""
    return Decimal(str(value)).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP,
    )


def _format_amount(value):
    """SEPA amounts: positive, exactly two decimal places, dot decimal."""
    return format(quantize_amount(value), 'f')


def _render_grphdr(parent, payload, namespace):
    from lxml import etree as _et
    grphdr = _et.SubElement(parent, '{%s}GrpHdr' % namespace)
    _et.SubElement(grphdr, '{%s}MsgId' % namespace).text = payload['message_id']
    _et.SubElement(grphdr, '{%s}CreDtTm' % namespace).text = (
        payload['creation_datetime'].strftime('%Y-%m-%dT%H:%M:%S')
    )
    total_count, total_sum = _aggregate_totals(payload['payments'])
    _et.SubElement(grphdr, '{%s}NbOfTxs' % namespace).text = str(total_count)
    _et.SubElement(grphdr, '{%s}CtrlSum' % namespace).text = _format_amount(
        total_sum,
    )
    initg = _et.SubElement(grphdr, '{%s}InitgPty' % namespace)
    _et.SubElement(initg, '{%s}Nm' % namespace).text = (
        payload['initiating_party']['name']
    )
    if payload['initiating_party'].get('identifier'):
        ident = _et.SubElement(initg, '{%s}Id' % namespace)
        org = _et.SubElement(ident, '{%s}OrgId' % namespace)
        othr = _et.SubElement(org, '{%s}Othr' % namespace)
        _et.SubElement(othr, '{%s}Id' % namespace).text = (
            payload['initiating_party']['identifier']
        )


def _render_pmtinf(parent, pmt, initiating_party, namespace, version):
    from lxml import etree as _et
    bic_tag = _bic_tag_for(version)
    pmtinf = _et.SubElement(parent, '{%s}PmtInf' % namespace)
    _et.SubElement(pmtinf, '{%s}PmtInfId' % namespace).text = pmt['payment_info_id']
    _et.SubElement(pmtinf, '{%s}PmtMtd' % namespace).text = 'TRF'
    _et.SubElement(pmtinf, '{%s}BtchBookg' % namespace).text = (
        'true' if pmt.get('batch_booking') else 'false'
    )
    nb_of_txs = len(pmt['transactions'])
    _et.SubElement(pmtinf, '{%s}NbOfTxs' % namespace).text = str(nb_of_txs)
    sum_amount = sum(
        quantize_amount(tx['amount']) for tx in pmt['transactions']
    )
    _et.SubElement(pmtinf, '{%s}CtrlSum' % namespace).text = _format_amount(
        sum_amount,
    )
    pmttpinf = _et.SubElement(pmtinf, '{%s}PmtTpInf' % namespace)
    if pmt.get('instruction_priority'):
        _et.SubElement(pmttpinf, '{%s}InstrPrty' % namespace).text = (
            pmt['instruction_priority']
        )
    svclvl = _et.SubElement(pmttpinf, '{%s}SvcLvl' % namespace)
    _et.SubElement(svclvl, '{%s}Cd' % namespace).text = 'SEPA'

    # ReqdExctnDt: in pain.001.001.03 it is a bare ISODate; in .09 it is a
    # DateAndDateTime2Choice and MUST wrap the value in a <Dt> child element,
    # or the file is schema-invalid and rejected by any bank that validates
    # against the .09 XSD (the 2024+ SEPA baseline).
    reqd = _et.SubElement(pmtinf, '{%s}ReqdExctnDt' % namespace)
    date_str = pmt['requested_execution_date'].strftime('%Y-%m-%d')
    if version == '03':
        reqd.text = date_str
    else:
        _et.SubElement(reqd, '{%s}Dt' % namespace).text = date_str

    debtor = pmt['debtor']
    dbtr = _et.SubElement(pmtinf, '{%s}Dbtr' % namespace)
    _et.SubElement(dbtr, '{%s}Nm' % namespace).text = debtor['name']
    dbtr_acct = _et.SubElement(pmtinf, '{%s}DbtrAcct' % namespace)
    dbtr_id = _et.SubElement(dbtr_acct, '{%s}Id' % namespace)
    _et.SubElement(dbtr_id, '{%s}IBAN' % namespace).text = debtor['iban']
    dbtr_agt = _et.SubElement(pmtinf, '{%s}DbtrAgt' % namespace)
    fin = _et.SubElement(dbtr_agt, '{%s}FinInstnId' % namespace)
    if debtor.get('bic'):
        _et.SubElement(fin, '{%s}%s' % (namespace, bic_tag)).text = debtor['bic']
    else:
        # The 'NOTPROVIDED' marker is the SEPA-rulebook idiom for an
        # IBAN-only payment when the BIC is not known. Banks lookup BIC
        # from IBAN when this marker is present.
        othr = _et.SubElement(fin, '{%s}Othr' % namespace)
        _et.SubElement(othr, '{%s}Id' % namespace).text = 'NOTPROVIDED'

    _et.SubElement(pmtinf, '{%s}ChrgBr' % namespace).text = (
        pmt.get('charge_bearer') or 'SLEV'
    )

    for tx in pmt['transactions']:
        _render_cdttrftx(pmtinf, tx, namespace=namespace, bic_tag=bic_tag)


def _render_cdttrftx(parent, tx, namespace, bic_tag):
    from lxml import etree as _et
    cdt_tx = _et.SubElement(parent, '{%s}CdtTrfTxInf' % namespace)
    pmt_id = _et.SubElement(cdt_tx, '{%s}PmtId' % namespace)
    _et.SubElement(pmt_id, '{%s}EndToEndId' % namespace).text = tx['end_to_end_id']

    amt = _et.SubElement(cdt_tx, '{%s}Amt' % namespace)
    instd = _et.SubElement(amt, '{%s}InstdAmt' % namespace, Ccy='EUR')
    instd.text = _format_amount(tx['amount'])

    cdtr_agt = _et.SubElement(cdt_tx, '{%s}CdtrAgt' % namespace)
    fin = _et.SubElement(cdtr_agt, '{%s}FinInstnId' % namespace)
    if tx['creditor'].get('bic'):
        _et.SubElement(fin, '{%s}%s' % (namespace, bic_tag)).text = (
            tx['creditor']['bic']
        )
    else:
        othr = _et.SubElement(fin, '{%s}Othr' % namespace)
        _et.SubElement(othr, '{%s}Id' % namespace).text = 'NOTPROVIDED'

    cdtr = _et.SubElement(cdt_tx, '{%s}Cdtr' % namespace)
    _et.SubElement(cdtr, '{%s}Nm' % namespace).text = tx['creditor']['name']

    cdtr_acct = _et.SubElement(cdt_tx, '{%s}CdtrAcct' % namespace)
    cdtr_id = _et.SubElement(cdtr_acct, '{%s}Id' % namespace)
    _et.SubElement(cdtr_id, '{%s}IBAN' % namespace).text = tx['creditor']['iban']

    # SEPA permits one remittance block per transaction: a structured
    # creditor reference (ISO 11649 / SCOR) when supplied, otherwise the
    # free-text unstructured line.
    if tx.get('structured_reference'):
        rmt = _et.SubElement(cdt_tx, '{%s}RmtInf' % namespace)
        strd = _et.SubElement(rmt, '{%s}Strd' % namespace)
        cdtr_ref_inf = _et.SubElement(strd, '{%s}CdtrRefInf' % namespace)
        tp = _et.SubElement(cdtr_ref_inf, '{%s}Tp' % namespace)
        cd_or_prtry = _et.SubElement(tp, '{%s}CdOrPrtry' % namespace)
        _et.SubElement(cd_or_prtry, '{%s}Cd' % namespace).text = 'SCOR'
        _et.SubElement(cdtr_ref_inf, '{%s}Ref' % namespace).text = (
            normalize_structured_reference(tx['structured_reference'])
        )
    elif tx.get('remittance_info'):
        rmt = _et.SubElement(cdt_tx, '{%s}RmtInf' % namespace)
        _et.SubElement(rmt, '{%s}Ustrd' % namespace).text = tx['remittance_info']


def _aggregate_totals(payments):
    total_count = sum(len(p['transactions']) for p in payments)
    total_sum = sum(
        quantize_amount(tx['amount'])
        for p in payments
        for tx in p['transactions']
    )
    return total_count, total_sum
