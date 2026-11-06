# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
Multi-company isolation regression for eh.bas.gst.recon.result.

The GST control reconciliation result is a transient holder linked to a BAS
run (run_id.company_id). Its ir.model.access grants full read/write to
group_eh_user, so without a company record rule a user assigned only to
company A could read (and overwrite/delete) company B's 1A/1B control
figures. A global ir.rule with domain [('run_id.company_id', 'in',
company_ids)] must scope every ordinary user to their own companies.

Exercised as a non-superuser (with_user), because the true superuser
bypasses record rules.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_l10n_au_bas', 'post_install', '-at_install')
class BasGstReconIsolationTest(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.env.company
        cls.company_b = cls.env['res.company'].sudo().create({
            'name': 'BAS Isolation Co B',
        })
        # A run + recon result in each company. Created under sudo so the
        # global company rule (which also binds the admin test user) never
        # blocks the fixture build.
        cls.run_a = cls.env['eh.bas.run'].sudo().create({
            'company_id': cls.company_a.id,
            'quarter': 'q3',
            'name': 'BAS iso A',
        })
        cls.run_b = cls.env['eh.bas.run'].sudo().create({
            'company_id': cls.company_b.id,
            'quarter': 'q3',
            'name': 'BAS iso B',
        })
        Recon = cls.env['eh.bas.gst.recon.result'].sudo()
        cls.recon_a = Recon.create({
            'run_id': cls.run_a.id,
            'label_1a': 1000.0,
            'label_1b': 400.0,
        })
        cls.recon_b = Recon.create({
            'run_id': cls.run_b.id,
            'label_1a': 9000.0,
            'label_1b': 3000.0,
        })
        # A plain operator assigned ONLY to company A. group_eh_user grants
        # full CRUD on the recon model, so only the ir.rule stands between
        # this user and company B's control figures.
        group = cls.env.ref(
            'eh_account_base.group_eh_user', raise_if_not_found=False)
        cls.plain_user = False
        if group:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'BAS Iso Plain User',
                'login': 'bas_iso_plain_user',
                'group_ids': [(6, 0, group.ids)],
                'company_id': cls.company_a.id,
            })

    def test_foreign_company_recon_is_not_readable(self):
        if not self.plain_user:
            self.skipTest("No eh_account_base.group_eh_user available in env")
        Recon = self.env['eh.bas.gst.recon.result'].with_user(self.plain_user)
        visible_ids = Recon.search([]).ids
        self.assertIn(
            self.recon_a.id, visible_ids,
            "Own-company GST reconciliation must remain readable",
        )
        self.assertNotIn(
            self.recon_b.id, visible_ids,
            "Foreign-company GST reconciliation leaked without a company "
            "ir.rule on eh.bas.gst.recon.result",
        )

    def test_foreign_company_recon_cannot_be_read_directly(self):
        if not self.plain_user:
            self.skipTest("No eh_account_base.group_eh_user available in env")
        rec_b = self.recon_b.with_user(self.plain_user)
        with self.assertRaises(AccessError):
            rec_b.read(['label_1a', 'label_1b'])

    def test_foreign_company_recon_cannot_be_unlinked(self):
        if not self.plain_user:
            self.skipTest("No eh_account_base.group_eh_user available in env")
        with self.assertRaises(AccessError):
            self.recon_b.with_user(self.plain_user).unlink()

    def test_public_reconciliation_method_rejects_foreign_run(self):
        if not self.plain_user:
            self.skipTest("No eh_account_base.group_eh_user available in env")
        with self.assertRaises(AccessError):
            self.run_b.with_user(
                self.plain_user,
            ).compute_gst_control_reconciliation()
