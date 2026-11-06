# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
PAIN.001.001.03 generator tests.

Builds a minimal payload, renders the XML, parses it back with lxml and
asserts the structure: namespace, message id, transaction count, control
sum, per-transaction amounts and IBAN. Errors are exercised by deleting
required keys or feeding invalid amounts and asserting the generator
names the failed field.
"""

from datetime import datetime, date
from decimal import Decimal
from pathlib import Path

from odoo import release
from odoo.tests import TransactionCase, tagged
from odoo.tools import config

from odoo.addons.eh_account_sepa_ct.tools.pain_001 import (
    render, Pain001GenerationError,
)


_NS = 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.03'


def _make_payload(**overrides):
    payload = {
        'message_id': 'MSG001',
        'creation_datetime': datetime(2026, 4, 15, 10, 0, 0),
        'initiating_party': {
            'name': 'ERP Heritage Demo',
            'identifier': None,
        },
        'payments': [
            {
                'payment_info_id': 'BATCH001',
                'requested_execution_date': date(2026, 4, 16),
                'debtor': {
                    'name': 'ERP Heritage Demo',
                    'iban': 'DE89370400440532013000',
                    'bic': 'COBADEFFXXX',
                },
                'transactions': [
                    {
                        'end_to_end_id': 'INV001',
                        'amount': Decimal('1500.50'),
                        'creditor': {
                            'name': 'Vendor Alpha GmbH',
                            'iban': 'DE89370400440532013000',
                            'bic': 'DEUTDEFFXXX',
                        },
                        'remittance_info': 'Invoice INV-100',
                    },
                    {
                        'end_to_end_id': 'INV002',
                        'amount': Decimal('250.00'),
                        'creditor': {
                            'name': 'Vendor Beta SARL',
                            'iban': 'FR1420041010050500013M02606',
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


@tagged('eh_account_sepa_ct', 'unit')
class TestPain001Render(TransactionCase):

    def test_render_returns_bytes_with_xml_declaration(self):
        result = render(_make_payload())
        self.assertIsInstance(result, bytes)
        self.assertTrue(result.startswith(b"<?xml"))
        self.assertIn(b"encoding='UTF-8'", result)

    def test_render_uses_correct_namespace(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        self.assertTrue(root.tag.startswith('{%s}' % _NS))

    def test_render_carries_message_id(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        msg_id = root.find('.//{%s}MsgId' % _NS).text
        self.assertEqual(msg_id, 'MSG001')

    def test_render_aggregates_count_and_sum_at_grphdr(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        grphdr = root.find('.//{%s}GrpHdr' % _NS)
        self.assertEqual(
            grphdr.find('{%s}NbOfTxs' % _NS).text, '2',
        )
        self.assertEqual(
            grphdr.find('{%s}CtrlSum' % _NS).text, '1750.50',
        )

    def test_render_amount_two_decimal_places(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        amounts = root.findall('.//{%s}InstdAmt' % _NS)
        self.assertEqual(amounts[0].text, '1500.50')
        self.assertEqual(amounts[1].text, '250.00')
        self.assertTrue(all(a.get('Ccy') == 'EUR' for a in amounts))

    def test_render_emits_notprovided_when_creditor_bic_missing(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        # Second transaction has no BIC; expect Othr/Id = NOTPROVIDED.
        cdtr_agts = root.findall('.//{%s}CdtrAgt' % _NS)
        self.assertEqual(len(cdtr_agts), 2)
        notprovided = cdtr_agts[1].find(
            './{%s}FinInstnId/{%s}Othr/{%s}Id' % (_NS, _NS, _NS),
        )
        self.assertIsNotNone(notprovided)
        self.assertEqual(notprovided.text, 'NOTPROVIDED')

    def test_render_includes_remittance_when_present(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        ustrd = root.findall('.//{%s}Ustrd' % _NS)
        self.assertEqual(len(ustrd), 1)
        self.assertEqual(ustrd[0].text, 'Invoice INV-100')

    def test_render_service_level_sepa(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        svclvl_cd = root.find('.//{%s}SvcLvl/{%s}Cd' % (_NS, _NS))
        self.assertEqual(svclvl_cd.text, 'SEPA')

    def test_render_charge_bearer_slev(self):
        from lxml import etree
        result = render(_make_payload())
        root = etree.fromstring(result)
        chrgbr = root.find('.//{%s}ChrgBr' % _NS)
        self.assertEqual(chrgbr.text, 'SLEV')

    # ---- error paths ----

    def test_missing_message_id_raises(self):
        payload = _make_payload()
        del payload['message_id']
        with self.assertRaises(Pain001GenerationError) as cm:
            render(payload)
        self.assertIn('message_id', str(cm.exception))

    def test_negative_amount_raises(self):
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['amount'] = Decimal('-1')
        with self.assertRaises(Pain001GenerationError) as cm:
            render(payload)
        self.assertIn('> 0', str(cm.exception))

    def test_too_long_message_id_raises(self):
        payload = _make_payload()
        payload['message_id'] = 'A' * 36
        with self.assertRaises(Pain001GenerationError):
            render(payload)

    def test_empty_payments_raises(self):
        payload = _make_payload()
        payload['payments'] = []
        with self.assertRaises(Pain001GenerationError):
            render(payload)

    def test_missing_iban_in_creditor_raises(self):
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['creditor']['iban'] = ''
        with self.assertRaises(Pain001GenerationError) as cm:
            render(payload)
        self.assertIn('iban', str(cm.exception))

    # ---- pain.001.001.09 variant ----

    def test_render_v09_uses_v09_namespace(self):
        from lxml import etree
        result = render(_make_payload(), version='09')
        root = etree.fromstring(result)
        v09_ns = 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.09'
        self.assertTrue(root.tag.startswith('{%s}' % v09_ns))

    def test_render_v09_emits_bicfi_not_bic(self):
        from lxml import etree
        result = render(_make_payload(), version='09')
        v09_ns = 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.09'
        root = etree.fromstring(result)
        # First transaction has BIC populated; expect BICFI in v09.
        bicfi = root.findall('.//{%s}BICFI' % v09_ns)
        bic = root.findall('.//{%s}BIC' % v09_ns)
        self.assertEqual(len(bicfi), 2)  # one for debtor, one for creditor
        self.assertFalse(bic)

    def test_render_v03_still_emits_bic(self):
        from lxml import etree
        result = render(_make_payload(), version='03')
        root = etree.fromstring(result)
        bic = root.findall('.//{%s}BIC' % _NS)
        bicfi = root.findall('.//{%s}BICFI' % _NS)
        self.assertEqual(len(bic), 2)
        self.assertFalse(bicfi)

    def test_render_v09_carries_same_message_id_and_totals(self):
        from lxml import etree
        result = render(_make_payload(), version='09')
        v09_ns = 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.09'
        root = etree.fromstring(result)
        msg_id = root.find('.//{%s}MsgId' % v09_ns).text
        self.assertEqual(msg_id, 'MSG001')
        ctrl_sum = root.find('.//{%s}GrpHdr/{%s}CtrlSum' % (v09_ns, v09_ns))
        self.assertEqual(ctrl_sum.text, '1750.50')

    def test_render_v09_wraps_requested_date_in_dt(self):
        from lxml import etree
        result = render(_make_payload(), version='09')
        namespace = 'urn:iso:std:iso:20022:tech:xsd:pain.001.001.09'
        root = etree.fromstring(result)
        requested = root.find(
            './/{%s}ReqdExctnDt/{%s}Dt' % (namespace, namespace),
        )
        self.assertEqual(requested.text, '2026-04-16')

    def test_both_versions_validate_against_enterprise_official_xsd(self):
        from lxml import etree
        candidates = {
            '03': [
                'account_iso20022/schemas/pain.001.001.03.xsd',
                'account_sepa/schemas/pain.001.001.03.xsd',
            ],
            '09': [
                'account_iso20022/schemas/pain.001.001.09.xsd',
                'account_sepa/schemas/pain.001.001.09.xsd',
                'account_sepa_pain_001_001_09/schemas/pain.001.001.09.xsd',
            ],
        }
        addons_paths = config['addons_path']
        if isinstance(addons_paths, str):
            addons_paths = addons_paths.split(',')
        search_roots = [Path(path) for path in addons_paths]
        search_roots.append(
            Path(release.__file__).resolve().parents[2] / 'enterprise',
        )
        for version in ('03', '09'):
            schema_path = False
            for candidate in candidates[version]:
                for addons_root in search_roots:
                    candidate_path = addons_root / candidate
                    if candidate_path.is_file():
                        schema_path = candidate_path
                        break
                if schema_path:
                    break
            self.assertTrue(schema_path, "Official Enterprise XSD unavailable")
            schema = etree.XMLSchema(etree.parse(str(schema_path)))
            schema.assertValid(etree.fromstring(render(
                _make_payload(), version=version,
            )))

    def test_render_unsupported_version_raises(self):
        with self.assertRaises(Pain001GenerationError) as cm:
            render(_make_payload(), version='99')
        self.assertIn('99', str(cm.exception))

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

    def test_structured_reference_takes_precedence_over_ustrd(self):
        from lxml import etree
        payload = _make_payload()
        tx = payload['payments'][0]['transactions'][0]
        tx['structured_reference'] = 'RF18539007547034'
        tx['remittance_info'] = 'Should be ignored'
        root = etree.fromstring(render(payload))
        ns = {'p': _NS}
        # First transaction carries Strd, not Ustrd.
        first_tx = root.findall('.//p:CdtTrfTxInf', ns)[0]
        self.assertTrue(first_tx.findall('.//p:Strd', ns))
        self.assertFalse(first_tx.findall('.//p:Ustrd', ns))

    def test_structured_reference_too_long_raises(self):
        payload = _make_payload()
        payload['payments'][0]['transactions'][0]['structured_reference'] = (
            'R' * 36)
        with self.assertRaises(Pain001GenerationError):
            render(payload)

    def test_structured_reference_bad_check_digits_raise(self):
        payload = _make_payload()
        payload['payments'][0]['transactions'][0][
            'structured_reference'
        ] = 'RF00539007547034'
        with self.assertRaises(Pain001GenerationError):
            render(payload)

    def test_control_sum_adds_quantised_transaction_amounts(self):
        from lxml import etree
        payload = _make_payload()
        transactions = payload['payments'][0]['transactions']
        transactions[0]['amount'] = Decimal('0.005')
        transactions[1]['amount'] = Decimal('0.005')
        root = etree.fromstring(render(payload))
        self.assertEqual(
            root.find('.//{%s}GrpHdr/{%s}CtrlSum' % (_NS, _NS)).text,
            '0.02',
        )
        self.assertEqual(
            root.find('.//{%s}PmtInf/{%s}CtrlSum' % (_NS, _NS)).text,
            '0.02',
        )

    # ---- charge bearer (ChrgBr) ----

    def test_charge_bearer_defaults_slev(self):
        from lxml import etree
        root = etree.fromstring(render(_make_payload()))
        chrg = root.findall('.//{%s}ChrgBr' % _NS)
        self.assertEqual(chrg[0].text, 'SLEV')

    def test_charge_bearer_configurable(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['charge_bearer'] = 'SHAR'
        root = etree.fromstring(render(payload))
        chrg = root.findall('.//{%s}ChrgBr' % _NS)
        self.assertEqual(chrg[0].text, 'SHAR')

    def test_invalid_charge_bearer_raises(self):
        payload = _make_payload()
        payload['payments'][0]['charge_bearer'] = 'XXXX'
        with self.assertRaises(Pain001GenerationError):
            render(payload)

    # ---- instruction priority + batch booking ----

    def test_instruction_priority_renders(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['instruction_priority'] = 'HIGH'
        root = etree.fromstring(render(payload))
        ns = {'p': _NS}
        prty = root.findall('.//p:PmtTpInf/p:InstrPrty', ns)
        self.assertEqual(prty[0].text, 'HIGH')

    def test_no_instruction_priority_by_default(self):
        from lxml import etree
        root = etree.fromstring(render(_make_payload()))
        self.assertFalse(root.findall('.//{%s}InstrPrty' % _NS))

    def test_invalid_instruction_priority_raises(self):
        payload = _make_payload()
        payload['payments'][0]['instruction_priority'] = 'URGENT'
        with self.assertRaises(Pain001GenerationError):
            render(payload)

    def test_batch_booking_configurable(self):
        from lxml import etree
        payload = _make_payload()
        payload['payments'][0]['batch_booking'] = True
        root = etree.fromstring(render(payload))
        btch = root.findall('.//{%s}BtchBookg' % _NS)
        self.assertEqual(btch[0].text, 'true')

    def test_batch_booking_defaults_false(self):
        from lxml import etree
        root = etree.fromstring(render(_make_payload()))
        btch = root.findall('.//{%s}BtchBookg' % _NS)
        self.assertEqual(btch[0].text, 'false')
