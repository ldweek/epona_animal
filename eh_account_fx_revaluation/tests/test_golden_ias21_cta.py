# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 21 / IFRS 9 worked examples: CTA reserve positions.

Covers the parent-books side of the foreign operation lifecycle:

* NIH effective portion parked in the position's CTA equity account,
  with the position balance ledger-fed off the tagged journal entry.
* Disposal reclassification of the FULL accumulated reserve to P&L
  (IAS 21.48), position frozen afterwards.
* Proportionate reclassification for a partial disposal of an
  associate / JV (IAS 21.48A-C simplified to pct of balance).
* Realized versus unrealized split on a revaluation run: source items
  settled since the run are realized, still-open ones unrealized.

Every expected amount is derived by hand from the inputs stated in the
test (derivation in the comment) and asserted exactly; nothing is read
back from the engine under test to build an expected value.
"""

from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases

from .compat import add_user_groups, user_group_values
from odoo.addons.eh_account_fx_revaluation.models.fx_integrity import (
    FX_MOVE_LINK_CAPABILITY,
)


class CtaPositionCase(EhGoldenTestCase):
    """Shared fixtures for the CTA position suites."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Dispose / post are manager-gated (SoD); the acting user is a
        # manager, SoD itself is proven with a separate operator below.
        add_user_groups(
            cls.env.user, cls.env.ref('eh_account_base.group_eh_manager'),
        )
        cls.cta = cls._ensure_account(
            cls.env, '3600', 'FX Translation Reserve', 'equity')
        cls.instrument = cls._ensure_account(
            cls.env, '1710', 'NIH Instrument', 'asset_current')
        cls.pl_hedge = cls._ensure_account(
            cls.env, '5960', 'Hedge Ineffectiveness', 'expense')
        cls.fx_gain = cls._ensure_account(
            cls.env, '4210', 'FX Gain on Disposal', 'income_other')
        cls.fx_loss = cls._ensure_account(
            cls.env, '5961', 'FX Loss on Disposal', 'expense')
        cls.nci = cls._ensure_account(
            cls.env, '3610', 'Non-controlling Interests', 'equity')

    def _position(self, **overrides):
        vals = {
            'name': 'Net investment in DE subsidiary',
            'cta_account_id': self.cta.id,
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.fx_gain.id,
            'loss_account_id': self.fx_loss.id,
            'foreign_operation_partner_id': self.partner_a.id,
            'operation_type': 'associate_joint_venture',
        }
        vals.update(overrides)
        return self.env['eh.fx.cta.position'].create(vals)

    def _seed_cta(self, position, amount, sign='gain'):
        """Feed the position's reserve with a posted tagged entry.

        gain: Cr CTA amount (credit-positive reserve balance +amount),
        counterleg Dr cash. loss: mirrored. This is the shape a
        consolidation export or an NIH effective portion produces.
        """
        if sign == 'gain':
            lines = [
                (0, 0, {'name': 'seed', 'account_id': self.account_cash.id,
                        'debit': amount, 'credit': 0.0}),
                (0, 0, {'name': 'seed', 'account_id': self.cta.id,
                        'debit': 0.0, 'credit': amount}),
            ]
        else:
            lines = [
                (0, 0, {'name': 'seed', 'account_id': self.cta.id,
                        'debit': amount, 'credit': 0.0}),
                (0, 0, {'name': 'seed', 'account_id': self.account_cash.id,
                        'debit': 0.0, 'credit': amount}),
            ]
        move = self.env['account.move'].with_context(
            eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
        ).create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-30',
            'ref': 'CTA seed',
            'line_ids': lines,
            'eh_cta_position_id': position.id,
        })
        move.action_post()
        position.invalidate_recordset(['balance'])
        return move

    def _nih_hedge(self, position, **overrides):
        vals = {
            'name': '/',
            'hedge_type': 'net_investment',
            'designation_date': '2026-01-01',
            'hedged_item_description': 'Net investment in DE subsidiary',
            'hedging_instrument_description': 'EUR loan',
            'hedged_currency_id': self.env.company.currency_id.id,
            'cta_position_id': position.id,
            'pl_account_id': self.pl_hedge.id,
            'instrument_account_id': self.instrument.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Hedge of the FX exposure of the net investment.',
        }
        vals.update(overrides)
        hedge = self.env['eh.fx.hedge'].create(vals)
        hedge.action_designate()
        # Passing dollar-offset test so the hedge qualifies and the
        # effective portion may be deferred to the reserve. Dated ahead
        # of every movement date used in the suite: posting requires a
        # qualifying test on or before the movement date.
        self.env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'test_date': '2026-06-01',
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1000.0,
            'cumulative_hedged_change': -1000.0,
        })
        self.assertEqual(hedge.state, 'effective')
        return hedge


