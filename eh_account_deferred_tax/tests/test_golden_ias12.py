# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Golden IAS 12 worked examples for eh_account_deferred_tax.

Hand-computed worked examples in the shape of the IAS 12 illustrative
material (numbers only, recomputed by hand from the inputs stated in each
test): enacted-rate table resolution at the reporting date (IAS 12.47-48),
rate-change remeasurement of opening balances split from origination
(IAS 12.60(b)), DTA/DTL offsetting per jurisdiction (IAS 12.74), the
effective-tax-rate reconciliation (IAS 12.81(c)), and carryforward expiry /
run-level recoverability ceilings (IAS 12.36(a), 12.81(e)). Every expected
amount is derived in a comment; nothing is read back from the engine to
build an expectation.

Engine conventions asserted here (from models/deferred_tax_*.py):
* Table resolution: latest effective_from on or before the run's reporting
  date, ignoring rows enacted after that date; no table row -> the run's
  statutory rate seeds an empty line rate (historic fallback).
* Rate-change effect is stated on the net liability (positive = charge):
  opening balance x (closing rate / opening rate - 1), DTA side negated.
* A carryforward expiring ON the reporting date counts as expired.
* The run-level projected-profit ceiling is DISCLOSURE (feeds the
  unrecognised row); posting recognition is driven by the line-level caps.
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_golden', 'eh_account_deferred_tax', 'post_install', '-at_install')
class TestGoldenIas12(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.dta = cls._ensure_account(
            cls.env, '1810', 'Deferred Tax Asset', 'asset_non_current')
        cls.dtl = cls._ensure_account(
            cls.env, '2810', 'Deferred Tax Liability', 'liability_non_current')
        cls.dtax_expense = cls._ensure_account(
            cls.env, '5810', 'Deferred Tax Expense', 'expense')
        cls.oci = cls._ensure_account(
            cls.env, '3810', 'OCI Reserve', 'equity')

    def _run(self, rate=25.0, period_end='2026-12-31', **vals):
        base = {
            'statutory_rate': rate,
            'period_end': period_end,
            'dta_account_id': self.dta.id,
            'dtl_account_id': self.dtl.id,
            'deferred_tax_expense_account_id': self.dtax_expense.id,
            'oci_account_id': self.oci.id,
            'journal_id': self.journal_misc.id,
        }
        base.update(vals)
        return self.env['eh.deferred.tax.run'].create(base)

    def _jurisdiction(self, name, rates=()):
        """rates: iterable of (effective_from, rate, enacted_date)."""
        jur = self.env['eh.tax.jurisdiction'].create({
            'name': name, 'company_id': self.company.id,
        })
        for effective_from, rate, enacted in rates:
            self.env['eh.tax.rate'].create({
                'jurisdiction_id': jur.id,
                'effective_from': effective_from,
                'rate': rate,
                'enacted_date': enacted,
            })
        return jur

    def _line(self, run, **vals):
        base = {'run_id': run.id, 'nature': 'asset'}
        base.update(vals)
        return self.env['eh.deferred.tax.line'].create(base)

    def _recon_by_kind(self, run):
        return {r.kind: r.amount for r in run.recon_line_ids}

    # ------------------------------------------------------------------
    # 1. Enacted-rate table resolution (IAS 12.47-48)
    # ------------------------------------------------------------------
    def test_golden_rate_table_resolution(self):
        """The line rate is the jurisdiction's latest enacted rate whose
        effective date is on or before the reporting date.

        Table: 30% effective 2020-01-01 (enacted 2019-06-01); 28% effective
        2026-01-01 (enacted 2025-09-15); a 10% row effective 2025-06-30 but
        only enacted 2026-03-01.

        Run at 2026-12-31 -> 28% (latest effective row, enacted in time):
        taxable diff 1000 x 28% = 280 DTL.
        Run at 2025-12-31 -> 30%: the 28% row is not yet effective and the
        10% row was not enacted by that date (IAS 12.48 substantive-
        enactment cut-off), so 1000 x 30% = 300 DTL.
        No table rows (company default jurisdiction) -> statutory 25%:
        1000 x 25% = 250 DTL, the pre-table fallback behaviour.
        """
        jur = self._jurisdiction('Mainland', rates=[
            ('2020-01-01', 30.0, '2019-06-01'),
            ('2026-01-01', 28.0, '2025-09-15'),
            ('2025-06-30', 10.0, '2026-03-01'),
        ])

        run_2026 = self._run(rate=25.0, period_end='2026-12-31')
        line_t = self._line(
            run_2026, name='Plant', carrying_amount=1000.0, tax_base=0.0,
            jurisdiction_id=jur.id)
        line_fallback = self._line(
            run_2026, name='Other', carrying_amount=1000.0, tax_base=0.0)
        run_2026.action_compute()
        self.assertAlmostEqual(line_t.tax_rate, 28.0, places=3)
        self.assertAlmostEqual(line_t.closing_dtl, 280.0, places=2)
        # The line created without a jurisdiction fell into the auto-created
        # company default, which has no rate rows -> statutory fallback.
        self.assertTrue(line_fallback.jurisdiction_id)
        self.assertTrue(line_fallback.jurisdiction_id.is_company_default)
        self.assertAlmostEqual(line_fallback.tax_rate, 25.0, places=3)
        self.assertAlmostEqual(line_fallback.closing_dtl, 250.0, places=2)

        run_2025 = self._run(rate=25.0, period_end='2025-12-31')
        line_p = self._line(
            run_2025, name='Plant', carrying_amount=1000.0, tax_base=0.0,
            jurisdiction_id=jur.id)
        run_2025.action_compute()
        self.assertAlmostEqual(line_p.tax_rate, 30.0, places=3)
        self.assertAlmostEqual(line_p.closing_dtl, 300.0, places=2)

    def test_golden_manual_override_needs_reason(self):
        """A manual rate override beats the table but must carry a reason
        (IAS 12 departure documentation)."""
        jur = self._jurisdiction('Mainland', rates=[
            ('2020-01-01', 30.0, '2019-06-01'),
        ])
        run = self._run(rate=25.0)
        line = self._line(
            run, name='Special regime', carrying_amount=1000.0,
            tax_base=0.0, jurisdiction_id=jur.id,
            manual_rate=15.0, manual_rate_reason='Concessional rate ruling')
        run.action_compute()
        # Override 15% beats the 30% table row: 1000 x 15% = 150 DTL.
        self.assertAlmostEqual(line.tax_rate, 15.0, places=3)
        self.assertAlmostEqual(line.closing_dtl, 150.0, places=2)
        # Reason is mandatory with an override.
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._line(
                run, name='No reason', carrying_amount=100.0, tax_base=0.0,
                jurisdiction_id=jur.id, manual_rate=15.0)

    # ------------------------------------------------------------------
    # 2. Rate-change remeasurement (IAS 12.47/60(b))
    # ------------------------------------------------------------------
    def test_golden_rate_change_remeasurement(self):
        """Opening balances measured at 30% are remeasured at the newly
        enacted 28%; the rate-change component is disclosed separately from
        origination and the posted movement covers both.

        Line A: taxable diff 1000 held flat. Opening DTL 300 (= 1000 x 30%).
        Closing DTL = 1000 x 28% = 280. Movement = 280 - 300 = -20.
        Rate change = 300 x (28/30 - 1) = -20. Origination = -20 - (-20) = 0.

        Line B: taxable diff grew to 1200. Opening DTL 300 at 30%.
        Closing DTL = 1200 x 28% = 336. Movement = +36.
        Rate change = 300 x (28/30 - 1) = -20. Origination = 36 - (-20) = 56.

        Run: rate_change_pl = -40; net DTL movement = -20 + 36 = +16, so the
        entry is Cr DTL 16 / Dr deferred tax expense 16.
        """
        jur = self._jurisdiction('Mainland', rates=[
            ('2020-01-01', 30.0, '2019-06-01'),
            ('2026-01-01', 28.0, '2025-09-15'),
        ])
        run = self._run(rate=30.0, period_end='2026-12-31')
        line_a = self._line(
            run, name='Flat difference', carrying_amount=1000.0,
            tax_base=0.0, jurisdiction_id=jur.id,
            opening_dtl=300.0, opening_rate=30.0)
        line_b = self._line(
            run, name='Grown difference', carrying_amount=1200.0,
            tax_base=0.0, jurisdiction_id=jur.id,
            opening_dtl=300.0, opening_rate=30.0)
        run.action_compute()

        self.assertAlmostEqual(line_a.closing_rate, 28.0, places=3)
        self.assertAlmostEqual(line_a.closing_dtl, 280.0, places=2)
        self.assertAlmostEqual(line_a.movement_dtl, -20.0, places=2)
        self.assertAlmostEqual(line_a.rate_change_effect, -20.0, places=2)
        self.assertAlmostEqual(line_a.origination_effect, 0.0, places=2)

        self.assertAlmostEqual(line_b.closing_dtl, 336.0, places=2)
        self.assertAlmostEqual(line_b.movement_dtl, 36.0, places=2)
        self.assertAlmostEqual(line_b.rate_change_effect, -20.0, places=2)
        self.assertAlmostEqual(line_b.origination_effect, 56.0, places=2)

        self.assertAlmostEqual(run.rate_change_pl, -40.0, places=2)
        self.assertAlmostEqual(run.rate_change_oci, 0.0, places=2)
        # The reconciliation carries a dedicated rate-change row.
        self.assertAlmostEqual(
            self._recon_by_kind(run).get('rate_change'), -40.0, places=2)

        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.dtl, 0.0, 16.0),
            (self.dtax_expense, 16.0, 0.0),
        ])
        self.assertBalanced(run.move_id)

    def test_golden_rate_change_routes_to_oci(self):
        """IAS 12.61A/63: the rate-change remeasurement of an OCI-related
        balance stays in OCI.

        Revaluation surplus difference: taxable diff 1000 flat, opening DTL
        300 at 30%, closing rate 28% -> movement -20, all rate change,
        flagged through OCI. Entry: Dr DTL 20 / Cr OCI reserve 20.
        """
        jur = self._jurisdiction('Mainland', rates=[
            ('2026-01-01', 28.0, '2025-09-15'),
        ])
        run = self._run(rate=30.0, period_end='2026-12-31')
        self._line(
            run, name='Revaluation surplus', carrying_amount=1000.0,
            tax_base=0.0, jurisdiction_id=jur.id, through_oci=True,
            opening_dtl=300.0, opening_rate=30.0)
        run.action_compute()
        self.assertAlmostEqual(run.rate_change_oci, -20.0, places=2)
        self.assertAlmostEqual(run.rate_change_pl, 0.0, places=2)
        # No P&L rate-change row: the OCI remeasurement never touches the
        # tax expense the reconciliation explains.
        self.assertNotIn('rate_change', self._recon_by_kind(run))
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.dtl, 20.0, 0.0),
            (self.oci, 0.0, 20.0),
        ])

    def test_golden_opening_rate_rolls_forward(self):
        """The opening rate rolls forward from the prior posted run's
        applied rate, so a statutory rate change between periods discloses
        the remeasurement without any re-keying.

        2025 run at 30%: taxable diff 1000 -> DTL 300, posted. A tax-loss
        line flagged not recoverable discloses 400 x 30% = 120 unrecognised.
        2026 run at 28%: same difference name -> opening DTL 300 and opening
        rate 30 roll forward; closing 280; rate change -20, origination 0.
        The opening unrecognised-DTA figure rolls to 120; with no
        unrecognised DTA left this period the reconciliation releases -120.
        """
        prior = self._run(rate=30.0, period_end='2025-12-31')
        self._line(prior, name='Accelerated depreciation',
                   carrying_amount=1000.0, tax_base=0.0)
        self._line(prior, name='Old loss', nature='tax_loss',
                   carrying_amount=400.0, recoverable=False)
        prior.action_compute()
        # 400 deductible x 30%, all unrecognised via the hard off-switch.
        self.assertAlmostEqual(prior.unrecognised_dta, 120.0, places=2)
        prior.action_post()

        current = self._run(rate=28.0, period_end='2026-12-31')
        line = self._line(current, name='Accelerated depreciation',
                          carrying_amount=1000.0, tax_base=0.0)
        current.action_compute()
        self.assertAlmostEqual(line.opening_dtl, 300.0, places=2)
        self.assertAlmostEqual(line.opening_rate, 30.0, places=3)
        self.assertAlmostEqual(line.tax_rate, 28.0, places=3)
        self.assertAlmostEqual(line.rate_change_effect, -20.0, places=2)
        self.assertAlmostEqual(line.origination_effect, 0.0, places=2)
        self.assertAlmostEqual(
            current.opening_unrecognised_dta, 120.0, places=2)
        self.assertAlmostEqual(
            self._recon_by_kind(current).get('unrecognised'), -120.0,
            places=2)

    # ------------------------------------------------------------------
    # 3. Offsetting per jurisdiction (IAS 12.74)
    # ------------------------------------------------------------------
    def _offsetting_lines(self, run, jur_a, jur_b):
        # Jurisdiction A: deductible 2000 x 25% = DTA 500 (warranty),
        #                 taxable 1200 x 25% = DTL 300 (depreciation).
        # Jurisdiction B: taxable  400 x 25% = DTL 100.
        self._line(run, name='Warranty provision A', nature='liability',
                   carrying_amount=2000.0, tax_base=0.0,
                   jurisdiction_id=jur_a.id)
        self._line(run, name='Depreciation A', carrying_amount=1200.0,
                   tax_base=0.0, jurisdiction_id=jur_a.id)
        self._line(run, name='Depreciation B', carrying_amount=400.0,
                   tax_base=0.0, jurisdiction_id=jur_b.id)

    def test_golden_offsetting_net_by_jurisdiction(self):
        """Net policy: jurisdiction A's DTA 500 offsets its DTL 300 into a
        single net DTA 200 leg; jurisdiction B's DTL 100 stays; the lines
        keep the gross detail for disclosure.

        Entry: Dr DTA 200 (A net) / Cr DTL 100 (B) / Cr deferred tax
        expense 100 (plug: net income of 500 - 300 - 100 = 100).
        Presented: DTA 200, DTL 100; gross closing unchanged (500 / 400).
        """
        jur_a = self._jurisdiction('Country A')
        jur_b = self._jurisdiction('Country B')
        run = self._run(rate=25.0,
                        offsetting_policy='net_by_jurisdiction')
        self._offsetting_lines(run, jur_a, jur_b)
        run.action_compute()
        # Gross detail preserved on the lines and gross totals.
        self.assertAlmostEqual(run.closing_dta, 500.0, places=2)
        self.assertAlmostEqual(run.closing_dtl, 400.0, places=2)
        # Presented (balance sheet) figures are netted per jurisdiction.
        self.assertAlmostEqual(run.net_dta_presented, 200.0, places=2)
        self.assertAlmostEqual(run.net_dtl_presented, 100.0, places=2)
        self.assertIn('IAS 12.74', run.offsetting_note)
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.dta, 200.0, 0.0),
            (self.dtl, 0.0, 100.0),
            (self.dtax_expense, 0.0, 100.0),
        ])
        self.assertBalanced(run.move_id)
        # The offsetting policy is an audited control point.
        self.assertTrue(self.env['eh.deferred.tax.run']
                        ._fields['offsetting_policy'].tracking)

    def test_golden_offsetting_gross_regression(self):
        """Gross policy (the default) posts the historical aggregate legs:
        Dr DTA 500 / Cr DTL 400 / Cr deferred tax expense 100."""
        jur_a = self._jurisdiction('Country A')
        jur_b = self._jurisdiction('Country B')
        run = self._run(rate=25.0)
        self.assertEqual(run.offsetting_policy, 'gross')
        self._offsetting_lines(run, jur_a, jur_b)
        run.action_compute()
        # Under gross the presented figures equal the gross totals.
        self.assertAlmostEqual(run.net_dta_presented, 500.0, places=2)
        self.assertAlmostEqual(run.net_dtl_presented, 400.0, places=2)
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.dta, 500.0, 0.0),
            (self.dtl, 0.0, 400.0),
            (self.dtax_expense, 0.0, 100.0),
        ])
        self.assertBalanced(run.move_id)

    # ------------------------------------------------------------------
    # 4. Effective-tax-rate reconciliation (IAS 12.81(c))
    # ------------------------------------------------------------------
    def test_golden_etr_reconciliation(self):
        """Pre-tax profit 10,000 at statutory 30% -> expected 3,000;
        permanent differences +150; rate change -20; unrecognised DTA
        movement +80; no residual -> total tax expense 3,210 and effective
        rate 32.1%.

        Mechanics behind each auto row:
        * rate change: taxable diff 1000 flat, opening DTL 300 at 30%,
          jurisdiction table gives the new reporting-date rate and the
          opening balance is remeasured against its prior rate
          = -20 (movement -20, origination 0).
        * unrecognised: tax loss 1,000 in a 20% jurisdiction capped by
          recoverable profit 600 -> DTA recognised 600 x 20% = 120,
          unrecognised (1000 - 600) x 20% = 80; opening unrecognised 0.
        * deferred P&L movement = -20 (DTL down) - 120 (DTA up) = -140
          credit, so current tax expense is set to 3,350 to make the rows
          tie residual-free: 3,350 - 140 = 3,210 = 3,000 + 150 - 20 + 80.
        """
        jur_j = self._jurisdiction('Mainland', rates=[
            ('2020-01-01', 30.0, '2019-06-01'),
            ('2026-01-01', 28.0, '2025-09-15'),
        ])
        jur_k = self._jurisdiction('Islands', rates=[
            ('2020-01-01', 20.0, '2019-06-01'),
        ])
        run = self._run(rate=30.0, period_end='2026-12-31',
                        accounting_profit=10000.0,
                        permanent_diff_tax=150.0,
                        current_tax_expense=3350.0)
        self._line(run, name='Accelerated depreciation',
                   carrying_amount=1000.0, tax_base=0.0,
                   jurisdiction_id=jur_j.id,
                   opening_dtl=300.0, opening_rate=30.0)
        self._line(run, name='Tax losses', nature='tax_loss',
                   carrying_amount=1000.0, recoverable_amount=600.0,
                   jurisdiction_id=jur_k.id)
        run.action_compute()

        self.assertAlmostEqual(run.expected_tax, 3000.0, places=2)
        self.assertAlmostEqual(run.pl_movement, -140.0, places=2)
        self.assertAlmostEqual(run.total_tax_expense, 3210.0, places=2)
        self.assertAlmostEqual(run.effective_rate, 32.1, places=3)

        rows = self._recon_by_kind(run)
        self.assertEqual(
            set(rows), {'expected', 'permanent', 'rate_change',
                        'unrecognised'},
            'exactly four reconciling rows, no residual')
        self.assertAlmostEqual(rows['expected'], 3000.0, places=2)
        self.assertAlmostEqual(rows['permanent'], 150.0, places=2)
        self.assertAlmostEqual(rows['rate_change'], -20.0, places=2)
        self.assertAlmostEqual(rows['unrecognised'], 80.0, places=2)
        self.assertTrue(all(run.recon_line_ids.mapped('is_auto')))
        # Rows always tie to the total tax expense by construction.
        self.assertAlmostEqual(
            sum(run.recon_line_ids.mapped('amount')), 3210.0, places=2)

    def test_golden_etr_manual_row_and_residual(self):
        """A manual prior-year row enters the reconciliation and the auto
        residual re-balances around it on recompute.

        Same fact pattern as the expected-only case (profit 1,000 at 25% =
        250 expected; taxable diff 400 -> DTL 100 = deferred movement;
        current tax 150 -> total 250). A manual prior-year row of +30 must
        then be offset by an auto residual of -30 for the rows to keep
        tying to 250.
        """
        run = self._run(rate=25.0, accounting_profit=1000.0,
                        current_tax_expense=150.0)
        self._line(run, name='Depreciation', carrying_amount=1000.0,
                   tax_base=600.0)
        run.action_compute()
        rows = self._recon_by_kind(run)
        self.assertEqual(set(rows), {'expected'})
        self.assertAlmostEqual(rows['expected'], 250.0, places=2)

        self.env['eh.deferred.tax.recon.line'].create({
            'run_id': run.id, 'kind': 'prior_year',
            'name': 'Prior year under-provision', 'amount': 30.0,
        })
        run.action_compute()
        rows = self._recon_by_kind(run)
        self.assertEqual(set(rows), {'expected', 'prior_year', 'other'})
        self.assertAlmostEqual(rows['other'], -30.0, places=2)
        self.assertAlmostEqual(
            sum(run.recon_line_ids.mapped('amount')), 250.0, places=2)
        # The manual row survived the recompute.
        manual = run.recon_line_ids.filtered(lambda r: not r.is_auto)
        self.assertEqual(manual.kind, 'prior_year')

    # ------------------------------------------------------------------
    # 5. Carryforward expiry + run-level recoverability (IAS 12.36(a))
    # ------------------------------------------------------------------
    def test_golden_expiry_derecognises_dta(self):
        """A loss expired on or before the reporting date recognises
        nothing; a live one recognises in full. Statutory 25%.

        L1: loss 800 expired 2026-06-30 -> DTA 0, unrecognised 200.
        L2: loss 800 expiring 2027-06-30 -> DTA 200, unrecognised 0.
        L3: loss 400 expiring exactly on the reporting date 2026-12-31 ->
            expired by convention -> DTA 0, unrecognised 100.
        Run: closing DTA 200, unrecognised 300; the reconciliation's
        unrecognised row shows the +300 period movement (opening 0).
        Entry: Dr DTA 200 / Cr deferred tax expense 200.
        """
        run = self._run(rate=25.0, period_end='2026-12-31')
        l1 = self._line(run, name='Loss 2019', nature='tax_loss',
                        carrying_amount=800.0, expiry_date='2026-06-30')
        l2 = self._line(run, name='Loss 2024', nature='tax_loss',
                        carrying_amount=800.0, expiry_date='2027-06-30')
        l3 = self._line(run, name='Loss 2020', nature='tax_loss',
                        carrying_amount=400.0, expiry_date='2026-12-31')
        run.action_compute()
        self.assertAlmostEqual(l1.closing_dta, 0.0, places=2)
        self.assertAlmostEqual(l1.unrecognised_dta, 200.0, places=2)
        self.assertAlmostEqual(l2.closing_dta, 200.0, places=2)
        self.assertAlmostEqual(l2.unrecognised_dta, 0.0, places=2)
        self.assertAlmostEqual(l3.closing_dta, 0.0, places=2)
        self.assertAlmostEqual(l3.unrecognised_dta, 100.0, places=2)
        self.assertAlmostEqual(run.closing_dta, 200.0, places=2)
        self.assertAlmostEqual(run.unrecognised_dta, 300.0, places=2)
        self.assertAlmostEqual(
            self._recon_by_kind(run).get('unrecognised'), 300.0, places=2)
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.dta, 200.0, 0.0),
            (self.dtax_expense, 0.0, 200.0),
        ])

    def test_golden_run_level_ceiling_controls_ledger(self):
        """The projected-profit ceiling limits recognition and disclosure.

        The deductible difference creates a full DTA. Projected taxable
        profit at the statutory rate sets a lower ceiling; the excess is
        unrecognised and flows to the reconciliation. Only the supported 300
        is recognised and posted: Dr DTA 300 / Cr income 300.
        """
        run = self._run(rate=25.0, projected_taxable_profit=1200.0,
                        recoverability_memo='Board-approved 3-year forecast')
        line = self._line(run, name='Warranty provision', nature='liability',
                          carrying_amount=2000.0, tax_base=0.0)
        run.action_compute()
        self.assertAlmostEqual(line.line_level_closing_dta, 500.0, places=2)
        self.assertAlmostEqual(line.closing_dta, 300.0, places=2)
        self.assertAlmostEqual(
            line.run_level_unrecognised_dta, 200.0, places=2)
        self.assertAlmostEqual(run.closing_dta, 300.0, places=2)
        self.assertAlmostEqual(run.dta_ceiling, 300.0, places=2)
        self.assertAlmostEqual(
            run.run_level_unrecognised_dta, 200.0, places=2)
        self.assertAlmostEqual(
            self._recon_by_kind(run).get('unrecognised'), 200.0, places=2)
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.dta, 300.0, 0.0),
            (self.dtax_expense, 0.0, 300.0),
        ])
        self.assertTrue(self.env['eh.deferred.tax.run']
                        ._fields['projected_taxable_profit'].tracking)

    def test_run_ceiling_allocation_preserves_pl_and_oci_routing(self):
        """A global ceiling is allocated to its underlying line routes.

        Two otherwise eligible DTAs are 300 in P&L and 200 in OCI. A 300
        ceiling recognises 60% of each: 180 P&L and 120 OCI. The balance-sheet
        leg, line movement, disclosure, and both counterpart routes agree.
        """
        run = self._run(
            rate=25.0,
            projected_taxable_profit=1200.0,
            recoverability_memo='Approved forecast',
        )
        pl_line = self._line(
            run, name='Warranty', nature='liability',
            carrying_amount=1200.0, tax_base=0.0,
        )
        oci_line = self._line(
            run, name='OCI deductible difference', nature='liability',
            carrying_amount=800.0, tax_base=0.0, through_oci=True,
        )
        run.action_compute()
        self.assertAlmostEqual(pl_line.line_level_closing_dta, 300.0, places=2)
        self.assertAlmostEqual(oci_line.line_level_closing_dta, 200.0, places=2)
        self.assertAlmostEqual(pl_line.closing_dta, 180.0, places=2)
        self.assertAlmostEqual(oci_line.closing_dta, 120.0, places=2)
        self.assertAlmostEqual(run.closing_dta, 300.0, places=2)
        self.assertAlmostEqual(run.pl_movement, -180.0, places=2)
        self.assertAlmostEqual(run.oci_movement, -120.0, places=2)
        self.assertAlmostEqual(run.run_level_unrecognised_dta, 200.0, places=2)
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.dta, 300.0, 0.0),
            (self.dtax_expense, 0.0, 180.0),
            (self.oci, 0.0, 120.0),
        ])

    def test_late_computed_ceiling_edit_rebuilds_reconciliation_on_post(self):
        """Posting cannot retain disclosure rows from the old ceiling."""
        run = self._run(rate=25.0)
        self._line(
            run, name='Late forecast warranty', nature='liability',
            carrying_amount=2000.0, tax_base=0.0,
        )
        run.action_compute()
        self.assertAlmostEqual(run.closing_dta, 500.0, places=2)

        # Computed-state inputs remain editable for review, but posting must
        # reject the stale Compute fingerprint until the reviewer recomputes.
        run.projected_taxable_profit = 1200.0
        run.recoverability_memo = 'Forecast approved after initial compute'
        self.assertAlmostEqual(run.closing_dta, 300.0, places=2)
        with self.assertRaises(UserError):
            run.action_post()
        self.assertFalse(run.move_id)
        run.action_compute()
        run.action_post()
        self.assertMoveLines(run.move_id, [
            (self.dta, 300.0, 0.0),
            (self.dtax_expense, 0.0, 300.0),
        ])
        self.assertAlmostEqual(
            self._recon_by_kind(run).get('unrecognised'), 200.0, places=2)

    def test_recon_rows_frozen_after_post(self):
        """The reconciliation is disclosure basis: frozen with the run."""
        run = self._run(rate=25.0, reconciliation_materiality=1000.0)
        self._line(run, name='Depreciation', carrying_amount=1000.0,
                   tax_base=600.0)
        run.action_compute()
        manual = self.env['eh.deferred.tax.recon.line'].create({
            'run_id': run.id, 'kind': 'credits',
            'name': 'R&D credit', 'amount': -10.0,
        })
        run.action_compute()
        run.action_post()
        with self.assertRaises(UserError):
            manual.amount = -20.0
        with self.assertRaises(UserError):
            manual.unlink()
        with self.assertRaises(UserError):
            self.env['eh.deferred.tax.recon.line'].create({
                'run_id': run.id, 'kind': 'other',
                'name': 'late row', 'amount': 1.0,
            })
