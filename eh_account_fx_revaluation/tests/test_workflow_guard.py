# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.workflow.guard retrofit regression.

Closes the systemic "state machine enforced in UI only" defect: the posting
state machines here (FX revaluation run, CTA position, hedge, hedge movement)
used to be protected only by a readonly widget and a write() guard that fired
solely when leaving a *frozen* state. A DRAFT record's state is not frozen, so
a plain user could RPC ``write({'state': 'posted'})`` straight past the
action_* method and its journal entry.

The eh.workflow.guard mixin now blocks ANY direct write to the guarded 'state'
field unless the write originates from one of the record's own actions (which
flag the write). These tests assert that a non-superuser plain accounting user
is refused the bypass on every guarded model, while the sanctioned actions
still transition state.

IMPORTANT: the test env acts as a trusted user, so the guard is exercised via
``with_user(a plain non-manager user)`` - otherwise the guard correctly does
not fire.
"""

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase

from .compat import user_group_values


@tagged('eh_account_fx_revaluation', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):
    """A plain user cannot RPC-write a guarded state field directly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fx_gain = cls._ensure_account(
            cls.env, '4900', 'Unrealised FX Gain', 'income_other',
        )
        cls.fx_loss = cls._ensure_account(
            cls.env, '5900', 'Unrealised FX Loss', 'expense',
        )
        cls.cta_equity = cls._ensure_account(
            cls.env, '3600', 'CTA Reserve', 'equity',
        )
        # A plain accounting user: has ORM write access (the CSV grants
        # group_eh_user write) but is NOT a manager. The guard must refuse the
        # direct state write regardless of that write ACL.
        cls.plain_user = cls.env['res.users'].create({
            'name': 'FX Workflow Plain User',
            'login': 'fx_workflow_guard_plain',
            'email': 'fx_workflow_guard_plain@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            **user_group_values(
                cls.env, cls.env.ref('eh_account_base.group_eh_user'),
            ),
        })

    def test_plain_user_cannot_write_run_state(self):
        """A draft FX revaluation run's state cannot be RPC-forced to posted:
        that would skip action_post and its journal entry."""
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.fx_gain.id,
            'loss_account_id': self.fx_loss.id,
            'auto_reverse': False,
        })
        self.assertEqual(run.state, 'draft')
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).write({'state': 'posted'})
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).with_context(
                eh_run_state_change=True,
            ).write({'state': 'posted'})
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).write({'total_gain': 999.0})
        with self.assertRaises(AccessError):
            run.with_user(self.plain_user).write({
                'posted_by_id': self.plain_user.id,
            })
        self.assertEqual(run.state, 'draft')
        self.assertEqual(run.total_gain, 0.0)

    def test_plain_user_cannot_write_cta_state(self):
        """An open CTA position cannot be RPC-forced to disposed: that would
        fabricate a closed reserve with no IAS 21.48 entry behind it."""
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Guard CTA position',
            'cta_account_id': self.cta_equity.id,
        })
        self.assertEqual(position.state, 'open')
        with self.assertRaises(AccessError):
            position.with_user(self.plain_user).write({'state': 'disposed'})
        with self.assertRaises(AccessError):
            position.with_user(self.plain_user).with_context(
                eh_cta_state_change=True,
            ).write({'state': 'disposed'})
        with self.assertRaises(AccessError):
            position.with_user(self.plain_user).write({
                'disposed_by_id': self.plain_user.id,
            })
        self.assertEqual(position.state, 'open')

    def test_plain_user_cannot_write_hedge_state(self):
        """A draft hedge cannot be RPC-forced to designated/effective: that
        would skip action_designate and its IFRS 9 checks."""
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'USD revenue Q3 2026',
            'hedging_instrument_description': 'AUD/USD forward 1M notional',
            'hedged_currency_id': self.company.currency_id.id,
        })
        self.assertEqual(hedge.state, 'draft')
        with self.assertRaises(AccessError):
            hedge.with_user(self.plain_user).write({'state': 'effective'})
        with self.assertRaises(AccessError):
            hedge.with_user(self.plain_user).write({
                'last_test_effective': True,
            })
        self.assertEqual(hedge.state, 'draft')

    def test_plain_user_cannot_write_movement_state(self):
        """A draft hedge movement cannot be RPC-forced to posted: that would
        finalise a gain/loss figure with no journal entry behind it."""
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'USD revenue Q3 2026',
            'hedging_instrument_description': 'AUD/USD forward 1M notional',
            'hedged_currency_id': self.company.currency_id.id,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1000.0,
            'effective_portion': 0.0,
        })
        self.assertEqual(movement.state, 'draft')
        with self.assertRaises(AccessError):
            movement.with_user(self.plain_user).write({'state': 'posted'})
        with self.assertRaises(AccessError):
            movement.with_user(self.plain_user).with_context(
                eh_hedge_state_change=True,
            ).write({'state': 'posted'})
        with self.assertRaises(AccessError):
            movement.with_user(self.plain_user).write({
                'ineffective_portion': 999.0,
            })
        self.assertEqual(movement.state, 'draft')

    def test_account_move_fx_provenance_is_engine_owned(self):
        """Readonly provenance fields are also protected at ORM level."""
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.fx_gain.id,
            'loss_account_id': self.fx_loss.id,
            'auto_reverse': False,
        })
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Provenance CTA position',
            'cta_account_id': self.cta_equity.id,
        })
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'USD revenue Q3 2026',
            'hedging_instrument_description': 'AUD/USD forward 1M notional',
            'hedged_currency_id': self.company.currency_id.id,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1000.0,
            'effective_portion': 0.0,
        })
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-03-31',
            'ref': 'Unrelated journal entry',
        })
        for field_name, record_id in (
            ('eh_fx_revaluation_run_id', run.id),
            ('eh_fx_revaluation_reversal_run_id', run.id),
            ('eh_cta_position_id', position.id),
            ('eh_fx_hedge_movement_id', movement.id),
            ('eh_fx_hedge_reclassification_movement_id', movement.id),
        ):
            with self.assertRaises(AccessError):
                move.write({field_name: record_id})
