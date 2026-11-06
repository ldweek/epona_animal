# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""IAS 40 investment property tests."""

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_investment_property', 'integration', 'post_install',
        '-at_install')
class TestInvestmentProperty(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.prop = cls._ensure_account(
            cls.env, '1660', 'Investment Property', 'asset_non_current')
        cls.fv_gl = cls._ensure_account(
            cls.env, '4660', 'Investment Property FV Gain/Loss', 'income_other')
        cls.dep_exp = cls._ensure_account(
            cls.env, '6660', 'Investment Property Depreciation', 'expense')
        cls.accum_dep = cls._ensure_account(
            cls.env, '1661', 'Accumulated Depreciation IP',
            'asset_non_current')
        cls.ppe = cls._ensure_account(
            cls.env, '1560', 'Owner-Occupied Property', 'asset_non_current')
        cls.opening_clear = cls._ensure_account(
            cls.env, '2160', 'Investment Property Acquisition Clearing',
            'liability_current')

    def _prop(self, **vals):
        base = {
            'name': '/', 'model_basis': 'fair_value', 'initial_cost': 500000.0,
            'acquisition_date': '2026-01-01',
            'property_account_id': self.prop.id,
            'opening_counterpart_account_id': self.opening_clear.id,
            'fv_gain_loss_account_id': self.fv_gl.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.investment.property'].create(base)

    def _bal(self, account):
        lines = self.env['account.move.line'].search([
            ('account_id', '=', account.id), ('parent_state', '=', 'posted')])
        return sum(lines.mapped('debit')) - sum(lines.mapped('credit'))

    def test_activate_at_cost(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        self.assertEqual(p.state, 'held')
        self.assertAlmostEqual(p.carrying_amount, 500000.0, places=2)
        self.assertEqual(p.opening_move_id.state, 'posted')
        self.assertTrue(p.opening_move_id.eh_sealed)

    def test_fair_value_gain_to_pl(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 560000.0
        p.action_remeasure()
        self.assertAlmostEqual(self._bal(self.prop), 560000.0, places=2)
        self.assertAlmostEqual(self._bal(self.fv_gl), -60000.0, places=2)
        self.assertAlmostEqual(p.carrying_amount, 560000.0, places=2)

    def test_fair_value_loss_to_pl(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 470000.0
        p.action_remeasure()
        self.assertAlmostEqual(self._bal(self.fv_gl), 30000.0, places=2)

    def test_zero_fair_value_requires_and_consumes_attestation(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 0.0
        with self.assertRaises(UserError):
            p.action_remeasure()
        self.assertEqual(len(p.move_ids), 1)

        p.write({
            'zero_fair_value_confirmed': True,
            'zero_fair_value_reason': 'Independent valuer confirmed nil value.',
        })
        p.action_remeasure()

        self.assertEqual(p.carrying_amount, 0.0)
        self.assertFalse(p.zero_fair_value_confirmed)
        self.assertFalse(p.zero_fair_value_reason)

    def test_controlled_reversal_neutralises_all_property_entries(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 560000.0
        p.action_remeasure()
        originals = p.move_ids
        p.reversal_date = fields.Date.context_today(p)

        p.action_reverse()

        self.assertEqual(p.state, 'reversed')
        self.assertEqual(p.carrying_amount, 0.0)
        reversals = p.move_ids.filtered('reversed_entry_id')
        self.assertEqual(len(reversals), len(originals))
        self.assertTrue(all(move.state == 'posted' for move in reversals))
        for original in originals:
            reversal = reversals.filtered(
                lambda move, source=original: move.reversed_entry_id == source
            )
            self.assertEqual(len(reversal), 1)
            self.assertEqual(
                sorted((line.account_id.id, line.debit, line.credit)
                       for line in reversal.line_ids),
                sorted((line.account_id.id, line.credit, line.debit)
                       for line in original.line_ids),
            )

    def test_cost_model_cannot_remeasure(self):
        p = self._prop(model_basis='cost', initial_cost=500000.0)
        p.action_activate()
        with self.assertRaises(UserError):
            p.action_remeasure()

    def test_incremental_remeasurement(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 560000.0
        p.action_remeasure()
        p.fair_value = 600000.0
        p.action_remeasure()
        # 60000 then 40000 = 100000 total uplift.
        self.assertAlmostEqual(self._bal(self.prop), 600000.0, places=2)

    def test_activate_requires_manager(self):
        p = self._prop(initial_cost=500000.0)
        user = self.env['res.users'].create({
            'name': 'p0', 'login': 'ip_plain0@test', 'email': 'ip_plain0@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            p.with_user(user).action_activate()
        # The recognition must not have taken effect for a non-manager.
        self.assertEqual(p.state, 'draft')

    def test_remeasure_requires_manager(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 560000.0
        user = self.env['res.users'].create({
            'name': 'p', 'login': 'ip_plain@test', 'email': 'ip_plain@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            p.with_user(user).action_remeasure()

    def test_entry_balances(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 560000.0
        p.action_remeasure()
        for move in p.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    # --- Cost model depreciation (IAS 40.56) -----------------------------

    def _cost_prop(self, **vals):
        base = {
            'model_basis': 'cost', 'initial_cost': 500000.0,
            'useful_life_years': 50,
            'depreciation_expense_account_id': self.dep_exp.id,
            'accumulated_depreciation_account_id': self.accum_dep.id,
        }
        base.update(vals)
        return self._prop(**base)

    def test_cost_model_depreciation_posts_and_reduces_carrying(self):
        p = self._cost_prop()
        p.action_activate()
        self.assertAlmostEqual(p.carrying_amount, 500000.0, places=2)
        p.action_depreciate()
        # 500000 / 50 = 10000 per period.
        self.assertAlmostEqual(p.accumulated_depreciation, 10000.0, places=2)
        self.assertAlmostEqual(p.carrying_amount, 490000.0, places=2)
        self.assertAlmostEqual(self._bal(self.dep_exp), 10000.0, places=2)
        self.assertAlmostEqual(self._bal(self.accum_dep), -10000.0, places=2)

    def test_cost_model_excludes_residual_value_from_depreciable_base(self):
        p = self._cost_prop(residual_value=50000.0)
        p.action_activate()
        p.depreciation_date = fields.Date.to_date('2026-12-31')

        p.action_depreciate()

        # (500,000 - 50,000) / 50 = 9,000.
        self.assertAlmostEqual(p.accumulated_depreciation, 9000.0, places=2)
        self.assertAlmostEqual(p.carrying_amount, 491000.0, places=2)
        event = p.depreciation_event_ids
        self.assertAlmostEqual(event.residual_value_snapshot, 50000.0, 2)

    def test_acquisition_year_depreciation_is_prorated_by_days(self):
        p = self._cost_prop(
            initial_cost=365000.0,
            useful_life_years=1,
            acquisition_date='2026-07-01',
        )
        p.action_activate()
        p.depreciation_date = fields.Date.to_date('2026-12-31')

        p.action_depreciate()

        # 184 inclusive service days in the 365-day 2026 fiscal year.
        self.assertAlmostEqual(p.accumulated_depreciation, 184000.0, 2)
        event = p.depreciation_event_ids
        self.assertEqual(event.coverage_start, fields.Date.to_date('2026-07-01'))
        self.assertEqual(event.coverage_end, fields.Date.to_date('2026-12-31'))

    def test_depreciation_entry_balances(self):
        p = self._cost_prop()
        p.action_activate()
        p.action_depreciate()
        for move in p.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_depreciate_blocked_under_fair_value(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        with self.assertRaises(UserError):
            p.action_depreciate()

    # --- Freeze-after-post of measurement inputs -------------------------

    def test_measurement_inputs_frozen_after_posted_move(self):
        # Cost model: one depreciation move posted. initial_cost and
        # useful_life_years drive charge = initial_cost / useful_life_years,
        # so re-basing them after posting would silently move every
        # subsequent charge; they must be frozen (IAS 40).
        p = self._cost_prop()
        p.action_activate()
        p.action_depreciate()
        self.assertTrue(p._has_posted_move())
        with self.assertRaises(UserError):
            p.initial_cost = 600000.0
        with self.assertRaises(UserError):
            p.useful_life_years = 25
        with self.assertRaises(UserError):
            p.model_basis = 'fair_value'

    def test_measurement_snapshot_freezes_on_activation(self):
        # Activation fixes the measurement basis even before the next GL
        # roll-forward. Otherwise a user can rewrite cost/life after the
        # carrying anchor is established and make the manager's first
        # depreciation post from a different basis.
        p = self._cost_prop()
        p.action_activate()
        self.assertTrue(p._has_posted_move())
        with self.assertRaises(UserError):
            p.initial_cost = 550000.0
        with self.assertRaises(UserError):
            p.useful_life_years = 40

    def test_fair_value_input_editable_after_post(self):
        # fair_value is the remeasure input, not a posted total, so it stays
        # editable after a remeasurement move exists (mirrors HFS FVLCTS).
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 540000.0
        p.action_remeasure()
        self.assertTrue(p._has_posted_move())
        p.fair_value = 560000.0
        self.assertAlmostEqual(p.fair_value, 560000.0, places=2)

    def test_depreciate_requires_positive_life(self):
        p = self._cost_prop(useful_life_years=0)
        p.action_activate()
        with self.assertRaises(UserError):
            p.action_depreciate()

    # --- IAS 40.57 transfer out ------------------------------------------

    def test_transfer_out_reclassifies_at_carrying(self):
        p = self._prop(initial_cost=500000.0,
                        transfer_target_account_id=self.ppe.id)
        p.action_activate()
        p.fair_value = 560000.0
        p.action_remeasure()
        # Carrying amount is now fair value = 560000 (deemed cost on transfer).
        self.assertAlmostEqual(p.carrying_amount, 560000.0, places=2)
        p.action_transfer_out()
        self.assertEqual(p.state, 'transferred')
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(p.accumulated_depreciation, 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.ppe), 560000.0, places=2)
        for move in p.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_transfer_out_cost_model_derecognises_accum_dep(self):
        # Cost model, cost 500000, one 10000 charge -> carrying 490000, accum
        # dep 10000. Transfer out must credit the property account at gross
        # cost (500000) and reverse the accumulated depreciation out (debit
        # 10000), carrying the 490000 net book value into the target. The
        # accumulated depreciation must not be left stranded (IAS 40.60/69).
        p = self._cost_prop(transfer_target_account_id=self.ppe.id)
        p.action_activate()
        p.action_depreciate()
        self.assertAlmostEqual(p.accumulated_depreciation, 10000.0, places=2)
        self.assertAlmostEqual(self._bal(self.accum_dep), -10000.0, places=2)
        p.action_transfer_out()
        self.assertEqual(p.state, 'transferred')
        xfer = p.move_ids.sorted('id')[-1]
        # Property account credited with the full gross cost (500000).
        prop_line = xfer.line_ids.filtered(
            lambda l: l.account_id == self.prop)
        self.assertAlmostEqual(prop_line.credit, 500000.0, places=2)
        self.assertAlmostEqual(prop_line.debit, 0.0, places=2)
        # Net book value (490000) carried into the transfer target.
        self.assertAlmostEqual(self._bal(self.ppe), 490000.0, places=2)
        # Accumulated depreciation reversed out (debit 10000): net ledger nil,
        # i.e. not stranded on the balance sheet.
        self.assertAlmostEqual(self._bal(self.accum_dep), 0.0, places=2)
        # Every move balances by construction.
        for move in p.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_transfer_out_requires_manager(self):
        p = self._prop(initial_cost=500000.0,
                        transfer_target_account_id=self.ppe.id)
        p.action_activate()
        user = self.env['res.users'].create({
            'name': 'p2', 'login': 'ip_plain2@test',
            'email': 'ip_plain2@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            p.with_user(user).action_transfer_out()

    def test_transfer_out_posts_depreciation_through_transfer_date(self):
        p = self._cost_prop(
            initial_cost=365000.0,
            useful_life_years=1,
            transfer_target_account_id=self.ppe.id,
        )
        p.action_activate()
        p.transfer_date = fields.Date.to_date('2026-06-30')

        p.action_transfer_out()

        event = p.depreciation_event_ids
        self.assertAlmostEqual(event.charge, 181000.0, 2)
        self.assertEqual(event.coverage_end, fields.Date.to_date('2026-06-30'))
        self.assertEqual(p.state, 'transferred')
        self.assertEqual(p.carrying_amount, 0.0)
        self.assertEqual(p.accumulated_depreciation, 0.0)

    # --- IAS 40.66-69 disposal / derecognition ---------------------------

    def _dispose_accounts(self):
        return {
            'disposal_cash_account_id': self.account_cash.id,
            'disposal_gain_loss_account_id': self.fv_gl.id,
        }

    def test_dispose_gain_derecognises_and_balances(self):
        # Fair value model, carrying 560000, sold for 600000 -> 40000 gain.
        p = self._prop(initial_cost=500000.0, **self._dispose_accounts())
        p.action_activate()
        p.fair_value = 560000.0
        p.action_remeasure()
        p.disposal_proceeds = 600000.0
        p.action_dispose()
        self.assertEqual(p.state, 'disposed')
        self.assertAlmostEqual(p.accumulated_depreciation, 0.0, places=2)
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)
        # The disposal move must credit the property account with the full
        # carrying amount so the asset leaves the balance sheet.
        disp = p.move_ids.sorted('id')[-1]
        self.assertAlmostEqual(sum(disp.line_ids.mapped('debit')),
                               sum(disp.line_ids.mapped('credit')), places=2)
        prop_line = disp.line_ids.filtered(
            lambda l: l.account_id == self.prop)
        self.assertAlmostEqual(prop_line.credit, 560000.0, places=2)
        self.assertAlmostEqual(prop_line.debit, 0.0, places=2)
        # Cash in for 600000.
        cash_line = disp.line_ids.filtered(
            lambda l: l.account_id == self.account_cash)
        self.assertAlmostEqual(cash_line.debit, 600000.0, places=2)
        # 40000 gain is a credit to the gain/loss account on the same move.
        gl_line = disp.line_ids.filtered(
            lambda l: l.account_id == self.fv_gl)
        self.assertAlmostEqual(gl_line.credit, 40000.0, places=2)
        self.assertAlmostEqual(gl_line.debit, 0.0, places=2)

    def test_dispose_loss_derecognises_and_balances(self):
        # Fair value model, carrying 500000, sold for 460000 -> 40000 loss.
        p = self._prop(initial_cost=500000.0, **self._dispose_accounts())
        p.action_activate()
        p.disposal_proceeds = 460000.0
        p.action_dispose()
        self.assertEqual(p.state, 'disposed')
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.prop), 0.0, places=2)
        self.assertAlmostEqual(self._bal(self.account_cash), 460000.0,
                               places=2)
        # Loss of 40000 is a debit to the gain/loss account.
        self.assertAlmostEqual(self._bal(self.fv_gl), 40000.0, places=2)
        for move in p.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_disposal_entry_uses_approved_disposal_date(self):
        p = self._prop(
            initial_cost=500000.0,
            acquisition_date='2026-01-01',
            disposal_date='2026-06-30',
            disposal_proceeds=500000.0,
            **self._dispose_accounts())
        p.action_activate()
        p.action_dispose()
        disposal_move = p.move_ids.sorted('id')[-1]
        self.assertEqual(
            disposal_move.date, fields.Date.to_date('2026-06-30'))
        self.assertEqual(p.disposal_date, disposal_move.date)
        with self.assertRaises(UserError):
            p.write({'disposal_date': '2026-07-01'})

    def test_dispose_cost_model_derecognises_accum_dep(self):
        # Cost model, cost 500000, one 10000 charge -> carrying 490000.
        # Sold for 490000 -> no gain/loss; accum dep must be reversed out.
        p = self._cost_prop(**self._dispose_accounts())
        p.disposal_gain_loss_account_id = self.dep_exp.id
        p.action_activate()
        p.action_depreciate()
        p.disposal_proceeds = 490000.0
        p.action_dispose()
        self.assertEqual(p.state, 'disposed')
        disp = p.move_ids.sorted('id')[-1]
        # Property account credited with the full gross cost (500000).
        prop_line = disp.line_ids.filtered(
            lambda l: l.account_id == self.prop)
        self.assertAlmostEqual(prop_line.credit, 500000.0, places=2)
        # Accumulated depreciation reversed out (debit 10000): net ledger nil.
        self.assertAlmostEqual(self._bal(self.accum_dep), 0.0, places=2)
        for move in p.move_ids:
            self.assertAlmostEqual(sum(move.line_ids.mapped('debit')),
                                   sum(move.line_ids.mapped('credit')),
                                   places=2)

    def test_dispose_requires_manager(self):
        p = self._prop(initial_cost=500000.0, **self._dispose_accounts())
        p.action_activate()
        p.disposal_proceeds = 600000.0
        user = self.env['res.users'].create({
            'name': 'p3', 'login': 'ip_plain3@test',
            'email': 'ip_plain3@test',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})
        with self.assertRaises(UserError):
            p.with_user(user).action_dispose()

    def test_disposal_posts_depreciation_through_disposal_date(self):
        p = self._cost_prop(
            initial_cost=365000.0,
            useful_life_years=1,
            disposal_date='2026-06-30',
            disposal_proceeds=184000.0,
            **self._dispose_accounts(),
        )
        p.disposal_gain_loss_account_id = self.dep_exp
        p.action_activate()

        p.action_dispose()

        event = p.depreciation_event_ids
        self.assertAlmostEqual(event.charge, 181000.0, 2)
        self.assertEqual(event.coverage_end, fields.Date.to_date('2026-06-30'))
        self.assertEqual(p.carrying_amount, 0.0)
        self.assertEqual(p.accumulated_depreciation, 0.0)

    def test_depreciation_expense_domain_accepts_odoo_depreciation_type(self):
        domain = self.env['eh.investment.property']._fields[
            'depreciation_expense_account_id'
        ].domain
        self.assertIn('expense_depreciation', str(domain))

    def test_revaluation_and_retained_accounts_must_be_equity(self):
        with self.assertRaises(ValidationError):
            self._prop(revaluation_surplus_account_id=self.dep_exp.id)
        with self.assertRaises(ValidationError):
            self._prop(retained_earnings_account_id=self.dep_exp.id)

    def test_depreciation_cannot_exceed_base(self):
        # Two-year life: after two charges the base is exhausted; a third
        # charge must be refused rather than driving carrying amount negative.
        p = self._cost_prop(useful_life_years=2)
        p.action_activate()
        p.depreciation_date = fields.Date.to_date('2026-06-30')
        p.action_depreciate()
        p.depreciation_date = fields.Date.to_date('2027-06-30')
        p.action_depreciate()
        self.assertAlmostEqual(p.carrying_amount, 0.0, places=2)
        self.assertAlmostEqual(p.accumulated_depreciation, 500000.0, places=2)
        with self.assertRaises(UserError):
            p.depreciation_date = fields.Date.to_date('2028-06-30')
            p.action_depreciate()

    def test_carrying_amount_frozen_after_move(self):
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 560000.0
        p.action_remeasure()
        with self.assertRaises(UserError):
            p.carrying_amount = 123.0

    # --- Posted-figure controls: unlink guard + raw state-reset gate ------

    def _plain_user(self, tag):
        return self.env['res.users'].create({
            'name': tag, 'login': '%s@test' % tag, 'email': '%s@test' % tag,
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id])]})

    def test_posted_property_cannot_be_unlinked(self):
        # Once a move is posted the property carries a GL position; deleting
        # it would orphan the entry, so unlink must be refused.
        p = self._cost_prop()
        p.action_activate()
        p.action_depreciate()
        self.assertTrue(p._has_posted_move())
        with self.assertRaises(UserError):
            p.unlink()

    def test_unposted_property_can_be_unlinked(self):
        # A draft property with no posted move is free to delete; the guard
        # must not change existing behaviour before anything is posted.
        p = self._prop(initial_cost=500000.0)
        self.assertFalse(p._has_posted_move())
        p.unlink()
        self.assertFalse(p.exists())

    def test_plain_user_cannot_raw_reset_state_out_of_posted(self):
        # A plain user must not be able to raw-write state back to draft on a
        # property that carries a posted move: that would lift the ledger
        # freeze. Manager-gated at the write layer.
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        p.fair_value = 560000.0
        p.action_remeasure()
        self.assertTrue(p._has_posted_move())
        user = self._plain_user('ip_reset')
        with self.assertRaises(UserError):
            p.with_user(user).write({'state': 'draft'})
        # State unchanged; the freeze still bites.
        self.assertEqual(p.state, 'held')
        with self.assertRaises(UserError):
            p.carrying_amount = 999.0

    def test_normal_remeasure_flow_still_posts(self):
        # The sanctioned recognise -> remeasure flow must keep working end to
        # end: state moves into the posted band and the move posts.
        p = self._prop(initial_cost=500000.0)
        p.action_activate()
        self.assertEqual(p.state, 'held')
        p.fair_value = 560000.0
        p.action_remeasure()
        self.assertTrue(p._has_posted_move())
        self.assertAlmostEqual(p.carrying_amount, 560000.0, places=2)
        self.assertTrue(all(m.state == 'posted' for m in p.move_ids))