@tagged('eh_golden', 'eh_account_fx_revaluation', 'post_install',
        '-at_install')
class TestGoldenNihToCta(CtaPositionCase):
    """IFRS 9 6.5.13(a): the effective portion of a net investment
    hedge goes to the foreign currency translation reserve."""

    def test_golden_nih_effective_portion_feeds_position(self):
        """Instrument gain 5,000, fully effective.

        Entry: Dr instrument 5,000 / Cr CTA equity (position account)
        5,000. No P&L leg (ineffective portion 0). The entry is tagged
        to the position, so the ledger-fed balance is exactly 5,000
        (credit-positive reserve).
        """
        position = self._position()
        hedge = self._nih_hedge(position)
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-06-30',
            'total_change': 5000.0,
            'effective_portion': 5000.0,
            'hedged_item_change': -5000.0,
        })
        mvt.action_post()
        self.assertEqual(mvt.state, 'posted')
        self.assertMoveLines(mvt.move_id, [
            (self.instrument, 5000.0, 0.0),
            (self.cta, 0.0, 5000.0),
        ])
        self.assertBalanced(mvt.move_id)
        self.assertEqual(mvt.move_id.eh_cta_position_id, position)
        self.assertAlmostEqual(position.balance, 5000.0, places=2)

    def test_golden_nih_split_effective_ineffective(self):
        """Instrument gain 5,000 of which 4,600 effective.

        Entry: Dr instrument 5,000 / Cr CTA 4,600 / Cr P&L 400
        (ineffective = 5,000 - 4,600 = 400 straight to P&L per IFRS 9
        6.5.13(b)). Position balance = 4,600 only: the reserve never
        absorbs the ineffective portion.
        """
        position = self._position(name='NIH split position')
        hedge = self._nih_hedge(position)
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-06-30',
            'total_change': 5000.0,
            'effective_portion': 4600.0,
            'hedged_item_change': -4600.0,
        })
        mvt.action_post()
        self.assertMoveLines(mvt.move_id, [
            (self.instrument, 5000.0, 0.0),
            (self.cta, 0.0, 4600.0),
            (self.pl_hedge, 0.0, 400.0),
        ])
        self.assertAlmostEqual(position.balance, 4600.0, places=2)

    def test_nih_designation_accepts_position_without_oci_account(self):
        """A position-linked NIH needs no direct OCI account: the
        reserve account comes from the position."""
        position = self._position(name='No-OCI NIH position')
        hedge = self._nih_hedge(position)  # created without oci_account_id
        self.assertFalse(hedge.oci_account_id)
        self.assertEqual(hedge.state, 'effective')

    def test_branch_nih_and_cta_disposal_use_parent_owned_dimensions(self):
        """Branch ledgers may use the root chart/journal without merging GL."""
        Company = self.env['res.company']
        if 'root_id' not in Company._fields:
            self.skipTest("Odoo 16 has no branch/root accounting model")
        root = self.company.root_id
        branch_vals = {
            'name': 'FX NIH branch',
            'parent_id': root.id,
        }
        if 'security_lead' in Company._fields:
            branch_vals['security_lead'] = 0.0
        branch = self._create_accounting_branch(branch_vals)
        self.env.user.write({'company_ids': [(4, branch.id)]})
        branch_env = self.env['eh.fx.cta.position'].with_context(
            allowed_company_ids=[branch.id, root.id],
        ).with_company(branch).env

        position = branch_env['eh.fx.cta.position'].create({
            'name': 'Branch net investment',
            'company_id': branch.id,
            'cta_account_id': self.cta.id,
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.fx_gain.id,
            'loss_account_id': self.fx_loss.id,
        })
        hedge = branch_env['eh.fx.hedge'].create({
            'name': '/',
            'company_id': branch.id,
            'hedge_type': 'net_investment',
            'designation_date': '2026-01-01',
            'hedged_item_description': 'Branch net investment',
            'hedging_instrument_description': 'EUR loan',
            'hedged_currency_id': branch.currency_id.id,
            'cta_position_id': position.id,
            'pl_account_id': self.pl_hedge.id,
            'instrument_account_id': self.instrument.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Documented branch NIH.',
        })
        hedge.action_designate()
        branch_env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'test_date': '2026-06-01',
            'method': 'dollar_offset',
            'cumulative_instrument_change': 500.0,
            'cumulative_hedged_change': -500.0,
        })
        movement = branch_env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-06-30',
            'total_change': 500.0,
            'effective_portion': 500.0,
            'hedged_item_change': -500.0,
        })
        movement.action_post()
        self.assertEqual(movement.move_id.company_id, branch)
        self.assertEqual(movement.move_id.journal_id, self.journal_misc)
        position.invalidate_recordset(['balance'])
        self.assertAlmostEqual(position.balance, 500.0, places=2)

        position.action_dispose()
        self.assertEqual(position.state, 'disposed')
        self.assertEqual(position.disposal_move_id.company_id, branch)
        self.assertEqual(position.disposal_move_id.journal_id, self.journal_misc)

    def test_nih_designation_requires_reserve(self):
        """NIH without an OCI account AND without a position must be
        refused at designation: there is nowhere to park the effective
        portion."""
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'net_investment',
            'hedged_item_description': 'X',
            'hedging_instrument_description': 'Y',
            'hedged_currency_id': self.env.company.currency_id.id,
            'pl_account_id': self.pl_hedge.id,
            'instrument_account_id': self.instrument.id,
            'journal_id': self.journal_misc.id,
            'notes': 'docs',
        })
        with self.assertRaises(ValidationError):
            hedge.action_designate()

    def test_cta_position_rejected_on_non_nih(self):
        """CFH cannot link a CTA position: only NIH effective portions
        accumulate in the translation reserve."""
        position = self._position(name='Wrong-type position')
        with self.assertRaises(ValidationError):
            self.env['eh.fx.hedge'].create({
                'name': '/',
                'hedge_type': 'cash_flow',
                'hedged_item_description': 'X',
                'hedging_instrument_description': 'Y',
                'hedged_currency_id': self.env.company.currency_id.id,
                'cta_position_id': position.id,
                'oci_account_id': self.cta.id,
                'pl_account_id': self.pl_hedge.id,
                'instrument_account_id': self.instrument.id,
                'journal_id': self.journal_misc.id,
                'notes': 'docs',
            })

    def test_nih_movement_reclassify_blocked_when_position_linked(self):
        """IAS 21.48 recycles the FULL reserve on disposal of the
        operation; a per-movement OCI reclass on a position-linked NIH
        would double-count against the position disposal, so it is
        refused and routed to the position's Dispose action."""
        position = self._position(name='Reclass-block position')
        hedge = self._nih_hedge(position)
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-06-30',
            'total_change': 1000.0,
            'effective_portion': 1000.0,
            'hedged_item_change': -1000.0,
        })
        mvt.action_post()
        with self.assertRaises(UserError):
            mvt.action_reclassify_to_pl()
        self.assertEqual(mvt.state, 'posted')

    def test_nih_post_blocked_on_disposed_position(self):
        """No effective portion can be parked in a reserve that was
        already recycled to P&L in full."""
        position = self._position(name='Disposed-target position')
        hedge = self._nih_hedge(position)
        self._seed_cta(position, 1000.0, 'gain')
        position.action_dispose()
        self.assertEqual(position.state, 'disposed')
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-07-31',
            'total_change': 500.0,
            'effective_portion': 500.0,
            'hedged_item_change': -500.0,
        })
        with self.assertRaises(UserError):
            mvt.action_post()
        self.assertEqual(mvt.state, 'draft')


