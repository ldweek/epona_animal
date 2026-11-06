# -*- encoding: utf-8 -*-
"""Exact regressions for FX workflow and marketplace metadata contracts."""

import re

from pathlib import Path

from lxml import etree

from odoo.tests import TransactionCase, tagged


@tagged('eh_account_fx_revaluation', 'unit')
class TestFxMediumLowMetadata(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.module_root = Path(__file__).parents[1]

    def test_run_buttons_match_server_authorization(self):
        arch = self.env.ref(
            'eh_account_fx_revaluation.view_eh_fx_revaluation_run_form'
        ).arch_db
        root = etree.fromstring(arch.encode())
        expected = {
            'action_fetch_missing_rates': 'eh_account_base.group_eh_user',
            'action_compute': 'eh_account_base.group_eh_user',
            'action_post': 'eh_account_base.group_eh_manager',
            'action_reverse': 'eh_account_base.group_eh_manager',
            'action_cancel': 'eh_account_base.group_eh_manager',
            'action_set_to_draft': 'eh_account_base.group_eh_manager',
        }
        for method, groups in expected.items():
            with self.subTest(method=method):
                button = root.xpath("//button[@name='%s']" % method)
                self.assertEqual(len(button), 1)
                self.assertEqual(button[0].get('groups'), groups)
        post = root.xpath("//button[@name='action_post']")[0]
        self.assertNotIn('irreversible', (post.get('confirm') or '').lower())

    def test_form_readonly_contract_matches_model_freeze(self):
        arch = self.env.ref(
            'eh_account_fx_revaluation.view_eh_fx_revaluation_run_form'
        ).arch_db
        root = etree.fromstring(arch.encode())
        for name in (
            'revaluation_date', 'journal_id', 'gain_account_id',
            'loss_account_id', 'auto_reverse', 'reversal_date',
            'aggregate_by_partner',
        ):
            nodes = root.xpath("//field[@name='%s']" % name)
            self.assertTrue(nodes, name)
            modifier = (
                nodes[0].get('readonly')
                or nodes[0].get('attrs')
                or ''
            )
            # Odoo 16's generated port converts the authored expression into
            # the legacy attrs-domain form; both encode the same freeze.
            self.assertIn('state', modifier)
            self.assertIn('draft', modifier)
        for name in ('journal_id', 'gain_account_id', 'loss_account_id'):
            self.assertTrue(
                self.env['eh.fx.revaluation.run']._fields[name].check_company,
                name,
            )

    def test_monetary_lists_carry_hidden_currency_field(self):
        for relative in (
            'views/fx_revaluation_line_views.xml',
            'views/fx_revaluation_run_views.xml',
        ):
            root = etree.parse(str(self.module_root / relative))
            hidden = root.xpath(
                "//*[self::list or self::tree]/field[@name='currency_id' "
                "and (@column_invisible='1' or @invisible='1')]"
            )
            self.assertTrue(hidden, relative)

    def test_listing_version_claims_cron_and_tour_are_truthful(self):
        manifest = (self.module_root / '__manifest__.py').read_text()
        listing = (
            self.module_root / 'static/description/index.html'
        ).read_text()
        cron = (self.module_root / 'data/cron.xml').read_text()
        tour = (
            self.module_root
            / 'static/tests/tours/fx_revaluation_test_tour.js'
        ).read_text()
        version_match = re.search(
            r"['\"]version['\"]\s*:\s*['\"]"
            r"(?P<version>\d+\.0\.1\.7\.0)['\"]",
            manifest,
        )
        self.assertTrue(version_match, manifest)
        version = version_match.group('version')
        self.assertIn('v%s' % version, listing)
        self.assertNotIn('1.6.9', listing)
        self.assertFalse((self.module_root / 'demo/fx_demo.xml').exists())
        self.assertNotIn('07:30 UTC', cron)
        self.assertNotIn('lands on kanban', tour)
        self.assertNotIn('waits for the statusbar to reach Computed', tour)

    def test_post_init_hook_is_declared(self):
        manifest = (self.module_root / '__manifest__.py').read_text()
        init = (self.module_root / '__init__.py').read_text()
        self.assertIn("'post_init_hook': 'post_init_hook'", manifest)
        self.assertIn('def post_init_hook(', init)
