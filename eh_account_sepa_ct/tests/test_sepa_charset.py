# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""SEPA character sanitisation and unique MsgId generation."""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)
from odoo.addons.eh_account_sepa_ct.tools.sepa_charset import (
    sanitize_sepa_text,
)


_SEPA_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789/-?:().,'+ "
)


@tagged('eh_account_sepa_ct', 'post_install', '-at_install')
class TestSepaCharset(TransactionCase):

    def test_passthrough_clean_text(self):
        self.assertEqual(
            sanitize_sepa_text("ACME Co Ltd 123 - Inv/2026"),
            "ACME Co Ltd 123 - Inv/2026",
        )

    def test_transliterates_accents(self):
        self.assertEqual(sanitize_sepa_text("Creme Brulee"), "Creme Brulee")
        self.assertEqual(sanitize_sepa_text("Muller GmbH"), "Muller GmbH")
        self.assertEqual(sanitize_sepa_text("Jose Pena"), "Jose Pena")
        # Accented forms fold to the same ASCII output.
        self.assertEqual(
            sanitize_sepa_text("Crème Brûlée"),
            "Creme Brulee",
        )
        self.assertEqual(
            sanitize_sepa_text("Müller GmbH"), "Muller GmbH",
        )
        self.assertEqual(
            sanitize_sepa_text("Łódź Øresund Straße"),
            "Lodz Oresund Strasse",
        )

    def test_replaces_disallowed_with_space(self):
        self.assertEqual(sanitize_sepa_text("a&b"), "a b")
        self.assertEqual(sanitize_sepa_text("x@y#z"), "x y z")

    def test_allowed_punctuation_kept(self):
        kept = "A/-?:().,'+"
        self.assertEqual(sanitize_sepa_text(kept), kept)

    def test_epc_slash_rules_are_enforced(self):
        self.assertEqual(sanitize_sepa_text("/INV//2026/"), "INV/2026")

    def test_non_latin_scripts_become_spaces(self):
        result = sanitize_sepa_text("北京 Co")
        self.assertTrue(all(c in _SEPA_ALLOWED for c in result))
        self.assertIn("Co", result)

    def test_empty_and_none_passthrough(self):
        self.assertEqual(sanitize_sepa_text(""), "")
        self.assertIsNone(sanitize_sepa_text(None))


@tagged('eh_account_sepa_ct', 'integration', 'post_install', '-at_install')
class TestSepaMsgId(EhAccountIntegrationTestCase):

    def _make_batch(self):
        journal = self.env['account.journal'].search([
            ('company_id', '=', self.company.id), ('type', '=', 'bank'),
        ], limit=1) or self.env['account.journal'].create({
            'name': 'SEPA Bank', 'code': 'SEPB', 'type': 'bank',
            'company_id': self.company.id,
        })
        return self.env['eh.batch.payment'].create({
            'journal_id': journal.id,
            'batch_type': 'outbound',
        })

    def test_msg_id_unique_per_call(self):
        """Two exports in the same second must not collide: the uuid
        fragment makes every MsgId unique."""
        batch = self._make_batch()
        ids = {batch._eh_sepa_msg_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_msg_id_length_and_charset(self):
        batch = self._make_batch()
        msg_id = batch._eh_sepa_msg_id()
        self.assertLessEqual(len(msg_id), 35)
        self.assertTrue(all(c in _SEPA_ALLOWED for c in msg_id))
