# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
ISO 20022 PAIN.008.001.02 SEPA Direct Debit XML generator.

Built from the public ISO 20022 message reference manual at
https://www.iso20022.org/ and the SEPA Direct Debit Scheme Rulebook
published by the European Payments Council. No code or comments
derive from any proprietary or third-party implementation.

The generator takes a normalised input dict assembled by the caller.
Shape:

    {
        'message_id': str,            # max 35 chars, unique per file
        'creation_datetime': datetime,
        'initiating_party': {
            'name': str,
            'identifier': str | None,
        },
        'payments': [                 # one entry per PmtInf block
            {
                'payment_info_id': str,
                'requested_collection_date': date,
                'sequence_type': 'FRST'|'RCUR'|'FNAL'|'OOFF',
                'local_instrument': 'CORE'|'B2B',
                'batch_booking': bool,
                'creditor': {
                    'name': str,
                    'iban': str,
                    'bic': str | None,
                    'identifier': str,           # SEPA Creditor Identifier
                },
                'transactions': [
                    {
                        'end_to_end_id': str,
                        'amount': Decimal,
                        'mandate': {
                            'id': str,
                            'signature_date': date,
                        },
                        'debtor': {
                            'name': str,
                            'iban': str,
                            'bic': str | None,
                        },
                        'remittance_info': str | None,
                    },
                    ...
                ],
            },
            ...
        ],
    }

