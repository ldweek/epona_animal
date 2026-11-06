# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Unique per-sequence-type MsgId generation for SEPA Direct Debit."""

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


_SEPA_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789/-?:().,'+ "
)


@tagged('eh_account_sepa_dd', 'integration', 'post_install', '-at_install')
class TestSepaDdMsgId(EhAccountIntegrationTestCase):

    def _make_batch(self):
        journal = self.env['account.journal'].search([
            ('company_id', '=', self.company.id), ('type', '=', 'bank'),
        ], limit=1) or self.env['account.journal'].create({
            'name': 'SEPA DD Bank', 'code': 'SDDB', 'type': 'bank',
            'company_id': self.company.id,
        })
        return self.env['eh.batch.payment'].create({
            'journal_id': journal.id,
            'batch_type': 'inbound',
        })

    def test_dd_msg_id_unique_per_call(self):
        """Two exports of the same sequence type in the same second must
        not collide: the uuid fragment makes every MsgId unique."""
        batch = self._make_batch()
        ids = {batch._eh_sepa_dd_msg_id('FRST') for _ in range(50)}
        self.assertEqual(len(ids), 50)

    def test_dd_msg_id_length_and_charset(self):
        batch = self._make_batch()
        for seq_type in ('FRST', 'RCUR', 'FNAL', 'OOFF'):
            msg_id = batch._eh_sepa_dd_msg_id(seq_type)
            self.assertLessEqual(len(msg_id), 35)
            self.assertTrue(all(c in _SEPA_ALLOWED for c in msg_id))
            self.assertIn(seq_type, msg_id)
