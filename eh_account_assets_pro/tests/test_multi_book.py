# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Multi-book + AU tax depreciation tests.
"""

from datetime import date

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from .common import EhAssetTestCase


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestMultiBook(EhAssetTestCase):

    def test_book_can_be_added_with_independent_method(self):
        asset = self._make_asset(method='straight_line')
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'AU Tax book',
            'book_type': 'tax',
            'method': 'diminishing_value',
            'useful_life_months': 60,
            'declining_factor': 2.0,
            'salvage_value': 0.0,
            'prorate_first_period': False,
        })
        self.assertEqual(asset.book_count, 1)
        self.assertEqual(book.method, 'diminishing_value')

    def test_book_compute_schedule_emits_lines(self):
        asset = self._make_asset(method='straight_line')
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'AU Tax book',
            'book_type': 'tax',
            'method': 'prime_cost',
            'useful_life_months': 36,
            'salvage_value': 0.0,
            'prorate_first_period': False,
        })
        book.action_compute_schedule()
        # Prime cost over 36 months on AUD 36k = 1000/month
        self.assertEqual(len(book.line_ids), 36)
        self.assertAlmostEqual(book.line_ids[0].amount, 1000.0, places=2)
        self.assertAlmostEqual(book.total_depreciation, 36000.0, places=2)

    def test_book_prorata_uses_final_stub_instead_of_last_month_catch_up(self):
        asset = self._make_asset(in_service_date='2026-01-31')
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'Prorated tax book',
            'book_type': 'tax',
            'method': 'prime_cost',
            'useful_life_months': 36,
            'salvage_value': 0.0,
            'prorate_first_period': True,
        })
        book.action_compute_schedule()
        lines = book.line_ids.sorted('sequence')
        self.assertEqual(len(lines), 37)
        self.assertAlmostEqual(lines[0].amount, 1000.0 / 31.0, places=2)
        self.assertAlmostEqual(lines[-2].amount, 1000.0, places=2)
        self.assertAlmostEqual(lines[-1].amount, 1000.0 * 30 / 31, places=2)
        self.assertEqual(lines[-1].depreciation_date, date(2029, 1, 31))
        self.assertAlmostEqual(book.total_depreciation, 36000.0, places=2)

    def test_book_diminishing_value_first_period_higher(self):
        asset = self._make_asset(method='straight_line')
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'DV book',
            'book_type': 'tax',
            'method': 'diminishing_value',
            'useful_life_months': 36,
            'declining_factor': 2.0,
            'salvage_value': 0.0,
            'prorate_first_period': False,
        })
        book.action_compute_schedule()
        # First period DV amount > last period DV amount.
        first = book.line_ids[0].amount
        last = book.line_ids[-1].amount
        self.assertGreater(first, last,
                           "Diminishing value should depreciate more in early periods")
        # Total still equals depreciable (cost - salvage).
        self.assertAlmostEqual(book.total_depreciation, 36000.0, places=2)

    def test_parallel_book_gl_flag_is_rejected(self):
        asset = self._make_asset()
        asset.action_activate()
        vals = {
            'asset_id': asset.id,
            'name': 'Reporting tax book',
            'book_type': 'tax',
            'method': 'prime_cost',
            'useful_life_months': 12,
        }
        with self.assertRaises(ValidationError):
            self.env['eh.asset.book'].create(dict(vals, posts_to_gl=True))
        book = self.env['eh.asset.book'].create(vals)
        book.action_compute_schedule()
        with self.assertRaises(UserError):
            book.write({'posts_to_gl': True})
        with self.assertRaises(UserError):
            book.action_post_due_lines()
        self.assertFalse(book.posts_to_gl)
        self.assertFalse(book.line_ids.mapped('move_id'))

    def test_reporting_only_book_cannot_post(self):
        asset = self._make_asset(code='BOOK-REPORT-ONLY')
        asset.action_activate()
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'Reporting-only book',
            'book_type': 'mgmt',
            'method': 'straight_line',
            'useful_life_months': 12,
            'posts_to_gl': False,
        })
        book.action_compute_schedule()
        with self.assertRaises(UserError):
            book.line_ids[0].action_post()
        self.assertFalse(book.line_ids[0].move_id)

    def test_reporting_book_cannot_post_after_asset_enters_pool(self):
        asset = self._make_asset(
            code='BOOK-POOL-GUARD', acquisition_cost=900.0,
        )
        asset.action_activate()
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'Reporting book remains reporting-only after pool transfer',
            'book_type': 'statutory',
            'method': 'straight_line',
            'useful_life_months': 12,
        })
        book.action_compute_schedule()
        pool = self.env['eh.asset.lvp.pool'].create({
            'name': 'Book exclusion pool',
            'company_id': self.company.id,
            'threshold': 1_000.0,
        })
        pool.action_transfer_asset(asset, transfer_date='2026-01-31')
        with self.assertRaises(UserError):
            book.line_ids.sorted('sequence')[0].action_post()

    def test_legacy_gl_book_evidence_is_frozen_and_reversible(self):
        asset = self._make_asset(code='BOOK-GL-WORKFLOW')
        asset.action_activate()
        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'Legacy parallel statutory GL book',
            'book_type': 'statutory',
            'method': 'straight_line',
            'useful_life_months': 12,
        })
        book.action_compute_schedule()
        line = book.line_ids.sorted('sequence')[0]

        # Production-shaped upgrade fixture: prior releases could post a
        # parallel book. Preserve that move as sealed evidence, but simulate
        # the legacy flags through SQL because current ORM correctly refuses
        # to create new GL-enabled books or forge posting identity.
        move = self.env['account.move']._eh_create_sealed({
            'move_type': 'entry',
            'eh_sealed': True,
            'date': line.depreciation_date,
            'journal_id': asset.journal_id.id,
            'ref': 'Legacy parallel-book depreciation',
            'line_ids': [
                (0, 0, {
                    'name': 'Legacy parallel-book depreciation',
                    'account_id': asset.depreciation_account_id.id,
                    'debit': line.amount,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'Legacy parallel-book depreciation',
                    'account_id': asset.accumulated_depreciation_account_id.id,
                    'debit': 0.0,
                    'credit': line.amount,
                }),
            ],
        })
        move.action_post()
        self.env.cr.execute(
            'UPDATE eh_asset_book SET posts_to_gl = TRUE WHERE id = %s',
            (book.id,),
        )
        self.env.cr.execute(
            'UPDATE eh_asset_book_line '
            'SET is_posted = TRUE, move_id = %s, posted_at = NOW(), '
            'posted_by_id = %s WHERE id = %s',
            (move.id, self.env.user.id, line.id),
        )
        book.invalidate_recordset(['posts_to_gl', 'line_ids'])
        line.invalidate_recordset([
            'is_posted', 'move_id', 'posted_at', 'posted_by_id',
        ])
        next_line = book.line_ids.sorted('sequence')[1]
        with self.assertRaises(UserError):
            next_line.action_post()
        with self.assertRaises(UserError):
            book.action_post_due_lines()
        self.assertFalse(next_line.is_posted)
        self.assertFalse(next_line.move_id)
        self.assertTrue(line.is_posted)
        self.assertTrue(line.posted_at)
        self.assertEqual(line.posted_by_id, self.env.user)
        self.assertEqual(move.state, 'posted')
        self.assertTrue(move.eh_sealed)
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')), line.amount, places=2,
        )
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('credit')), line.amount, places=2,
        )
        with self.assertRaises(UserError):
            move.button_draft()

        # A retry/double click is idempotent under the row lock.
        move_count = self.env['account.move'].search_count([])
        line.action_post()
        self.assertEqual(line.move_id, move)
        self.assertEqual(
            self.env['account.move'].search_count([]), move_count,
        )

        # Every source measurement and every parent policy input remains
        # explanatory; exercise the declarations so a newly added field
        # cannot silently fall out of regression coverage.
        for field_name in line._FROZEN_AFTER_POST:
            value = line[field_name]
            if line._fields[field_name].type == 'many2one':
                value = value.id or False
            with self.subTest(model=line._name, field=field_name):
                with self.assertRaises(UserError):
                    line.write({field_name: value})
        with self.assertRaises(UserError):
            line.unlink()
        for field_name in book._FROZEN_AFTER_BOOKING:
            value = book[field_name]
            if book._fields[field_name].type == 'many2one':
                value = value.id or False
            with self.subTest(model=book._name, field=field_name):
                with self.assertRaises(UserError):
                    book.write({field_name: value})
        with self.assertRaises(UserError):
            book.unlink()
        with self.assertRaises(UserError):
            asset.write({'acquisition_cost': asset.acquisition_cost})
        with self.assertRaises(UserError):
            asset.unlink()

        clerk = self._make_non_manager_user()
        with self.assertRaises(AccessError):
            line.with_user(clerk).write({'is_posted': False})
        with self.assertRaises(UserError):
            line.with_user(clerk).action_reverse()

        # Corrections are append-only: seal a counter-entry and keep both
        # immutable links. A reversal retry is also a no-op.
        line.action_reverse()
        reversal = line.reversal_move_id
        self.assertTrue(reversal)
        self.assertEqual(reversal.state, 'posted')
        self.assertTrue(reversal.eh_sealed)
        with self.assertRaises(UserError):
            reversal.button_draft()
        self.assertTrue(line.reversed_at)
        self.assertEqual(line.reversed_by_id, self.env.user)
        reversal_count = self.env['account.move'].search_count([])
        line.action_reverse()
        line.action_post()
        self.assertEqual(line.reversal_move_id, reversal)
        self.assertEqual(
            self.env['account.move'].search_count([]), reversal_count,
        )
        self.assertAlmostEqual(
            sum((move | reversal).line_ids.mapped('balance')), 0.0, places=2,
        )

    def test_books_independent_of_primary_schedule(self):
        """Adding a tax book does not modify the primary GL schedule."""
        asset = self._make_asset(method='straight_line')
        asset.action_compute_schedule()
        primary_count = len(asset.depreciation_line_ids)
        primary_total = sum(asset.depreciation_line_ids.mapped('amount'))

        book = self.env['eh.asset.book'].create({
            'asset_id': asset.id,
            'name': 'Tax book',
            'book_type': 'tax',
            'method': 'diminishing_value',
            'useful_life_months': 60,
            'salvage_value': 0.0,
        })
        book.action_compute_schedule()
        self.assertEqual(len(asset.depreciation_line_ids), primary_count)
        self.assertAlmostEqual(
            sum(asset.depreciation_line_ids.mapped('amount')),
            primary_total, places=2,
        )

    def test_book_salvage_above_cost_blocked(self):
        asset = self._make_asset(acquisition_cost=10000.0)
        with self.assertRaises(Exception):
            self.env['eh.asset.book'].create({
                'asset_id': asset.id,
                'name': 'Bad salvage',
                'book_type': 'tax',
                'method': 'prime_cost',
                'useful_life_months': 12,
                'salvage_value': 99999.0,
            })


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestAuPrimaryMethods(EhAssetTestCase):

    def test_prime_cost_on_primary_matches_straight_line(self):
        a1 = self._make_asset(method='straight_line', useful_life_months=12)
        a2 = self._make_asset(
            method='prime_cost', useful_life_months=12, code='IT-PC-1',
        )
        a1.action_compute_schedule()
        a2.action_compute_schedule()
        # Total depreciation identical; per-period amounts identical.
        self.assertEqual(len(a1.depreciation_line_ids),
                         len(a2.depreciation_line_ids))
        self.assertAlmostEqual(
            sum(a1.depreciation_line_ids.mapped('amount')),
            sum(a2.depreciation_line_ids.mapped('amount')),
            places=2,
        )

    def test_diminishing_value_on_primary_uses_factor_2(self):
        a = self._make_asset(
            method='diminishing_value',
            useful_life_months=36,
            declining_factor=2.0,
        )
        a.action_compute_schedule()
        # First period > last period.
        first = a.depreciation_line_ids[0].amount
        last = a.depreciation_line_ids[-1].amount
        self.assertGreater(first, last)


@tagged('eh_account_assets_pro', 'integration', 'post_install', '-at_install')
class TestInstantWriteOff(EhAssetTestCase):

    def test_instant_write_off_emits_one_line(self):
        a = self._make_asset(
            acquisition_cost=18000.0,
            salvage_value=0.0,
            is_instant_write_off=True,
            method='straight_line',  # ignored when instant write-off
            useful_life_months=60,
        )
        a.action_compute_schedule()
        self.assertEqual(len(a.depreciation_line_ids), 1)
        self.assertAlmostEqual(
            a.depreciation_line_ids[0].amount, 18000.0, places=2,
        )

    def test_instant_write_off_honours_salvage(self):
        a = self._make_asset(
            acquisition_cost=20000.0,
            salvage_value=2000.0,
            is_instant_write_off=True,
        )
        a.action_compute_schedule()
        self.assertEqual(len(a.depreciation_line_ids), 1)
        self.assertAlmostEqual(
            a.depreciation_line_ids[0].amount, 18000.0, places=2,
        )

    def test_instant_write_off_overrides_useful_life(self):
        """Even with useful_life_months=120, only one line emits."""
        a = self._make_asset(
            acquisition_cost=15000.0,
            is_instant_write_off=True,
            useful_life_months=120,
        )
        a.action_compute_schedule()
        self.assertEqual(len(a.depreciation_line_ids), 1)
