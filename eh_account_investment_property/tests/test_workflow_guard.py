# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""eh.workflow.guard regression: a plain user cannot RPC-write the state of
an investment property to skip an action and its journal entry (IAS 40)."""

from ast import literal_eval
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_investment_property', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.prop_account = cls._ensure_account(
            cls.env, '1660', 'Investment Property', 'asset_non_current')
        cls.fv_gl = cls._ensure_account(
            cls.env, '4660', 'Investment Property FV Gain/Loss',
            'income_other')
        cls.opening_clear = cls._ensure_account(
            cls.env, '2160', 'Investment Property Acquisition Clearing',
            'liability_current')
        # A low-privilege interactive user: no EH manager group. The guard
        # only fires when NOT self.env.su, so the write must be attempted as
        # this user via with_user(), never as the superuser test env.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'IP Plain User',
            'login': 'ip_plain_user',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.manager_user = cls.env['res.users'].create({
            'name': 'IP Real Manager',
            'login': 'ip_real_manager',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def _draft_prop(self):
        return self.env['eh.investment.property'].create({
            'name': '/', 'model_basis': 'fair_value', 'initial_cost': 500000.0,
            'acquisition_date': '2026-01-01',
            'property_account_id': self.prop_account.id,
            'opening_counterpart_account_id': self.opening_clear.id,
            'fv_gain_loss_account_id': self.fv_gl.id,
            'journal_id': self.journal_misc.id,
        })

    def test_plain_user_cannot_jump_state(self):
        """A non-superuser direct write to a guarded field is refused."""
        prop = self._draft_prop()
        self.assertEqual(prop.state, 'draft')
        with self.assertRaises(AccessError):
            prop.with_user(self.plain_user).write({'state': 'held'})
        # State did not move; no journal entry was manufactured.
        self.assertEqual(prop.state, 'draft')
        self.assertFalse(prop.move_ids)

    def test_action_path_still_works(self):
        """The sanctioned action carries the flag and posts as normal."""
        prop = self._draft_prop()
        prop.action_activate()
        self.assertEqual(prop.state, 'held')

    def test_disposal_accounts_are_configurable_before_activation(self):
        """The form and ORM expose the same pre-recognition control point."""
        view = self.env.ref(
            'eh_account_investment_property.'
            'view_eh_investment_property_form'
        )
        arch = etree.fromstring(view.arch_db.encode())
        for field_name in (
            'disposal_cash_account_id', 'disposal_gain_loss_account_id',
        ):
            nodes = arch.xpath("//field[@name='%s']" % field_name)
            self.assertEqual(len(nodes), 1)
            node = nodes[0]
            readonly = node.get('readonly')
            if readonly is not None:
                self.assertIn("state not in ('draft', 'held')", readonly)
            else:
                readonly_domain = literal_eval(
                    node.get('attrs', '{}'),
                ).get('readonly', [])
                self.assertIn(
                    ('state', 'not in', ['draft', 'held']),
                    readonly_domain,
                )
            for ancestor in node.iterancestors():
                invisible = ancestor.get('invisible')
                if invisible is None and ancestor.get('attrs'):
                    invisible = literal_eval(
                        ancestor.get('attrs'),
                    ).get('invisible')
                self.assertNotEqual(invisible, "state == 'draft'")
                if isinstance(invisible, list):
                    self.assertNotIn(
                        ('state', '=', 'draft'), invisible,
                    )

        prop = self._draft_prop()
        prop.write({
            'disposal_cash_account_id': self.account_cash.id,
            'disposal_gain_loss_account_id': self.fv_gl.id,
        })
        prop.action_activate()
        with self.assertRaises(UserError):
            prop.write({'disposal_cash_account_id': self.fv_gl.id})

    def test_real_user_can_switch_draft_cost_basis_without_guard_collision(self):
        prop = self._draft_prop()
        prop.write({'model_basis': 'cost'})
        prop._eh_ledger_write({'accumulated_depreciation': 125.0})

        prop.with_user(self.plain_user).write({'model_basis': 'fair_value'})

        self.assertEqual(prop.model_basis, 'fair_value')
        self.assertEqual(prop.accumulated_depreciation, 0.0)

    def test_manager_can_fill_missing_later_account_once(self):
        prop = self._draft_prop()
        prop.action_activate()

        prop.with_user(self.manager_user).write({
            'transfer_target_account_id': self.account_cash.id,
        })
        self.assertEqual(prop.transfer_target_account_id, self.account_cash)
        with self.assertRaises(UserError):
            prop.with_user(self.manager_user).write({
                'transfer_target_account_id': self.fv_gl.id,
            })

    def test_form_buttons_match_manager_server_guards(self):
        view = self.env.ref(
            'eh_account_investment_property.'
            'view_eh_investment_property_form'
        )
        arch = etree.fromstring(view.arch_db.encode())
        activate = arch.xpath("//button[@name='action_activate']")[0]
        cancel = arch.xpath("//button[@name='action_cancel']")[0]
        self.assertEqual(
            activate.get('groups'), 'eh_account_base.group_eh_manager',
        )
        self.assertEqual(
            cancel.get('groups'), 'eh_account_base.group_eh_manager',
        )
        invisible = cancel.get('invisible')
        if invisible is not None:
            self.assertEqual(invisible, "state != 'draft'")
        else:
            # Odoo 16 backports translate direct modifiers to attrs domains.
            self.assertEqual(
                literal_eval(cancel.get('attrs'))['invisible'],
                [('state', '!=', 'draft')],
            )

    def test_cost_model_fair_value_disclosure_remains_visible(self):
        view = self.env.ref(
            'eh_account_investment_property.'
            'view_eh_investment_property_form'
        )
        arch = etree.fromstring(view.arch_db.encode())
        fair_value = arch.xpath("//field[@name='fair_value']")[0]
        self.assertIsNone(fair_value.get('invisible'))
        help_text = self.env['eh.investment.property']._fields[
            'fair_value'
        ].help
        self.assertIn('IAS 40.79(e)', help_text)

    def test_search_view_exposes_state_basis_and_company_grouping(self):
        view = self.env.ref(
            'eh_account_investment_property.'
            'view_eh_investment_property_search'
        )
        arch = etree.fromstring(view.arch_db.encode())
        names = set(arch.xpath('//filter/@name'))
        self.assertTrue({
            'held', 'draft', 'terminal', 'fair_value_basis', 'cost_basis',
            'group_state', 'group_basis', 'group_company',
        }.issubset(names))

    def test_measurement_anchors_are_engine_owned_after_activation(self):
        prop = self._draft_prop()
        prop.action_activate()
        for field_name in (
            'carrying_amount', 'accumulated_depreciation', 'remeasurement',
            'transfer_in_asset_display', 'currency_id',
        ):
            value = self.company.currency_id.id \
                if field_name == 'currency_id' else 123.0
            expected = UserError if field_name in (
                'carrying_amount', 'accumulated_depreciation',
            ) else AccessError
            with self.subTest(field=field_name), self.assertRaises(expected):
                prop.with_user(self.plain_user).write({field_name: value})

    def test_transfer_log_cannot_be_fabricated(self):
        prop = self._draft_prop()
        with self.assertRaises(AccessError):
            self.env['eh.investment.property.transfer'].create({
                'property_id': prop.id,
                'company_id': prop.company_id.id,
                'currency_id': prop.currency_id.id,
                'date': '2026-06-30',
                'direction': 'out',
                'basis': 'fair_value',
            })

    def test_move_provenance_is_immutable(self):
        prop = self._draft_prop()
        prop.action_activate()
        prop.fair_value = 510000.0
        prop.action_remeasure()
        move = prop.move_ids[:1]
        with self.assertRaises(AccessError):
            move.write({'eh_investment_property_id': False})

    def test_financial_action_takes_property_row_lock(self):
        prop = self._draft_prop()
        prop.action_activate()
        prop.fair_value = 510000.0
        seen = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            prop.action_remeasure()
        self.assertTrue(any(
            'eh_investment_property' in query and 'FOR UPDATE' in query
            for query in seen
        ))
