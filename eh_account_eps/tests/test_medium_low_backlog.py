# -*- encoding: utf-8 -*-

import ast
import csv
from pathlib import Path
from xml.etree import ElementTree

from odoo.exceptions import UserError, ValidationError
from odoo.modules.module import get_module_path
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_eps', 'post_install', '-at_install')
class TestEpsMediumLowBacklog(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        group_field = (
            'group_ids' if 'group_ids' in cls.env.user._fields
            else 'groups_id'
        )
        cls.env.user.write({group_field: [(4, cls.env.ref(
            'eh_account_base.group_eh_manager',
        ).id)]})
        cls.Run = cls.env['eh.eps.run']
        cls.Movement = cls.env['eh.eps.share.movement']
        cls.Potential = cls.env['eh.eps.potential']
        cls.Event = cls.env['eh.eps.restatement.event']
        cls.Observation = cls.env['eh.eps.price.observation']

    def _run(self, **values):
        data = {
            'period_start': '2026-01-01',
            'period_end': '2026-12-31',
            'net_profit': 100000.0,
        }
        data.update(values)
        return self.Run.create(data)

    def _opening(self, run, shares=50000.0):
        return self.Movement.create({
            'run_id': run.id,
            'effective_date': run.period_start,
            'shares_outstanding': shares,
        })

    def test_same_day_share_balances_are_refused(self):
        run = self._run()
        self._opening(run)
        with self.assertRaises(ValidationError):
            self.Movement.create({
                'run_id': run.id,
                'effective_date': run.period_start,
                'shares_outstanding': 60000.0,
            })
        self.assertEqual(len(run.movement_ids), 1)

    def test_event_removal_restores_separate_manual_factor(self):
        run = self._run(restatement_factor=1.25)
        self._opening(run)
        event = self.Event.create({
            'run_id': run.id,
            'date': '2026-06-30',
            'kind': 'split',
            'factor': 2.0,
        })
        self.assertAlmostEqual(run.restatement_factor, 2.0, places=6)
        event.unlink()
        run.invalidate_recordset([
            'restatement_factor', 'weighted_avg_shares',
        ])
        self.assertAlmostEqual(run.restatement_factor, 1.25, places=6)
        self.assertAlmostEqual(run.weighted_avg_shares, 62500.0, places=2)
        with self.assertRaises(ValidationError):
            run.manual_restatement_factor = 0.0

    def test_price_observation_removal_restores_manual_value(self):
        run = self._run()
        potential = self.Potential.create({
            'run_id': run.id,
            'name': 'Observed options',
            'instrument_type': 'options',
            'potential_shares': 10000.0,
            'average_market_price': 5.0,
        })
        in_period = self.Observation.create({
            'potential_id': potential.id,
            'date': '2026-06-30',
            'price': 6.0,
        })
        self.assertTrue(potential.has_observations)
        self.assertAlmostEqual(potential.average_market_price, 6.0, places=2)
        in_period.unlink()
        potential.invalidate_recordset([
            'has_observations', 'average_market_price',
        ])
        self.assertFalse(potential.has_observations)
        self.assertAlmostEqual(potential.average_market_price, 5.0, places=2)

        self.Observation.create({
            'potential_id': potential.id,
            'date': '2025-12-31',
            'price': 99.0,
        })
        potential.average_market_price = 7.0
        self.assertFalse(potential.has_observations)
        self.assertAlmostEqual(potential.average_market_price, 7.0, places=2)

    def test_dated_and_special_instrument_classes(self):
        run = self._run()
        self._opening(run, shares=100000.0)
        dated = self.Potential.create({
            'run_id': run.id,
            'name': 'Second-half share-settled award',
            'instrument_type': 'share_settled_contract',
            'potential_shares': 36500.0,
            'effective_from': '2026-07-01',
        })
        contingent = self.Potential.create({
            'run_id': run.id,
            'name': 'Unmet contingent award',
            'instrument_type': 'contingently_issuable',
            'potential_shares': 50000.0,
            'contingency_satisfied': False,
        })
        put = self.Potential.create({
            'run_id': run.id,
            'name': 'Written put',
            'instrument_type': 'written_put_forward',
            'potential_shares': 10000.0,
            'exercise_price': 12.0,
            'average_market_price': 10.0,
        })
        # 36,500 * 184/365 = 18,400 exactly.
        self.assertAlmostEqual(
            dated.weighted_incremental_shares, 18400.0, places=2,
        )
        self.assertEqual(contingent.weighted_incremental_shares, 0.0)
        self.assertAlmostEqual(put.net_incremental_shares, 2000.0, places=2)
        run.action_compute()
        self.assertAlmostEqual(run.diluted_shares, 120400.0, places=2)

    def test_multi_instrument_sequence_and_loss_control(self):
        run = self._run()
        self._opening(run)
        option = self.Potential.create({
            'run_id': run.id, 'name': 'Options',
            'instrument_type': 'options', 'potential_shares': 10000.0,
        })
        convertible = self.Potential.create({
            'run_id': run.id, 'name': 'Convertible',
            'instrument_type': 'convertible_bond',
            'potential_shares': 20000.0,
            'earnings_adjustment': 20000.0,
        })
        rich = self.Potential.create({
            'run_id': run.id, 'name': 'Anti-dilutive convertible',
            'instrument_type': 'convertible_bond',
            'potential_shares': 10000.0,
            'earnings_adjustment': 50000.0,
        })
        run.action_compute()
        self.assertTrue(option.is_dilutive)
        self.assertTrue(convertible.is_dilutive)
        self.assertFalse(rich.is_dilutive)
        self.assertAlmostEqual(run.diluted_shares, 80000.0, places=2)
        self.assertAlmostEqual(run.diluted_eps, 1.5, places=6)

        loss = self._run(net_profit=-100000.0)
        self._opening(loss)
        loss_option = self.Potential.create({
            'run_id': loss.id, 'name': 'Loss-period options',
            'instrument_type': 'options', 'potential_shares': 10000.0,
        })
        loss.action_compute()
        self.assertFalse(loss_option.is_dilutive)
        self.assertAlmostEqual(loss.diluted_eps, loss.basic_eps, places=6)

    def test_events_plus_dated_potential_matches_hand_oracle(self):
        run = self._run(net_profit=200000.0)
        self._opening(run, shares=100000.0)
        self.Event.create({
            'run_id': run.id, 'date': '2026-09-01',
            'kind': 'split', 'factor': 2.0,
        })
        potential = self.Potential.create({
            'run_id': run.id, 'name': 'Half-year pre-split award',
            'instrument_type': 'share_settled_contract',
            'potential_shares': 36500.0,
            'effective_from': '2026-07-01',
            'share_count_date': '2026-07-01',
        })
        run.action_compute()
        # Basic: 100,000 * 2 = 200,000. Potential: 36,500 * 184/365
        # = 18,400, then September split * 2 = 36,800.
        self.assertAlmostEqual(run.weighted_avg_shares, 200000.0, places=2)
        self.assertAlmostEqual(
            potential.weighted_incremental_shares, 18400.0, places=2,
        )
        self.assertAlmostEqual(run.diluted_shares, 236800.0, places=2)
        self.assertAlmostEqual(
            run.diluted_eps, 200000.0 / 236800.0, places=6,
        )

    def test_computed_lineage_cannot_be_deleted_after_reopen(self):
        run = self._run()
        self._opening(run)
        run.action_compute()
        self.assertTrue(run.ever_computed)
        with self.assertRaisesRegex(UserError, 'cannot be deleted'):
            run.unlink()
        run.action_set_to_draft()
        self.assertTrue(run.active)
        self.assertEqual(run.state, 'draft')
        with self.assertRaisesRegex(UserError, 'cannot be deleted'):
            run.unlink()

    def test_cancel_archives_but_preserves_audit_figures(self):
        run = self._run()
        self._opening(run)
        run.action_compute()
        basic = run.basic_eps
        run.action_cancel()
        self.assertEqual(run.state, 'cancelled')
        self.assertFalse(run.active)
        self.assertAlmostEqual(run.basic_eps, basic, places=6)
        self.assertFalse(self.Run.search([('id', '=', run.id)]))
        self.assertEqual(
            self.Run.with_context(active_test=False).search([
                ('id', '=', run.id),
            ]),
            run,
        )

    def test_period_year_and_company_own_sequence(self):
        company_a = self.env['res.company'].create({
            'name': 'EPS Sequence Company A',
            'currency_id': self.env.company.currency_id.id,
        })
        company_b = self.env['res.company'].create({
            'name': 'EPS Sequence Company B',
            'currency_id': self.env.company.currency_id.id,
        })
        run_a = self._run(
            company_id=company_a.id,
            period_start='2024-01-01', period_end='2024-12-31',
        )
        run_b = self._run(
            company_id=company_b.id,
            period_start='2024-01-01', period_end='2024-12-31',
        )
        self.assertRegex(run_a.name, r'^EPS/2024/0*1$')
        self.assertRegex(run_b.name, r'^EPS/2024/0*1$')

    def test_views_acl_metadata_and_shared_guard_truth(self):
        root = Path(get_module_path('eh_account_eps'))
        views = ElementTree.parse(root / 'views/eps_run_views.xml').getroot()
        self.assertIsNotNone(views.find('.//search'))
        self.assertIsNotNone(views.find(
            ".//filter[@name='cancelled']",
        ))
        self.assertIsNotNone(views.find(
            ".//button[@name='action_set_to_draft']",
        ))
        for field_name in ('period_start', 'period_end', 'movement_ids',
                           'potential_ids', 'restatement_event_ids'):
            fields_found = views.findall(
                ".//form//field[@name='%s']" % field_name,
            )
            self.assertTrue(fields_found)
            self.assertTrue(any(
                'state' in (
                    field.attrib.get('readonly', '')
                    + field.attrib.get('attrs', '')
                )
                for field in fields_found
            ))

        with (root / 'security/ir.model.access.csv').open() as stream:
            acl_ids = {row['id'] for row in csv.DictReader(stream)}
        for model_name in ('share_movement', 'potential',
                           'price_observation', 'restatement_event'):
            self.assertIn('access_eh_eps_%s_auditor' % model_name, acl_ids)

        sources = '\n'.join(
            path.read_text() for path in (root / 'models').glob('*.py')
        )
        self.assertEqual(sources.count('def _eh_prepare_runs('), 1)
        lock_body = sources.split('def _eh_lock(self):', 1)[1].split(
            'def _eh_check_caller_scope', 1,
        )[0]
        self.assertNotIn('invalidate_recordset()', lock_body)

        manifest = ast.literal_eval((root / '__manifest__.py').read_text())
        listing = (root / 'static/description/index.html').read_text()
        self.assertLessEqual(len(manifest['name']), 25)
        self.assertLessEqual(len(manifest['summary']), 180)
        self.assertIn('v%s' % manifest['version'], listing)
        self.assertEqual(listing.count('v%s' % manifest['version']), 2)
