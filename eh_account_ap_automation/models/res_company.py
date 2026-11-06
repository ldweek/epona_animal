# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.company extension: vendor-bill OCR extractor credentials.

The line-item extractor adapters (OpenAI / local / Claude vision
packages) read their API credentials from this single JSON field so the
core AP automation module carries no provider-specific configuration and
no provider dependency. Absent any value, the regex-only path runs
unchanged.
"""

import json

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .regex_policy import compile_safe_regex


_MAX_OCR_CONFIG_BYTES = 64 * 1024
_LEGACY_INVOICE_REF_PATTERN = r'(?im)Invoice[:#\s]+([A-Z0-9\-]+)'
_LEGACY_TOTAL_PATTERN = r'(?im)Total[:\s]+([0-9][0-9,\.]*)'


class ResCompany(models.Model):
    _inherit = 'res.company'

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        for company in companies:
            parser_values = {}
            if company.eh_ap_invoice_ref_regex == _LEGACY_INVOICE_REF_PATTERN:
                parser_values['eh_ap_invoice_ref_regex'] = False
            if company.eh_ap_total_regex == _LEGACY_TOTAL_PATTERN:
                parser_values['eh_ap_total_regex'] = False
            if parser_values:
                company.write(parser_values)
        self.env['eh.ap.tolerance.profile']._eh_ensure_company_defaults(
            companies,
        )
        return companies

    eh_ap_ocr_config = fields.Text(
        string="AP OCR credentials (JSON)",
        groups="base.group_system",
        help=(
            "JSON-encoded credentials handed to the configured vendor-"
            "bill line-item extractor. Shape varies per extractor "
            "(api_key + model for hosted vision, endpoint + model for "
            "local). Only read when an intake names a registered "
            "extractor; the deterministic regex parser needs none. "
            "Restricted to system administrators because it holds the "
            "extractor API key; the intake reads it via sudo so OCR "
            "still works for ordinary users without exposing the "
            "secret to them."
        ),
    )

    @api.constrains('eh_ap_ocr_config')
    def _check_eh_ap_ocr_config(self):
        for company in self:
            raw = company.eh_ap_ocr_config or ''
            if len(raw.encode('utf-8')) > _MAX_OCR_CONFIG_BYTES:
                raise ValidationError(_(
                    "AP OCR configuration exceeds the 64 KiB safety limit."
                ))
            if not raw.strip():
                continue
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValidationError(_(
                    "AP OCR configuration must be valid JSON."
                )) from exc
            if not isinstance(parsed, dict):
                raise ValidationError(_(
                    "AP OCR configuration must be a JSON object."
                ))

    @api.constrains('eh_ap_invoice_ref_regex', 'eh_ap_total_regex')
    def _check_eh_ap_parser_regexes(self):
        for company in self:
            for pattern in (
                    company.eh_ap_invoice_ref_regex,
                    company.eh_ap_total_regex):
                if not pattern:
                    continue
                try:
                    compile_safe_regex(pattern)
                except ValueError as exc:
                    raise ValidationError(_(
                        "Unsafe AP parser regular expression: %s.", exc,
                    )) from exc
