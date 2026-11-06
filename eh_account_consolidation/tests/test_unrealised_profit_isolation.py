# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression guard: eh.consol.unrealised.profit is company-scoped.

The defect this closes: every sibling consolidation model (run, run line,
elimination, elimination line, entity, member) carries a global company
ir.rule, but eh.consol.unrealised.profit had ir.model.access rows granting
group_eh_user read AND create/write with NO record rule. That left the
child model fully unscoped, so an ordinary accounting user in Company A
could (a) search_read every other company's intra-group unrealised-margin
rows and (b) create({'run_id': <foreign run>, 'unrealised_amount': ...}) to
inject a fabricated elimination margin into another group's consolidation
run (the posting loop at consol_run.py feeds each row into a real
elimination journal line).

The fix applies the same complete-company-closure rule as every sibling:
parent, dedicated ledger and every member must all be enabled in the current
request context. The negative paths MUST run as a non-superuser (env.su
bypasses record rules); a group_eh_user is used so the model ACL grants
read/create and the failure observed is the record rule, not a coarse
access-rights denial.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)

from .common import make_account


@tagged('eh_account_consolidation', 'post_install', '-at_install')
class TestUnrealisedProfitIsolation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls.company

        # A foreign group parent (Company B) with its own entity, run and
        # unrealised-profit row. Same currency as A so no FX rate is needed.
        cls.company_b = cls.env['res.company'].create({
            'name': 'Consol Isolation B',
            'currency_id': cls.company_a.currency_id.id,
        })
        # The superuser building the B fixtures needs B among its allowed
        # companies; this does not touch the separate scoped user below.
        cls.env.user.write({'company_ids': [(4, cls.company_b.id)]})

        Entity = cls.env['eh.consol.entity']
        Run = cls.env['eh.consol.run']
        Up = cls.env['eh.consol.unrealised.profit']
        cls.inventory_a = make_account(
            cls.env, cls.company_a, 'ISOA1400',
            'Isolation A Inventory', 'asset_current')
        cls.cogs_a = make_account(
            cls.env, cls.company_a, 'ISOA5100',
            'Isolation A COGS', 'expense_direct_cost')
        cls.inventory_b = make_account(
            cls.env, cls.company_b, 'ISOB1400',
            'Isolation B Inventory', 'asset_current')
        cls.cogs_b = make_account(
            cls.env, cls.company_b, 'ISOB5100',
            'Isolation B COGS', 'expense_direct_cost')

        cls.entity_b = Entity.create({
            'name': 'Foreign Group B',
            'code': 'iso_group_b',
            'parent_company_id': cls.company_b.id,
            'presentation_currency_id': cls.company_b.currency_id.id,
        })
        cls.run_b = Run.create({
            'entity_id': cls.entity_b.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        cls.up_b = Up.create({
            'run_id': cls.run_b.id,
            'name': 'B intra-group margin',
            'unrealised_amount': 9_000_000.0,
            'inventory_account_id': cls.inventory_b.id,
            'cogs_or_re_account_id': cls.cogs_b.id,
        })

        # A home-company (Company A) entity/run/row, used as a positive
        # control so the rule is proven to scope rather than blanket-deny.
        cls.entity_a = Entity.create({
            'name': 'Home Group A',
            'code': 'iso_group_a',
            'parent_company_id': cls.company_a.id,
            'presentation_currency_id': cls.company_a.currency_id.id,
        })
        cls.run_a = Run.create({
            'entity_id': cls.entity_a.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        cls.up_a = Up.create({
            'run_id': cls.run_a.id,
            'name': 'A intra-group margin',
            'unrealised_amount': 1_000.0,
            'inventory_account_id': cls.inventory_a.id,
            'cogs_or_re_account_id': cls.cogs_a.id,
        })

        # A non-superuser scoped only to Company A. group_eh_user grants
        # model-level read/create, so a leak/inject reaching the ORM proves
        # the record rule blocks it, not a plain ACL denial. Odoo 19 uses
        # group_ids (transform maps to groups_id on 16/17).
        try:
            cls.user = cls.env['res.users'].create({
                'name': 'Consol Iso User A',
                'login': 'eh_consol_iso_user_a',
                'company_id': cls.company_a.id,
                'company_ids': [(6, 0, cls.company_a.ids)],
                'group_ids': [
                    (4, cls.env.ref('base.group_user').id),
                    (4, cls.env.ref('eh_account_base.group_eh_user').id),
                ],
            })
        except Exception:  # noqa: BLE001
            cls.user = False

    def test_foreign_company_rows_not_readable(self):
        """A Company A user cannot read Company B's unrealised-profit rows."""
        if not self.user:
            self.skipTest("No non-superuser could be provisioned.")
        visible = self.env['eh.consol.unrealised.profit'].with_user(
            self.user).search([])
        self.assertIn(
            self.up_a, visible,
            "The user must still see its own company's rows.")
        self.assertNotIn(
            self.up_b, visible,
            "Company B's unrealised-profit row leaked across companies.")

    def test_inject_into_foreign_run_blocked(self):
        """A Company A user cannot inject a row onto Company B's run."""
        if not self.user:
            self.skipTest("No non-superuser could be provisioned.")
        with self.assertRaises(AccessError):
            self.env['eh.consol.unrealised.profit'].with_user(
                self.user).create({
                    'run_id': self.run_b.id,
                    'name': 'injected fabricated margin',
                    'unrealised_amount': 9_000_000.0,
                })

    def test_own_company_create_allowed(self):
        """The rule scopes, not blocks: an in-company create still works."""
        if not self.user:
            self.skipTest("No non-superuser could be provisioned.")
        rec = self.env['eh.consol.unrealised.profit'].with_user(
            self.user).create({
                'run_id': self.run_a.id,
                'name': 'own-company margin',
                'unrealised_amount': 250.0,
                'inventory_account_id': self.inventory_a.id,
                'cogs_or_re_account_id': self.cogs_a.id,
            })
        self.assertTrue(rec.exists())
