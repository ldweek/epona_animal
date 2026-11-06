# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Render regression for the two shipped asset PDFs.

Both reports (Asset Register and Asset Card & Schedule) previously
shipped without a render test, and both broke at render time. These
HTML render checks prove the QWeb templates resolve every field and
helper on a valid eh.asset, so any future KeyError or template break is
caught before release. HTML render is used so no wkhtmltopdf binary is
required in CI.
"""

from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAssetReports(EhAssetTestCase):

    def test_asset_pdf_reports_render(self):
        asset = self._make_asset()

        register = self.env.ref(
            'eh_account_assets_pro.action_report_asset_register',
        )
        html, ftype = register._render_qweb_html(
            register.report_name, asset.ids,
        )
        self.assertEqual(ftype, 'html')
        self.assertTrue(html)
        self.assertIn(b'Asset Register', html)

        card = self.env.ref(
            'eh_account_assets_pro.action_report_asset_card',
        )
        card_html, card_ftype = card._render_qweb_html(
            card.report_name, asset.ids,
        )
        self.assertEqual(card_ftype, 'html')
        self.assertTrue(card_html)
