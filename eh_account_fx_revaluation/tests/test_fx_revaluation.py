# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
FX revaluation tests.

Set up a foreign currency receivable, change the closing rate, run
revaluation. Verify line aggregation, gain/loss classification, posted
move balance and reversal.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase
from odoo.addons.eh_account_fx_revaluation.tools import rate_providers as rp

from .compat import add_user_groups, user_group_values


@tagged('eh_account_fx_revaluation', 'integration', 'post_install', '-at_install')
class TestFxRevaluation(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_user_groups(
            cls.env.user,
            cls.env.ref('account.group_account_manager'),
            cls.env.ref('eh_account_base.group_eh_manager'),
        )

        cls.eur = cls.env.ref('base.EUR')
        cls.eur.active = True
        cls.usd = cls.env.ref('base.USD')
        cls.usd.active = True

        # Force company currency to USD by setting the company.
        # Using whatever is set already works too; we test with the
        # foreign side being EUR regardless.
        cls.gain_account = cls._ensure_account(
            cls.env, '4920', 'Unrealised FX Gain', 'income_other',
        )
        cls.loss_account = cls._ensure_account(
            cls.env, '5930', 'Unrealised FX Loss', 'expense',
        )

        cls.account_receivable.eh_fx_revalue = True

        # Seed an opening EUR rate.
        Rate = cls.env['res.currency.rate']
        Rate.search([('currency_id', '=', cls.eur.id)]).unlink()
        Rate.create({
            'currency_id': cls.eur.id,
            'name': '2026-01-01',
            'rate': 1.0,
            'company_id': cls.company.id,
        })

    def _post_eur_invoice_balance(self, amount_eur, amount_company, partner=None):
        """Post a balanced manual entry that mimics a foreign currency
        invoice: Dr Receivable (EUR), Cr Revenue (company currency).
        """
        partner = partner or self.partner_a
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR receivable',
                    'account_id': self.account_receivable.id,
                    'partner_id': partner.id,
                    'currency_id': self.eur.id,
                    'amount_currency': amount_eur,
                    'debit': amount_company,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'EUR revenue (in company ccy)',
                    'account_id': self.account_revenue.id,
                    'debit': 0.0,
                    'credit': amount_company,
                }),
            ],
        })
        move.action_post()
        return move

    def _set_closing_rate(self, date_str, rate):
        """Set the EUR rate at `date_str`. Odoo convention: rate is units
        of foreign currency per 1 unit of company currency. So if 1 USD =
        0.5 EUR, rate = 0.5.
        """
        Rate = self.env['res.currency.rate']
        return Rate.create({
            'currency_id': self.eur.id,
            'name': date_str,
            'rate': rate,
            'company_id': self.company.id,
        })

    def _post_currency_exposures(self, exposures):
        """Post [(currency, foreign, company, partner), ...] in one move."""
        lines = []
        for currency, foreign, company_amount, partner in exposures:
            lines.extend([
                (0, 0, {
                    'name': '%s receivable' % currency.name,
                    'account_id': self.account_receivable.id,
                    'partner_id': partner.id,
                    'currency_id': currency.id,
                    'amount_currency': foreign,
                    'debit': company_amount,
                }),
                (0, 0, {
                    'name': '%s revenue' % currency.name,
                    'account_id': self.account_revenue.id,
                    'credit': company_amount,
                }),
            ])
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': lines,
        })
        move.action_post()
        return move

    def _two_computed_runs(self):
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        self._set_closing_rate('2026-04-30', 0.75)
        Run = self.env['eh.fx.revaluation.run']
        runs = Run.create([
            {
                'revaluation_date': revaluation_date,
                'journal_id': self.journal_misc.id,
                'gain_account_id': self.gain_account.id,
                'loss_account_id': self.loss_account.id,
                'auto_reverse': False,
            }
            for revaluation_date in ('2026-03-31', '2026-04-30')
        ]).sorted('id')
        for run in runs:
            run.action_compute()
            self.assertTrue(run.line_ids)
        return runs

    def test_aggregate_without_partner_computes_one_bucket(self):
        self._post_currency_exposures([
            (self.eur, 400.0, 400.0, self.partner_a),
            (self.eur, 600.0, 600.0, self.partner_b),
        ])
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'aggregate_by_partner': False,
            'auto_reverse': False,
        })

        run.action_compute()

        self.assertEqual(len(run.line_ids), 1)
        self.assertFalse(run.line_ids.partner_id)
        self.assertAlmostEqual(run.line_ids.balance_foreign, 1000.0, places=2)

    def test_genuine_two_foreign_currency_run_computes_and_posts(self):
        currencies = self.env['res.currency'].with_context(
            active_test=False,
        ).search([
            ('id', '!=', self.company.currency_id.id),
            ('name', 'in', ['EUR', 'GBP', 'JPY']),
        ], limit=2)
        self.assertEqual(len(currencies), 2)
        currencies.sudo().write({'active': True})
        first, second = currencies
        self._post_currency_exposures([
            (first, 1000.0, 1000.0, self.partner_a),
            (second, 500.0, 500.0, self.partner_b),
        ])
        Rate = self.env['res.currency.rate']
        for currency, rate in ((first, 0.8), (second, 0.5)):
            Rate.create({
                'currency_id': currency.id,
                'name': '2026-03-31',
                'rate': rate,
                'company_id': self.company.id,
            })
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })

        run.action_compute()

        self.assertEqual(len(run.line_ids), 2)
        self.assertEqual(
            set(run.line_ids.foreign_currency_id.ids), set(currencies.ids),
        )
        self.assertTrue(all(run.line_ids.mapped('source_move_line_ids')))
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertEqual(run.move_id.state, 'posted')
        self.assertAlmostEqual(
            sum(run.move_id.line_ids.mapped('balance')), 0.0, places=2,
        )

    def test_company_rules_isolate_runs_lines_configs_and_overrides(self):
        other = self.env['res.company'].create({
            'name': 'FX isolated company',
            'currency_id': self.company.currency_id.id,
        })
        self.env.user.write({'company_ids': [(4, other.id)]})
        other_env = self.env['eh.fx.revaluation.run'].with_context(
            allowed_company_ids=[self.company.id, other.id],
        ).with_company(other).env
        journal = other_env['account.journal'].create({
            'name': 'FX isolated journal',
            'code': 'FXIS',
            'type': 'general',
            'company_id': other.id,
        })
        gain = self._ensure_account(
            other_env, '4920', 'FX isolated gain', 'income_other',
        )
        loss = self._ensure_account(
            other_env, '5930', 'FX isolated loss', 'expense',
        )
        receivable = self._ensure_account(
            other_env, '1100', 'FX isolated receivable', 'asset_receivable',
        )
        foreign_run = other_env['eh.fx.revaluation.run'].create({
            'company_id': other.id,
            'revaluation_date': '2026-09-30',
            'journal_id': journal.id,
            'gain_account_id': gain.id,
            'loss_account_id': loss.id,
            'auto_reverse': False,
        })
        foreign_line = other_env['eh.fx.revaluation.line'].sudo().create({
            'run_id': foreign_run.id,
            'account_id': receivable.id,
            'foreign_currency_id': self.eur.id,
            'balance_foreign': 10.0,
            'balance_company': 10.0,
            'closing_rate': 1.1,
            'new_balance_company': 11.0,
            'adjustment': 1.0,
            'nature': 'gain',
        })
        foreign_config = other_env['eh.fx.rate.config'].create({
            'company_id': other.id,
            'provider': 'manual',
        })
        foreign_override = other_env['eh.fx.rate.override'].create({
            'config_id': foreign_config.id,
            'currency_id': self.eur.id,
            'inverse_company_rate': 0.75,
        })

        plain = self._plain_user()
        scoped = self.env['res.users'].with_user(plain).with_context(
            allowed_company_ids=[self.company.id],
        ).env
        for model_name, record in (
            ('eh.fx.revaluation.run', foreign_run),
            ('eh.fx.revaluation.line', foreign_line),
            ('eh.fx.rate.config', foreign_config),
            ('eh.fx.rate.override', foreign_override),
        ):
            with self.subTest(model=model_name):
                self.assertFalse(scoped[model_name].search([
                    ('id', '=', record.id),
                ]))

    # ---- run lifecycle ----

    def test_create_run_assigns_sequence(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        self.assertNotEqual(run.name, '/')
        self.assertTrue(run.name.startswith('FXR/'))

    def test_gain_and_loss_account_types_are_enforced_server_side(self):
        base = {
            'revaluation_date': '2026-12-30',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        }
        for field_name, account in (
            ('gain_account_id', self.account_equity),
            ('loss_account_id', self.account_revenue),
        ):
            with self.subTest(field=field_name), self.assertRaises(
                    ValidationError):
                self.env['eh.fx.revaluation.run'].create(
                    dict(base, **{field_name: account.id}))

        # A regular income account is a valid local-chart FX gain account;
        # ``income_other`` is not a universal accounting requirement.
        regular_income = self.env['eh.fx.revaluation.run'].create(dict(
            base,
            revaluation_date='2026-12-29',
            gain_account_id=self.account_revenue.id,
        ))
        self.assertEqual(regular_income.gain_account_id.account_type, 'income')

    def test_unique_date_per_company(self):
        first = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'), \
                self.env.cr.savepoint():
            self.env['eh.fx.revaluation.run'].create({
                'revaluation_date': '2026-03-31',
                'journal_id': self.journal_misc.id,
                'gain_account_id': self.gain_account.id,
                'loss_account_id': self.loss_account.id,
            })
        first.action_cancel()
        correction = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        self.assertEqual(correction.state, 'draft')
        self.assertTrue(first.exists(), "cancelled audit history was deleted")
        with self.assertRaisesRegex(UserError, 'already has active FX run'):
            first.action_set_to_draft()
        self.assertEqual(first.state, 'cancelled')

    def test_compute_with_no_open_balances(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertEqual(len(run.line_ids), 0)

    def test_legacy_rate_review_hold_blocks_compute_and_post(self):
        Config = self.env['eh.fx.rate.config']
        Config.search([('company_id', '=', self.company.id)]).unlink()
        Config.create({
            'company_id': self.company.id,
            'provider': 'ecb',
            'enabled': False,
            'rate_direction_review_required': True,
        })
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-12-26',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })

        with self.assertRaisesRegex(UserError, 'blocked'):
            run.action_compute()
        self.assertEqual(run.state, 'draft')

        # Posting must enforce the same hold before checking downstream
        # computed-line prerequisites.
        run.write({'state': 'computed'})
        with self.assertRaisesRegex(UserError, 'blocked'):
            run.action_post()

    def test_draft_line_forged_parent_state_is_not_an_exposure(self):
        draft = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'Draft forged FX receivable',
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                    'currency_id': self.eur.id,
                    'amount_currency': 100.0,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'name': 'Draft forged FX counterpart',
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                }),
            ],
        })
        source = draft.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable,
        )
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move_line SET parent_state = 'posted' "
            "WHERE id = %s",
            (source.id,),
        )
        source.invalidate_recordset(['parent_state'])
        self.assertEqual(source.parent_state, 'posted')
        self.assertEqual(draft.state, 'draft')

        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        run.action_compute()
        self.assertFalse(run.line_ids)

    def test_computed_measurement_basis_requires_reset(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': True,
            'aggregate_by_partner': True,
        })
        run.action_compute()
        alternate_journal = self.env['account.journal'].create({
            'name': 'Alternate FX general journal',
            'code': 'FXA2',
            'type': 'general',
            'company_id': self.company.id,
        })
        alternate_gain = self._ensure_account(
            self.env, '4921', 'Alternate FX Gain', 'income_other',
        )
        alternate_loss = self._ensure_account(
            self.env, '5931', 'Alternate FX Loss', 'expense',
        )
        changes = (
            {'revaluation_date': '2026-04-30'},
            {'journal_id': alternate_journal.id},
            {'gain_account_id': alternate_gain.id},
            {'loss_account_id': alternate_loss.id},
            {'auto_reverse': False},
            {'reversal_date': '2026-05-15'},
            {'aggregate_by_partner': False},
        )
        for vals in changes:
            with self.subTest(vals=vals), self.assertRaises(UserError):
                run.write(vals)
        self.assertEqual(run.state, 'computed')
        self.assertEqual(run.revaluation_date.isoformat(), '2026-03-31')

    def test_revaluation_requires_general_journal(self):
        bank_journal = self._ensure_journal(
            self.env, self.company, 'bank', 'FXBK', 'FX test bank',
        )
        with self.assertRaises(ValidationError):
            self.env['eh.fx.revaluation.run'].create({
                'revaluation_date': '2026-03-31',
                'journal_id': bank_journal.id,
                'gain_account_id': self.gain_account.id,
                'loss_account_id': self.loss_account.id,
            })

    def test_post_blocks_without_lines(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        run.action_compute()
        with self.assertRaises(UserError):
            run.action_post()

    def test_compute_blocked_after_posted(self):
        # Post one line worth of activity so the run can be posted.
        self._post_eur_invoice_balance(1000.0, 1000.0)
        # Default rate is 1.0; keep it for the closing date so adjustment is 0
        # but we still get a line. To produce a non zero adjustment, change rate.
        self._set_closing_rate('2026-03-31', 1.25)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            run.action_compute()

    def test_compute_recordset_is_atomic_on_second_run_late_failure(self):
        runs = self._two_computed_runs()
        first, second = runs
        line_ids_before = {
            run.id: tuple(run.line_ids.ids)
            for run in runs
        }
        RunClass = type(first)
        original_build_lines = RunClass._build_lines

        def fail_second_build(run):
            if run.id == second.id:
                raise UserError("Injected second-run compute failure")
            return original_build_lines(run)

        with patch.object(RunClass, '_build_lines', fail_second_build):
            try:
                runs.action_compute()
            except UserError:
                pass
            else:
                self.fail("Injected second-run compute failure was not raised")

        self.env['eh.fx.revaluation.line'].invalidate_model()
        runs.invalidate_recordset()
        self.assertEqual(set(runs.mapped('state')), {'computed'})
        for run in runs:
            self.assertEqual(
                tuple(run.line_ids.ids), line_ids_before[run.id],
                "A later compute failure must restore every earlier run.",
            )

    def test_post_recordset_is_atomic_on_second_run_late_failure(self):
        runs = self._two_computed_runs()
        first, second = runs
        RunClass = type(first)
        original_write = RunClass.write
        Move = self.env['account.move']
        moves_before = Move.search([
            ('eh_fx_revaluation_run_id', 'in', runs.ids),
        ])

        def fail_second_anchor(records, vals):
            if vals.get('move_id') and second.id in records.ids:
                raise UserError("Injected second-run posting failure")
            return original_write(records, vals)

        with patch.object(RunClass, 'write', fail_second_anchor):
            try:
                runs.action_post()
            except UserError:
                pass
            else:
                self.fail("Injected second-run posting failure was not raised")

        Move.invalidate_model()
        runs.invalidate_recordset()
        self.assertEqual(set(runs.mapped('state')), {'computed'})
        self.assertFalse(first.move_id)
        self.assertFalse(second.move_id)
        self.assertEqual(
            Move.search([('eh_fx_revaluation_run_id', 'in', runs.ids)]),
            moves_before,
            "A later post failure must remove every earlier GL prefix.",
        )

    def test_post_recordset_refuses_second_stale_source_move_plain_try(self):
        runs = self._two_computed_runs()
        first, second = runs
        stale_version = second.source_move_version
        self._post_eur_invoice_balance(250.0, 250.0)
        first.action_compute()
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertEqual(
            first.source_move_version, self.company.eh_move_version,
        )
        self.assertEqual(second.source_move_version, stale_version)
        self.assertNotEqual(
            second.source_move_version, self.company.eh_move_version,
        )
        Move = self.env['account.move']
        moves_before = Move.search([
            ('eh_fx_revaluation_run_id', 'in', runs.ids),
        ])

        # Deliberately avoid assertRaises: its nested savepoint can hide a
        # missing action-owned rollback after the first record was processed.
        try:
            runs.action_post()
        except UserError:
            pass
        else:
            self.fail("Second-run stale source version was not refused")

        Move.invalidate_model()
        runs.invalidate_recordset()
        self.assertEqual(set(runs.mapped('state')), {'computed'})
        self.assertFalse(first.move_id)
        self.assertFalse(second.move_id)
        self.assertEqual(
            Move.search([('eh_fx_revaluation_run_id', 'in', runs.ids)]),
            moves_before,
            "A stale later run must leave no earlier GL prefix.",
        )

    def test_post_refuses_closing_rate_drift_after_compute(self):
        self._post_eur_invoice_balance(1000.0, 1000.0)
        closing_rate = self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        stamped_version = run.source_move_version
        closing_rate.write({'rate': 0.75})
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertGreater(self.company.eh_move_version, stamped_version)

        with self.assertRaises(UserError):
            run.action_post()

        run.invalidate_recordset()
        self.assertEqual(run.state, 'computed')
        self.assertFalse(run.move_id)

    def test_closing_rate_rejects_nonfinite_technical_conversion(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        RunClass = type(run)
        for invalid_rate in (float('nan'), float('inf')):
            with self.subTest(rate=invalid_rate), patch.object(
                RunClass, '_eh_technical_rate_as_of',
                return_value=invalid_rate,
            ):
                with self.assertRaises(UserError):
                    run._closing_rate(self.eur)

    def test_closing_rate_rejects_missing_and_later_only_fixings(self):
        rate_company = (
            self.company.root_id
            if 'root_id' in self.env['res.company']._fields
            else self.company
        )
        Rate = self.env['res.currency.rate'].sudo()
        Rate.search([
            ('currency_id', '=', self.eur.id),
            ('company_id', 'in', [False, rate_company.id]),
        ]).unlink()
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })

        with self.assertRaisesRegex(UserError, 'dated on or before'):
            run._closing_rate(self.eur)

        Rate.create({
            'currency_id': self.eur.id,
            'name': '2026-04-01',
            'rate': 0.5,
            'company_id': rate_company.id,
        })
        with self.assertRaisesRegex(UserError, 'dated on or before'):
            run._closing_rate(self.eur)

        Rate.create({
            'currency_id': self.eur.id,
            'name': '2026-03-28',
            'rate': 0.8,
            'company_id': rate_company.id,
        })
        self.assertAlmostEqual(run._closing_rate(self.eur), 1.25, places=6)

    def test_fetch_missing_rates_recordset_is_atomic_on_second_failure_plain_try(self):
        self._post_eur_invoice_balance(1000.0, 1000.0)
        Config = self.env['eh.fx.rate.config']
        Config.search([('company_id', '=', self.company.id)]).unlink()
        config = Config.create({
            'company_id': self.company.id,
            'provider': 'manual',
            'enabled': True,
        })
        runs = self.env['eh.fx.revaluation.run'].create([
            {
                'revaluation_date': revaluation_date,
                'journal_id': self.journal_misc.id,
                'gain_account_id': self.gain_account.id,
                'loss_account_id': self.loss_account.id,
                'auto_reverse': False,
            }
            for revaluation_date in ('2026-11-29', '2026-11-30')
        ]).sorted('id')
        second = runs[-1]
        summary_before = config.last_fetch_summary
        messages_before = set(runs.mapped('message_ids').ids)
        ConfigClass = type(config)

        def fail_second_after_mutation(record, **kwargs):
            record.write({'last_fetch_summary': 'partial rate fetch'})
            if kwargs.get('on_date') == second.revaluation_date:
                raise UserError('Injected second-run rate fetch failure')
            return {}

        with patch.object(ConfigClass, 'fetch_rates', fail_second_after_mutation):
            try:
                runs.action_fetch_missing_rates()
            except UserError:
                pass
            else:
                self.fail("Injected second-run rate fetch failure was not raised")

        config.invalidate_recordset(['last_fetch_summary'])
        self.env['mail.message'].invalidate_model()
        runs.invalidate_recordset(['message_ids'])
        self.assertEqual(config.last_fetch_summary, summary_before)
        self.assertEqual(set(runs.mapped('message_ids').ids), messages_before)

    def test_fetched_company_rate_drives_conversion_and_revaluation(self):
        """Provider quote direction must survive through posted ledger math."""
        provider_name = 't_revaluation_direction'

        class _DirectionProvider:
            name = provider_name

            def __init__(self, timeout=10, api_key=None, **_kwargs):
                pass

            def fetch(self, base, quotes, on_date):
                self_outer.assertEqual(base, self_outer.company.currency_id.name)
                self_outer.assertIn('EUR', quotes)
                return rp.ProviderRates(
                    {'EUR': Decimal('0.8')},
                    effective_date=on_date,
                )

        self_outer = self
        rp.register(provider_name, _DirectionProvider)
        self.addCleanup(rp._PROVIDERS.pop, provider_name, None)
        self.addCleanup(rp._LABELS.pop, provider_name, None)

        on_date = fields.Date.from_string('2026-09-30')
        Config = self.env['eh.fx.rate.config']
        Config.search([('company_id', '=', self.company.id)]).unlink()
        config = Config.create({
            'company_id': self.company.id,
            'provider': provider_name,
            'fallback_provider': 'manual',
        })
        rate_company = (
            self.company.root_id
            if 'root_id' in self.env['res.company']._fields
            else self.company
        )
        self.env['res.currency.rate'].sudo().search([
            ('currency_id', '=', self.eur.id),
            ('name', '=', on_date),
            ('company_id', '=', rate_company.id),
        ]).unlink()
        self._post_eur_invoice_balance(1000.0, 1000.0)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': on_date,
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })

        run.action_fetch_missing_rates()
        rate_row = self.env['res.currency.rate'].search([
            ('currency_id', '=', self.eur.id),
            ('name', '=', on_date),
            ('company_id', '=', rate_company.id),
        ], limit=1)
        self.assertTrue(rate_row)
        self.assertAlmostEqual(rate_row.company_rate, 0.8, places=6)
        self.assertAlmostEqual(
            self.company.currency_id._convert(
                1.0, self.eur, self.company, on_date, round=False,
            ),
            0.8,
            places=6,
        )
        self.assertAlmostEqual(
            self.eur._convert(
                1.0, self.company.currency_id, self.company, on_date,
                round=False,
            ),
            1.25,
            places=6,
        )

        run.action_compute()
        self.assertEqual(len(run.line_ids), 1)
        self.assertAlmostEqual(run.line_ids.closing_rate, 1.25, places=6)
        self.assertAlmostEqual(
            run.line_ids.new_balance_company, 1250.0, places=2,
        )
        self.assertAlmostEqual(run.line_ids.adjustment, 250.0, places=2)
        self.assertEqual(run.line_ids.nature, 'gain')
        self.assertIn('1 rates written', config.last_fetch_summary)

    def test_plain_run_user_fetches_source_dated_rate_end_to_end(self):
        """Fetch elevates execution, not provider configuration authority."""
        provider_name = 't_operator_prior_fixing'
        fixing_delta = timedelta(days=2)
        self_outer = self

        class _OperatorProvider:
            name = provider_name

            def __init__(self, timeout=10, api_key=None, **_kwargs):
                self_outer.assertEqual(api_key, 'provider-secret')

            def fetch(self, base, quotes, on_date):
                self_outer.assertIn('EUR', quotes)
                return rp.ProviderRates(
                    {'EUR': Decimal('0.8')},
                    effective_date=on_date - fixing_delta,
                )

        rp.register(provider_name, _OperatorProvider)
        self.addCleanup(rp._PROVIDERS.pop, provider_name, None)
        self.addCleanup(rp._LABELS.pop, provider_name, None)

        requested_date = fields.Date.from_string('2026-09-30')
        fixing_date = requested_date - fixing_delta
        rate_company = (
            self.company.root_id
            if 'root_id' in self.env['res.company']._fields
            else self.company
        )
        Rate = self.env['res.currency.rate'].sudo()
        Rate.search([
            ('currency_id', '=', self.eur.id),
            ('name', 'in', [requested_date, fixing_date]),
            ('company_id', '=', rate_company.id),
        ]).unlink()
        self._post_eur_invoice_balance(1000.0, 1000.0)
        Config = self.env['eh.fx.rate.config']
        Config.search([('company_id', '=', self.company.id)]).unlink()
        config = Config.create({
            'company_id': self.company.id,
            'provider': provider_name,
            'fallback_provider': 'manual',
            'api_key': 'provider-secret',
        })
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': requested_date,
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        operator = self._plain_user()

        with self.assertRaises(AccessError):
            config.with_user(operator).write({'provider': 'manual'})
        run.with_user(operator).action_fetch_missing_rates()

        self.assertFalse(Rate.search([
            ('currency_id', '=', self.eur.id),
            ('name', '=', requested_date),
            ('company_id', '=', rate_company.id),
        ], limit=1))
        fixing = Rate.search([
            ('currency_id', '=', self.eur.id),
            ('name', '=', fixing_date),
            ('company_id', '=', rate_company.id),
        ], limit=1)
        self.assertTrue(fixing)
        self.assertAlmostEqual(fixing.company_rate, 0.8, places=6)
        self.assertIn(fixing_date.isoformat(), config.last_fetch_summary)

        run.with_user(operator).action_compute()
        run.invalidate_recordset()
        self.assertEqual(run.state, 'computed')
        self.assertAlmostEqual(run.line_ids.closing_rate, 1.25, places=6)

    def test_branch_run_uses_parent_chart_and_journal_but_only_branch_ledger(self):
        Company = self.env['res.company']
        if 'root_id' not in Company._fields:
            self.skipTest("Odoo 16 has no branch/root accounting model")
        root = self.company.root_id
        branch_vals = {
            'name': 'FX revaluation branch',
            'parent_id': root.id,
        }
        if 'security_lead' in Company._fields:
            branch_vals['security_lead'] = 0.0
        branch = self._create_accounting_branch(branch_vals)
        self.env.user.write({'company_ids': [(4, branch.id)]})
        branch_env = self.env['eh.fx.revaluation.run'].with_context(
            allowed_company_ids=[branch.id, root.id],
        ).with_company(branch).env

        # The chart and general journal remain root-owned/shared.  The branch
        # ledger itself must stay exact: a root exposure must not leak into a
        # branch close merely because the master data is ancestor-owned.
        self._post_eur_invoice_balance(700.0, 700.0)
        branch_move = branch_env['account.move'].create({
            'move_type': 'entry',
            'company_id': branch.id,
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'Branch EUR receivable',
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                    'currency_id': self.eur.id,
                    'amount_currency': 1000.0,
                    'debit': 1000.0,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'Branch EUR revenue',
                    'account_id': self.account_revenue.id,
                    'debit': 0.0,
                    'credit': 1000.0,
                }),
            ],
        })
        branch_move.action_post()
        self._set_closing_rate('2026-03-31', 0.8)

        run = branch_env['eh.fx.revaluation.run'].create({
            'company_id': branch.id,
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertEqual(len(run.line_ids), 1)
        self.assertEqual(run.line_ids.account_id, self.account_receivable)
        self.assertAlmostEqual(run.line_ids.balance_foreign, 1000.0, places=2)
        self.assertAlmostEqual(run.line_ids.adjustment, 250.0, places=2)
        run.action_post()
        self.assertEqual(run.move_id.company_id, branch)
        self.assertEqual(run.move_id.journal_id, self.journal_misc)

    def test_post_refuses_account_revaluation_flag_drift_after_compute(self):
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertTrue(run.line_ids)
        stamped_version = run.source_move_version

        self.account_receivable.write({'eh_fx_revalue': False})
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertGreater(self.company.eh_move_version, stamped_version)

        with self.assertRaises(UserError):
            run.action_post()

        run.invalidate_recordset()
        self.assertEqual(run.state, 'computed')
        self.assertFalse(run.move_id)

    # ---- nature classification ----

    def test_classify_nature_asset_gain(self):
        from odoo.addons.eh_account_fx_revaluation.models.fx_revaluation_run import (
            EhFxRevaluationRun,
        )
        nature = EhFxRevaluationRun._classify_nature(
            self.account_receivable, 100.0,
        )
        self.assertEqual(nature, 'gain')

    def test_classify_nature_asset_loss(self):
        from odoo.addons.eh_account_fx_revaluation.models.fx_revaluation_run import (
            EhFxRevaluationRun,
        )
        nature = EhFxRevaluationRun._classify_nature(
            self.account_receivable, -100.0,
        )
        self.assertEqual(nature, 'loss')

    def test_classify_nature_liability_loss(self):
        # Liability balance is signed-negative in Odoo. A more-negative
        # adjustment means the debt grew, which is a loss. -100 means
        # the payable went from -X to -(X+100).
        from odoo.addons.eh_account_fx_revaluation.models.fx_revaluation_run import (
            EhFxRevaluationRun,
        )
        nature = EhFxRevaluationRun._classify_nature(
            self.account_payable, -100.0,
        )
        self.assertEqual(nature, 'loss')

    def test_classify_nature_flat(self):
        from odoo.addons.eh_account_fx_revaluation.models.fx_revaluation_run import (
            EhFxRevaluationRun,
        )
        nature = EhFxRevaluationRun._classify_nature(
            self.account_receivable, 0.0,
        )
        self.assertEqual(nature, 'flat')

    # ---- account flag ----

    def test_account_account_default_revalue_flag(self):
        receivable = self.env['account.account'].create({
            'code': '1199',
            'name': 'Test Receivable',
            'account_type': 'asset_receivable',
            'company_ids': [(6, 0, self.company.ids)],
            'reconcile': True,
        })
        self.assertTrue(receivable.eh_fx_revalue)

    def test_post_install_seed_flags_preexisting_monetary_account(self):
        receivable = self.env['account.account'].create({
            'code': '1198',
            'name': 'Pre-install-style Receivable',
            'account_type': 'asset_receivable',
            'company_ids': [(6, 0, self.company.ids)],
            'reconcile': True,
        })
        receivable.write({'eh_fx_revalue': False})
        self.assertFalse(receivable.eh_fx_revalue)

        seeded = self.env['account.account']._eh_seed_existing_monetary_accounts()

        self.assertIn(receivable, seeded)
        self.assertTrue(receivable.eh_fx_revalue)

    def test_account_account_non_monetary_default_off(self):
        equity = self.env['account.account'].create({
            'code': '3199',
            'name': 'Test Equity',
            'account_type': 'equity',
            'company_ids': [(6, 0, self.company.ids)],
        })
        self.assertFalse(equity.eh_fx_revalue)

    def test_account_account_non_monetary_flag_rejected(self):
        fixed = self.env['account.account'].create({
            'code': '1500',
            'name': 'Test Fixed Asset',
            'account_type': 'asset_fixed',
            'company_ids': [(6, 0, self.company.ids)],
        })
        with self.assertRaises(ValidationError):
            fixed.eh_fx_revalue = True
        # Odoo 19 exposes expense_other as a distinct account type. Older
        # series fold it into expense, whose non-monetary rejection is already
        # covered above/by the blocklist; only create the series-specific type
        # where core actually supports it.
        account_types = self.env[
            'account.account'
        ]._fields['account_type'].get_values(self.env)
        if 'expense_other' not in account_types:
            return
        other_expense = self.env['account.account'].create({
            'code': '6998',
            'name': 'Test Other Expense',
            'account_type': 'expense_other',
            'company_ids': [(6, 0, self.company.ids)],
        })
        with self.assertRaises(ValidationError):
            other_expense.eh_fx_revalue = True

    # ---- cancel / set to draft ----

    def test_cancel_and_reset(self):
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        run.action_compute()
        self.assertGreater(run.source_move_version, 0)
        run.action_cancel()
        self.assertEqual(run.state, 'cancelled')
        self.assertEqual(run.source_move_version, 0)
        run.action_set_to_draft()
        self.assertEqual(run.state, 'draft')
        self.assertEqual(run.source_move_version, 0)

    def test_cancel_blocked_when_posted(self):
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 1.25)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            run.action_cancel()

    # ---- SoD: manager-only transitions ----

    def _plain_user(self):
        """A user with only eh_account_base.group_eh_user (no manager).

        Segregation of duties: a plain accounting user must never be
        able to post, reverse or cancel an FX revaluation run.
        """
        User = self.env['res.users']
        group_user = self.env.ref('eh_account_base.group_eh_user')
        user = User.create({
            'name': 'FX Plain User',
            'login': 'fx_plain_user',
            'email': 'fx_plain_user@example.com',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            **user_group_values(self.env, group_user),
        })
        self.assertFalse(
            user.has_group('eh_account_base.group_eh_manager'),
            "Fixture user must NOT be a manager for the SoD test to mean "
            "anything.",
        )
        return user

    def test_sod_post_blocked_for_plain_user(self):
        """A plain accounting user cannot post a computed run."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        with self.assertRaises(UserError):
            run.with_user(self._plain_user()).action_post()
        # State is unchanged: the block happened before any transition.
        self.assertEqual(run.state, 'computed')

    def test_sod_reverse_blocked_for_plain_user(self):
        """A plain accounting user cannot reverse a posted run."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        with self.assertRaises(UserError):
            run.with_user(self._plain_user()).action_reverse()
        self.assertEqual(run.state, 'posted')
        self.assertFalse(run.reversal_move_id)

    def test_sod_cancel_blocked_for_plain_user(self):
        """A plain accounting user cannot cancel a computed run.

        This is the core regression: action_cancel previously had no
        group check at all, so any user could cancel a computed run.
        """
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        with self.assertRaises(UserError):
            run.with_user(self._plain_user()).action_cancel()
        # The run was not cancelled by the unauthorised user.
        self.assertEqual(run.state, 'computed')

    # ---- journal balance regression ----

    def _post_eur_payable_balance(self, amount_eur, amount_company, partner=None):
        partner = partner or self.partner_a
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR payable',
                    'account_id': self.account_payable.id,
                    'partner_id': partner.id,
                    'currency_id': self.eur.id,
                    'amount_currency': -amount_eur,
                    'debit': 0.0,
                    'credit': amount_company,
                }),
                (0, 0, {
                    'name': 'EUR expense (in company ccy)',
                    'account_id': self.account_expense.id,
                    'debit': amount_company,
                    'credit': 0.0,
                }),
            ],
        })
        move.action_post()
        return move

    def test_posted_move_balances_for_asset_gain(self):
        """Asset receivable strengthens: DR receivable, CR FX gain.

        Regression for the bug where the leg followed adjustment sign
        only and produced unbalanced entries when the natural side and
        the adjustment sign disagreed.

        Odoo rate convention: rate stored on res.currency.rate is
        "units of foreign per 1 unit of company". Setting EUR rate=0.8
        means 1 USD = 0.8 EUR, so 1 EUR = 1.25 USD. A 1000 EUR
        receivable is now worth 1250 USD (asset strengthened) -> gain.
        """
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertTrue(run.line_ids)
        self.assertEqual(run.line_ids[:1].nature, 'gain')
        measured = run.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable,
        )
        self.assertEqual(len(measured), 1)
        self.assertAlmostEqual(measured.balance_company, 1000.0, places=2)
        self.assertAlmostEqual(measured.balance_foreign, 1000.0, places=2)
        self.assertAlmostEqual(measured.closing_rate, 1.25, places=6)
        self.assertAlmostEqual(measured.new_balance_company, 1250.0, places=2)
        self.assertAlmostEqual(measured.adjustment, 250.0, places=2)
        run.action_post()
        move = run.move_id
        self.assertTrue(move and move.state == 'posted')
        debit = sum(move.line_ids.mapped('debit'))
        credit = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(debit, credit)
        receivable_leg = move.line_ids.filtered(
            lambda l: l.account_id == self.account_receivable,
        )
        self.assertEqual(len(receivable_leg), 1)
        self.assertAlmostEqual(receivable_leg.debit, 250.0, places=2)
        self.assertEqual(receivable_leg.credit, 0)
        gain_leg = move.line_ids.filtered(
            lambda l: l.account_id == self.gain_account,
        )
        self.assertEqual(len(gain_leg), 1)
        self.assertAlmostEqual(gain_leg.credit, 250.0, places=2)
        self.assertEqual(gain_leg.debit, 0)

    def test_auto_reverse_uses_controlled_date_and_posts_exact_inverse(self):
        """The default workflow posts, seals and reverses exact measured GL.

        A reversed run remains immutable evidence but no longer consumes the
        one-live-run date, so finance can post a controlled correction.
        """
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': True,
            'reversal_date': '2026-04-07',
        })
        run.action_compute()
        run.action_post()

        original = run.move_id
        reversal = run.reversal_move_id
        self.assertEqual(run.state, 'reversed')
        self.assertEqual(original.state, 'posted')
        self.assertEqual(reversal.state, 'posted')
        self.assertTrue(original.eh_sealed)
        self.assertTrue(reversal.eh_sealed)
        self.assertEqual(reversal.reversed_entry_id, original)
        self.assertEqual(reversal.date.isoformat(), '2026-04-07')
        self.assertEqual(
            reversal.eh_fx_revaluation_reversal_run_id, run,
        )
        original_by_account = {
            line.account_id.id: (line.debit, line.credit)
            for line in original.line_ids
        }
        reversal_by_account = {
            line.account_id.id: (line.debit, line.credit)
            for line in reversal.line_ids
        }
        self.assertEqual(original_by_account[self.account_receivable.id],
                         (250.0, 0.0))
        self.assertEqual(original_by_account[self.gain_account.id],
                         (0.0, 250.0))
        self.assertEqual(reversal_by_account[self.account_receivable.id],
                         (0.0, 250.0))
        self.assertEqual(reversal_by_account[self.gain_account.id],
                         (250.0, 0.0))

    def test_reversal_date_must_follow_revaluation_date(self):
        for reversal_date in ('2026-03-30', '2026-03-31'):
            with self.subTest(reversal_date=reversal_date), self.assertRaises(
                    ValidationError):
                self.env['eh.fx.revaluation.run'].create({
                    'revaluation_date': '2026-03-31',
                    'reversal_date': reversal_date,
                    'journal_id': self.journal_misc.id,
                    'gain_account_id': self.gain_account.id,
                    'loss_account_id': self.loss_account.id,
                })

        correction = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        self.assertEqual(correction.state, 'draft')
        self.assertTrue(correction.exists())

    def test_posted_move_balances_for_liability_loss(self):
        """Liability payable strengthens: CR payable, DR FX loss.

        EUR rate=0.8 means 1 USD = 0.8 EUR, so 1 EUR = 1.25 USD.
        A 2000 EUR payable was originally worth 2000 USD; it is now
        worth 2500 USD owed. Liability up = LOSS.
        """
        self.account_payable.eh_fx_revalue = True
        self._post_eur_payable_balance(2000.0, 2000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertTrue(run.line_ids)
        self.assertEqual(run.line_ids[:1].nature, 'loss')
        run.action_post()
        move = run.move_id
        self.assertTrue(move and move.state == 'posted')
        debit = sum(move.line_ids.mapped('debit'))
        credit = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(debit, credit)
        payable_leg = move.line_ids.filtered(
            lambda l: l.account_id == self.account_payable,
        )
        self.assertEqual(len(payable_leg), 1)
        self.assertGreater(payable_leg.credit, 0)
        self.assertEqual(payable_leg.debit, 0)
        loss_leg = move.line_ids.filtered(
            lambda l: l.account_id == self.loss_account,
        )
        self.assertEqual(len(loss_leg), 1)
        self.assertGreater(loss_leg.debit, 0)
        self.assertEqual(loss_leg.credit, 0)

    def test_revaluation_leg_keeps_exposure_currency_with_zero_units(self):
        """Translation changes carrying value, never foreign units."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 1.25)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        exposure_leg = run.move_id.line_ids.filtered(
            lambda leg: leg.account_id == self.account_receivable,
        )
        self.assertEqual(exposure_leg.currency_id, self.eur)
        self.assertEqual(exposure_leg.amount_currency, 0.0)

    def test_non_reversing_later_run_posts_only_incremental_adjustment(self):
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        first = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        first.action_compute()
        first.action_post()
        self.assertAlmostEqual(first.line_ids.adjustment, 250.0, places=2)

        self._set_closing_rate('2026-04-30', 0.75)
        second = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-04-30',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        second.action_compute()
        self.assertAlmostEqual(second.line_ids.adjustment, 83.33, places=2)

    def test_fixed_foreign_currency_account_can_post_revaluation(self):
        eur_bank = self._ensure_account(
            self.env, '1098', 'EUR fixed-currency bank', 'asset_cash',
        )
        eur_bank.currency_id = self.eur
        eur_bank.eh_fx_revalue = True
        source = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR bank exposure',
                    'account_id': eur_bank.id,
                    'currency_id': self.eur.id,
                    'amount_currency': 1000.0,
                    'debit': 1000.0,
                }),
                (0, 0, {
                    'name': 'Counterpart',
                    'account_id': self.account_revenue.id,
                    'credit': 1000.0,
                }),
            ],
        })
        source.action_post()
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        leg = run.move_id.line_ids.filtered(
            lambda move_line: move_line.account_id == eur_bank,
        )
        self.assertEqual(leg.currency_id, self.eur)
        self.assertEqual(leg.amount_currency, 0.0)

    # ---- B3: point-in-time residual reconstruction ----

    def test_residual_at_date_ignores_later_settlement(self):
        inv = self._post_eur_invoice_balance(1000.0, 1000.0)
        ar_line = inv.line_ids.filtered(
            lambda l: l.account_id == self.account_receivable)
        # A partial settlement dated AFTER the revaluation date.
        pay = self.env['account.move'].create({
            'move_type': 'entry', 'date': '2026-05-10',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {'name': 'pmt',
                        'account_id': self.account_receivable.id,
                        'partner_id': self.partner_a.id,
                        'currency_id': self.eur.id,
                        'amount_currency': -400.0,
                        'debit': 0.0, 'credit': 400.0}),
                (0, 0, {'name': 'bank',
                        'account_id': self.account_revenue.id,
                        'debit': 400.0, 'credit': 0.0}),
            ],
        })
        pay.action_post()
        pay_ar = pay.line_ids.filtered(
            lambda l: l.account_id == self.account_receivable)
        (ar_line + pay_ar).reconcile()
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        # Before the settlement: full 1000 EUR was open.
        _comp, foreign = run._residual_at_date(
            ar_line, fields.Date.from_string('2026-03-31'))
        self.assertEqual(foreign, 1000.0)
        # After the settlement: residual drops to 600 EUR.
        _comp2, foreign2 = run._residual_at_date(
            ar_line, fields.Date.from_string('2026-06-30'))
        self.assertEqual(foreign2, 600.0)
        candidates = self.env['account.move.line'].search(
            run._open_line_candidate_domain(run._eligible_accounts()),
        )
        self.assertIn(
            ar_line, candidates,
            "a line settled after close must survive SQL pruning",
        )
        exposure_rows = run._open_lines_query(run._eligible_accounts())
        source_row = next(
            row for row in exposure_rows if row['line_id'] == ar_line.id
        )
        self.assertEqual(source_row['balance_foreign'], 1000.0)

    def test_historical_settlements_are_sql_pruned_before_residual_scan(self):
        invoice = self._post_eur_invoice_balance(1000.0, 1000.0)
        source = invoice.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable,
        )
        payment = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-03-01',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR settlement before close',
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                    'currency_id': self.eur.id,
                    'amount_currency': -1000.0,
                    'credit': 1000.0,
                }),
                (0, 0, {
                    'name': 'Settlement counterpart',
                    'account_id': self.account_cash.id,
                    'debit': 1000.0,
                }),
            ],
        })
        payment.action_post()
        settlement = payment.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable,
        )
        (source + settlement).reconcile()
        open_invoice = self._post_eur_invoice_balance(250.0, 250.0)
        open_source = open_invoice.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable,
        )
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })

        candidates = self.env['account.move.line'].search(
            run._open_line_candidate_domain(run._eligible_accounts()),
        )
        self.assertNotIn(source, candidates)
        self.assertNotIn(settlement, candidates)
        self.assertIn(open_source, candidates)
        RunClass = type(run)
        with patch.object(
                RunClass, '_residual_at_date',
                side_effect=AssertionError(
                    'Fetch Rates must not reconstruct Python residuals')):
            self.assertEqual(run._open_currency_ids(
                run._eligible_accounts()), {self.eur.id})

    # ---- B3: fetch-frequency scheduler ----

    def test_fetch_frequency_due_logic(self):
        Config = self.env['eh.fx.rate.config']
        cfg = Config.search([('company_id', '=', self.company.id)], limit=1)
        if not cfg:
            cfg = Config.create({'company_id': self.company.id})
        now = fields.Datetime.now()

        cfg.fetch_frequency = 'manual'
        cfg.last_fetched_at = False
        self.assertFalse(cfg._is_fetch_due())

        cfg.fetch_frequency = 'daily'
        cfg.last_fetched_at = False
        self.assertTrue(cfg._is_fetch_due(), "never fetched -> due")
        cfg.last_fetched_at = now - timedelta(hours=1)
        self.assertFalse(cfg._is_fetch_due())
        cfg.last_fetched_at = now - timedelta(hours=25)
        self.assertTrue(cfg._is_fetch_due())

        cfg.fetch_frequency = 'weekly'
        cfg.last_fetched_at = now - timedelta(days=3)
        self.assertFalse(cfg._is_fetch_due())
        cfg.last_fetched_at = now - timedelta(days=8)
        self.assertTrue(cfg._is_fetch_due())

    # ---- line freeze on post ----

    def _posted_run_with_line(self):
        """Compute and post a run that produces at least one line, so the
        line-freeze behaviour can be exercised."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertTrue(run.line_ids)
        run.action_post()
        self.assertEqual(run.state, 'posted')
        return run

    def test_posted_line_write_blocked(self):
        """The model docstring promises lines are frozen on post; editing a
        line whose run has posted must raise."""
        run = self._posted_run_with_line()
        line = run.line_ids[:1]
        with self.assertRaises(UserError):
            line.adjustment = 12345.0

    def test_posted_line_unlink_blocked(self):
        """Deleting a line whose run has posted must raise."""
        run = self._posted_run_with_line()
        line = run.line_ids[:1]
        with self.assertRaises(UserError):
            line.unlink()

    # ---- run freeze on post ----

    def test_posted_run_input_field_write_blocked(self):
        """A posted run's measurement/input field is frozen at the ORM
        write layer; touching it must raise even outside the action
        methods."""
        run = self._posted_run_with_line()
        with self.assertRaises(UserError):
            run.write({'revaluation_date': '2026-04-30'})

    def test_posted_run_unlink_blocked(self):
        """A posted run cannot be unlinked; it is the record behind a
        posted journal entry."""
        run = self._posted_run_with_line()
        with self.assertRaises(UserError):
            run.unlink()

    def test_post_and_reverse_flow_still_works(self):
        """The normal action_post -> action_reverse flow is unaffected by
        the freeze guard: both write only state + audit stamps + move
        links, which are never frozen fields."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertGreater(run.source_move_version, 0)
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertTrue(run.move_id)
        run.action_reverse()
        self.assertEqual(run.state, 'reversed')
        self.assertTrue(run.reversal_move_id)
        self.assertEqual(run.source_move_version, 0)

    def test_draft_run_input_field_write_allowed(self):
        """Editing input fields while still draft/computed must NOT be
        blocked, so recompute and normal editing stay possible."""
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        # A computed run is not frozen: input edits pass.
        run.write({'description': 'March 2026 month end'})
        self.assertEqual(run.description, 'March 2026 month end')

    def test_draft_line_write_allowed(self):
        """Users cannot edit engine output; sudo recompute remains allowed."""
        self._post_eur_invoice_balance(1000.0, 1000.0)
        self._set_closing_rate('2026-03-31', 0.8)
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        line = run.line_ids[:1]
        plain = self._plain_user()
        with self.assertRaises(AccessError):
            line.with_user(plain).write({'source_line_count': 99})
        self.assertNotEqual(line.source_line_count, 99)
        # The trusted engine can still rebuild/correct draft output.
        line.sudo().write({'source_line_count': 99})
        self.assertEqual(line.source_line_count, 99)
        # Recompute rebuilds lines (unlink of computed lines is allowed).
        run.action_compute()
        self.assertTrue(run.line_ids)
