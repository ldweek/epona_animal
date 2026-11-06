# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Focused regressions for the ECL medium/low audit closure."""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_ecl', 'integration', 'post_install', '-at_install')
class TestEclMediumLowBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager'
        )
        cls.impairment = cls._ensure_account(
            cls.env, '5298', 'ECL Backlog Impairment', 'expense'
        )
        cls.allowance = cls._ensure_account(
            cls.env, '1298', 'ECL Backlog Allowance', 'asset_current'
        )

    @staticmethod
    def _matrix():
        return [
            {
                'name': 'Current', 'days_from': 0, 'days_to': 30,
                'loss_rate': 1.0,
            },
            {
                'name': '31-90', 'days_from': 31, 'days_to': 90,
                'loss_rate': 5.0, 'stage': '2',
            },
            {
                'name': '91+', 'days_from': 91, 'days_to': 0,
                'loss_rate': 25.0, 'stage': '3',
            },
        ]

    def _run(self, reporting_date='2026-06-30', buckets=None, **extra):
        vals = {
            'reporting_date': reporting_date,
            'impairment_expense_account_id': self.impairment.id,
            'loss_allowance_account_id': self.allowance.id,
            'journal_id': self.journal_misc.id,
            'bucket_ids': [(0, 0, vals) for vals in (
                self._matrix() if buckets is None else buckets
            )],
        }
        vals.update(extra)
        return self.env['eh.ecl.run'].create(vals)

    def _invoice(self, partner=None, amount=1000.0,
                 invoice_date='2026-01-01', due='2026-01-15'):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': (partner or self.partner_a).id,
            'invoice_date': invoice_date,
            'invoice_date_due': due,
            'invoice_line_ids': [(0, 0, {
                'name': 'ECL backlog exposure',
                'quantity': 1.0,
                'price_unit': amount,
                'account_id': self.account_revenue.id,
            })],
        })
        move.action_post()
        return move

    def test_computed_inputs_are_readonly_and_explicit_revision_reopens(self):
        run = self._run()
        run.action_compute()
        with self.assertRaisesRegex(UserError, 'frozen'):
            run.write({'opening_allowance': 10.0})
        run.action_revise()
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.computed_at)
        run.write({'opening_allowance': 10.0})
        self.assertEqual(run.opening_allowance, 10.0)

        arch = self.env.ref(
            'eh_account_ecl.view_eh_ecl_run_form'
        ).arch_db
        self.assertIn('name="action_revise"', arch)
        self.assertTrue(
            'readonly="state != \'draft\'"' in arch
            or "'readonly': [('state', '!=', 'draft')]" in arch,
            arch,
        )

    def test_population_rejects_gap_overlap_and_missing_open_end(self):
        cases = [
            (
                '2026-01-31',
                [
                    {'name': '0-30', 'days_from': 0, 'days_to': 30},
                    {'name': '40+', 'days_from': 40, 'days_to': 0},
                ],
                'uncovered gap',
            ),
            (
                '2026-02-28',
                [
                    {'name': '0-40', 'days_from': 0, 'days_to': 40},
                    {'name': '40+', 'days_from': 40, 'days_to': 0},
                ],
                'overlaps a prior band',
            ),
            (
                '2026-03-31',
                [
                    {'name': '0-30', 'days_from': 0, 'days_to': 30},
                    {'name': '31-90', 'days_from': 31, 'days_to': 90},
                ],
                'no open-ended final band',
            ),
        ]
        for reporting_date, buckets, message in cases:
            with self.subTest(message=message):
                run = self._run(reporting_date, buckets=buckets)
                with self.assertRaisesRegex(UserError, message):
                    run.action_populate_from_receivables()

    def test_receivable_allowance_account_is_rejected(self):
        with self.assertRaisesRegex(UserError, 'non-receivable contra-asset'):
            self._run(
                reporting_date='2026-04-30',
                loss_allowance_account_id=self.account_receivable.id,
            )

    def test_population_uses_run_lock_grouped_residuals_and_bulk_write(self):
        self._invoice()
        run = self._run()
        real_execute = self.env.cr.execute
        statements = []

        def _record_execute(query, params=None, *args, **kwargs):
            statements.append(str(query).upper())
            return real_execute(query, params, *args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            run.action_populate_from_receivables()

        company_locks = [
            query for query in statements
            if 'RES_COMPANY' in query and 'FOR UPDATE' in query
        ]
        grouped_partials = [
            query for query in statements
            if 'FROM ACCOUNT_PARTIAL_RECONCILE' in query
            and 'GROUP BY' in query
        ]
        bulk_updates = [
            query for query in statements
            if 'UPDATE EH_ECL_BUCKET AS BUCKET' in query
            and 'FROM (VALUES' in query
        ]
        self.assertFalse(company_locks)
        self.assertEqual(len(grouped_partials), 2)
        self.assertEqual(len(bulk_updates), 1)
        self.assertAlmostEqual(
            run.bucket_ids.filtered(
                lambda bucket: bucket.name == '91+'
            ).gross_carrying,
            1000.0,
        )

    def test_provider_failure_rolls_population_back(self):
        run = self._run()
        oldest = run.bucket_ids.filtered(lambda bucket: bucket.name == '91+')
        oldest.gross_carrying = 321.0
        RunClass = type(run)
        PartnerClass = type(self.env['res.partner'])
        with patch.object(
            RunClass,
            '_eh_ecl_exposure_providers',
            return_value=['res.partner'],
        ), patch.object(
            PartnerClass,
            'eh_ecl_exposure_lines',
            side_effect=RuntimeError('provider unavailable'),
            create=True,
        ), self.assertRaisesRegex(UserError, 'Population was rolled back'):
            run.action_populate_from_exposures()
        oldest.invalidate_recordset(['gross_carrying'])
        self.assertEqual(oldest.gross_carrying, 321.0)

    def test_stage3_default_policy_is_explicit_and_deterministic(self):
        bucket = {
            'name': 'Defaulted', 'days_from': 91, 'days_to': 0,
            'stage': '3', 'loss_rate': 0.0,
            'exposure_at_default': 1000.0,
            'lgd': 40.0, 'pd_12m': 2.0, 'pd_lifetime': 20.0,
        }
        keyed = self._run(
            '2026-05-31', buckets=[bucket],
            measurement_approach='general',
            stage3_pd_policy='keyed_lifetime',
        )
        keyed.action_compute()
        self.assertEqual(keyed.closing_allowance, 80.0)

        defaulted = self._run(
            '2026-06-30', buckets=[bucket],
            measurement_approach='general',
            stage3_pd_policy='defaulted_100',
            scenario_ids=[(0, 0, {
                'name': 'Lower macro PD', 'weight': 1.0,
                'pd_factor': 0.5, 'lgd_factor': 1.0,
            })],
        )
        defaulted.action_compute()
        self.assertEqual(defaulted.closing_allowance, 400.0)

    def test_reversal_requires_exact_explicit_open_date(self):
        run = self._run('2025-12-31')
        run.bucket_ids.filtered(
            lambda bucket: bucket.name == '91+'
        ).gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        with self.assertRaisesRegex(UserError, 'explicit reversal'):
            run.action_reverse()
        run.reversal_date = '2025-12-31'
        run.action_reverse()
        self.assertEqual(str(run.reversal_move_id.date), '2025-12-31')

    def test_reversal_preflights_new_accounting_lock(self):
        if 'fiscalyear_lock_date' not in self.company._fields:
            self.skipTest('fiscal-year lock is unavailable on this series')
        run = self._run('2025-11-30')
        run.bucket_ids.filtered(
            lambda bucket: bucket.name == '91+'
        ).gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        run.reversal_date = '2025-11-30'
        self.env.cr.execute(
            "UPDATE res_company SET fiscalyear_lock_date = %s WHERE id = %s",
            ('2025-11-30', self.company.id),
        )
        self.company.invalidate_recordset(['fiscalyear_lock_date'])
        with self.assertRaisesRegex(UserError, 'accounting lock date'):
            run.action_reverse()
        self.assertEqual(run.state, 'posted')
        self.assertFalse(run.reversal_move_id)

    def test_foreign_currency_writeoff_preserves_source_currency(self):
        foreign = self.env['res.currency'].create({
            'name': 'ECW', 'symbol': 'ECW', 'rounding': 0.01, 'active': True,
        })
        source = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-04-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'Foreign receivable',
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                    'currency_id': foreign.id,
                    'amount_currency': 200.0,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'name': 'Foreign sale',
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                }),
            ],
        })
        source.action_post()
        receivable = source.line_ids.filtered(
            lambda line: line.account_id == self.account_receivable
        )
        run = self._run('2026-06-30')
        run.bucket_ids.filtered(
            lambda bucket: bucket.name == '91+'
        ).write({'gross_carrying': 1600.0})
        run.action_compute()
        run.action_post()
        writeoff = self.env['eh.ecl.writeoff'].create({
            'run_id': run.id,
            'move_line_id': receivable.id,
            'amount': 40.0,
            'stage': '3',
            'date': '2026-07-01',
        })
        writeoff.action_post_writeoff()
        foreign_lines = writeoff.move_id.line_ids.filtered(
            lambda line: line.currency_id == foreign
        )
        self.assertEqual(len(foreign_lines), 2)
        self.assertEqual(
            sorted(foreign_lines.mapped('amount_currency')),
            [-80.0, 80.0],
        )
        receivable.invalidate_recordset([
            'amount_residual', 'amount_residual_currency',
        ])
        self.assertEqual(receivable.amount_residual, 60.0)
        self.assertEqual(receivable.amount_residual_currency, 120.0)

    def test_stage_from_ageing_uses_run_backstops(self):
        run = self._run(
            buckets=[
                {'name': 'Clean', 'days_from': 0, 'days_to': 10},
                {'name': 'SICR', 'days_from': 11, 'days_to': 20},
                {'name': 'Default', 'days_from': 21, 'days_to': 0},
            ],
            backstop_dpd_stage2=10,
            backstop_dpd_stage3=20,
        )
        run.action_stage_from_ageing()
        self.assertEqual(
            {bucket.name: bucket.stage for bucket in run.bucket_ids},
            {'Clean': '1', 'SICR': '2', 'Default': '3'},
        )

    def test_segment_matching_uses_commercial_partner(self):
        tag = self.env['res.partner.category'].create({
            'name': 'ECL commercial group',
        })
        parent = self.partner_a.commercial_partner_id
        parent.category_id = [(4, tag.id)]
        contact = self.env['res.partner'].create({
            'name': 'ECL invoice contact',
            'parent_id': parent.id,
        })
        segment = self.env['eh.ecl.segment'].create({
            'name': 'Tagged commercial customers',
            'partner_category_ids': [(6, 0, tag.ids)],
        })
        run = self._run()
        run.action_populate_from_receivables()
        self.assertTrue(segment._matches_partner(contact))
        bucket = run._bucket_for_exposure(
            10, contact, self.env['eh.ecl.segment'].search([
                ('company_id', '=', self.company.id),
            ]),
        )
        self.assertEqual(bucket.segment_id, segment)

    def test_navigation_search_and_writeoff_company_domain_are_aligned(self):
        run_menu = self.env.ref('eh_account_ecl.menu_eh_ecl_run')
        self.assertIn(
            self.env.ref('eh_account_base.group_eh_user'),
            run_menu.group_ids,
        )
        action = self.env.ref('eh_account_ecl.action_eh_ecl_run')
        self.assertEqual(
            action.search_view_id,
            self.env.ref('eh_account_ecl.view_eh_ecl_run_search'),
        )
        self.assertTrue(self.env.ref('eh_account_ecl.menu_eh_ecl_writeoff'))
        domain = self.env['eh.ecl.writeoff']._fields['move_line_id'].domain
        self.assertIn("('company_id', '=', company_id)", domain)

    def test_legal_entity_scope_is_intentionally_exact(self):
        run = self._run()
        self.assertIn(
            ('company_id', '=', self.company.id),
            run._eh_receivable_domain(),
        )
        other = self.env['res.company'].create({
            'name': 'Separate ECL Legal Entity',
            'currency_id': self.company.currency_id.id,
        })
        foreign_journal = self.env['account.journal'].create({
            'name': 'Foreign ECL Journal',
            'code': 'FECL',
            'type': 'general',
            'company_id': other.id,
        })
        with self.assertRaisesRegex(UserError, 'journal must belong'):
            run.write({'journal_id': foreign_journal.id})

    def test_simplified_reconciliation_does_not_invent_transfers(self):
        run = self._run()
        run.bucket_ids.filtered(
            lambda bucket: bucket.name == '91+'
        ).gross_carrying = 1000.0
        run.action_compute()
        run.action_post()
        stage3 = run.recon_ids.filtered(lambda line: line.stage == '3')
        self.assertEqual(stage3.transfers_in, 0.0)
        self.assertEqual(stage3.transfers_out, 0.0)
        self.assertEqual(stage3.remeasurement, 250.0)
        self.assertEqual(stage3.closing, 250.0)
