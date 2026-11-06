# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Producer/consumer seam: eh.deferred.tax.run.action_gather_from_engines pulls
temporary differences from any model implementing eh_deferred_tax_temp_diffs
(IFRS 9 ECL, IAS 37 provisions, ...), so the category hooks are driven by a
real producer instead of being hand-keyed. Exercised here with a stub
provider so the test needs no cross-module install; the real ECL and
provision producers are validated when the full suite installs them together.
"""

from unittest.mock import patch

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


def _stub_provider(records, reporting_date):
    return [{
        'name': 'Stub warranty provision',
        'source_key': 'stub:warranty',
        'category': 'provision',
        'nature': 'liability',
        'carrying_amount': 10000.0,
        'tax_base': 0.0,
        'through_oci': False,
    }]


@tagged('eh_account_deferred_tax', 'post_install', '-at_install')
class TestDeferredTaxGatherSeam(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager',
        )
        cls.dta = cls._ensure_account(
            cls.env, '1818', 'Gather DTA', 'asset_non_current',
        )
        cls.dtl = cls._ensure_account(
            cls.env, '2818', 'Gather DTL', 'liability_non_current',
        )
        cls.expense = cls._ensure_account(
            cls.env, '5818', 'Gather Tax Expense', 'expense',
        )
        cls.oci = cls._ensure_account(
            cls.env, '3818', 'Gather OCI', 'equity',
        )

    def _run(self, period_end='2026-12-31'):
        return self.env['eh.deferred.tax.run'].create({
            'statutory_rate': 25.0,
            'period_end': period_end,
            'company_id': self.company.id,
            'dta_account_id': self.dta.id,
            'dtl_account_id': self.dtl.id,
            'deferred_tax_expense_account_id': self.expense.id,
            'oci_account_id': self.oci.id,
            'journal_id': self.journal_misc.id,
        })

    def _gather_with_stub(self, run):
        Company = type(self.env['res.company'])
        with patch.object(type(run), '_eh_deferred_tax_providers',
                          return_value=['res.company']), \
                patch.object(Company, 'eh_deferred_tax_temp_diffs',
                             _stub_provider, create=True):
            run.action_gather_from_engines()

    def test_gather_pulls_producer_diffs_as_dta(self):
        """A deductible temporary difference from a producer becomes a
        gathered line with the right category and a DTA (deductible_diff =
        carrying for a liability with a nil tax base)."""
        run = self._run()
        self._gather_with_stub(run)
        gathered = run.line_ids.filtered('eh_auto_gathered')
        self.assertEqual(len(gathered), 1)
        self.assertEqual(gathered.category, 'provision')
        self.assertEqual(gathered.nature, 'liability')
        # A liability carried at 10,000 with a nil tax base is a DEDUCTIBLE
        # temporary difference (-> DTA). The rate is resolved later on compute;
        # the DTA = rate x diff arithmetic is covered by the golden tests.
        self.assertAlmostEqual(gathered.deductible_diff, 10000.0, places=2)
        self.assertAlmostEqual(gathered.taxable_diff, 0.0, places=2)

    def test_gather_is_idempotent_and_spares_manual_lines(self):
        """Re-gathering replaces the auto lines (no duplication) and never
        touches hand-keyed lines."""
        run = self._run()
        manual = self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Hand-keyed depreciation',
            'category': 'depreciation', 'nature': 'asset',
            'carrying_amount': 5000.0, 'tax_base': 3000.0,
        })
        self._gather_with_stub(run)
        self._gather_with_stub(run)  # second gather must not duplicate
        self.assertEqual(
            len(run.line_ids.filtered('eh_auto_gathered')), 1)
        self.assertIn(manual, run.line_ids)
        self.assertFalse(manual.eh_auto_gathered)

    def test_gather_no_providers_is_clean(self):
        """With no producers installed the gather is a clean no-op."""
        run = self._run()
        with patch.object(type(run), '_eh_deferred_tax_providers',
                          return_value=[]):
            run.action_gather_from_engines()
        self.assertFalse(run.line_ids.filtered('eh_auto_gathered'))

    def test_stable_source_key_rolls_opening_across_renamed_runs(self):
        def provider_2025(records, reporting_date):
            return [{
                'name': 'ECL loss allowance: FY2025 run',
                'source_key': 'company:%s:ecl_loss_allowance' % self.company.id,
                'category': 'receivable_ecl',
                'nature': 'asset',
                'carrying_amount': 0.0,
                'tax_base': 400.0,
            }]

        def provider_2026(records, reporting_date):
            return [{
                'name': 'ECL loss allowance: FY2026 renamed run',
                'source_key': 'company:%s:ecl_loss_allowance' % self.company.id,
                'category': 'receivable_ecl',
                'nature': 'asset',
                'carrying_amount': 0.0,
                'tax_base': 600.0,
            }]

        Company = type(self.env['res.company'])
        prior = self._run('2025-12-31')
        with patch.object(type(prior), '_eh_deferred_tax_providers',
                          return_value=['res.company']), \
                patch.object(Company, 'eh_deferred_tax_temp_diffs',
                             provider_2025, create=True):
            prior.action_gather_from_engines()
        prior.action_compute()
        prior.action_post()

        current = self._run('2026-12-31')
        with patch.object(type(current), '_eh_deferred_tax_providers',
                          return_value=['res.company']), \
                patch.object(Company, 'eh_deferred_tax_temp_diffs',
                             provider_2026, create=True):
            current.action_gather_from_engines()
        current.action_compute()
        line = current.line_ids.filtered('eh_auto_gathered')
        self.assertEqual(line.source_model, 'res.company')
        self.assertEqual(
            line.source_key,
            'company:%s:ecl_loss_allowance' % self.company.id,
        )
        self.assertEqual(line.opening_dta, 100.0)
        self.assertEqual(line.closing_dta, 150.0)
        self.assertEqual(line.movement_dta, 50.0)

    def test_real_provision_producer_rolls_two_periods_in_full_suite(self):
        """The optional IAS 37 producer drives a real two-period gather.

        The standalone Deferred Tax SKU intentionally has no hard dependency
        on producer SKUs; the all-suite gate installs eh_account_provisions and
        executes this branch without mocking its provider method.
        """
        if self.env.get('eh.provision') is None:
            self.skipTest('eh_account_provisions is not installed')
        provision_liability = self._ensure_account(
            self.env, '2918', 'Gather Provision Liability',
            'liability_current',
        )
        finance_cost = self._ensure_account(
            self.env, '5718', 'Gather Provision Finance Cost', 'expense',
        )
        reimbursement = self._ensure_account(
            self.env, '1918', 'Gather Provision Reimbursement',
            'asset_current',
        )
        provision = self.env['eh.provision'].create({
            'name': '/',
            'classification': 'provision',
            'best_estimate': 1000.0,
            'provision_account_id': provision_liability.id,
            'expense_account_id': self.account_expense.id,
            'finance_cost_account_id': finance_cost.id,
            'settlement_account_id': self.account_cash.id,
            'reimbursement_account_id': reimbursement.id,
            'journal_id': self.journal_misc.id,
            'action_accounting_date': '2025-01-01',
        })
        provision.action_recognise()

        prior = self._run('2025-12-31')
        prior.action_gather_from_engines()
        prior_line = prior.line_ids.filtered(
            lambda line: line.source_model == 'eh.provision'
        )
        self.assertEqual(len(prior_line), 1)
        prior.action_compute()
        prior.action_post()

        current = self._run('2026-12-31')
        current.action_gather_from_engines()
        current.action_compute()
        line = current.line_ids.filtered(
            lambda row: row.source_model == 'eh.provision'
            and row.source_key == prior_line.source_key
        )
        self.assertEqual(len(line), 1)
        self.assertAlmostEqual(line.opening_dta, prior_line.closing_dta)
        self.assertAlmostEqual(line.closing_dta, prior_line.closing_dta)
        self.assertAlmostEqual(line.movement_dta, 0.0)