@tagged('eh_golden', 'eh_account_fx_revaluation', 'post_install',
        '-at_install')
class TestGoldenCtaDisposal(CtaPositionCase):
    """IAS 21.48 / 48A-C: reclassification of the cumulative
    translation reserve on (partial) disposal of a foreign operation."""

    def test_disposal_account_types_are_enforced_server_side(self):
        with self.assertRaises(ValidationError):
            self._position(gain_account_id=self.account_equity.id)
        with self.assertRaises(ValidationError):
            self._position(loss_account_id=self.fx_gain.id)

    def _zero_decimal_position_fixture(self):
        currency = self.env['res.currency'].create({
            'name': 'ZDC', 'symbol': 'Z', 'rounding': 1.0, 'active': True,
        })
        company = self.env['res.company'].create({
            'name': 'Zero Decimal CTA Company',
            'currency_id': currency.id,
        })
        self.env.user.write({'company_ids': [(4, company.id)]})
        scoped = self.env['eh.fx.cta.position'].with_context(
            allowed_company_ids=(self.env.company | company).ids,
        ).with_company(company).env
        journal = scoped['account.journal'].create({
            'name': 'Zero Decimal CTA Journal',
            'code': 'ZDCT',
            'type': 'general',
            'company_id': company.id,
        })
        cash = self._ensure_account(
            scoped, '1000', 'Zero Decimal Cash', 'asset_cash',
        )
        cta = self._ensure_account(
            scoped, '3600', 'Zero Decimal CTA', 'equity',
        )
        gain = self._ensure_account(
            scoped, '4210', 'Zero Decimal CTA Gain', 'income_other',
        )
        loss = self._ensure_account(
            scoped, '5961', 'Zero Decimal CTA Loss', 'expense',
        )

        def position(name, pct):
            return scoped['eh.fx.cta.position'].create({
                'name': name,
                'company_id': company.id,
                'cta_account_id': cta.id,
                'journal_id': journal.id,
                'gain_account_id': gain.id,
                'loss_account_id': loss.id,
                'disposal_pct': pct,
                'operation_type': 'associate_joint_venture',
            })

        def seed(record, amount):
            move = scoped['account.move'].with_context(
                eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
            ).create({
                'move_type': 'entry',
                'company_id': company.id,
                'journal_id': journal.id,
                'date': '2026-06-30',
                'line_ids': [
                    (0, 0, {
                        'name': 'zero-decimal seed',
                        'account_id': cash.id,
                        'debit': amount,
                    }),
                    (0, 0, {
                        'name': 'zero-decimal seed',
                        'account_id': cta.id,
                        'credit': amount,
                    }),
                ],
                'eh_cta_position_id': record.id,
            })
            move.action_post()
            record.invalidate_recordset(['balance'])

        return position, seed

    def test_zero_decimal_currency_keeps_99_6_pct_partial(self):
        position_factory, seed = self._zero_decimal_position_fixture()
        position = position_factory('Zero-decimal 99.6 percent', 99.6)
        seed(position, 1000.0)
        position.action_dispose()
        self.assertEqual(position.state, 'open')
        position.invalidate_recordset(['balance'])
        self.assertEqual(position.balance, 4.0)

    def test_partial_disposal_that_rounds_to_zero_is_refused(self):
        position_factory, seed = self._zero_decimal_position_fixture()
        position = position_factory('Zero-rounded partial disposal', 1.0)
        seed(position, 1.0)
        with self.assertRaises(UserError):
            position.action_dispose()
        self.assertEqual(position.state, 'open')

    def test_golden_full_disposal_reclass(self):
        """Accumulated CTA credit balance 12,000 (net gain). Full
        disposal reclassifies the whole reserve:

        Dr CTA equity 12,000 / Cr FX gain P&L 12,000.

        The disposal entry is itself tagged to the position, so the
        ledger-fed balance drops to 12,000 - 12,000 = 0. The position
        is disposed and frozen; a second dispose is refused.
        """
        position = self._position(name='Full disposal position')
        self._seed_cta(position, 12000.0, 'gain')
        self.assertAlmostEqual(position.balance, 12000.0, places=2)
        position.action_dispose()
        self.assertEqual(position.state, 'disposed')
        self.assertTrue(position.disposal_move_id)
        self.assertMoveLines(position.disposal_move_id, [
            (self.cta, 12000.0, 0.0),
            (self.fx_gain, 0.0, 12000.0),
        ])
        self.assertBalanced(position.disposal_move_id)
        self.assertEqual(
            position.disposal_move_id.eh_cta_position_id, position)
        self.assertAlmostEqual(position.balance, 0.0, places=2)
        # Frozen after: second dispose and identity edits are refused.
        with self.assertRaises(UserError):
            position.action_dispose()
        with self.assertRaises(UserError):
            position.write({'cta_account_id': self.account_equity.id})
        with self.assertRaises(UserError):
            position.unlink()

    def test_golden_partial_then_full_disposal(self):
        """Accumulated balance 12,000; partial disposal of 30%.

        Reclass amount = 12,000 x 30% = 3,600:
        Dr CTA 3,600 / Cr FX gain 3,600.
        Remaining balance = 12,000 - 3,600 = 8,400; position stays
        open (IAS 21.48A-C, retained interest). The subsequent full
        disposal reclassifies the remaining 8,400 and closes it.
        """
        position = self._position(name='Partial disposal position')
        self._seed_cta(position, 12000.0, 'gain')
        position.disposal_pct = 30.0
        position.action_dispose()
        self.assertEqual(position.state, 'open')
        partial_move = self.env['account.move'].search([
            ('eh_cta_position_id', '=', position.id),
            ('ref', 'like', 'CTA disposal%'),
        ], order='id desc', limit=1)
        self.assertMoveLines(partial_move, [
            (self.cta, 3600.0, 0.0),
            (self.fx_gain, 0.0, 3600.0),
        ])
        self.assertAlmostEqual(position.balance, 8400.0, places=2)
        # Full disposal of the retained interest.
        position.disposal_pct = 100.0
        position.action_dispose()
        self.assertEqual(position.state, 'disposed')
        self.assertMoveLines(position.disposal_move_id, [
            (self.cta, 8400.0, 0.0),
            (self.fx_gain, 0.0, 8400.0),
        ])
        self.assertAlmostEqual(position.balance, 0.0, places=2)

    def test_subsidiary_partial_disposal_reattributes_cta_to_nci(self):
        """IAS 21.48B keeps the proportionate CTA within equity."""
        position = self._position(
            name='Subsidiary partial disposal retaining control',
            operation_type='subsidiary',
            nci_account_id=self.nci.id,
            disposal_pct=30.0,
            disposal_date='2026-07-15',
        )
        self._seed_cta(position, 12000.0, 'gain')

        position.action_dispose()

        move = self.env['account.move'].search([
            ('eh_cta_position_id', '=', position.id),
            ('ref', 'like', 'CTA disposal%'),
        ], order='id desc', limit=1)
        self.assertEqual(str(move.date), '2026-07-15')
        self.assertMoveLines(move, [
            (self.cta, 3600.0, 0.0),
            (self.nci, 0.0, 3600.0),
        ])
        self.assertFalse(move.line_ids.filtered(
            lambda line: line.account_id in (self.fx_gain | self.fx_loss),
        ))
        self.assertEqual(position.state, 'open')
        position.invalidate_recordset(['balance'])
        self.assertAlmostEqual(position.balance, 8400.0, places=2)

    def test_posted_reserve_freezes_open_position_identity(self):
        position = self._position(name='Open evidenced position')
        self._seed_cta(position, 1000.0, 'gain')
        alternate_cta = self._ensure_account(
            self.env, '3601', 'Alternate CTA Reserve', 'equity',
        )
        for vals in (
            {'name': 'Rehomed reserve'},
            {'cta_account_id': alternate_cta.id},
            {'foreign_operation_partner_id': self.partner_b.id},
            {'operation_currency_id': self.env.ref('base.EUR').id},
        ):
            with self.subTest(vals=vals), self.assertRaises(UserError):
                position.write(vals)
        # Future-event configuration remains editable while position stays
        # open; identity and existing ledger history do not.
        position.write({'disposal_pct': 25.0})
        self.assertEqual(position.disposal_pct, 25.0)

    def test_golden_loss_side_disposal(self):
        """Accumulated CTA debit balance 7,000 (net translation loss,
        ledger balance -7,000 credit-positive). Disposal recognises
        the loss in P&L:

        Dr FX loss P&L 7,000 / Cr CTA equity 7,000.
        """
        position = self._position(name='Loss disposal position')
        self._seed_cta(position, 7000.0, 'loss')
        self.assertAlmostEqual(position.balance, -7000.0, places=2)
        position.action_dispose()
        self.assertEqual(position.state, 'disposed')
        self.assertMoveLines(position.disposal_move_id, [
            (self.fx_loss, 7000.0, 0.0),
            (self.cta, 0.0, 7000.0),
        ])
        self.assertAlmostEqual(position.balance, 0.0, places=2)

    def test_golden_nih_portion_recycled_with_position(self):
        """The disposal reclassifies the FULL balance including NIH
        effective portions parked in the reserve.

        Seed 12,000 (consolidation export) + NIH effective portion
        5,000 = reserve 17,000. Disposal: Dr CTA 17,000 / Cr FX gain
        17,000.
        """
        position = self._position(name='Mixed-source position')
        self._seed_cta(position, 12000.0, 'gain')
        hedge = self._nih_hedge(position)
        mvt = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-06-30',
            'total_change': 5000.0,
            'effective_portion': 5000.0,
            'hedged_item_change': -5000.0,
        })
        mvt.action_post()
        self.assertAlmostEqual(position.balance, 17000.0, places=2)
        position.action_dispose()
        self.assertMoveLines(position.disposal_move_id, [
            (self.cta, 17000.0, 0.0),
            (self.fx_gain, 0.0, 17000.0),
        ])
        self.assertAlmostEqual(position.balance, 0.0, places=2)

    def test_dispose_blocked_on_zero_balance(self):
        position = self._position(name='Zero balance position')
        with self.assertRaises(UserError):
            position.action_dispose()

    def test_disposal_pct_bounds_enforced(self):
        position = self._position(name='Pct bounds position')
        for invalid in (
            0.0, 120.0, float('nan'), float('inf'), float('-inf'),
        ):
            with self.assertRaises(ValidationError):
                position.write({'disposal_pct': invalid})

    def test_disposal_is_atomic_on_late_chatter_failure(self):
        position = self._position(name='Atomic late failure position')
        self._seed_cta(position, 1200.0, 'gain')
        moves_before = self.env['account.move'].search([
            ('eh_cta_position_id', '=', position.id),
        ])
        with patch.object(
            type(position), 'message_post',
            side_effect=UserError('Injected late CTA chatter failure'),
        ):
            try:
                position.action_dispose()
            except UserError:
                pass
            else:
                self.fail("Injected CTA chatter failure was not raised")
        position.invalidate_recordset()
        self.assertEqual(position.state, 'open')
        self.assertFalse(position.disposal_move_id)
        self.assertEqual(
            self.env['account.move'].search([
                ('eh_cta_position_id', '=', position.id),
            ]),
            moves_before,
        )

    def test_disposal_recordset_is_atomic_on_second_position_failure(self):
        first = self._position(name='Atomic first position')
        second = self._position(name='Atomic zero second position')
        self._seed_cta(first, 900.0, 'gain')
        first_moves_before = self.env['account.move'].search([
            ('eh_cta_position_id', '=', first.id),
        ])
        try:
            (first | second).action_dispose()
        except UserError:
            pass
        else:
            self.fail("Second-position disposal failure was not raised")
        first.invalidate_recordset()
        self.assertEqual(first.state, 'open')
        self.assertFalse(first.disposal_move_id)
        self.assertEqual(
            self.env['account.move'].search([
                ('eh_cta_position_id', '=', first.id),
            ]),
            first_moves_before,
        )

    def test_cta_account_must_be_equity(self):
        with self.assertRaises(ValidationError):
            self._position(
                name='Non-equity position',
                cta_account_id=self.account_cash.id)

    def test_position_cannot_be_created_disposed(self):
        with self.assertRaises(UserError):
            self._position(name='Born disposed', state='disposed')

    def test_position_with_tagged_moves_cannot_be_deleted(self):
        position = self._position(name='Anchored position')
        self._seed_cta(position, 100.0, 'gain')
        with self.assertRaises(UserError):
            position.unlink()

    def test_tagged_draft_freezes_identity_and_cannot_feed_disposed_reserve(self):
        position = self._position(name='Tagged draft position')
        draft = self.env['account.move'].with_context(
            eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
        )._eh_create_sealed({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-06-30',
            'ref': 'CTA tagged sealed draft',
            'line_ids': [
                (0, 0, {
                    'name': 'draft seed',
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'name': 'draft seed',
                    'account_id': self.cta.id,
                    'credit': 100.0,
                }),
            ],
            'eh_cta_position_id': position.id,
        })
        self.assertEqual(draft.state, 'draft')
        self.assertTrue(draft.eh_sealed)
        with self.assertRaises(UserError):
            position.write({'name': 'Rewritten after tagged draft'})

        self._seed_cta(position, 1000.0, 'gain')
        position.action_dispose()
        with self.assertRaises(UserError):
            draft.action_post()
        self.assertEqual(draft.state, 'draft')

    def test_non_manager_cannot_dispose(self):
        """SoD: disposal recycles equity into P&L and is manager-only.
        The CSV grants group_eh_user write, so the gate must live in
        the action."""
        position = self._position(name='SoD position')
        self._seed_cta(position, 1000.0, 'gain')
        operator = self.env['res.users'].create({
            'name': 'CTA Operator',
            'login': 'cta_operator_sod',
            'email': 'cta_operator_sod@example.com',
            **user_group_values(
                self.env, self.env.ref('eh_account_base.group_eh_user'),
            ),
        })
        with self.assertRaises(UserError):
            position.with_user(operator).action_dispose()
        self.assertEqual(position.state, 'open')

    def test_plain_user_cannot_raw_reset_state(self):
        """Reopening a disposed position by a raw state write would
        lift the freeze on a recycled reserve; it is manager-gated."""
        position = self._position(name='Raw reset position')
        self._seed_cta(position, 1000.0, 'gain')
        position.action_dispose()
        operator = self.env['res.users'].create({
            'name': 'CTA Operator 2',
            'login': 'cta_operator_reset',
            'email': 'cta_operator_reset@example.com',
            **user_group_values(
                self.env, self.env.ref('eh_account_base.group_eh_user'),
            ),
        })
        with self.assertRaises(UserError):
            position.with_user(operator).write({'state': 'open'})
        self.assertEqual(position.state, 'disposed')