Per the SEPA scheme: each PmtInf groups transactions of the SAME
sequence_type and local_instrument. The caller is expected to split
mixed-sequence collections into separate PmtInf blocks; the generator
does not auto-split because the choice of split (per cycle, per
collection date, per mandate type) is policy-specific.
"""

from decimal import Decimal, ROUND_HALF_UP


_NAMESPACE = 'urn:iso:std:iso:20022:tech:xsd:pain.008.001.02'
_XSI_NS = 'http://www.w3.org/2001/XMLSchema-instance'
_SCHEMA_LOCATION = (
    'urn:iso:std:iso:20022:tech:xsd:pain.008.001.02 '
    'pain.008.001.02.xsd'
)


_MAX_MSG_ID = 35
_MAX_PMT_INFO_ID = 35
_MAX_END_TO_END_ID = 35
_MAX_NAME = 70
_MAX_REMIT_INFO = 140
_MAX_INITGPTY_ID = 35
_MAX_MANDATE_ID = 35
_MAX_CREDITOR_ID = 35
_MAX_STRD_REF = 35

_VALID_SEQ_TYPES = ('FRST', 'RCUR', 'FNAL', 'OOFF')
_VALID_LOCAL_INSTR = ('CORE', 'B2B')
# ISO 20022 ChargeBearerType1Code values permitted under the SEPA rulebook.
_CHARGE_BEARERS = frozenset({'SLEV', 'SHAR', 'CRED', 'DEBT'})
# Amendment detail keys we render under AmdmntInfDtls.
_AMENDMENT_KEYS = frozenset({
    'original_mandate_id', 'original_iban', 'original_debtor_name',
    'same_mandate_new_debtor_agent',
})


class Pain008GenerationError(ValueError):
    """Raised when the input dict cannot produce a compliant PAIN.008."""


def render(payload):
    """Render the PAIN.008 XML and return UTF-8 bytes with declaration.

    Performs structural validation upfront. IBAN/BIC validation is the
    caller's responsibility; using the eh_account_sepa_ct.tools
    validators is the recommended path.
    """
    _validate_payload(payload)
    from lxml import etree as _et
    root = _et.Element(
        '{%s}Document' % _NAMESPACE,
        nsmap={None: _NAMESPACE, 'xsi': _XSI_NS},
        attrib={'{%s}schemaLocation' % _XSI_NS: _SCHEMA_LOCATION},
    )
    cstmr = _et.SubElement(root, '{%s}CstmrDrctDbtInitn' % _NAMESPACE)
    _render_grphdr(cstmr, payload)
    for pmt in payload['payments']:
        _render_pmtinf(cstmr, pmt, payload['initiating_party'])
    return _et.tostring(
        root, xml_declaration=True, encoding='UTF-8', standalone=True,
    )


# ---- structural validation ----


def _validate_payload(payload):
    if not isinstance(payload, dict):
        raise Pain008GenerationError("payload must be a dict")
    for field in ('message_id', 'creation_datetime',
                  'initiating_party', 'payments'):
        if field not in payload:
            raise Pain008GenerationError(
                "payload missing required field %r" % field,
            )
    msg_id = payload['message_id']
    if not msg_id or len(msg_id) > _MAX_MSG_ID:
        raise Pain008GenerationError(
            "message_id must be 1..%d characters" % _MAX_MSG_ID,
        )
    init = payload['initiating_party']
    if not init.get('name') or len(init['name']) > _MAX_NAME:
        raise Pain008GenerationError(
            "initiating_party.name is required and capped at %d"
            % _MAX_NAME,
        )
    if init.get('identifier') and len(init['identifier']) > _MAX_INITGPTY_ID:
        raise Pain008GenerationError(
            "initiating_party.identifier exceeds %d characters"
            % _MAX_INITGPTY_ID,
        )
    if not payload['payments']:
        raise Pain008GenerationError(
            "payload must contain at least one payment block",
        )
    for idx, pmt in enumerate(payload['payments']):
        _validate_payment(idx, pmt)


def _validate_payment(idx, pmt):
    for field in ('payment_info_id', 'requested_collection_date',
                  'sequence_type', 'local_instrument',
                  'creditor', 'transactions'):
        if field not in pmt:
            raise Pain008GenerationError(
                "payments[%d] missing %r" % (idx, field),
            )
    if pmt['sequence_type'] not in _VALID_SEQ_TYPES:
        raise Pain008GenerationError(
            "payments[%d].sequence_type must be one of %s; got %r"
            % (idx, _VALID_SEQ_TYPES, pmt['sequence_type']),
        )
    if pmt['local_instrument'] not in _VALID_LOCAL_INSTR:
        raise Pain008GenerationError(
            "payments[%d].local_instrument must be one of %s; got %r"
            % (idx, _VALID_LOCAL_INSTR, pmt['local_instrument']),
        )
    charge_bearer = pmt.get('charge_bearer')
    if charge_bearer is not None and charge_bearer not in _CHARGE_BEARERS:
        raise Pain008GenerationError(
            "payments[%d].charge_bearer must be one of %s"
            % (idx, ", ".join(sorted(_CHARGE_BEARERS))),
        )
    if not isinstance(pmt.get('batch_booking', False), bool):
        raise Pain008GenerationError(
            "payments[%d].batch_booking must be a boolean" % idx,
        )
    if not pmt['payment_info_id'] or len(pmt['payment_info_id']) > _MAX_PMT_INFO_ID:
        raise Pain008GenerationError(
            "payments[%d].payment_info_id must be 1..%d characters"
            % (idx, _MAX_PMT_INFO_ID),
        )
    cdtr = pmt['creditor']
    for field in ('name', 'iban', 'identifier'):
        if not cdtr.get(field):
            raise Pain008GenerationError(
                "payments[%d].creditor.%s is required" % (idx, field),
            )
    if len(cdtr['name']) > _MAX_NAME:
        raise Pain008GenerationError(
            "payments[%d].creditor.name exceeds %d characters"
            % (idx, _MAX_NAME),
        )
    if len(cdtr['identifier']) > _MAX_CREDITOR_ID:
        raise Pain008GenerationError(
            "payments[%d].creditor.identifier exceeds %d characters"
            % (idx, _MAX_CREDITOR_ID),
        )
    if not pmt['transactions']:
        raise Pain008GenerationError(
            "payments[%d] has no transactions" % idx,
        )
    for tidx, tx in enumerate(pmt['transactions']):
        _validate_transaction(idx, tidx, tx)


def _validate_transaction(idx, tidx, tx):
    for field in ('end_to_end_id', 'amount', 'mandate', 'debtor'):
        if field not in tx:
            raise Pain008GenerationError(
                "payments[%d].transactions[%d] missing %r"
                % (idx, tidx, field),
            )
    e2e = tx['end_to_end_id']
    if not e2e or len(e2e) > _MAX_END_TO_END_ID:
        raise Pain008GenerationError(
            "payments[%d].transactions[%d].end_to_end_id must be "
            "1..%d characters" % (idx, tidx, _MAX_END_TO_END_ID),
        )
    amt = tx['amount']
    if not isinstance(amt, (int, float, Decimal)):
        raise Pain008GenerationError(
            "payments[%d].transactions[%d].amount must be numeric"
            % (idx, tidx),
        )
    if Decimal(str(amt)) <= 0:
        raise Pain008GenerationError(
            "payments[%d].transactions[%d].amount must be > 0"
            % (idx, tidx),
        )
    mandate = tx['mandate']
    for field in ('id', 'signature_date'):
        if not mandate.get(field):
            raise Pain008GenerationError(
                "payments[%d].transactions[%d].mandate.%s is required"
                % (idx, tidx, field),
            )
    if len(mandate['id']) > _MAX_MANDATE_ID:
        raise Pain008GenerationError(
            "payments[%d].transactions[%d].mandate.id exceeds %d "
            "characters" % (idx, tidx, _MAX_MANDATE_ID),
        )
    amendment = mandate.get('amendment')
    if amendment is not None:
        if not isinstance(amendment, dict) or not amendment:
            raise Pain008GenerationError(
                "payments[%d].transactions[%d].mandate.amendment must be a "
                "non-empty dict" % (idx, tidx),
            )
        unknown = set(amendment) - _AMENDMENT_KEYS
        if unknown:
            raise Pain008GenerationError(
                "payments[%d].transactions[%d].mandate.amendment has "
                "unknown keys: %s" % (idx, tidx, ", ".join(sorted(unknown))),
            )
        present = sum(bool(amendment.get(key)) for key in _AMENDMENT_KEYS)
        if present != 1:
            raise Pain008GenerationError(
                "payments[%d].transactions[%d].mandate.amendment must "
                "identify exactly one original attribute" % (idx, tidx),
            )
        if len(amendment.get('original_debtor_name') or '') > _MAX_NAME:
            raise Pain008GenerationError(
                "payments[%d].transactions[%d].mandate.amendment original "
                "debtor name exceeds %d characters" % (idx, tidx, _MAX_NAME),
            )
    debtor = tx['debtor']
    for field in ('name', 'iban'):
        if not debtor.get(field):
            raise Pain008GenerationError(
                "payments[%d].transactions[%d].debtor.%s is required"
                % (idx, tidx, field),
            )
    if len(debtor['name']) > _MAX_NAME:
        raise Pain008GenerationError(
            "payments[%d].transactions[%d].debtor.name exceeds %d "
            "characters" % (idx, tidx, _MAX_NAME),
        )
    if tx.get('remittance_info') and len(tx['remittance_info']) > _MAX_REMIT_INFO:
        raise Pain008GenerationError(
            "payments[%d].transactions[%d].remittance_info exceeds %d "
            "characters" % (idx, tidx, _MAX_REMIT_INFO),
        )
    structured = tx.get('structured_reference')
    if structured is not None:
        if not isinstance(structured, str) or not structured:
            raise Pain008GenerationError(
                "payments[%d].transactions[%d].structured_reference must "
                "be a non-empty string" % (idx, tidx),
            )
        if len(structured) > _MAX_STRD_REF:
            raise Pain008GenerationError(
                "payments[%d].transactions[%d].structured_reference exceeds "
                "%d characters" % (idx, tidx, _MAX_STRD_REF),
            )


# ---- XML rendering ----


def _format_amount(value):
    quantised = Decimal(str(value)).quantize(
        Decimal('0.01'), rounding=ROUND_HALF_UP,
    )
    return format(quantised, 'f')


def _render_grphdr(parent, payload):
    from lxml import etree as _et
    grphdr = _et.SubElement(parent, '{%s}GrpHdr' % _NAMESPACE)
    _et.SubElement(grphdr, '{%s}MsgId' % _NAMESPACE).text = payload['message_id']
    _et.SubElement(grphdr, '{%s}CreDtTm' % _NAMESPACE).text = (
        payload['creation_datetime'].strftime('%Y-%m-%dT%H:%M:%S')
    )
    total_count, total_sum = _aggregate_totals(payload['payments'])
    _et.SubElement(grphdr, '{%s}NbOfTxs' % _NAMESPACE).text = str(total_count)
    _et.SubElement(grphdr, '{%s}CtrlSum' % _NAMESPACE).text = _format_amount(
        total_sum,
    )
    initg = _et.SubElement(grphdr, '{%s}InitgPty' % _NAMESPACE)
    _et.SubElement(initg, '{%s}Nm' % _NAMESPACE).text = (
        payload['initiating_party']['name']
    )
    if payload['initiating_party'].get('identifier'):
        ident = _et.SubElement(initg, '{%s}Id' % _NAMESPACE)
        org = _et.SubElement(ident, '{%s}OrgId' % _NAMESPACE)
        othr = _et.SubElement(org, '{%s}Othr' % _NAMESPACE)
        _et.SubElement(othr, '{%s}Id' % _NAMESPACE).text = (
            payload['initiating_party']['identifier']
        )


def _render_pmtinf(parent, pmt, initiating_party):
    from lxml import etree as _et
    pmtinf = _et.SubElement(parent, '{%s}PmtInf' % _NAMESPACE)
    _et.SubElement(pmtinf, '{%s}PmtInfId' % _NAMESPACE).text = pmt['payment_info_id']
    _et.SubElement(pmtinf, '{%s}PmtMtd' % _NAMESPACE).text = 'DD'
    _et.SubElement(pmtinf, '{%s}BtchBookg' % _NAMESPACE).text = (
        'true' if pmt.get('batch_booking', False) else 'false'
    )
    nb = len(pmt['transactions'])
    _et.SubElement(pmtinf, '{%s}NbOfTxs' % _NAMESPACE).text = str(nb)
    sum_amount = sum(Decimal(str(tx['amount'])) for tx in pmt['transactions'])
    _et.SubElement(pmtinf, '{%s}CtrlSum' % _NAMESPACE).text = _format_amount(
        sum_amount,
    )
    pmttpinf = _et.SubElement(pmtinf, '{%s}PmtTpInf' % _NAMESPACE)
    svclvl = _et.SubElement(pmttpinf, '{%s}SvcLvl' % _NAMESPACE)
    _et.SubElement(svclvl, '{%s}Cd' % _NAMESPACE).text = 'SEPA'
    lclinstrm = _et.SubElement(pmttpinf, '{%s}LclInstrm' % _NAMESPACE)
    _et.SubElement(lclinstrm, '{%s}Cd' % _NAMESPACE).text = pmt['local_instrument']
    _et.SubElement(pmttpinf, '{%s}SeqTp' % _NAMESPACE).text = pmt['sequence_type']

    _et.SubElement(pmtinf, '{%s}ReqdColltnDt' % _NAMESPACE).text = (
        pmt['requested_collection_date'].strftime('%Y-%m-%d')
    )

    cdtr = pmt['creditor']
    cdtr_el = _et.SubElement(pmtinf, '{%s}Cdtr' % _NAMESPACE)
    _et.SubElement(cdtr_el, '{%s}Nm' % _NAMESPACE).text = cdtr['name']
    cdtr_acct = _et.SubElement(pmtinf, '{%s}CdtrAcct' % _NAMESPACE)
    cdtr_id = _et.SubElement(cdtr_acct, '{%s}Id' % _NAMESPACE)
    _et.SubElement(cdtr_id, '{%s}IBAN' % _NAMESPACE).text = cdtr['iban']
    cdtr_agt = _et.SubElement(pmtinf, '{%s}CdtrAgt' % _NAMESPACE)
    fin = _et.SubElement(cdtr_agt, '{%s}FinInstnId' % _NAMESPACE)
    if cdtr.get('bic'):
        _et.SubElement(fin, '{%s}BIC' % _NAMESPACE).text = cdtr['bic']
    else:
        othr = _et.SubElement(fin, '{%s}Othr' % _NAMESPACE)
        _et.SubElement(othr, '{%s}Id' % _NAMESPACE).text = 'NOTPROVIDED'

    # Charge bearer SLEV per the SEPA scheme.
    _et.SubElement(pmtinf, '{%s}ChrgBr' % _NAMESPACE).text = (
        pmt.get('charge_bearer') or 'SLEV'
    )

    # Creditor identifier block.
    sch_id = _et.SubElement(pmtinf, '{%s}CdtrSchmeId' % _NAMESPACE)
    sch_idid = _et.SubElement(sch_id, '{%s}Id' % _NAMESPACE)
    prv = _et.SubElement(sch_idid, '{%s}PrvtId' % _NAMESPACE)
    othr = _et.SubElement(prv, '{%s}Othr' % _NAMESPACE)
    _et.SubElement(othr, '{%s}Id' % _NAMESPACE).text = cdtr['identifier']
    sch = _et.SubElement(othr, '{%s}SchmeNm' % _NAMESPACE)
    _et.SubElement(sch, '{%s}Prtry' % _NAMESPACE).text = 'SEPA'

    for tx in pmt['transactions']:
        _render_drctdbttx(pmtinf, tx)


def _render_drctdbttx(parent, tx):
    from lxml import etree as _et
    drctdbt = _et.SubElement(parent, '{%s}DrctDbtTxInf' % _NAMESPACE)
    pmt_id = _et.SubElement(drctdbt, '{%s}PmtId' % _NAMESPACE)
    _et.SubElement(pmt_id, '{%s}EndToEndId' % _NAMESPACE).text = tx['end_to_end_id']

    instd = _et.SubElement(drctdbt, '{%s}InstdAmt' % _NAMESPACE, Ccy='EUR')
    instd.text = _format_amount(tx['amount'])

    drctdbt_tx = _et.SubElement(drctdbt, '{%s}DrctDbtTx' % _NAMESPACE)
    mndt = _et.SubElement(drctdbt_tx, '{%s}MndtRltdInf' % _NAMESPACE)
    _et.SubElement(mndt, '{%s}MndtId' % _NAMESPACE).text = tx['mandate']['id']
    _et.SubElement(mndt, '{%s}DtOfSgntr' % _NAMESPACE).text = (
        tx['mandate']['signature_date'].strftime('%Y-%m-%d')
    )
    amendment = tx['mandate'].get('amendment')
    if amendment:
        _et.SubElement(mndt, '{%s}AmdmntInd' % _NAMESPACE).text = 'true'
        dtls = _et.SubElement(mndt, '{%s}AmdmntInfDtls' % _NAMESPACE)
        if amendment.get('original_mandate_id'):
            _et.SubElement(dtls, '{%s}OrgnlMndtId' % _NAMESPACE).text = (
                amendment['original_mandate_id']
            )
        if amendment.get('original_iban') or amendment.get(
                'same_mandate_new_debtor_agent'):
            orgnl_acct = _et.SubElement(
                dtls, '{%s}OrgnlDbtrAcct' % _NAMESPACE)
            acct_id = _et.SubElement(orgnl_acct, '{%s}Id' % _NAMESPACE)
            if amendment.get('original_iban'):
                _et.SubElement(acct_id, '{%s}IBAN' % _NAMESPACE).text = (
                    amendment['original_iban']
                )
            else:
                othr = _et.SubElement(acct_id, '{%s}Othr' % _NAMESPACE)
                _et.SubElement(othr, '{%s}Id' % _NAMESPACE).text = 'SMNDA'
        if amendment.get('original_debtor_name'):
            original_debtor = _et.SubElement(
                dtls, '{%s}OrgnlDbtr' % _NAMESPACE)
            _et.SubElement(
                original_debtor, '{%s}Nm' % _NAMESPACE,
            ).text = amendment['original_debtor_name']

    dbtr_agt = _et.SubElement(drctdbt, '{%s}DbtrAgt' % _NAMESPACE)
    fin = _et.SubElement(dbtr_agt, '{%s}FinInstnId' % _NAMESPACE)
    if tx['debtor'].get('bic'):
        _et.SubElement(fin, '{%s}BIC' % _NAMESPACE).text = tx['debtor']['bic']
    else:
        othr = _et.SubElement(fin, '{%s}Othr' % _NAMESPACE)
        _et.SubElement(othr, '{%s}Id' % _NAMESPACE).text = 'NOTPROVIDED'

    dbtr = _et.SubElement(drctdbt, '{%s}Dbtr' % _NAMESPACE)
    _et.SubElement(dbtr, '{%s}Nm' % _NAMESPACE).text = tx['debtor']['name']

    dbtr_acct = _et.SubElement(drctdbt, '{%s}DbtrAcct' % _NAMESPACE)
    dbtr_id = _et.SubElement(dbtr_acct, '{%s}Id' % _NAMESPACE)
    _et.SubElement(dbtr_id, '{%s}IBAN' % _NAMESPACE).text = tx['debtor']['iban']

    # One remittance block per transaction: structured creditor reference
    # (ISO 11649 / SCOR) when supplied, otherwise the unstructured line.
    if tx.get('structured_reference'):
        rmt = _et.SubElement(drctdbt, '{%s}RmtInf' % _NAMESPACE)
        strd = _et.SubElement(rmt, '{%s}Strd' % _NAMESPACE)
        cdtr_ref_inf = _et.SubElement(strd, '{%s}CdtrRefInf' % _NAMESPACE)
        tp = _et.SubElement(cdtr_ref_inf, '{%s}Tp' % _NAMESPACE)
        cd_or_prtry = _et.SubElement(tp, '{%s}CdOrPrtry' % _NAMESPACE)
        _et.SubElement(cd_or_prtry, '{%s}Cd' % _NAMESPACE).text = 'SCOR'
        _et.SubElement(cdtr_ref_inf, '{%s}Ref' % _NAMESPACE).text = (
            tx['structured_reference']
        )
    elif tx.get('remittance_info'):
        rmt = _et.SubElement(drctdbt, '{%s}RmtInf' % _NAMESPACE)
        _et.SubElement(rmt, '{%s}Ustrd' % _NAMESPACE).text = tx['remittance_info']


def _aggregate_totals(payments):
    total_count = sum(len(p['transactions']) for p in payments)
    total_sum = sum(
        Decimal(str(tx['amount']))
        for p in payments
        for tx in p['transactions']
    )
    return total_count, total_sum
