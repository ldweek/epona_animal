# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pairwise scenario tests for the IAS 12 deferred tax engine.

Pairwise sweep over jurisdiction count x offsetting policy x OCI routing x
rate-change remeasurement, asserting engine invariants (posted legs
balance, presented positions preserve the net position, disclosure splits
tie to the movement, reconciliation rows tie to the total tax expense)
rather than hand-picked amounts. The exact worked examples live in
test_golden_ias12.py.
"""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase
from odoo.addons.eh_account_base.tests.pairwise import pairwise_cases


@tagged('eh_golden', 'eh_account_deferred_tax', 'post_install', '-at_install')
class TestPropertyIas12(EhGoldenTestCase):

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

    _seq = 0

    @classmethod
    def _next(cls):
        # One run per company and reporting date (unique constraint) and
        # one jurisdiction name per company, so every case gets its own
        # date and jurisdiction names.
        cls._seq += 1
        return cls._seq

    AXES = {
        'jurisdictions': [1, 2],
        'policy': ['gross', 'net_by_jurisdiction'],
        'oci': [False, True],
        'rate_change': [False, True],
    }

    def _build_case(self, case, seq):
        Jurisdiction = self.env['eh.tax.jurisdiction']
        jur_a = Jurisdiction.create({
            'name': 'PW A %d' % seq, 'company_id': self.company.id})
        jur_b = jur_a if case['jurisdictions'] == 1 else Jurisdiction.create({
            'name': 'PW B %d' % seq, 'company_id': self.company.id})
        run = self.env['eh.deferred.tax.run'].create({
            'statutory_rate': 25.0,
            'period_end': '2026-%02d-%02d' % (
                1 + (seq - 1) // 28, 1 + (seq - 1) % 28),
            'offsetting_policy': case['policy'],
            'offsetting_policy_change_reason': (
                'Pairwise scenario isolates presentation policy behavior.'
            ),
            'accounting_profit': 1000.0,
            'current_tax_expense': 100.0,
            # Pairwise engine trials deliberately vary measurements without
            # constructing a worked ETR explanation for every case.
            'reconciliation_materiality': 100000.0,
            'dta_account_id': self.dta.id,
            'dtl_account_id': self.dtl.id,
            'deferred_tax_expense_account_id': self.dtax_expense.id,
            'oci_account_id': self.oci.id,
            'journal_id': self.journal_misc.id,
        })
        # Line names are unique per case: earlier cases post runs for the
        # same company, and a matching name would roll their closing into
        # this case's opening (the engine's roll-forward feature).
        # Deductible 2000 x 25% = DTA 500 in jurisdiction A.
        self.env['eh.deferred.tax.line'].create({
            'run_id': run.id, 'name': 'Provision %d' % seq,
            'nature': 'liability',
            'carrying_amount': 2000.0, 'tax_base': 0.0,
            'jurisdiction_id': jur_a.id,
        })
        # Taxable 1200 x 25% = DTL 300 in jurisdiction A or B, optionally
        # through OCI, optionally with an opening measured at 30% so the
        # engine discloses a rate-change component (60 x (25/30 - 1) = -10).
        line_vals = {
            'run_id': run.id, 'name': 'Depreciation %d' % seq,
            'nature': 'asset',
            'carrying_amount': 1200.0, 'tax_base': 0.0,
            'jurisdiction_id': jur_b.id,
            'through_oci': case['oci'],
        }
        if case['rate_change']:
            line_vals.update({'opening_dtl': 60.0, 'opening_rate': 30.0})
        self.env['eh.deferred.tax.line'].create(line_vals)
        return run

    def _assert_invariants(self, run, label):
        lines = run.line_ids
        # Presented positions preserve the net position under any policy.
        self.assertAlmostEqual(
            run.net_dtl_presented - run.net_dta_presented,
            run.closing_dtl - run.closing_dta, places=2,
            msg='net position broken for %s' % label)
        self.assertGreaterEqual(run.net_dta_presented, -0.005, label)
        self.assertGreaterEqual(run.net_dtl_presented, -0.005, label)
        if run.offsetting_policy == 'gross':
            self.assertAlmostEqual(
                run.net_dta_presented, run.closing_dta, places=2, msg=label)
            self.assertAlmostEqual(
                run.net_dtl_presented, run.closing_dtl, places=2, msg=label)
        # The movement split (rate change + origination) ties to the
        # movement itself, line by line and in total.
        for line in lines:
            self.assertAlmostEqual(
                line.rate_change_effect + line.origination_effect,
                line.movement_dtl - line.movement_dta, places=2,
                msg='movement split broken for %s' % label)
        self.assertAlmostEqual(
            run.rate_change_pl + run.rate_change_oci,
            sum(lines.mapped('rate_change_effect')), places=2, msg=label)
        # Reconciliation rows always tie to the total tax expense (the
        # residual row balances by construction).
        self.assertAlmostEqual(
            sum(run.recon_line_ids.mapped('amount')),
            run.total_tax_expense, places=2,
            msg='reconciliation does not tie for %s' % label)

    def _assert_posting(self, run, label):
        move = run.move_id
        self.assertTrue(move, 'no movement entry for %s' % label)
        self.assertBalanced(move)
        lines = run.line_ids

        def net_debit(account):
            legs = move.line_ids.filtered(lambda l: l.account_id == account)
            return sum(legs.mapped('debit')) - sum(legs.mapped('credit'))

        # The balance-sheet legs carry the same total movement under both
        # policies; only their aggregation differs. Tolerance: each posted
        # leg is currency-rounded once, so with several jurisdictions the
        # rounded total may sit up to a few cents off the raw 4dp sum.
        self.assertAlmostEqual(
            net_debit(self.dta) + net_debit(self.dtl),
            sum(lines.mapped('movement_dta'))
            - sum(lines.mapped('movement_dtl')),
            delta=0.03, msg='balance-sheet legs broken for %s' % label)
        # OCI leg equals the OCI movement; P&L plug equals the negative of
        # everything else.
        oci_lines = lines.filtered(lambda l: l.through_oci)
        self.assertAlmostEqual(
            net_debit(self.oci),
            sum(oci_lines.mapped('movement_dtl'))
            - sum(oci_lines.mapped('movement_dta')),
            places=2, msg='OCI leg broken for %s' % label)
        self.assertAlmostEqual(
            net_debit(self.dtax_expense),
            -(net_debit(self.dta) + net_debit(self.dtl)
              + net_debit(self.oci)),
            places=2, msg='expense plug broken for %s' % label)

    def test_pairwise_matrix(self):
        for case in pairwise_cases(self.AXES):
            label = repr(case)
            run = self._build_case(case, self._next())
            run.action_compute()
            self._assert_invariants(run, label)
            if not case['rate_change']:
                self.assertAlmostEqual(run.rate_change_pl, 0.0, places=2,
                                       msg=label)
                self.assertAlmostEqual(run.rate_change_oci, 0.0, places=2,
                                       msg=label)
            else:
                # 60 opening DTL x (25/30 - 1) = -10, routed by the flag.
                routed = (run.rate_change_oci if case['oci']
                          else run.rate_change_pl)
                self.assertAlmostEqual(routed, -10.0, places=2, msg=label)
            run.action_post()
            self._assert_posting(run, label)

    def test_seeded_random_trials(self):
        """Randomized gross-vs-net equivalence: for any line population the
        two policies post different aggregation but identical totals."""
        rng = self.seeded_rng(20260705)
        Jurisdiction = self.env['eh.tax.jurisdiction']
        for trial in range(6):
            seq_a, seq_b = self._next(), self._next()
            jurs = [
                Jurisdiction.create({
                    'name': 'RT %d-%d' % (trial, i),
                    'company_id': self.company.id,
                })
                for i in range(rng.randint(1, 3))
            ]
            runs = {}
            for policy, seq in (('gross', seq_a),
                                ('net_by_jurisdiction', seq_b)):
                run = self.env['eh.deferred.tax.run'].create({
                    'statutory_rate': 25.0,
                    'period_end': '2027-%02d-%02d' % (
                        1 + (seq - 1) // 28, 1 + (seq - 1) % 28),
                    'offsetting_policy': policy,
                    'offsetting_policy_change_reason': (
                        'Seeded equivalence scenario switches presentation.'
                    ),
                    'dta_account_id': self.dta.id,
                    'dtl_account_id': self.dtl.id,
                    'deferred_tax_expense_account_id': self.dtax_expense.id,
                    'oci_account_id': self.oci.id,
                    'journal_id': self.journal_misc.id,
                })
                runs[policy] = run
            # Same seeded line population on both runs.
            population = []
            for i in range(rng.randint(2, 6)):
                population.append({
                    'name': 'L%d-%d' % (trial, i),
                    'nature': rng.choice(['asset', 'liability']),
                    'carrying_amount': round(rng.uniform(0.0, 5000.0), 2),
                    'tax_base': round(rng.uniform(0.0, 5000.0), 2),
                    'jurisdiction': rng.randrange(len(jurs)),
                })
            for run in runs.values():
                for spec in population:
                    self.env['eh.deferred.tax.line'].create({
                        'run_id': run.id, 'name': spec['name'],
                        'nature': spec['nature'],
                        'carrying_amount': spec['carrying_amount'],
                        'tax_base': spec['tax_base'],
                        'jurisdiction_id': jurs[spec['jurisdiction']].id,
                    })
                run.action_compute()
            label = 'trial %d (seed 20260705)' % trial
            gross, net = runs['gross'], runs['net_by_jurisdiction']
            # Gross measurement identical; only presentation differs.
            self.assertAlmostEqual(gross.closing_dta, net.closing_dta,
                                   places=2, msg=label)
            self.assertAlmostEqual(gross.closing_dtl, net.closing_dtl,
                                   places=2, msg=label)
            self.assertAlmostEqual(
                net.net_dtl_presented - net.net_dta_presented,
                gross.closing_dtl - gross.closing_dta, places=2, msg=label)
            # Offsetting can only shrink (never grow) the presented sides.
            self.assertLessEqual(
                net.net_dta_presented, gross.closing_dta + 0.005, label)
            self.assertLessEqual(
                net.net_dtl_presented, gross.closing_dtl + 0.005, label)
            for run in runs.values():
                self._assert_invariants(run, label)
                try:
                    run.action_post()
                except UserError:
                    # Nil movement (e.g. every leg rounds to zero after
                    # netting): nothing was posted, nothing to assert.
                    continue
                self._assert_posting(run, label)
