# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tests for the shared OCR helper: response parsing, credential coercion,
and multimodal content building. Pure-logic, no network.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_ap_automation.extractors import ocr_common
from odoo.addons.eh_account_ap_automation.extractors.base import (
    ExtractorError,
)


@tagged('post_install', '-at_install')
class OcrCommonTest(TransactionCase):

    def test_lines_from_text_parses_array(self):
        raw = (
            'Here:\n```json\n[{"description":"A","quantity":2,'
            '"unit_price":5,"line_total":10,"confidence":0.9}]\n```'
        )
        lines = ocr_common.lines_from_text(raw)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].description, 'A')
        self.assertEqual(lines[0].line_total, 10)
        self.assertEqual(lines[0].confidence, 0.9)

    def test_lines_from_text_garbage_is_empty(self):
        self.assertEqual(ocr_common.lines_from_text('no json'), [])

    def test_line_total_defaulted_from_qty_price(self):
        raw = '[{"description":"B","quantity":3,"unit_price":4}]'
        lines = ocr_common.lines_from_text(raw)
        self.assertEqual(lines[0].line_total, 12)

    def test_confidence_clamped(self):
        raw = '[{"description":"C","quantity":1,"unit_price":1,"confidence":5}]'
        self.assertEqual(ocr_common.lines_from_text(raw)[0].confidence, 1.0)

    def test_parse_credentials_requires_fields(self):
        with self.assertRaises(ExtractorError):
            ocr_common.parse_credentials('{"api_key":"a"}',
                                         required=('api_key', 'model'))

    def test_parse_credentials_bad_json(self):
        with self.assertRaises(ExtractorError):
            ocr_common.parse_credentials('{not json')

    def test_openai_content_image(self):
        content = ocr_common.build_openai_user_content(
            b'\x89PNG', 'image/png', {})
        self.assertIn('image_url', [p['type'] for p in content])

    def test_anthropic_blocks_document(self):
        blocks = ocr_common.build_anthropic_user_blocks(
            b'%PDF', 'application/pdf', {})
        self.assertIn('document', [b['type'] for b in blocks])
