# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Golden tests: AOCI sub-reserve architecture at year-end close (IAS 1.106).

Hand-derived worked examples, exact-2dp journal-entry assertions:

* AOCI roll: revaluation surplus +10,000 and CTA +4,000 accumulate during
  the year alongside a 50,000 P&L profit. The close credits retained
  earnings exactly 50,000 (P&L only), sweeps 10,000 into the revaluation
  sub-reserve and 4,000 into the CTA sub-reserve, and zeroes the flow
  accounts. Exact JE asserted, no unexpected lines.
* No double-move: CTA +4,000 accumulates and is fully recycled to P&L in
  the same year (IAS 21.48 disposal reclass posted by another module).
  Net flow-account movement is zero, so the close moves nothing for CTA;
  the recycled gain reaches retained earnings through P&L only.
* Unmapped-OCI governance: a flow account on an incomplete mapping row
  (no sub-reserve account) blocks posting; a manager override with a
  documented reason posts and logs the reason in the chatter.
* Lock governance: lock_after_post defaults True; disabling requires a
  reason, logged in the chatter on post.
* Chronology: a later fiscal year already closed blocks posting an
  earlier year's close; reversing the later close unblocks it.
* RE purity (the audit's core finding): retained earnings receives ONLY
  the P&L result, never an OCI component, in profit and in loss years.

Convention: company currency (USD, 2dp), fiscal year = calendar year,
ledger-signed balances (debit positive). OCI gains accumulate as credits
on the flow accounts; the close reclassifies the NET period movement.
"""

from datetime import date

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_year_end.models.aoci_reserve_map import (
    AOCI_KINDS,
)


@tagged('eh_golden', 'eh_account_year_end', 'post_install', '-at_install')
class TestGoldenYearEndAoci(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Run = cls.env['eh.year.end.run']
        cls.Map = cls.env['eh.aoci.reserve.map']
        cls.fy_start = date(2026, 1, 1)
        cls.fy_end = date(2026, 12, 31)

        cls.retained_earnings = cls._ensure_account(
            cls.env, '3100', 'Retained Earnings', 'equity')
        # OCI flow accounts (what the suite modules post into in-year).
        cls.flow_reval = cls._ensure_account(
            cls.env, '3901', 'Revaluation Surplus Flow', 'equity')
        cls.flow_cta = cls._ensure_account(
            cls.env, '3902', 'FX Translation Reserve Flow', 'equity')
        # Dedicated AOCI sub-reserve accounts (per-component carrying).
        cls.res_reval = cls._ensure_account(
            cls.env, '3911', 'AOCI Revaluation Reserve', 'equity')
        cls.res_cta = cls._ensure_account(
            cls.env, '3912', 'AOCI CTA Reserve', 'equity')
        # P&L account taking a recycled CTA gain (IAS 21.48).
        cls.recycle_gain = cls._ensure_account(
            cls.env, '4905', 'Reclassification Gains', 'income_other')

        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @classmethod
    def _post_at(cls, on_date, lines):
        return cls.post_balanced_move(lines, date=on_date)

    def _run(self, **overrides):
        vals = {
            'fiscal_year_start': self.fy_start,
            'fiscal_year_end': self.fy_end,
            'company_id': self.env.company.id,
            'journal_id': self.journal_misc.id,
            'retained_earnings_account_id': self.retained_earnings.id,
            'lock_after_post': False,
            'no_lock_reason': 'golden fixture: lock exercised separately',
        }
        vals.update(overrides)
        return self.Run.create(vals)

    def _map(self, kind, sources, reserve=None):
        return self.Map.create({
            'company_id': self.env.company.id,
            'kind': kind,
            'source_account_ids': [(6, 0, [a.id for a in sources])],
            'reserve_account_id': reserve.id if reserve else False,
        })

    # ------------------------------------------------------------------
    # 1. AOCI roll golden (IAS 1.106)
    # ------------------------------------------------------------------
    def test_golden_aoci_roll_close(self):
        """Profit 50,000; revaluation OCI +10,000; CTA OCI +4,000.

        Derivation (all hand-computed from the postings below):
        * P&L close: income 4000 carries a 50,000 credit balance ->
          close debits 4000 by 50,000 and credits retained earnings
          50,000 (the whole net profit, nothing else).
        * Revaluation flow 3901 net period movement = -10,000 (credit)
          -> close debits 3901 by 10,000, credits sub-reserve 3911
          by 10,000.
        * CTA flow 3902 net period movement = -4,000 (credit) -> close
          debits 3902 by 4,000, credits sub-reserve 3912 by 4,000.
        Entry balances: debits 50,000 + 10,000 + 4,000 = credits
        50,000 + 10,000 + 4,000 = 64,000.
        """
        self._post_at(date(2026, 3, 1), [
            {'account': self.account_revenue, 'credit': 50000.0},
            {'account': self.account_cash, 'debit': 50000.0},
        ])
        # OCI gains posted by "other modules" during the year.
        self._post_at(date(2026, 5, 1), [
            {'account': self.flow_reval, 'credit': 10000.0},
            {'account': self.account_cash, 'debit': 10000.0},
        ])
        self._post_at(date(2026, 7, 1), [
            {'account': self.flow_cta, 'credit': 4000.0},
            {'account': self.account_cash, 'debit': 4000.0},
        ])
        self._map('revaluation_surplus', [self.flow_reval], self.res_reval)
        self._map('cta', [self.flow_cta], self.res_cta)

        run = self._run()
        run.action_compute()
        # Snapshot: P&L result excludes OCI; OCI total is gain-positive.
        self.assertAlmostEqual(run.net_profit, 50000.0, places=2)
        self.assertAlmostEqual(run.total_oci_reclass, 14000.0, places=2)
        oci_lines = run.line_ids.filtered(lambda l: l.line_kind == 'oci')
        self.assertEqual(len(oci_lines), 2)
        self.assertFalse(run.has_unmapped_oci)

        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.account_revenue, 50000.0, 0.0),
            (self.retained_earnings, 0.0, 50000.0),
            (self.flow_reval, 10000.0, 0.0),
            (self.res_reval, 0.0, 10000.0),
            (self.flow_cta, 4000.0, 0.0),
            (self.res_cta, 0.0, 4000.0),
        ])
        self.assertBalanced(run.move_id)
        # Sources zeroed: net posted balance of each flow account is nil
        # after the close; the sub-reserves now carry the components.
        self.assertAlmostEqual(
            self.posted_balance(self.flow_reval), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.flow_cta), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.res_reval), -10000.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.res_cta), -4000.0, places=2)

    # ------------------------------------------------------------------
    # 2. Recycling: net movement only, never a double move (IAS 21.48)
    # ------------------------------------------------------------------
    def test_golden_cta_recycle_no_double_move(self):
        """CTA +4,000 accumulates, then fully recycles to P&L in-year.

        Derivation:
        * 1 Mar: translation gain credits CTA flow 3902 by 4,000.
        * 1 Sep: disposal of the foreign operation; the FX module
          reclassifies the reserve to P&L (IAS 21.48): debit 3902
          4,000, credit recycling gain 4905 4,000.
        * Net 3902 period movement = -4,000 + 4,000 = 0 -> the close
          moves NOTHING for CTA (moving the gross 4,000 again would
          double-count the recycled amount in equity).
        * P&L close: revenue 4000 (10,000 credit) + recycled gain 4905
          (4,000 credit) -> net profit 14,000, all to retained
          earnings through P&L.
        """
        self._post_at(date(2026, 2, 1), [
            {'account': self.account_revenue, 'credit': 10000.0},
            {'account': self.account_cash, 'debit': 10000.0},
        ])
        self._post_at(date(2026, 3, 1), [
            {'account': self.flow_cta, 'credit': 4000.0},
            {'account': self.account_cash, 'debit': 4000.0},
        ])
        # Recycle posted by "another module" (disposal reclass).
        self._post_at(date(2026, 9, 1), [
            {'account': self.flow_cta, 'debit': 4000.0},
            {'account': self.recycle_gain, 'credit': 4000.0},
        ])
        self._map('cta', [self.flow_cta], self.res_cta)

        run = self._run()
        run.action_compute()
        self.assertAlmostEqual(run.net_profit, 14000.0, places=2)
        # Net CTA movement is zero: no OCI reclassification row at all.
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.line_kind == 'oci'))
        self.assertAlmostEqual(run.total_oci_reclass, 0.0, places=2)

        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.account_revenue, 10000.0, 0.0),
            (self.recycle_gain, 4000.0, 0.0),
            (self.retained_earnings, 0.0, 14000.0),
        ])
        # The CTA flow account and its sub-reserve are untouched by the
        # close: the recycled amount reached RE through P&L exactly once.
        self.assertAlmostEqual(
            self.posted_balance(self.flow_cta), 0.0, places=2)
        self.assertAlmostEqual(
            self.posted_balance(self.res_cta), 0.0, places=2)

    # ------------------------------------------------------------------
    # 3. Unmapped-OCI governance: blocking with override
    # ------------------------------------------------------------------
    def test_unmapped_oci_blocks_and_override(self):
        self._post_at(date(2026, 2, 1), [
            {'account': self.account_revenue, 'credit': 5000.0},
            {'account': self.account_cash, 'debit': 5000.0},
        ])
        self._post_at(date(2026, 3, 1), [
            {'account': self.flow_cta, 'credit': 4000.0},
            {'account': self.account_cash, 'debit': 4000.0},
        ])
        # Incomplete row: source listed, sub-reserve missing.
        self._map('cta', [self.flow_cta], reserve=None)

        run = self._run()
        run.action_compute()
        self.assertTrue(run.has_unmapped_oci)
        self.assertIn('3902', run.unmapped_oci_note)
        # Incomplete row produces no reclassification rows either.
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.line_kind == 'oci'))

        # Blocked without override.
        with self.assertRaises(UserError):
            run.action_post()
        self.assertEqual(run.state, 'computed')
        # Override without a reason is still blocked.
        run.override_unmapped_oci = True
        with self.assertRaises(UserError):
            run.action_post()
        # Override with a documented reason posts and logs the reason.
        run.override_unmapped_reason = (
            'CTA reserve account opened late; mapped next period')
        run.action_post()
        self.assertEqual(run.state, 'posted')
        # The close stayed pure P&L: no leg on the unmapped flow account.
        self.assertMoveLines(run.move_id, [
            (self.account_revenue, 5000.0, 0.0),
            (self.retained_earnings, 0.0, 5000.0),
        ])
        messages = self.env['mail.message'].search([
            ('model', '=', 'eh.year.end.run'),
            ('res_id', '=', run.id),
        ])
        self.assertTrue(any(
            'CTA reserve account opened late' in (m.body or '')
            for m in messages))

    def test_unmapped_ignored_without_movement(self):
        """A known OCI account that did not move in the period cannot
        commingle anything, so it never warns nor blocks."""
        self._post_at(date(2026, 2, 1), [
            {'account': self.account_revenue, 'credit': 5000.0},
            {'account': self.account_cash, 'debit': 5000.0},
        ])
        self._map('cta', [self.flow_cta], reserve=None)  # incomplete
        run = self._run()
        run.action_compute()
        self.assertFalse(run.has_unmapped_oci)
        run.action_post()
        self.assertEqual(run.state, 'posted')

    # ------------------------------------------------------------------
    # 4. Lock governance
    # ------------------------------------------------------------------
    def test_lock_after_post_defaults_true(self):
        run = self.Run.create({
            'fiscal_year_start': self.fy_start,
            'fiscal_year_end': self.fy_end,
            'company_id': self.env.company.id,
            'journal_id': self.journal_misc.id,
            'retained_earnings_account_id': self.retained_earnings.id,
        })
        self.assertTrue(run.lock_after_post)

    def test_lock_disable_requires_reason_and_logs_chatter(self):
        self._post_at(date(2026, 2, 1), [
            {'account': self.account_revenue, 'credit': 5000.0},
            {'account': self.account_cash, 'debit': 5000.0},
        ])
        run = self._run(no_lock_reason=False)  # lock off, no reason
        run.action_compute()
        with self.assertRaises(UserError):
            run.action_post()
        self.assertEqual(run.state, 'computed')

        lock_before = self.env.company.fiscalyear_lock_date
        run.no_lock_reason = 'Group close performed in consolidation system'
        run.action_post()
        self.assertEqual(run.state, 'posted')
        # Lock date untouched, reason logged in the chatter.
        self.assertEqual(
            self.env.company.fiscalyear_lock_date, lock_before)
        messages = self.env['mail.message'].search([
            ('model', '=', 'eh.year.end.run'),
            ('res_id', '=', run.id),
        ])
        self.assertTrue(any(
            'Group close performed in consolidation system'
            in (m.body or '') for m in messages))

    def test_chronology_guard_blocks_earlier_year(self):
        """A later year's posted close blocks an earlier close; reversing
        the later close unblocks it."""
        self._post_at(date(2027, 2, 1), [
            {'account': self.account_revenue, 'credit': 7000.0},
            {'account': self.account_cash, 'debit': 7000.0},
        ])
        run27 = self._run(
            fiscal_year_start=date(2027, 1, 1),
            fiscal_year_end=date(2027, 12, 31))
        run27.action_compute()
        run27.action_post()
        self.assertEqual(run27.state, 'posted')

        # A back-dated 2026 adjustment after the later close exercises the
        # chronology guard without violating the separate prior-P&L gate.
        self._post_at(date(2026, 2, 1), [
            {'account': self.account_revenue, 'credit': 5000.0},
            {'account': self.account_cash, 'debit': 5000.0},
        ])
        run26 = self._run()
        run26.action_compute()
        with self.assertRaises(UserError):
            run26.action_post()
        self.assertEqual(run26.state, 'computed')

        # A reversed later close no longer stands: 2026 may then post.
        run27.action_reverse()
        self.assertEqual(run27.state, 'reversed')
        run26.action_post()
        self.assertEqual(run26.state, 'posted')

    # ------------------------------------------------------------------
    # 5. Retained-earnings purity (the audit's core finding)
    # ------------------------------------------------------------------
    def test_re_purity_loss_year_with_oci_gain(self):
        """Loss year with an OCI gain: RE takes ONLY the 6,000 loss.

        Derivation: revenue 2,000 credit; expense 8,000 debit -> net
        loss 6,000 -> RE debited 6,000. Revaluation OCI +10,000 goes
        to its sub-reserve, never to RE. Entry balances: debits
        2,000 + 6,000 + 10,000 = credits 8,000 + 10,000 = 18,000.
        """
        self._post_at(date(2026, 2, 1), [
            {'account': self.account_revenue, 'credit': 2000.0},
            {'account': self.account_cash, 'debit': 2000.0},
        ])
        self._post_at(date(2026, 3, 1), [
            {'account': self.account_expense, 'debit': 8000.0},
            {'account': self.account_cash, 'credit': 8000.0},
        ])
        self._post_at(date(2026, 5, 1), [
            {'account': self.flow_reval, 'credit': 10000.0},
            {'account': self.account_cash, 'debit': 10000.0},
        ])
        self._map('revaluation_surplus', [self.flow_reval], self.res_reval)

        run = self._run()
        run.action_compute()
        self.assertAlmostEqual(run.net_profit, -6000.0, places=2)
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.account_revenue, 2000.0, 0.0),
            (self.account_expense, 0.0, 8000.0),
            (self.retained_earnings, 6000.0, 0.0),
            (self.flow_reval, 10000.0, 0.0),
            (self.res_reval, 0.0, 10000.0),
        ])
        # RE purity: exactly one RE leg, equal to the P&L result.
        re_legs = run.move_id.line_ids.filtered(
            lambda l: l.account_id == self.retained_earnings)
        self.assertEqual(len(re_legs), 1)
        self.assertAlmostEqual(re_legs.debit, 6000.0, places=2)
        self.assertAlmostEqual(re_legs.credit, 0.0, places=2)

    def test_re_purity_profit_year_regression(self):
        """RE receives exactly net profit even when OCI moves both ways."""
        self._post_at(date(2026, 2, 1), [
            {'account': self.account_revenue, 'credit': 12000.0},
            {'account': self.account_cash, 'debit': 12000.0},
        ])
        # Revaluation gain +3,000; CTA loss -1,000 (debit movement).
        self._post_at(date(2026, 4, 1), [
            {'account': self.flow_reval, 'credit': 3000.0},
            {'account': self.account_cash, 'debit': 3000.0},
        ])
        self._post_at(date(2026, 6, 1), [
            {'account': self.flow_cta, 'debit': 1000.0},
            {'account': self.account_cash, 'credit': 1000.0},
        ])
        self._map('revaluation_surplus', [self.flow_reval], self.res_reval)
        self._map('cta', [self.flow_cta], self.res_cta)

        run = self._run()
        run.action_compute()
        self.assertAlmostEqual(run.net_profit, 12000.0, places=2)
        # OCI total is gain-positive: +3,000 - 1,000 = +2,000.
        self.assertAlmostEqual(run.total_oci_reclass, 2000.0, places=2)
        run.action_post()
        # CTA loss: flow account is credited back to zero, sub-reserve
        # debited (a debit AOCI balance = accumulated OCI loss).
        self.assertMoveLines(run.move_id, [
            (self.account_revenue, 12000.0, 0.0),
            (self.retained_earnings, 0.0, 12000.0),
            (self.flow_reval, 3000.0, 0.0),
            (self.res_reval, 0.0, 3000.0),
            (self.flow_cta, 0.0, 1000.0),
            (self.res_cta, 1000.0, 0.0),
        ])
        re_legs = run.move_id.line_ids.filtered(
            lambda l: l.account_id == self.retained_earnings)
        self.assertEqual(len(re_legs), 1)
        self.assertAlmostEqual(re_legs.credit, 12000.0, places=2)

    # ------------------------------------------------------------------
    # 6. Pairwise-style property: every kind x both directions
    # ------------------------------------------------------------------
    def test_property_all_kinds_roll(self):
        """All six AOCI kinds close to their own sub-reserve.

        Deterministic construction: kind i moves (i+1) * 1,000; even
        ranks accumulate a gain (credit flow), odd ranks a loss (debit
        flow). Expected legs follow directly from the construction, and
        retained earnings still receives only the 9,000 P&L profit.
        """
        self._post_at(date(2026, 2, 1), [
            {'account': self.account_revenue, 'credit': 9000.0},
            {'account': self.account_cash, 'debit': 9000.0},
        ])
        expected = [
            (self.account_revenue, 9000.0, 0.0),
            (self.retained_earnings, 0.0, 9000.0),
        ]
        for rank, (kind, _label) in enumerate(AOCI_KINDS):
            amount = (rank + 1) * 1000.0
            flow = self._ensure_account(
                self.env, '39%d1' % (rank + 2),
                'Flow %s' % kind, 'equity')
            reserve = self._ensure_account(
                self.env, '39%d2' % (rank + 2),
                'Reserve %s' % kind, 'equity')
            if rank % 2 == 0:
                # Gain: credit flow in-year; close debits flow, credits
                # reserve.
                self._post_at(date(2026, 4, 1), [
                    {'account': flow, 'credit': amount},
                    {'account': self.account_cash, 'debit': amount},
                ])
                expected.append((flow, amount, 0.0))
                expected.append((reserve, 0.0, amount))
            else:
                # Loss: debit flow in-year; close credits flow, debits
                # reserve.
                self._post_at(date(2026, 4, 1), [
                    {'account': flow, 'debit': amount},
                    {'account': self.account_cash, 'credit': amount},
                ])
                expected.append((flow, 0.0, amount))
                expected.append((reserve, amount, 0.0))
            self._map(kind, [flow], reserve)

        run = self._run()
        run.action_compute()
        self.assertEqual(
            len(run.line_ids.filtered(lambda l: l.line_kind == 'oci')),
            len(AOCI_KINDS))
        run.action_post()
        self.assertMoveLines(run.move_id, expected)
        self.assertBalanced(run.move_id)

    # ------------------------------------------------------------------
    # 7. Old behaviour preserved: no mapping rows -> pure P&L close
    # ------------------------------------------------------------------
    def test_no_maps_close_unchanged(self):
        """With no mapping rows, equity movement (even on an OCI-looking
        account) neither warns nor moves: the close is the original pure
        P&L close."""
        self._post_at(date(2026, 2, 1), [
            {'account': self.account_revenue, 'credit': 5000.0},
            {'account': self.account_cash, 'debit': 5000.0},
        ])
        self._post_at(date(2026, 3, 1), [
            {'account': self.flow_reval, 'credit': 2500.0},
            {'account': self.account_cash, 'debit': 2500.0},
        ])
        run = self._run()
        run.action_compute()
        self.assertFalse(run.has_unmapped_oci)
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.line_kind == 'oci'))
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.account_revenue, 5000.0, 0.0),
            (self.retained_earnings, 0.0, 5000.0),
        ])

    # ------------------------------------------------------------------
    # 8. Mapping governance and discovery
    # ------------------------------------------------------------------
    def test_map_constraints(self):
        self._map('cta', [self.flow_cta], self.res_cta)
        # One row per company per kind (DB unique constraint; savepoint
        # keeps the transaction usable after the integrity error).
        with self.assertRaises(Exception), \
                mute_logger('odoo.sql_db'), self.env.cr.savepoint():
            self._map('cta', [self.flow_reval], self.res_reval)
        # A source may feed exactly one sub-reserve.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._map('other', [self.flow_cta], self.res_reval)
        # The reserve cannot be one of its own sources.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._map('revaluation_surplus',
                      [self.res_reval], self.res_reval)
        # Sources must be equity accounts.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._map('revaluation_surplus',
                      [self.account_revenue], self.res_reval)

    def test_discovery_returns_all_kinds(self):
        found = self.Map._discover_oci_sources(self.env.company)
        self.assertEqual(
            set(found), {kind for kind, _label in AOCI_KINDS})

    def test_seed_from_modules_cta(self):
        """Seeding picks up a CTA position's equity account when the FX
        module is installed; idempotent on re-run."""
        if 'eh.fx.cta.position' not in self.env:
            self.skipTest('eh_account_fx_revaluation not installed')
        self.env['eh.fx.cta.position'].create({
            'name': 'Net investment in DE subsidiary',
            'company_id': self.env.company.id,
            'cta_account_id': self.flow_cta.id,
        })
        touched = self.Map.action_seed_from_modules(self.env.company)
        row = self.Map.search([
            ('company_id', '=', self.env.company.id),
            ('kind', '=', 'cta'),
        ])
        self.assertEqual(len(row), 1)
        self.assertIn(self.flow_cta, row.source_account_ids)
        self.assertIn(row, touched)
        # Seeded rows carry no reserve yet: the user completes them.
        self.assertFalse(row.reserve_account_id)
        # Idempotent: re-seeding neither duplicates the row nor re-adds
        # the already-claimed source.
        ids_before = sorted(row.source_account_ids.ids)
        self.Map.action_seed_from_modules(self.env.company)
        rows = self.Map.search([
            ('company_id', '=', self.env.company.id),
            ('kind', '=', 'cta'),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows.source_account_ids.ids), ids_before)
