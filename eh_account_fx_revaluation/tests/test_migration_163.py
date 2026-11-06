# -*- encoding: utf-8 -*-
"""Fail-closed upgrade checks for FX journal provenance backfill."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase
from odoo.addons.eh_account_fx_revaluation.models.fx_integrity import (
    FX_MOVE_LINK_CAPABILITY,
)
from odoo.addons.eh_account_fx_revaluation.migration_utils import (
    diagnose_fx_provenance,
)

from .compat import add_user_groups


@tagged('eh_account_fx_revaluation', 'post_install', '-at_install')
class TestFxMigration163(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_user_groups(
            cls.env.user, cls.env.ref('eh_account_base.group_eh_manager'),
        )
        cls.gain = cls._ensure_account(
            cls.env, '4928', 'Migration FX Gain', 'income_other',
        )
        cls.loss = cls._ensure_account(
            cls.env, '5938', 'Migration FX Loss', 'expense',
        )
        cls.cta = cls._ensure_account(
            cls.env, '3938', 'Migration CTA', 'equity',
        )
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.6.3' / 'post-migration.py'
        )
        spec = spec_from_file_location('eh_fx_post_migration_163', path)
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _posted_move(self, ref):
        move = self.env['account.move'].with_context(
            eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
        )._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.journal_misc.id,
            'date': '2026-03-31',
            'ref': ref,
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {
                    'name': ref,
                    'account_id': self.account_cash.id,
                    'debit': 1.0,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': ref,
                    'account_id': self.account_equity.id,
                    'debit': 0.0,
                    'credit': 1.0,
                }),
            ],
        })
        move.action_post()
        return move

    def _legacy_sql(self, query, params):
        """Flush ORM work before simulating a legacy row-level corruption."""
        self.env.flush_all()
        self.env.cr.execute(query, params)
        self.env.invalidate_all()

    def _migrate(self):
        """Mirror a real upgrade boundary, where preceding ORM work is durable."""
        self.env.flush_all()
        self.migration.migrate(self.env.cr, '19.0.1.6.2')
        self.env.invalidate_all()

    def _foreign_exposure_move(self, foreign_currency, ref):
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.journal_misc.id,
            'date': '2026-03-30',
            'ref': ref,
            'line_ids': [
                (0, 0, {
                    'name': 'foreign exposure',
                    'account_id': self.account_cash.id,
                    'currency_id': foreign_currency.id,
                    'amount_currency': 10.0,
                    'debit': 9.0,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'foreign exposure offset',
                    'account_id': self.account_equity.id,
                    'debit': 0.0,
                    'credit': 9.0,
                }),
            ],
        })
        move.action_post()
        source_line = move.line_ids.filtered(
            lambda line: line.account_id == self.account_cash
        )
        self.assertEqual(len(source_line), 1)
        return move, source_line

    def _legacy_run_source(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
            'auto_reverse': False,
        })
        foreign_currency = self.env['res.currency'].with_context(
            active_test=False,
        ).search([
            ('id', '!=', self.company.currency_id.id),
        ], limit=1)
        self.assertTrue(foreign_currency)
        _source_move, source_line = self._foreign_exposure_move(
            foreign_currency, 'Legacy revaluation exposure',
        )
        self.env['eh.fx.revaluation.line'].sudo().create({
            'run_id': run.id,
            'account_id': self.account_cash.id,
            'foreign_currency_id': foreign_currency.id,
            'balance_company': 9.0,
            'balance_foreign': 10.0,
            'closing_rate': 1.0,
            'new_balance_company': 10.0,
            'adjustment': 1.0,
            'nature': 'gain',
            'source_line_count': 1,
            'source_move_line_ids': [(6, 0, source_line.ids)],
        })
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.journal_misc.id,
            'date': '2026-03-31',
            'ref': 'Legacy run source',
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {
                    'name': 'source', 'account_id': self.account_cash.id,
                    'debit': 1.0, 'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'source', 'account_id': self.gain.id,
                    'debit': 0.0, 'credit': 1.0,
                }),
            ],
        })
        move.action_post()
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'posted', "
            "move_id = %s WHERE id = %s",
            [move.id, run.id],
        )
        return run, move

    def _legacy_effective_movement(self, suffix):
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'designation_date': '2026-01-01',
            'hedged_item_description': 'Migration hedge %s' % suffix,
            'hedging_instrument_description': 'Migration instrument %s'
                                                  % suffix,
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'oci_account_id': self.cta.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Effectiveness migration fixture',
        })
        hedge.action_designate()
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'test_date': '2026-03-01',
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1.0,
            'cumulative_hedged_change': -1.0,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1.0,
            'effective_portion': 1.0,
            'hedged_item_change': -1.0,
        })
        movement.action_post()
        return hedge, test, movement

    def _cta_reserve_move(self, position, amount):
        move = self.env['account.move'].with_context(
            eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
        )._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.journal_misc.id,
            'date': '2026-03-30',
            'ref': 'Legacy CTA reserve',
            'eh_sealed': True,
            'eh_cta_position_id': position.id,
            'line_ids': [
                (0, 0, {
                    'name': 'reserve',
                    'account_id': self.account_cash.id,
                    'debit': amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'reserve',
                    'account_id': position.cta_account_id.id,
                    'debit': 0.0,
                    'credit': amount,
                }),
            ],
        })
        move.action_post()
        return move

    def _cta_disposal_move(self, position, amount, *, reverse=False):
        debit_account = position.loss_account_id if reverse \
            else position.cta_account_id
        credit_account = position.cta_account_id if reverse \
            else position.gain_account_id
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.journal_misc.id,
            'date': '2026-03-31',
            'ref': 'Legacy CTA disposal',
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {
                    'name': 'disposal', 'account_id': debit_account.id,
                    'debit': amount, 'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'disposal', 'account_id': credit_account.id,
                    'debit': 0.0, 'credit': amount,
                }),
            ],
        })
        move.action_post()
        return move

    def test_diagnostic_clean_run_is_read_only(self):
        result = diagnose_fx_provenance(self.env.cr)
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['error'])
        self.assertIn('No remediation required', result['remediation'])

    def test_diagnostic_failure_rolls_back_backfill_and_guides_remediation(self):
        run, move = self._legacy_run_source()
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'designation_date': '2026-01-01',
            'hedged_item_description': 'Diagnostic corrupt hedge',
            'hedging_instrument_description': 'Diagnostic instrument',
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'oci_account_id': self.cta.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Diagnostic corruption fixture',
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 0.0,
            'effective_portion': 0.0,
        })
        self._legacy_sql(
            "UPDATE eh_fx_hedge_movement SET state = 'posted' WHERE id = %s",
            (movement.id,),
        )
        self.assertFalse(move.eh_fx_revaluation_run_id)

        result = diagnose_fx_provenance(self.env.cr)

        self.assertFalse(result['ok'])
        self.assertIn('no original journal entry', result['error'])
        self.assertIn('rerun diagnose_fx_provenance', result['remediation'])
        move.invalidate_recordset(['eh_fx_revaluation_run_id'])
        self.assertFalse(
            move.eh_fx_revaluation_run_id,
            'dry-run backfill must roll back after a later validation error',
        )
        self.assertEqual(run.state, 'posted')

    def test_disposed_cta_requires_explicit_move_provenance(self):
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Corrupt migration CTA',
            'cta_account_id': self.account_equity.id,
        })
        move = self._posted_move('Corrupt CTA disposal')
        self._legacy_sql(
            "UPDATE eh_fx_cta_position SET state = 'disposed', "
            "disposal_move_id = %s WHERE id = %s",
            [move.id, position.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_revaluation_source_and_reversal_cannot_share_move(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
        })
        move = self._posted_move('Corrupt FX source and reversal')
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'reversed', "
            "move_id = %s, reversal_move_id = %s WHERE id = %s",
            [move.id, move.id, run.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_computed_run_cannot_carry_posted_move_link(self):
        run, move = self._legacy_run_source()
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'computed', "
            "move_id = %s WHERE id = %s",
            [move.id, run.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_posted_run_cannot_carry_reversal_link(self):
        run, source = self._legacy_run_source()
        reversal = source._eh_reverse_with_verified_capability([{
            'date': '2026-04-01',
            'journal_id': self.journal_misc.id,
            'ref': 'Legacy premature reversal',
        }], cancel=False)
        reversal._eh_post_verified_reversal()
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'posted', "
            "move_id = %s, reversal_move_id = %s WHERE id = %s",
            [source.id, reversal.id, run.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_extra_move_cannot_claim_valid_revaluation_source(self):
        run, source = self._legacy_run_source()
        unrelated = self._posted_move('Forged duplicate FX backlink')
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'posted', move_id = %s "
            "WHERE id = %s",
            [source.id, run.id],
        )
        self._legacy_sql(
            "UPDATE account_move SET eh_fx_revaluation_run_id = %s "
            "WHERE id = %s",
            [run.id, unrelated.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_revaluation_wrong_signed_amount_cannot_be_backfilled(self):
        _run, move = self._legacy_run_source()
        self._legacy_sql(
            "UPDATE account_move_line SET debit = debit * 999.0, "
            "credit = credit * 999.0, balance = balance * 999.0 "
            "WHERE move_id = %s",
            [move.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_revaluation_balance_projection_drift_cannot_be_backfilled(self):
        _run, move = self._legacy_run_source()
        self._legacy_sql(
            "UPDATE account_move_line SET balance = balance + 7.0, "
            "amount_currency = amount_currency + 7.0 "
            "WHERE move_id = %s",
            [move.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_foreign_currency_engine_leg_cannot_be_backfilled(self):
        _run, move = self._legacy_run_source()
        foreign_currency = self.env['res.currency'].with_context(
            active_test=False,
        ).search([
            ('id', '!=', self.company.currency_id.id),
        ], limit=1)
        self.assertTrue(foreign_currency)
        self._legacy_sql(
            "UPDATE account_move_line SET currency_id = %s "
            "WHERE move_id = %s",
            [foreign_currency.id, move.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_revaluation_support_arithmetic_drift_cannot_be_backfilled(self):
        run, _move = self._legacy_run_source()
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_line "
            "SET balance_company = balance_company + 3.0 WHERE run_id = %s",
            [run.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_revaluation_support_without_exact_sources_cannot_be_backfilled(self):
        run, _move = self._legacy_run_source()
        self._legacy_sql(
            "DELETE FROM eh_fx_reval_line_source_rel "
            "WHERE reval_line_id IN ("
            "SELECT id FROM eh_fx_revaluation_line WHERE run_id = %s)",
            [run.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_self_consistent_support_must_match_source_residuals(self):
        run, _move = self._legacy_run_source()
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_line SET balance_company = 12.0, "
            "balance_foreign = 13.0, new_balance_company = 13.0, "
            "adjustment = 1.0 WHERE run_id = %s",
            [run.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_one_source_line_cannot_feed_two_support_buckets(self):
        run, _move = self._legacy_run_source()
        support = run.line_ids
        self.assertEqual(len(support), 1)
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'draft' WHERE id = %s",
            [run.id],
        )
        self.env['eh.fx.revaluation.line'].sudo().create({
            'run_id': run.id,
            'account_id': support.account_id.id,
            'partner_id': support.partner_id.id,
            'foreign_currency_id': support.foreign_currency_id.id,
            'balance_company': 9.0,
            'balance_foreign': 10.0,
            'closing_rate': 0.9,
            'new_balance_company': 9.0,
            'adjustment': 0.0,
            'nature': 'flat',
            'source_line_count': 1,
            'source_move_line_ids': [
                (6, 0, support.source_move_line_ids.ids),
            ],
        })
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'posted' WHERE id = %s",
            [run.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_distinct_sources_cannot_split_one_runtime_bucket(self):
        run, _move = self._legacy_run_source()
        support = run.line_ids
        _second_move, second_source = self._foreign_exposure_move(
            support.foreign_currency_id, 'Second legacy exposure',
        )
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'draft' WHERE id = %s",
            [run.id],
        )
        self.env['eh.fx.revaluation.line'].sudo().create({
            'run_id': run.id,
            'account_id': support.account_id.id,
            'partner_id': support.partner_id.id,
            'foreign_currency_id': support.foreign_currency_id.id,
            'balance_company': 9.0,
            'balance_foreign': 10.0,
            'closing_rate': 1.0,
            'new_balance_company': 10.0,
            'adjustment': 1.0,
            'nature': 'gain',
            'source_line_count': 1,
            'source_move_line_ids': [(6, 0, second_source.ids)],
        })
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'posted' WHERE id = %s",
            [run.id],
        )
        with self.assertRaisesRegex(UserError, 'duplicate support buckets'):
            self._migrate()

    def test_non_partner_aggregated_source_partners_are_valid(self):
        run, move = self._legacy_run_source()
        partner = self.env['res.partner'].create({
            'name': 'Legacy source-only partner',
        })
        source_ids = run.line_ids.source_move_line_ids.ids
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET aggregate_by_partner = false "
            "WHERE id = %s",
            [run.id],
        )
        self._legacy_sql(
            "UPDATE account_move_line SET partner_id = %s WHERE id IN %s",
            [partner.id, tuple(source_ids)],
        )
        self._migrate()
        move.invalidate_recordset(['eh_fx_revaluation_run_id'])
        self.assertEqual(move.eh_fx_revaluation_run_id, run)

    def test_source_line_projection_drift_cannot_be_backfilled(self):
        run, _move = self._legacy_run_source()
        source_ids = run.line_ids.source_move_line_ids.ids
        self._legacy_sql(
            "UPDATE account_move_line SET date = '2026-04-01' "
            "WHERE id IN %s",
            [tuple(source_ids)],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_revaluation_wrong_support_partner_cannot_be_backfilled(self):
        run, _move = self._legacy_run_source()
        partner = self.env['res.partner'].create({
            'name': 'Wrong migration support partner',
        })
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_line SET partner_id = %s "
            "WHERE run_id = %s",
            [partner.id, run.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_hedge_source_and_reclass_cannot_share_move(self):
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'Migration hedge item',
            'hedging_instrument_description': 'Migration instrument',
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'oci_account_id': self.account_equity.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Migration corruption fixture',
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1.0,
            'effective_portion': 0.0,
        })
        move = self._posted_move('Corrupt hedge source and reclass')
        self._legacy_sql(
            "UPDATE eh_fx_hedge_movement SET state = 'reclassified', "
            "move_id = %s, reclassification_move_id = %s WHERE id = %s",
            [move.id, move.id, movement.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_effective_movement_without_qualifying_test_is_rejected(self):
        _hedge, test, _movement = self._legacy_effective_movement(
            'missing test',
        )
        self._legacy_sql(
            "DELETE FROM eh_fx_hedge_test WHERE id = %s",
            [test.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_forged_effectiveness_result_is_rejected(self):
        _hedge, test, _movement = self._legacy_effective_movement(
            'forged test',
        )
        self._legacy_sql(
            "UPDATE eh_fx_hedge_test SET "
            "cumulative_instrument_change = 2.0, offset_ratio = 2.0, "
            "is_effective = true WHERE id = %s",
            [test.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_invalid_hedge_threshold_without_tests_is_rejected(self):
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'hedged_item_description': 'Invalid threshold item',
            'hedging_instrument_description': 'Invalid threshold instrument',
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'oci_account_id': self.cta.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Invalid threshold migration fixture',
        })
        self._legacy_sql(
            "UPDATE eh_fx_hedge SET rsquared_threshold = -0.1 "
            "WHERE id = %s",
            [hedge.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_effective_movement_after_hedge_closure_is_rejected(self):
        _hedge, _test, movement = self._legacy_effective_movement(
            'closed before movement',
        )
        self._legacy_sql(
            "UPDATE eh_fx_hedge SET state = 'terminated', "
            "termination_date = %s WHERE id = %s",
            [movement.movement_date, movement.hedge_id.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_historical_effective_movement_before_closure_is_backfilled(self):
        _hedge, _test, movement = self._legacy_effective_movement(
            'closed after movement',
        )
        self._legacy_sql(
            "UPDATE eh_fx_hedge SET state = 'terminated', "
            "termination_date = %s WHERE id = %s",
            ['2026-04-01', movement.hedge_id.id],
        )
        self._migrate()
        movement.move_id.invalidate_recordset([
            'eh_fx_hedge_movement_id',
        ])
        self.assertEqual(
            movement.move_id.eh_fx_hedge_movement_id, movement,
        )

    def test_unrelated_move_cannot_replace_hedge_reclassification(self):
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'designation_date': '2026-01-01',
            'hedged_item_description': 'Migration hedge item',
            'hedging_instrument_description': 'Migration instrument',
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'oci_account_id': self.cta.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Migration reclassification fixture',
        })
        hedge.action_designate()
        self.env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'test_date': '2026-03-01',
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1.0,
            'cumulative_hedged_change': -1.0,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1.0,
            'effective_portion': 1.0,
            'hedged_item_change': -1.0,
        })
        movement.action_post()
        movement.action_reclassify_to_pl()
        unrelated = self._posted_move('Unrelated hedge reclassification')
        self._legacy_sql(
            "UPDATE eh_fx_hedge_movement SET reclassification_move_id = %s "
            "WHERE id = %s",
            [unrelated.id, movement.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_sign_reversed_hedge_move_cannot_be_backfilled(self):
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'designation_date': '2026-01-01',
            'hedged_item_description': 'Migration signed hedge item',
            'hedging_instrument_description': 'Migration signed instrument',
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'oci_account_id': self.cta.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Signed migration fixture',
        })
        hedge.action_designate()
        self.env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'test_date': '2026-03-01',
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1.0,
            'cumulative_hedged_change': -1.0,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1.0,
            'effective_portion': 1.0,
            'hedged_item_change': -1.0,
        })
        movement.action_post()
        move = movement.move_id
        self._legacy_sql(
            "UPDATE account_move_line SET debit = credit, credit = debit, "
            "balance = -balance, amount_currency = -amount_currency "
            "WHERE move_id = %s",
            [move.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_draft_hedge_movement_cannot_carry_posted_move_link(self):
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'designation_date': '2026-01-01',
            'hedged_item_description': 'State-link hedge item',
            'hedging_instrument_description': 'State-link instrument',
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'oci_account_id': self.cta.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'State-link migration fixture',
        })
        hedge.action_designate()
        self.env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'test_date': '2026-03-01',
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1.0,
            'cumulative_hedged_change': -1.0,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1.0,
            'effective_portion': 1.0,
            'hedged_item_change': -1.0,
        })
        movement.action_post()
        self._legacy_sql(
            "UPDATE eh_fx_hedge_movement SET state = 'draft' WHERE id = %s",
            [movement.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_sign_reversed_hedge_reclass_cannot_be_backfilled(self):
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'designation_date': '2026-01-01',
            'hedged_item_description': 'Migration reclass item',
            'hedging_instrument_description': 'Migration reclass instrument',
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'oci_account_id': self.cta.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Signed reclass migration fixture',
        })
        hedge.action_designate()
        self.env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'test_date': '2026-03-01',
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1.0,
            'cumulative_hedged_change': -1.0,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1.0,
            'effective_portion': 1.0,
            'hedged_item_change': -1.0,
        })
        movement.action_post()
        movement.action_reclassify_to_pl()
        reclass = movement.reclassification_move_id
        self._legacy_sql(
            "UPDATE account_move_line SET debit = credit, credit = debit, "
            "balance = -balance, amount_currency = -amount_currency "
            "WHERE move_id = %s",
            [reclass.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_unrelated_sealed_move_cannot_be_blessed_as_reversal(self):
        run, source = self._legacy_run_source()
        unrelated = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company.id,
            'journal_id': self.journal_misc.id,
            'date': '2026-04-01',
            'ref': 'Not a reversal',
            'eh_sealed': True,
            'line_ids': [
                (0, 0, {
                    'name': 'not reversal', 'account_id': self.gain.id,
                    'debit': 1.0, 'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'not reversal', 'account_id': self.account_cash.id,
                    'debit': 0.0, 'credit': 1.0,
                }),
            ],
        })
        unrelated.action_post()
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'reversed', "
            "move_id = %s, reversal_move_id = %s WHERE id = %s",
            [source.id, unrelated.id, run.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_exact_reversal_is_backfilled(self):
        run, source = self._legacy_run_source()
        reversal = source._eh_reverse_with_verified_capability([{
            'date': '2026-04-01',
            'journal_id': self.journal_misc.id,
            'ref': 'Legacy exact reversal',
        }], cancel=False)
        reversal._eh_post_verified_reversal()
        run._eh_seal_reversal(reversal)
        self._legacy_sql(
            "UPDATE eh_fx_revaluation_run SET state = 'reversed', "
            "move_id = %s, reversal_move_id = %s WHERE id = %s",
            [source.id, reversal.id, run.id],
        )
        self._migrate()
        source.invalidate_recordset(['eh_fx_revaluation_run_id'])
        reversal.invalidate_recordset(['eh_fx_revaluation_reversal_run_id'])
        self.assertEqual(source.eh_fx_revaluation_run_id, run)
        self.assertEqual(reversal.eh_fx_revaluation_reversal_run_id, run)

    def test_open_cta_rejects_forged_tag_without_reserve_line(self):
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Open migration CTA',
            'cta_account_id': self.cta.id,
        })
        move = self._posted_move('Unrelated open CTA entry')
        self._legacy_sql(
            "UPDATE account_move SET eh_cta_position_id = %s WHERE id = %s",
            [position.id, move.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_open_nih_cta_and_movement_provenance_are_backfilled(self):
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Legacy open NIH CTA',
            'cta_account_id': self.cta.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
            'journal_id': self.journal_misc.id,
        })
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'net_investment',
            'designation_date': '2026-01-01',
            'hedged_item_description': 'Legacy net investment',
            'hedging_instrument_description': 'Legacy NIH instrument',
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'cta_position_id': position.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Legacy NIH provenance fixture',
        })
        hedge.action_designate()
        self.env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'test_date': '2026-02-01',
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1.0,
            'cumulative_hedged_change': -1.0,
        })
        movement = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-03-31',
            'total_change': 1.0,
            'effective_portion': 1.0,
            'hedged_item_change': -1.0,
        })
        movement.action_post()
        move = movement.move_id
        ineffective = self.env['eh.fx.hedge.movement'].create({
            'hedge_id': hedge.id,
            'movement_date': '2026-04-30',
            'total_change': 2.0,
            'effective_portion': 0.0,
        })
        ineffective.action_post()
        self._legacy_sql(
            "UPDATE account_move SET eh_cta_position_id = NULL, "
            "eh_fx_hedge_movement_id = NULL WHERE id = %s",
            [move.id],
        )
        self._migrate()
        move.invalidate_recordset([
            'eh_cta_position_id', 'eh_fx_hedge_movement_id',
        ])
        self.assertEqual(move.eh_cta_position_id, position)
        self.assertEqual(move.eh_fx_hedge_movement_id, movement)
        self.assertFalse(ineffective.move_id.eh_cta_position_id)

    def test_valid_legacy_cta_disposal_link_is_backfilled(self):
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Legacy disposed CTA',
            'cta_account_id': self.cta.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
            'journal_id': self.journal_misc.id,
        })
        self._cta_reserve_move(position, 1.0)
        move = self._cta_disposal_move(position, 1.0)
        self._legacy_sql(
            "UPDATE eh_fx_cta_position SET state = 'disposed', "
            "disposal_move_id = %s WHERE id = %s",
            [move.id, position.id],
        )
        self._migrate()
        move.invalidate_recordset(['eh_cta_position_id'])
        self.assertEqual(move.eh_cta_position_id, position)

    def test_wrong_amount_cta_disposal_cannot_be_backfilled(self):
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Wrong amount CTA disposal',
            'cta_account_id': self.cta.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
            'journal_id': self.journal_misc.id,
        })
        self._cta_reserve_move(position, 10.0)
        move = self._cta_disposal_move(position, 1.0)
        self._legacy_sql(
            "UPDATE eh_fx_cta_position SET state = 'disposed', "
            "disposal_move_id = %s WHERE id = %s",
            [move.id, position.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_wrong_direction_cta_disposal_cannot_be_backfilled(self):
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Wrong direction CTA disposal',
            'cta_account_id': self.cta.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
            'journal_id': self.journal_misc.id,
        })
        self._cta_reserve_move(position, 1.0)
        move = self._cta_disposal_move(position, 1.0, reverse=True)
        self._legacy_sql(
            "UPDATE eh_fx_cta_position SET state = 'disposed', "
            "disposal_move_id = %s WHERE id = %s",
            [move.id, position.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_partial_percentage_cannot_claim_full_cta_disposal(self):
        position = self.env['eh.fx.cta.position'].create({
            'name': 'Partial percentage CTA disposal',
            'cta_account_id': self.cta.id,
            'gain_account_id': self.gain.id,
            'loss_account_id': self.loss.id,
            'journal_id': self.journal_misc.id,
        })
        self._cta_reserve_move(position, 1.0)
        move = self._cta_disposal_move(position, 1.0)
        self._legacy_sql(
            "UPDATE eh_fx_cta_position SET state = 'disposed', "
            "disposal_pct = 50.0, disposal_move_id = %s WHERE id = %s",
            [move.id, position.id],
        )
        with self.assertRaises(UserError):
            self._migrate()

    def test_pre_designation_test_fails_upgrade_closed(self):
        hedge = self.env['eh.fx.hedge'].create({
            'name': '/',
            'hedge_type': 'cash_flow',
            'designation_date': '2026-03-01',
            'hedged_item_description': 'Migration hedge item',
            'hedging_instrument_description': 'Migration instrument',
            'hedged_currency_id': self.company.currency_id.id,
            'pl_account_id': self.loss.id,
            'oci_account_id': self.cta.id,
            'instrument_account_id': self.account_cash.id,
            'journal_id': self.journal_misc.id,
            'notes': 'Migration evidence fixture',
        })
        hedge.action_designate()
        test = self.env['eh.fx.hedge.test'].create({
            'hedge_id': hedge.id,
            'test_date': '2026-03-31',
            'method': 'dollar_offset',
            'cumulative_instrument_change': 1.0,
            'cumulative_hedged_change': -1.0,
        })
        self._legacy_sql(
            "UPDATE eh_fx_hedge SET state = 'draft' WHERE id = %s",
            [hedge.id],
        )
        self.assertTrue(test.exists())
        with self.assertRaises(UserError):
            self._migrate()
