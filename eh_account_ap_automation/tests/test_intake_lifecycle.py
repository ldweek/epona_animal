# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Intake lifecycle tests: state transitions, parsing, sequence assignment.
"""

import odoo

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import EhApTestCase


SAMPLE_BILL_TEXT = """
ACME Supplies
Invoice: INV-2026-00042
Date: 2026-04-30

WIDGET-001 10 12.50
GADGET-002 5 25.00

Subtotal: 250.00
Total: 250.00
"""


@tagged('eh_account_ap_automation', 'integration', 'post_install', '-at_install')
class TestIntakeLifecycle(EhApTestCase):

    def test_sequence_assigned_on_create(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        self.assertNotEqual(intake.name, '/')
        self.assertTrue(intake.name.startswith('APIN/'))

    def test_list_statistics_use_stored_counts_and_omit_zeroes(self):
        empty_intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        empty_intake.invalidate_recordset(['eh_list_statistics'])
        self.assertFalse(empty_intake.eh_list_statistics)
        empty_domain = [('intake_id', '=', empty_intake.id)]
        self._assert_line_list_action(
            empty_intake.action_view_lines(), empty_domain, 0,
        )
        self._assert_line_list_action(
            empty_intake.action_view_matched_lines(),
            empty_domain + [(
                'match_status', 'in', ['ok', 'overridden'],
            )],
            0,
        )
        self._assert_line_list_action(
            empty_intake.action_view_exception_lines(),
            empty_domain + [(
                'match_status', 'in', [
                    'qty_exception', 'price_exception',
                    'no_match', 'over_received',
                ],
            )],
            0,
        )

        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        self.env['eh.ap.intake.line'].sudo().create([
            {
                'intake_id': intake.id,
                'product_code': 'MATCHED',
                'match_status': 'ok',
            },
            {
                'intake_id': intake.id,
                'product_code': 'EXCEPTION',
                'match_status': 'no_match',
            },
        ])
        intake.invalidate_recordset([
            'line_count', 'ok_count', 'exception_count',
            'eh_list_statistics',
        ])
        self.assertEqual(
            [(item['label'], item['value'])
             for item in intake.eh_list_statistics],
            [("Lines", 2), ("Matched", 1), ("Exceptions", 1)],
        )
        self.assertEqual(
            [(item['label'], item['actionMethod'])
             for item in intake._eh_list_statistics_hook()[intake.id]],
            [
                ("Lines", 'action_view_lines'),
                ("Matched", 'action_view_matched_lines'),
                ("Exceptions", 'action_view_exception_lines'),
            ],
        )

        domain = [('intake_id', '=', intake.id)]
        self._assert_line_list_action(
            intake.action_view_lines(), domain, intake.line_count,
        )
        self._assert_line_list_action(
            intake.action_view_matched_lines(),
            domain + [('match_status', 'in', ['ok', 'overridden'])],
            intake.ok_count,
        )
        self._assert_line_list_action(
            intake.action_view_exception_lines(),
            domain + [(
                'match_status', 'in', [
                    'qty_exception', 'price_exception',
                    'no_match', 'over_received',
                ],
            )],
            intake.exception_count,
        )

    def _assert_line_list_action(self, action, domain, expected_count):
        expected_list_type = (
            'list' if odoo.release.version_info[0] >= 18 else 'tree'
        )
        self.assertEqual(action['res_model'], 'eh.ap.intake.line')
        self.assertEqual(
            action['view_mode'], '%s,form' % expected_list_type,
        )
        self.assertEqual(
            action['views'][0], (False, expected_list_type),
        )
        self.assertNotIn('res_id', action)
        self.assertEqual(action['domain'], domain)
        self.assertEqual(
            self.env['eh.ap.intake.line'].search_count(action['domain']),
            expected_count,
        )

    def test_parse_extracts_header_and_lines(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': SAMPLE_BILL_TEXT,
        })
        intake.action_parse()
        self.assertEqual(intake.state, 'parsed')
        self.assertEqual(intake.vendor_reference, 'INV-2026-00042')
        self.assertAlmostEqual(intake.extracted_total, 250.0, places=2)
        self.assertEqual(len(intake.line_ids), 2)
        line_a = intake.line_ids.filtered(lambda l: l.product_code == 'WIDGET-001')
        self.assertTrue(line_a)
        self.assertEqual(line_a.invoice_qty, 10.0)
        self.assertEqual(line_a.invoice_price, 12.5)
        self.assertEqual(line_a.product_id, self.product_a)

    def test_parse_blocks_without_text(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        with self.assertRaises(UserError):
            intake.action_parse()

    def test_parse_does_not_clobber_existing_lines(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': SAMPLE_BILL_TEXT,
        })
        # Manually seed a line first.
        self.env['eh.ap.intake.line'].create({
            'intake_id': intake.id,
            'product_code': 'MANUAL-1',
            'invoice_qty': 1.0,
            'invoice_price': 9.99,
        })
        intake.action_parse()
        # Should not have appended the parsed lines because manual lines exist.
        self.assertEqual(len(intake.line_ids), 1)
        self.assertEqual(intake.line_ids.product_code, 'MANUAL-1')

    def test_match_blocks_when_no_lines(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': 'Garbage with no parseable lines',
        })
        intake.action_parse()
        with self.assertRaises(UserError):
            intake.action_match()

    def test_post_blocks_in_received_state(self):
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        with self.assertRaises(UserError):
            intake.action_post()

    def test_reject_intake(self):
        self.env.user.group_ids |= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
        })
        intake.rejection_reason = 'Duplicate bill'
        intake.action_reject()
        self.assertEqual(intake.state, 'rejected')
        self.assertEqual(intake.rejection_reason, 'Duplicate bill')

    def test_parse_decimal_handles_commas(self):
        from odoo.addons.eh_account_ap_automation.models.ap_intake import (
            EhApIntake,
        )
        self.assertEqual(EhApIntake._parse_decimal('1,234.56'), 1234.56)
        self.assertEqual(EhApIntake._parse_decimal('1.234,56'), 1234.56)
        self.assertEqual(EhApIntake._parse_decimal('1,234'), 1234.0)
        self.assertEqual(EhApIntake._parse_decimal('1234,56'), 1234.56)
        self.assertEqual(EhApIntake._parse_decimal('1234.56'), 1234.56)

    def test_partner_specific_regex(self):
        self.partner_a.eh_ap_invoice_ref_regex = r'(?im)REF[:#\s]+([A-Z0-9-]+)'
        intake = self.env['eh.ap.intake'].create({
            'partner_id': self.partner_a.id,
            'raw_text': 'Some text REF: CUSTOM-9999\n',
        })
        intake.action_parse()
        self.assertEqual(intake.vendor_reference, 'CUSTOM-9999')
