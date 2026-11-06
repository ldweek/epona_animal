# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: the AP OCR extractor credentials (eh_ap_ocr_config) hold a
live third-party API key and must not be readable by every internal user.

res.company is readable by base.group_user, so a plain accounting user could
previously ``read(['eh_ap_ocr_config'])`` over RPC and lift the extractor API
key. The field now carries groups="base.group_system"; a non-admin read must
be refused while the intake (which reads the value via sudo) still functions.

The test env runs as SUPERUSER, so the negative assertion is made through
``with_user(a plain user)``.
"""

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_ap_automation', 'post_install', '-at_install')
class TestOcrCredentialsGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # Stash a credential blob as the superuser; the whole point is that
        # a plain user must not be able to read it back out.
        cls.company.sudo().eh_ap_ocr_config = (
            '{"api_key": "sk-live-SECRET", "model": "vision-1"}'
        )
        # A plain accounting user (group_eh_user): NOT a manager, NOT
        # superuser, so the field-level group guard still fires. On Odoo 19
        # res.users uses group_ids (the 16/17 transform rewrites it to
        # groups_id).
        try:
            cls.user = cls.env['res.users'].create({
                'name': 'EH AP OCR Plain User',
                'login': 'eh_ap_ocr_plain_user',
                'group_ids': [(6, 0, [
                    cls.env.ref('base.group_user').id,
                    cls.env.ref('eh_account_base.group_eh_user').id])],
            })
        except Exception:  # pragma: no cover - hardened env may forbid it
            cls.user = None

    def test_plain_user_cannot_read_ocr_credentials(self):
        """A non-admin RPC read of the extractor key must be refused."""
        if not self.user:
            self.skipTest("No plain user could be created in this environment.")
        company = self.company.with_user(self.user)
        # Attribute access forces a fetch -> field-level access check.
        with self.assertRaises(AccessError):
            company.eh_ap_ocr_config  # noqa: B018
        # The explicit read() path must be refused the same way.
        with self.assertRaises(AccessError):
            company.read(['eh_ap_ocr_config'])

    def test_admin_can_still_read_ocr_credentials(self):
        """The guard must not lock out administrators / sudo callers, or
        intake processing would lose its credentials."""
        self.assertIn(
            'sk-live-SECRET',
            self.company.sudo().eh_ap_ocr_config,
        )
