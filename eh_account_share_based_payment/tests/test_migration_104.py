# -*- encoding: utf-8 -*-
"""Seeded migration regressions for stored SBP child/output provenance."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.golden_common import EhGoldenTestCase


@tagged('eh_account_share_based_payment', 'post_install', '-at_install')
class TestSbpMigration105(EhGoldenTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        root = Path(__file__).resolve().parents[1]
        cls.pre = cls._load(
            root / 'migrations' / '19.0.1.0.5' / 'pre-migration.py',
            'eh_sbp_migration_105_pre_test',
        )
        cls.post = cls._load(
            root / 'migrations' / '19.0.1.0.5' / 'post-migration.py',
            'eh_sbp_migration_105_post_test',
        )
        cls.expense = cls._ensure_account(
            cls.env, '6188', 'SBP Migration Expense', 'expense')
        cls.reserve = cls._ensure_account(
            cls.env, '3188', 'SBP Migration Reserve', 'equity')

    @staticmethod
    def _load(path, name):
        spec = spec_from_file_location(name, path)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _plan(self):
        return self.env['eh.sbp.plan'].create({
            'name': '/',
            'grant_date': '2026-01-01',
            'vesting_years': 3,
            'expense_account_id': self.expense.id,
            'equity_account_id': self.reserve.id,
            'journal_id': self.journal_misc.id,
            'grant_ids': [(0, 0, {
                'partner_id': self.partner_a.id,
                'instruments_granted': 300,
                'grant_date_fair_value': 10.0,
            })],
        })

    def test_pre_migration_quarantines_forged_child_company(self):
        plan = self._plan()
        foreign = self.env['res.company'].create({
            'name': 'SBP Migration Foreign',
        })
        grant = plan.grant_ids[:1]
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_sbp_grant SET company_id = %s WHERE id = %s",
            (foreign.id, grant.id),
        )
        self.pre.migrate(self.env.cr, '19.0.1.0.3')
        plan.invalidate_recordset([
            'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        self.assertTrue(plan.legacy_provenance_quarantined)

    def test_post_quarantines_fully_self_consistent_intermediate_graph(self):
        plan = self._plan()
        plan.action_activate()
        run = self.env['eh.sbp.period.run'].create({
            'plan_id': plan.id, 'period_end': '2027-01-01',
        })
        run.action_post()
        # All outputs, links, lines, seals, and even the intermediate stamp
        # match exactly. The migration still fails closed because the plan's
        # pre-control cumulative anchor could have distorted this first run.
        self.env.flush_all()
        self.post.migrate(self.env.cr, '19.0.1.0.4')
        plan.invalidate_recordset([
            'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        self.assertTrue(plan.legacy_provenance_quarantined)

    def test_post_quarantines_raw_nonzero_anchor_without_evidence(self):
        plan = self._plan()
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE eh_sbp_plan SET recognised_cumulative = 1234.56 "
            "WHERE id = %s",
            (plan.id,),
        )
        self.post.migrate(self.env.cr, '19.0.1.0.4')
        plan.invalidate_recordset([
            'legacy_provenance_quarantined',
            'legacy_provenance_quarantine_note',
        ])
        self.assertTrue(plan.legacy_provenance_quarantined)