@tagged('eh_golden', 'eh_account_fx_revaluation', 'post_install',
        '-at_install')
class TestCtaDisposalPairwise(CtaPositionCase):
    """Pairwise sweep over the disposal axes: reserve sign x disposal
    pct x amount. Invariants per case:

    * reclass amount == round(amount x pct / 100, 2dp), booked
      Dr CTA / Cr gain for a gain reserve and Dr loss / Cr CTA for a
      loss reserve;
    * remaining ledger balance == signed(amount - reclass);
    * the position is disposed iff pct == 100, and a disposed
      position refuses a second dispose.
    """

    AXES = {
        'sign': ['gain', 'loss'],
        'pct': [25.0, 60.0, 100.0],
        'amount': [1234.56, 10000.0],
    }

    def test_pairwise_disposal_matrix(self):
        for idx, case in enumerate(pairwise_cases(self.AXES)):
            with self.subTest(case=case):
                position = self._position(name='PW position %d' % idx)
                self._seed_cta(position, case['amount'], case['sign'])
                position.disposal_pct = case['pct']
                position.action_dispose()
                expected = round(case['amount'] * case['pct'] / 100.0, 2)
                move = self.env['account.move'].search([
                    ('eh_cta_position_id', '=', position.id),
                    ('ref', 'like', 'CTA disposal%'),
                ], order='id desc', limit=1)
                if case['sign'] == 'gain':
                    self.assertMoveLines(move, [
                        (self.cta, expected, 0.0),
                        (self.fx_gain, 0.0, expected),
                    ])
                else:
                    self.assertMoveLines(move, [
                        (self.fx_loss, expected, 0.0),
                        (self.cta, 0.0, expected),
                    ])
                remaining = case['amount'] - expected
                signed_remaining = (
                    remaining if case['sign'] == 'gain' else -remaining)
                position.invalidate_recordset(['balance'])
                self.assertAlmostEqual(
                    position.balance, signed_remaining, places=2)
                if case['pct'] == 100.0:
                    self.assertEqual(position.state, 'disposed')
                    with self.assertRaises(UserError):
                        position.action_dispose()
                else:
                    self.assertEqual(position.state, 'open')


