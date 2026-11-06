# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression guard: the consolidation run and elimination state machines
cannot be skipped by a direct RPC/ORM write.

The defect this closes: a state machine enforced only by a readonly
statusbar and an input-freeze on posted figures still lets any user with
model write access RPC-write ``write({'state': 'closed'})`` straight past
``action_compute``/``action_review``/``action_close`` and the sealed
consolidation entries they produce. Both models inherit eh.workflow.guard,
so 'state' may only move through the record's own actions (which run as
su); a direct non-superuser write is refused.

The test runner env is SUPERUSER (env.su True), which would sail past the
guard, so the negative path MUST run as a non-superuser (with_user). A
manager user is used so the model-level ACL grants write and the failure
observed is the workflow guard itself, not a plain access-rights denial.
"""

import importlib.util
from pathlib import Path

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_consolidation', 'post_install', '-at_install')
class TestWorkflowGuard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.entity = cls.env['eh.consol.entity'].create({
            'name': 'Guard Group',
            'code': 'guard_group',
            'parent_company_id': cls.company.id,
            'presentation_currency_id': cls.company.currency_id.id,
        })
        cls.doc = cls.env['eh.consol.run'].create({
            'entity_id': cls.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        cls.elim = cls.env['eh.consol.elimination'].create({
            'run_id': cls.doc.id,
        })

        # A non-superuser manager: model ACL grants write, so a direct state
        # write reaching the ORM proves the WORKFLOW guard blocks it, not a
        # coarse access-rights denial. Odoo 19 res.users uses group_ids.
        try:
            cls.user = cls.env['res.users'].create({
                'name': 'Guard Manager',
                'login': 'eh_consol_guard_manager',
                'company_id': cls.company.id,
                'company_ids': [(6, 0, cls.company.ids)],
                'group_ids': [(4, cls.env.ref('base.group_user').id),
                              (4, cls.env.ref(
                                  'eh_account_base.group_eh_manager').id)],
            })
        except Exception:  # noqa: BLE001
            cls.user = False
        try:
            cls.non_manager = cls.env['res.users'].create({
                'name': 'Guard Preparer',
                'login': 'eh_consol_guard_preparer',
                'company_id': cls.company.id,
                'company_ids': [(6, 0, cls.company.ids)],
                'group_ids': [(4, cls.env.ref('base.group_user').id),
                              (4, cls.env.ref(
                                  'eh_account_base.group_eh_user').id)],
            })
        except Exception:  # noqa: BLE001
            cls.non_manager = False

    def test_run_state_write_blocked_for_normal_user(self):
        """A non-superuser cannot RPC a run past its actions into 'closed'."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        self.assertEqual(self.doc.state, 'draft')
        with self.assertRaises(AccessError):
            self.doc.with_user(self.user).write({'state': 'closed'})
        self.assertEqual(self.doc.state, 'draft')

    def test_run_cannot_be_born_with_context_or_ir_defaults(self):
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        Run = self.env['eh.consol.run'].with_user(self.user)
        context_run = Run.with_context(
            default_state='closed',
            default_engine_provenance_version=99,
            default_closed_by_id=self.user.id,
        ).create({
            'entity_id': self.entity.id,
            'period_from': '2027-01-01',
            'period_to': '2027-12-31',
        })
        self.assertEqual(context_run.state, 'draft')
        self.assertEqual(context_run.engine_provenance_version, 0)
        self.assertFalse(context_run.closed_by_id)

        self.env['ir.default'].set(
            'eh.consol.run', 'state', 'closed', user_id=self.user.id,
        )
        self.env['ir.default'].set(
            'eh.consol.run', 'engine_provenance_version', 99,
            user_id=self.user.id,
        )
        ir_default_run = Run.create({
            'entity_id': self.entity.id,
            'period_from': '2028-01-01',
            'period_to': '2028-12-31',
        })
        self.assertEqual(ir_default_run.state, 'draft')
        self.assertEqual(ir_default_run.engine_provenance_version, 0)

    def test_non_manager_defaults_cannot_seed_policy_override(self):
        if not self.non_manager:
            self.skipTest("No non-manager preparer could be provisioned.")
        Run = self.env['eh.consol.run'].with_user(self.non_manager)
        context_run = Run.with_context(
            default_override_policy_checks=True,
            default_override_policy_reason='Caller-forged context override',
            default_goodwill_recoverable_amount=999.0,
        ).create({
            'entity_id': self.entity.id,
            'period_from': '2029-01-01',
            'period_to': '2029-12-31',
        })
        self.assertFalse(context_run.override_policy_checks)
        self.assertFalse(context_run.override_policy_reason)
        self.assertEqual(context_run.goodwill_recoverable_amount, 0.0)

        self.env['ir.default'].set(
            'eh.consol.run', 'override_policy_checks', True,
            user_id=self.non_manager.id,
        )
        self.env['ir.default'].set(
            'eh.consol.run', 'override_policy_reason',
            'Caller-forged personal default', user_id=self.non_manager.id,
        )
        default_run = Run.create({
            'entity_id': self.entity.id,
            'period_from': '2030-01-01',
            'period_to': '2030-12-31',
        })
        self.assertFalse(default_run.override_policy_checks)
        self.assertFalse(default_run.override_policy_reason)

    def test_elimination_state_write_blocked_for_normal_user(self):
        """A non-superuser cannot RPC an elimination straight to 'posted'."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        self.assertEqual(self.elim.state, 'draft')
        with self.assertRaises(AccessError):
            self.elim.with_user(self.user).write({'state': 'posted'})
        self.assertEqual(self.elim.state, 'draft')

    def test_sudo_state_write_passes(self):
        """The sanctioned server path (su) writes state normally."""
        self.assertEqual(self.doc.state, 'draft')
        self.doc.sudo().write({'state': 'computed'})
        self.assertEqual(self.doc.state, 'computed')

    def test_entity_hidden_until_every_member_company_is_allowed(self):
        """One forbidden member hides the complete consolidation graph."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        allowed_member_company = self.env['res.company'].create({
            'name': 'Guard Allowed Member Company',
            'currency_id': self.company.currency_id.id,
        })
        forbidden_member_company = self.env['res.company'].create({
            'name': 'Guard Forbidden Member Company',
            'currency_id': self.company.currency_id.id,
        })
        self.user.write({
            'company_ids': [
                Command.link(allowed_member_company.id),
                Command.link(forbidden_member_company.id),
            ],
        })
        mixed_entity = self.env['eh.consol.entity'].create({
            'name': 'Guard Mixed Group',
            'code': 'guard_mixed_group',
            'parent_company_id': self.company.id,
            'presentation_currency_id': self.company.currency_id.id,
            'member_ids': [
                Command.create({'company_id': allowed_member_company.id}),
                Command.create({'company_id': forbidden_member_company.id}),
            ],
        })
        allowed_member = mixed_entity.member_ids.filtered(
            lambda member: member.company_id == allowed_member_company,
        )
        mixed_run = self.env['eh.consol.run'].create({
            'entity_id': mixed_entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        mixed_elimination = self.env['eh.consol.elimination'].create({
            'run_id': mixed_run.id,
        })
        mixed_elimination_line = self.env[
            'eh.consol.elimination.line'
        ].create({
            'elimination_id': mixed_elimination.id,
            'account_id': self.account_revenue.id,
            'debit': 1.0,
        })
        mixed_unrealised = self.env['eh.consol.unrealised.profit'].create({
            'run_id': mixed_run.id,
            'name': 'Mixed-scope unrealised profit',
            'unrealised_amount': 0.0,
        })
        # Run-line evidence freezes every source/input graph. Create source
        # fixtures first, then add the output line last.
        mixed_run_line = self.env['eh.consol.run.line'].create({
            'run_id': mixed_run.id,
            'kind': 'parent_balance',
            'company_id': self.company.id,
            'amount': 1.0,
        })
        graph = {
            'eh.consol.entity': mixed_entity,
            'eh.consol.member': allowed_member,
            'eh.consol.run': mixed_run,
            'eh.consol.run.line': mixed_run_line,
            'eh.consol.elimination': mixed_elimination,
            'eh.consol.elimination.line': mixed_elimination_line,
            'eh.consol.unrealised.profit': mixed_unrealised,
        }
        restricted_scope = (self.company | allowed_member_company).ids
        full_scope = (
            self.company | allowed_member_company | forbidden_member_company
        ).ids

        def visible(model_name, record, scope):
            return self.env[model_name].with_user(self.user).with_context(
                allowed_company_ids=scope,
            ).search([('id', '=', record.id)])

        for model_name, record in graph.items():
            self.assertFalse(
                visible(model_name, record, restricted_scope),
                "%s leaked a mixed-company consolidation graph" % model_name,
            )
        for model_name, record in graph.items():
            self.assertEqual(
                visible(model_name, record, full_scope),
                record,
                "%s stayed hidden after the full scope was enabled"
                % model_name,
            )
        # Prime the non-stored Boolean in the wide context, then reuse the
        # same record/environment cache under a narrow context. The field's
        # cache key must include allowed_company_ids (and uid/su state), or a
        # prior True leaks into Python-side access-rule enforcement.
        wide_entity = mixed_entity.with_user(self.user).with_context(
            allowed_company_ids=full_scope,
        )
        self.assertTrue(wide_entity.eh_company_scope_allowed)
        narrow_entity = wide_entity.with_context(
            allowed_company_ids=restricted_scope,
        )
        self.assertFalse(narrow_entity.eh_company_scope_allowed)
        with self.assertRaises(AccessError):
            narrow_entity._eh_check_access('read')
        # Re-enter the restricted context to prove the rule is request-scoped,
        # not a stale result cached after the wider search above.
        for model_name, record in graph.items():
            self.assertFalse(
                visible(model_name, record, restricted_scope),
                "%s reused a stale wider-company decision" % model_name,
            )

    def _make_unrealised_basis(self, run=None):
        inventory = self._ensure_account(
            self.env, '1097', 'Consolidation Inventory Guard',
            'asset_current',
        )
        return self.env['eh.consol.unrealised.profit'].create({
            'run_id': (run or self.doc).id,
            'name': 'Guard unrealised basis',
            'unrealised_amount': 125.0,
            'inventory_account_id': inventory.id,
            'cogs_or_re_account_id': self.account_expense.id,
        })

    def test_unrealised_basis_is_immutable_after_compute(self):
        """A frozen source cannot be edited, moved, deleted or extended."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        basis = self._make_unrealised_basis()
        other_run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2027-01-01',
            'period_to': '2027-12-31',
        })
        self.doc.sudo().write({'state': 'computed'})
        actor_basis = basis.with_user(self.user)

        with self.assertRaises(UserError):
            actor_basis.write({'unrealised_amount': 250.0})
        with self.assertRaises(UserError):
            actor_basis.write({'run_id': other_run.id})
        with self.assertRaises(UserError):
            actor_basis.unlink()
        with self.assertRaises(UserError):
            self.env['eh.consol.unrealised.profit'].with_user(
                self.user,
            ).create({
                'run_id': self.doc.id,
                'name': 'Late fabricated margin',
                'unrealised_amount': 500.0,
            })

        basis.invalidate_recordset()
        self.assertEqual(basis.run_id, self.doc)
        self.assertEqual(basis.unrealised_amount, 125.0)

    def test_unrealised_basis_stays_frozen_with_move_history(self):
        """Resetting a run never makes prior GL source evidence editable."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        basis = self._make_unrealised_basis()
        history_move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2026-12-31',
        })
        self.env.cr.execute(
            "INSERT INTO eh_consol_run_move_history_rel (run_id, move_id) "
            "VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (self.doc.id, history_move.id),
        )
        self.doc.invalidate_recordset(['move_history_ids'])
        self.assertEqual(self.doc.state, 'draft')

        with self.assertRaises(UserError):
            basis.with_user(self.user).write({'name': 'Rewritten evidence'})
        with self.assertRaises(UserError):
            self.env['eh.consol.unrealised.profit'].with_user(
                self.user,
            ).create({
                'run_id': self.doc.id,
                'name': 'Late basis after reversal',
                'unrealised_amount': 1.0,
            })

    def test_unrealised_account_roles_are_server_validated(self):
        """RPC payloads cannot substitute an income account for inventory."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        with self.assertRaises(ValidationError):
            self.env['eh.consol.unrealised.profit'].with_user(
                self.user,
            ).create({
                'run_id': self.doc.id,
                'name': 'Wrong account classification',
                'unrealised_amount': 10.0,
                'inventory_account_id': self.account_revenue.id,
                'cogs_or_re_account_id': self.account_expense.id,
            })

        basis = self._make_unrealised_basis()
        with self.assertRaises(AccessError):
            basis.with_user(self.user).write({
                'presentation_currency_id': self.company.currency_id.id,
            })

    def test_unrealised_amount_must_be_finite_and_non_negative(self):
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        for invalid_amount in (-1.0, float('nan'), float('inf')):
            with self.assertRaises(ValidationError):
                self.env['eh.consol.unrealised.profit'].with_user(
                    self.user,
                ).create({
                    'run_id': self.doc.id,
                    'name': 'Invalid unrealised amount',
                    'unrealised_amount': invalid_amount,
                })

    def test_compute_revalidates_legacy_unrealised_basis(self):
        """A pre-control draft row cannot feed a substituted account to GL."""
        if not self.user:
            self.skipTest("No non-superuser manager could be provisioned.")
        basis = self._make_unrealised_basis()
        self.env.cr.execute(
            "UPDATE eh_consol_unrealised_profit "
            "SET inventory_account_id = %s WHERE id = %s",
            (self.account_revenue.id, basis.id),
        )
        basis.invalidate_recordset(['inventory_account_id'])
        with self.assertRaises(ValidationError):
            self.doc.with_user(self.user).action_compute()

    def test_124_upgrade_replaces_all_noupdate_company_scope_rules(self):
        expected = "[('eh_company_scope_allowed', '=', True)]"
        legacy_rules = (
            (
                'eh_account_consolidation.eh_consol_entity_company_rule',
                'member_ids.company_id',
            ),
            (
                'eh_account_consolidation.eh_consol_member_company_rule',
                'entity_id.member_ids.company_id',
            ),
            (
                'eh_account_consolidation.eh_consol_run_company_rule',
                'entity_id.member_ids.company_id',
            ),
            (
                'eh_account_consolidation.eh_consol_run_line_company_rule',
                'run_id.entity_id.member_ids.company_id',
            ),
            (
                'eh_account_consolidation.eh_consol_elimination_company_rule',
                'run_id.entity_id.member_ids.company_id',
            ),
            (
                'eh_account_consolidation.'
                'eh_consol_elimination_line_company_rule',
                'elimination_id.run_id.entity_id.member_ids.company_id',
            ),
            (
                'eh_account_consolidation.'
                'eh_consol_unrealised_profit_company_rule',
                'run_id.entity_id.member_ids.company_id',
            ),
        )
        rules = self.env['ir.rule'].browse([
            self.env.ref(xmlid).id for xmlid, _legacy_path in legacy_rules
        ])
        self.assertEqual(len(rules), 7)
        for xmlid, legacy_path in legacy_rules:
            rule = self.env.ref(xmlid)
            rule.write({
                'domain_force': "[('%s', 'not in', company_ids)]"
                % legacy_path,
            })

        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.2.4' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_consolidation_migration_124', migration_path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.2.3')
        rules.invalidate_recordset(['domain_force'])
        self.assertEqual(set(rules.mapped('domain_force')), {expected})

        # The forced update is idempotent and preserves the seven rule rows.
        rule_ids = rules.ids
        migration.migrate(self.env.cr, '19.0.1.2.3')
        rules.invalidate_recordset(['domain_force'])
        self.assertEqual(rules.ids, rule_ids)
        self.assertEqual(set(rules.mapped('domain_force')), {expected})

    def test_125_upgrade_quarantines_baseline_evidence_without_blessing(self):
        isolated_inverted = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2023-01-01',
            'period_to': '2023-12-31',
        })
        self.env.cr.execute(
            "UPDATE eh_consol_run SET period_from = %s, "
            "engine_provenance_version = 0 WHERE id = %s",
            ('2024-01-01', isolated_inverted.id),
        )
        isolated_inverted.invalidate_recordset()
        member_company = self.env['res.company'].create({
            'name': 'Legacy member currency repair',
            'currency_id': self.company.currency_id.id,
        })
        legacy_member = self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': member_company.id,
        })
        self.env.cr.execute(
            "UPDATE eh_consol_member SET investment_currency_id = NULL "
            "WHERE id = %s",
            (legacy_member.id,),
        )
        legacy_member.invalidate_recordset(['investment_currency_id'])
        legacy = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2025-01-01',
            'period_to': '2025-12-31',
        })
        elimination = self.env['eh.consol.elimination'].create({
            'run_id': legacy.id,
            'name': 'Legacy elimination evidence',
            'line_ids': [
                Command.create({
                    'account_id': self.account_cash.id,
                    'debit': 10.0,
                }),
                Command.create({
                    'account_id': self.account_revenue.id,
                    'credit': 10.0,
                }),
            ],
        })
        legacy_line = self.env['eh.consol.run.line'].create({
            'run_id': legacy.id,
            'kind': 'cta',
            'account_id': self.account_revenue.id,
            'amount': 12.0,
        })
        linked_move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': '2025-12-31',
        })
        self.env.cr.execute(
            "UPDATE eh_consol_run SET state = 'reviewed', move_id = %s, "
            "presentation_currency_id = NULL, cta_amount = 999.0, "
            "nci_amount = 888.0, goodwill_impairment_amount = 777.0, "
            "engine_provenance_version = 0, "
            "legacy_evidence_quarantined = FALSE WHERE id = %s",
            (linked_move.id, legacy.id),
        )
        self.env.cr.execute(
            "INSERT INTO eh_consol_run_move_history_rel (run_id, move_id) "
            "VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (legacy.id, linked_move.id),
        )
        self.env.cr.execute(
            "UPDATE eh_consol_elimination SET state = 'posted' WHERE id = %s",
            (elimination.id,),
        )
        self.env.cr.execute(
            "UPDATE eh_consol_elimination_line SET amount = 1234.0, "
            "presentation_currency_id = NULL WHERE elimination_id = %s",
            (elimination.id,),
        )
        legacy.invalidate_recordset()
        isolated_inverted.invalidate_recordset()
        elimination.invalidate_recordset()
        elimination.line_ids.invalidate_recordset()
        original_line_ids = legacy.line_ids.ids

        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.2.5' / 'pre-migration.py'
        )
        source = migration_path.read_text()
        # The deployed 1.2.0 schema has none of these later columns. The pre
        # migration must discover them before mentioning them in predicates.
        self.assertIn("_column_exists", source)
        for optional in (
            'reversal_move_id', 'reversed_at', 'reversed_by_id',
        ):
            self.assertIn(optional, source)
        spec = importlib.util.spec_from_file_location(
            'eh_consolidation_migration_125', migration_path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.2.0')

        legacy.invalidate_recordset()
        elimination.invalidate_recordset()
        elimination.line_ids.invalidate_recordset()
        self.assertTrue(legacy.legacy_evidence_quarantined)
        self.assertTrue(isolated_inverted.legacy_evidence_quarantined)
        legacy_member.invalidate_recordset(['investment_currency_id'])
        self.assertEqual(
            legacy_member.investment_currency_id,
            self.entity.presentation_currency_id,
        )
        self.assertEqual(legacy.engine_provenance_version, 0)
        self.assertEqual(legacy.state, 'reviewed')
        self.assertEqual(legacy.move_id, linked_move)
        self.assertIn(linked_move, legacy.move_history_ids)
        self.assertEqual(legacy.line_ids.ids, original_line_ids)
        self.assertEqual(legacy.cta_amount, legacy_line.amount)
        self.assertEqual(legacy.nci_amount, 0.0)
        self.assertEqual(legacy.goodwill_impairment_amount, 0.0)
        self.assertEqual(
            legacy.presentation_currency_id,
            self.entity.presentation_currency_id,
        )
        self.assertEqual(
            set(elimination.line_ids.mapped('amount')), {10.0, -10.0},
        )
        with self.assertRaises(UserError):
            legacy.action_close()

        # A repeat cannot quarantine a version-1 current-engine graph or append
        # duplicate quarantine notes to the already-preserved legacy row.
        current = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2024-01-01',
            'period_to': '2024-12-31',
        })
        self.env.cr.execute(
            "UPDATE eh_consol_run SET state = 'computed', "
            "engine_provenance_version = 1 WHERE id = %s",
            (current.id,),
        )
        current.invalidate_recordset()
        note = legacy.legacy_evidence_quarantine_reason
        migration.migrate(self.env.cr, '19.0.1.2.0')
        legacy.invalidate_recordset()
        current.invalidate_recordset()
        self.assertEqual(legacy.legacy_evidence_quarantine_reason, note)
        self.assertFalse(current.legacy_evidence_quarantined)
        self.assertEqual(current.engine_provenance_version, 1)

    def test_125_pre_migration_executes_against_baseline_optional_schema(self):
        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.2.5' / 'pre-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_consolidation_migration_125_baseline', migration_path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        class BaselineCursor:
            def __init__(self):
                self.queries = []
                self._row = None

            def execute(self, query, params=None):
                text = str(query)
                self.queries.append((text, params))
                if 'information_schema.columns' in text:
                    self._row = None
                elif 'to_regclass' in text:
                    self._row = (None,)
                else:
                    self._row = None

            def fetchone(self):
                return self._row

        baseline = BaselineCursor()
        migration.migrate(baseline, '19.0.1.2.0')
        quarantine_sql = next(
            query for query, _params in baseline.queries
            if 'legacy_evidence_quarantined = TRUE' in query
        )
        self.assertNotIn('run.reversal_move_id', quarantine_sql)
        self.assertNotIn('run.reversed_at', quarantine_sql)
        self.assertNotIn('run.reversed_by_id', quarantine_sql)
        self.assertNotIn('eh_consol_run_move_history_rel', quarantine_sql)

    def test_125_pre_migration_executes_on_real_104_schema(self):
        """The shipped 1.0.4 column set upgrades without UndefinedColumn."""
        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.2.5' / 'pre-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_consolidation_migration_125_real_104', migration_path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        schema = 'eh_consol_legacy_%d' % self.doc.id

        class RollbackLegacyProbe(Exception):
            pass

        try:
            with self.env.cr.savepoint():
                self.env.cr.execute('CREATE SCHEMA %s' % schema)
                self.env.cr.execute('SET LOCAL search_path TO %s' % schema)
                self.env.cr.execute(
                    "CREATE TABLE res_currency ("
                    "id integer PRIMARY KEY, rounding numeric)"
                )
                self.env.cr.execute(
                    "CREATE TABLE eh_consol_entity ("
                    "id integer PRIMARY KEY, presentation_currency_id integer)"
                )
                self.env.cr.execute(
                    "CREATE TABLE eh_consol_run ("
                    "id integer PRIMARY KEY, entity_id integer, "
                    "period_from date, period_to date, state varchar, "
                    "presentation_currency_id integer, cta_amount numeric, "
                    "nci_amount numeric, computed_at timestamp, "
                    "computed_by_id integer, reviewed_at timestamp, "
                    "reviewed_by_id integer, closed_at timestamp, "
                    "closed_by_id integer)"
                )
                self.env.cr.execute(
                    "CREATE TABLE eh_consol_member ("
                    "id integer PRIMARY KEY, entity_id integer, "
                    "ownership_pct numeric)"
                )
                self.env.cr.execute(
                    "CREATE TABLE eh_consol_run_line ("
                    "id integer PRIMARY KEY, run_id integer, kind varchar, "
                    "amount numeric, presentation_currency_id integer)"
                )
                self.env.cr.execute(
                    "CREATE TABLE eh_consol_elimination ("
                    "id integer PRIMARY KEY, run_id integer, state varchar)"
                )
                self.env.cr.execute(
                    "CREATE TABLE eh_consol_elimination_line ("
                    "id integer PRIMARY KEY, elimination_id integer, "
                    "debit numeric, credit numeric, amount numeric, "
                    "presentation_currency_id integer)"
                )
                self.env.cr.execute(
                    "INSERT INTO res_currency VALUES (1, 0.01); "
                    "INSERT INTO eh_consol_entity VALUES (1, 1); "
                    "INSERT INTO eh_consol_run "
                    "(id, entity_id, period_from, period_to, state, "
                    "presentation_currency_id, cta_amount, nci_amount) "
                    "VALUES (1, 1, '2025-01-01', '2025-12-31', 'draft', "
                    "1, 0, 0), (2, 1, '2024-01-01', '2024-12-31', "
                    "'reviewed', 1, 0, 0)"
                )
                migration.migrate(self.env.cr, '19.0.1.0.4')
                self.env.cr.execute(
                    "SELECT id, engine_provenance_version, "
                    "legacy_evidence_quarantined FROM eh_consol_run "
                    "ORDER BY id"
                )
                self.assertEqual(
                    self.env.cr.fetchall(),
                    [(1, 0, False), (2, 0, True)],
                )
                raise RollbackLegacyProbe()
        except RollbackLegacyProbe:
            pass

    def test_126_upgrade_quarantines_evidence_but_not_empty_draft(self):
        legacy_draft = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2022-01-01',
            'period_to': '2022-12-31',
        })
        legacy_evidence = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2023-01-01',
            'period_to': '2023-12-31',
        })
        self.env.cr.execute(
            "UPDATE eh_consol_run SET state = 'computed' WHERE id = %s",
            (legacy_evidence.id,),
        )
        legacy_evidence.invalidate_recordset(['state'])
        current_entity = self.env['eh.consol.entity'].create({
            'name': 'V2 Current Group',
            'code': 'v2_current_group',
            'parent_company_id': self.company.id,
        })
        current = self.env['eh.consol.run'].create({
            'entity_id': current_entity.id,
            'period_from': '2021-01-01',
            'period_to': '2021-12-31',
        })
        current.action_compute()
        self.assertEqual(current.engine_provenance_version, 2)

        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.2.6' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_consolidation_migration_126', migration_path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.2.5')
        legacy_draft.invalidate_recordset()
        legacy_evidence.invalidate_recordset()
        current.invalidate_recordset()
        self.assertFalse(legacy_draft.legacy_evidence_quarantined)
        self.assertTrue(legacy_evidence.legacy_evidence_quarantined)
        self.assertFalse(current.legacy_evidence_quarantined)
        note = legacy_evidence.legacy_evidence_quarantine_reason
        migration.migrate(self.env.cr, '19.0.1.2.5')
        legacy_evidence.invalidate_recordset()
        self.assertEqual(
            legacy_evidence.legacy_evidence_quarantine_reason, note,
        )

    def test_129_upgrade_releases_only_provably_empty_quarantined_drafts(self):
        deletable = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2031-01-01',
            'period_to': '2031-12-31',
        })
        rebuildable = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2032-01-01',
            'period_to': '2032-12-31',
        })
        evidenced = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2033-01-01',
            'period_to': '2033-12-31',
        })
        self.env['eh.consol.run.line'].create({
            'run_id': evidenced.id,
            'kind': 'parent_balance',
            'account_id': self.account_cash.id,
            'amount': 1.0,
        })
        self.env.cr.execute(
            "UPDATE eh_consol_run SET legacy_evidence_quarantined = TRUE, "
            "legacy_evidence_quarantine_reason = %s WHERE id IN %s",
            ('[19.0.1.2.6] blanket legacy quarantine',
             tuple((deletable | rebuildable | evidenced).ids)),
        )
        (deletable | rebuildable | evidenced).invalidate_recordset()
        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.2.9' / 'post-migration.py'
        )
        spec = importlib.util.spec_from_file_location(
            'eh_consolidation_migration_129', migration_path,
        )
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.2.8')
        (deletable | rebuildable | evidenced).invalidate_recordset()
        self.assertFalse(deletable.legacy_evidence_quarantined)
        self.assertFalse(rebuildable.legacy_evidence_quarantined)
        self.assertTrue(evidenced.legacy_evidence_quarantined)
        self.assertIn('released', rebuildable.legacy_evidence_quarantine_reason)

        deletable.unlink()
        self.assertFalse(deletable.exists())
        rebuildable.action_compute()
        self.assertEqual(rebuildable.engine_provenance_version, 2)
        with self.assertRaises(UserError):
            evidenced.action_compute()
