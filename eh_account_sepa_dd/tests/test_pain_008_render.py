# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
PAIN.008.001.02 generator tests.

Builds a minimal payload, renders the XML, parses it back with lxml
and asserts every required scheme element: namespace, sequence type,
local instrument, mandate id and signature date, creditor identifier,
charge bearer, amount format, NOTPROVIDED fallback when BIC missing.
Error paths exercise structural validation upfront.
"""

from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

from odoo import release
from odoo.tests import TransactionCase, tagged
from odoo.tools import config

from odoo.addons.eh_account_sepa_dd.tools.pain_008 import (
    render, Pain008GenerationError,
)


_NS = 'urn:iso:std:iso:20022:tech:xsd:pain.008.001.02'


def _make_payload(**overrides):
    payload = {
        'message_id': 'DD-MSG001',
        'creation_datetime': datetime(2026, 4, 15, 10, 0, 0),
        'initiating_party': {
            'name': 'ERP Heritage Demo',
            'identifier': None,
        },
        'payments': [
            {
                'payment_info_id': 'COL001',
                'requested_collection_date': date(2026, 5, 1),
                'sequence_type': 'FRST',
                'local_instrument': 'CORE',
                'creditor': {
                    'name': 'ERP Heritage Demo',
                    'iban': 'DE89370400440532013000',
                    'bic': 'COBADEFFXXX',
                    'identifier': 'DE98ZZZ09999999999',
                },
                'transactions': [
                    {
                        'end_to_end_id': 'INV-300',
                        'amount': Decimal('99.95'),
                        'mandate': {
                            'id': 'MNDT-001',
                            'signature_date': date(2026, 1, 15),
                        },
                        'debtor': {
                            'name': 'Customer Alpha SARL',
                            'iban': 'FR1420041010050500013M02606',
                            'bic': 'BNPAFRPPXXX',
                        },
                        'remittance_info': 'Subscription April',
                    },
                    {
                        'end_to_end_id': 'INV-301',
                        'amount': Decimal('149.00'),
                        'mandate': {
                            'id': 'MNDT-002',
                            'signature_date': date(2026, 2, 20),
                        },
                        'debtor': {
                            'name': 'Customer Beta GmbH',
                            'iban': 'DE89370400440532013000',
                            'bic': None,
                        },
                        'remittance_info': None,
                    },
                ],
            },
        ],
    }
    payload.update(overrides)
    return payload


@tagged('eh_account_sepa_dd', 'unit')
class TestPain008Render(TransactionCase):

    def test_render_returns_xml_bytes(self):
        result = render(_make_payload())
        self.assertIsInstance(result, bytes)
        self.assertTrue(result.startswith(b"<?xml"))

    def test_render_uses_correct_namespace(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        self.assertTrue(root.tag.startswith('{%s}' % _NS))

    def test_render_carries_sequence_type(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        seq = root.find('.//{%s}SeqTp' % _NS).text
        self.assertEqual(seq, 'FRST')

    def test_render_carries_local_instrument(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        cd = root.find('.//{%s}LclInstrm/{%s}Cd' % (_NS, _NS)).text
        self.assertEqual(cd, 'CORE')

    def test_render_includes_mandate_block(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        mandates = root.findall('.//{%s}MndtRltdInf' % _NS)
        self.assertEqual(len(mandates), 2)
        mids = sorted(m.find('{%s}MndtId' % _NS).text for m in mandates)
        self.assertEqual(mids, ['MNDT-001', 'MNDT-002'])
        sigs = sorted(m.find('{%s}DtOfSgntr' % _NS).text for m in mandates)
        self.assertEqual(sigs, ['2026-01-15', '2026-02-20'])

    def test_render_creditor_identifier_in_scheme_block(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        # The CdtrSchmeId/Id/PrvtId/Othr/Id is the creditor identifier.
        path = (
            './/{%s}CdtrSchmeId/{%s}Id/{%s}PrvtId/{%s}Othr/{%s}Id'
            % (_NS, _NS, _NS, _NS, _NS)
        )
        cid = root.find(path).text
        self.assertEqual(cid, 'DE98ZZZ09999999999')

    def test_render_charge_bearer_slev(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        chrg = root.find('.//{%s}ChrgBr' % _NS)
        self.assertEqual(chrg.text, 'SLEV')

    def test_render_amount_two_decimals(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        amounts = root.findall('.//{%s}InstdAmt' % _NS)
        amount_texts = [a.text for a in amounts]
        self.assertIn('99.95', amount_texts)
        self.assertIn('149.00', amount_texts)
        for a in amounts:
            self.assertEqual(a.get('Ccy'), 'EUR')

    def test_render_emits_notprovided_when_debtor_bic_missing(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        dbtr_agts = root.findall('.//{%s}DbtrAgt' % _NS)
        self.assertEqual(len(dbtr_agts), 2)
        # Second transaction has BIC None.
        notprovided = dbtr_agts[1].find(
            './{%s}FinInstnId/{%s}Othr/{%s}Id' % (_NS, _NS, _NS),
        )
        self.assertEqual(notprovided.text, 'NOTPROVIDED')

    def test_render_aggregate_count_and_sum(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        grp = root.find('.//{%s}GrpHdr' % _NS)
        self.assertEqual(grp.find('{%s}NbOfTxs' % _NS).text, '2')
        self.assertEqual(grp.find('{%s}CtrlSum' % _NS).text, '248.95')

    # ---- error paths ----

    def test_invalid_sequence_type_raises(self):
        payload = _make_payload()
        payload['payments'][0]['sequence_type'] = 'XYZ'
        with self.assertRaises(Pain008GenerationError) as cm:
            render(payload)
        self.assertIn('sequence_type', str(cm.exception))

    def test_invalid_local_instrument_raises(self):
        payload = _make_payload()
        payload['payments'][0]['local_instrument'] = 'NOPE'
        with self.assertRaises(Pain008GenerationError) as cm:
            render(payload)
        self.assertIn('local_instrument', str(cm.exception))

    def test_missing_mandate_id_raises(self):
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['mandate']['id'] = ''
        with self.assertRaises(Pain008GenerationError) as cm:
            render(payload)
        self.assertIn('mandate.id', str(cm.exception))

    def test_missing_creditor_identifier_raises(self):
        payload = _make_payload()
        payload['payments'][0]['creditor']['identifier'] = ''
        with self.assertRaises(Pain008GenerationError) as cm:
            render(payload)
        self.assertIn('creditor.identifier', str(cm.exception))

    def test_negative_amount_raises(self):
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['amount'] = Decimal('-10')
        with self.assertRaises(Pain008GenerationError):
            render(payload)

    # ---- mandate amendment (AmdmntInf) ----

    def test_amendment_renders_original_mandate_id(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['mandate']['amendment'] = {
            'original_mandate_id': 'OLD-MNDT-001'}
        root = etree.fromstring(render(payload))
        ns = {'p': _NS}
        mndt = root.findall('.//p:MndtRltdInf', ns)[0]
        self.assertEqual(mndt.find('p:AmdmntInd', ns).text, 'true')
        self.assertEqual(
            mndt.find('p:AmdmntInfDtls/p:OrgnlMndtId', ns).text,
            'OLD-MNDT-001')

    def test_amendment_renders_original_iban(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['mandate']['amendment'] = {
            'original_iban': 'DE89370400440532013000'}
        root = etree.fromstring(render(payload))
        ns = {'p': _NS}
        dtls = root.findall('.//p:AmdmntInfDtls', ns)[0]
        self.assertEqual(
            dtls.find('p:OrgnlDbtrAcct/p:Id/p:IBAN', ns).text,
            'DE89370400440532013000')

    def test_smnda_uses_post_2016_original_debtor_account_placement(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['mandate']['amendment'] = {
            'same_mandate_new_debtor_agent': True}
        root = etree.fromstring(render(payload))
        ns = {'p': _NS}
        dtls = root.findall('.//p:AmdmntInfDtls', ns)[0]
        self.assertEqual(
            dtls.find('p:OrgnlDbtrAcct/p:Id/p:Othr/p:Id', ns).text,
            'SMNDA')
        self.assertIsNone(dtls.find('p:OrgnlDbtrAgt', ns))

    def test_debtor_name_amendment_renders_original_debtor(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['mandate']['amendment'] = {
            'original_debtor_name': 'Original Debtor GmbH'}
        root = etree.fromstring(render(payload))
        ns = {'p': _NS}
        self.assertEqual(
            root.find('.//p:AmdmntInfDtls/p:OrgnlDbtr/p:Nm', ns).text,
            'Original Debtor GmbH',
        )

    def test_no_amendment_block_by_default(self):
        from lxml import etree
        root = etree.fromstring(render(_make_payload()))
        self.assertFalse(root.findall('.//{%s}AmdmntInd' % _NS))

    def test_unknown_amendment_key_raises(self):
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['mandate']['amendment'] = {
            'bogus_key': 'x'}
        with self.assertRaises(Pain008GenerationError):
            render(payload)

    def test_batch_booking_is_configurable(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['batch_booking'] = True
        root = etree.fromstring(render(payload))
        self.assertEqual(root.find('.//{%s}BtchBookg' % _NS).text, 'true')

    def test_output_validates_against_official_enterprise_xsd(self):
        from lxml import etree
        addons_paths = config['addons_path']
        if isinstance(addons_paths, str):
            addons_paths = addons_paths.split(',')
        roots = [Path(path) for path in addons_paths]
        roots.append(Path(release.__file__).resolve().parents[2] / 'enterprise')
        relative = Path(
            'account_sepa_direct_debit/schemas/pain.008.001.02.xsd',
        )
        schema_path = next(
            (root / relative for root in roots if (root / relative).is_file()),
            None,
        )
        self.assertTrue(schema_path, "Official Enterprise XSD unavailable")
        schema = etree.XMLSchema(etree.parse(str(schema_path)))
        schema.assertValid(etree.fromstring(render(_make_payload())))

    # ---- configurable charge bearer ----

    def test_charge_bearer_configurable(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['charge_bearer'] = 'SHAR'
        root = etree.fromstring(render(payload))
        self.assertEqual(root.find('.//{%s}ChrgBr' % _NS).text, 'SHAR')

    def test_invalid_charge_bearer_raises(self):
        payload = _make_payload()
        payload['payments'][0]['charge_bearer'] = 'XXXX'
        with self.assertRaises(Pain008GenerationError):
            render(payload)

    # ---- structured creditor reference (Strd / SCOR) ----

    def test_structured_reference_renders_scor(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['structured_reference'] = (
            'RF18539007547034')
        root = etree.fromstring(render(payload))
        ns = {'p': _NS}
        ref = root.findall('.//p:Strd/p:CdtrRefInf/p:Ref', ns)
        self.assertEqual(ref[0].text, 'RF18539007547034')
        cd = root.findall(
            './/p:Strd/p:CdtrRefInf/p:Tp/p:CdOrPrtry/p:Cd', ns)
        self.assertEqual(cd[0].text, 'SCOR')

    def test_structured_reference_too_long_raises(self):
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['structured_reference'] = (
            'R' * 36)
        with self.assertRaises(Pain008GenerationError):
            render(payload)
