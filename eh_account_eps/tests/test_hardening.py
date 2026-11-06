# -*- coding: utf-8 -*-
"""Security and freeze regressions for the IAS 33 evidence graph."""

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_eps', 'post_install', '-at_install')
class TestEpsHardening(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.user = cls.env['res.users'].create({
            'name': 'EPS Operator', 'login': 'eps_hardening_operator',
            'company_id': cls.env.company.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        cls.manager = cls.env['res.users'].create({
            'name': 'EPS Manager', 'login': 'eps_hardening_manager',
            'company_id': cls.env.company.id,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })

    def _run(self, suffix, with_opening=False):
        run = self.env['eh.eps.run'].create({
            'name': suffix,
            'period_start': '2026-01-01',
            'period_end': '2026-12-31',
            'net_profit': 1000.0,
        })
        if with_opening:
            self.env['eh.eps.share.movement'].create({
                'run_id': run.id,
                'effective_date': run.period_start,
                'shares_outstanding': 100.0,
            })
        return run

    def test_cancel_and_reset_are_manager_only(self):
        run = self._run('Manager transitions', with_opening=True)
        with self.assertRaises(UserError):
            run.with_user(self.user).action_cancel()
        run.action_compute()
        with self.assertRaises(UserError):
            run.with_user(self.user).action_set_to_draft()

    def test_non_superuser_manager_can_compute_and_reset(self):
        run = self._run('Ordinary manager transition', with_opening=True)
        manager_run = run.with_user(self.manager).with_company(self.env.company)
        manager_run.action_compute()
        self.assertEqual(run.state, 'computed')
        manager_run.action_set_to_draft()
        self.assertEqual(run.state, 'draft')

    def test_child_related_and_output_fields_are_server_owned(self):
        run = self._run('Owned outputs')
        with self.assertRaises(AccessError):
            self.env['eh.eps.potential'].create({
                'run_id': run.id, 'name': 'Forged potential',
                'potential_shares': 10.0,
                'is_dilutive': True,
            })
        movement = self.env['eh.eps.share.movement'].create({
            'run_id': run.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 100.0,
        })
        with self.assertRaises(AccessError):
            movement.write({'company_id': self.env.company.id})

    def test_every_child_reparent_rejects_computed_target(self):
        source = self._run('Draft source')
        target = self._run('Computed target', with_opening=True)
        target_potential = self.env['eh.eps.potential'].create({
            'run_id': target.id, 'name': 'Target option',
            'potential_shares': 10.0,
        })
        target.action_compute()

        movement = self.env['eh.eps.share.movement'].create({
            'run_id': source.id, 'effective_date': '2026-01-01',
            'shares_outstanding': 100.0,
        })
        event = self.env['eh.eps.restatement.event'].create({
            'run_id': source.id, 'date': '2026-06-01',
            'kind': 'split', 'factor': 2.0,
        })
        potential = self.env['eh.eps.potential'].create({
            'run_id': source.id, 'name': 'Source option',
            'potential_shares': 10.0,
        })
        observation = self.env['eh.eps.price.observation'].create({
            'potential_id': potential.id, 'date': '2026-03-01',
            'price': 5.0,
        })

        for record, values in (
                (movement, {'run_id': target.id}),
                (event, {'run_id': target.id}),
                (potential, {'run_id': target.id}),
                (observation, {'potential_id': target_potential.id})):
            with self.assertRaises(UserError):
                record.write(values)