@tagged('eh_golden', 'eh_account_fx_revaluation', 'post_install',
        '-at_install')
class TestGoldenRealizedUnrealizedSplit(EhGoldenTestCase):
    """Run-level realized vs unrealized split.

    Two EUR 1,000 receivables booked at 1 EUR = 1.00 USD (company
    currency USD). Closing rate at the revaluation date 1 EUR = 1.10
    USD, so each line restates 1,000 -> 1,100: adjustment +100 per
    line, run net +200.

    Receivable B is settled (fully reconciled) AFTER the revaluation
    date, receivable A stays open. The run therefore still revalues
    both (point-in-time residual), but the split reads live
    reconciliation state: realized = +100 (B), unrealized = +100 (A),
    and realized + unrealized == net_adjustment.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_user_groups(
            cls.env.user, cls.env.ref('eh_account_base.group_eh_manager'),
        )
        cls.eur = cls.env.ref('base.EUR')
        # Deterministic rate table: clear any pre-existing EUR rates so
        # the closing-rate lookup can only hit the pinned fixtures.
        cls.env['res.currency.rate'].search([
            ('currency_id', '=', cls.eur.id),
        ]).unlink()
        # Rates are stored as units of EUR per 1 USD (Odoo convention).
        # 1 EUR = 1.00 USD at booking; 1 EUR = 1.10 USD at close.
        cls._set_rate(cls.eur, '2026-01-15', 1.0)
        cls._set_rate(cls.eur, '2026-03-31', 1.0 / 1.1)
        cls.account_receivable.eh_fx_revalue = True
        cls.unreal_gain = cls._ensure_account(
            cls.env, '4220', 'Unrealised FX Gain', 'income_other')
        cls.unreal_loss = cls._ensure_account(
            cls.env, '5962', 'Unrealised FX Loss', 'expense')

    def _receivable(self, partner, date):
        """EUR 1,000 receivable booked at 1 EUR = 1.00 USD."""
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': date,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR receivable',
                    'account_id': self.account_receivable.id,
                    'partner_id': partner.id,
                    'currency_id': self.eur.id,
                    'amount_currency': 1000.0,
                    'debit': 1000.0,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'revenue',
                    'account_id': self.account_revenue.id,
                    'debit': 0.0,
                    'credit': 1000.0,
                }),
            ],
        })
        move.action_post()
        return move.line_ids.filtered(
            lambda l: l.account_id == self.account_receivable)

    def _settle(self, receivable_line, date):
        """Settle the receivable in full at the SAME company amount so
        reconciliation closes both residuals exactly (no exchange
        difference entry needed for the fixture)."""
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': date,
            'line_ids': [
                (0, 0, {
                    'name': 'settlement',
                    'account_id': self.account_cash.id,
                    'debit': 1000.0,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'settlement',
                    'account_id': self.account_receivable.id,
                    'partner_id': receivable_line.partner_id.id,
                    'currency_id': self.eur.id,
                    'amount_currency': -1000.0,
                    'debit': 0.0,
                    'credit': 1000.0,
                }),
            ],
        })
        move.action_post()
        pay_line = move.line_ids.filtered(
            lambda l: l.account_id == self.account_receivable)
        (receivable_line + pay_line).reconcile()
        self.assertTrue(receivable_line.reconciled)

    def _run(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'name': '/',
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.unreal_gain.id,
            'loss_account_id': self.unreal_loss.id,
            'auto_reverse': False,
            'aggregate_by_partner': True,
        })
        run.action_compute()
        return run

    def test_golden_split_one_open_one_settled(self):
        line_a = self._receivable(self.partner_a, '2026-01-15')
        line_b = self._receivable(self.partner_b, '2026-01-15')
        # B settles after the revaluation date; A stays open.
        self._settle(line_b, '2026-04-10')
        run = self._run()
        self.assertEqual(run.line_count, 2)
        reval_a = run.line_ids.filtered(
            lambda l: l.partner_id == self.partner_a)
        reval_b = run.line_ids.filtered(
            lambda l: l.partner_id == self.partner_b)
        # Both were open AS OF 2026-03-31, so both revalue:
        # 1,000 EUR x 1.10 = 1,100 USD restated, adjustment +100 each.
        self.assertAlmostEqual(reval_a.adjustment, 100.0, places=2)
        self.assertAlmostEqual(reval_b.adjustment, 100.0, places=2)
        # Split off live reconciliation state.
        self.assertFalse(reval_a.is_realized)
        self.assertTrue(reval_b.is_realized)
        self.assertAlmostEqual(run.realized_gain_loss, 100.0, places=2)
        self.assertAlmostEqual(run.unrealized_gain_loss, 100.0, places=2)
        self.assertAlmostEqual(run.net_adjustment, 200.0, places=2)
        self.assertAlmostEqual(
            run.realized_gain_loss + run.unrealized_gain_loss,
            run.net_adjustment, places=2)
        # The posted entry books both account legs and one gain leg:
        # Dr 1100 (A) 100 / Dr 1100 (B) 100 / Cr unrealised gain 200.
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.account_receivable, 100.0, 0.0),
            (self.account_receivable, 100.0, 0.0),
            (self.unreal_gain, 0.0, 200.0),
        ])
        self.assertBalanced(run.move_id)

    def test_golden_split_all_open(self):
        """With nothing settled the whole net adjustment is
        unrealized: realized 0, unrealized +100."""
        self._receivable(self.partner_a, '2026-01-15')
        run = self._run()
        self.assertEqual(run.line_count, 1)
        self.assertAlmostEqual(run.realized_gain_loss, 0.0, places=2)
        self.assertAlmostEqual(run.unrealized_gain_loss, 100.0, places=2)

    def test_realized_filter_search(self):
        """The is_realized search materialises the compute so the list
        filter works."""
        line_a = self._receivable(self.partner_a, '2026-01-15')
        line_b = self._receivable(self.partner_b, '2026-01-15')
        self._settle(line_b, '2026-04-10')
        run = self._run()
        realized = self.env['eh.fx.revaluation.line'].search([
            ('run_id', '=', run.id), ('is_realized', '=', True)])
        unrealized = self.env['eh.fx.revaluation.line'].search([
            ('run_id', '=', run.id), ('is_realized', '=', False)])
        self.assertEqual(realized.partner_id, self.partner_b)
        self.assertEqual(unrealized.partner_id, self.partner_a)
        # line_a/line_b referenced to keep the fixture explicit.
        self.assertIn(line_a, unrealized.source_move_line_ids)
        self.assertIn(line_b, realized.source_move_line_ids)
