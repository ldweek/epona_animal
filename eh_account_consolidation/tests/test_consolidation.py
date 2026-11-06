# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Group consolidation tests.

Covers entity / member configuration constraints, the run lifecycle,
elimination balance enforcement, and the constraint that the parent
company cannot be a member.
"""

from unittest.mock import patch

from psycopg2 import OperationalError, errorcodes, errors

from odoo import fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools import float_is_zero

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase

from .common import (
    account_company_field as _acc_company_field,
    make_account as _make_account,
    make_general_journal as _make_general_journal,
    make_unaffected_account as _make_unaffected_account,
    post_move as _post_move,
)


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolEntity(EhAccountIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.Entity = self.env['eh.consol.entity']
        self.Member = self.env['eh.consol.member']

    def test_entity_create_happy_path(self):
        e = self.Entity.create({
            'name': 'Acme Group',
            'code': 'acme_group',
            'presentation_currency_id': self.company.currency_id.id,
            'parent_company_id': self.company.id,
        })
        self.assertEqual(e.code, 'acme_group')
        self.assertEqual(e.member_count, 0)

    def test_code_format_constraint(self):
        with self.assertRaises(ValidationError):
            self.Entity.create({
                'name': 'Bad code',
                'code': '123-bad',
                'parent_company_id': self.company.id,
            })

    def test_unique_code_constraint(self):
        self.Entity.create({
            'name': 'A', 'code': 'unique_code',
            'parent_company_id': self.company.id,
        })
        with self.assertRaises(ValidationError):
            self.Entity.create({
                'name': 'B', 'code': 'unique_code',
                'parent_company_id': self.company.id,
            })

    def test_parent_cannot_be_member(self):
        e = self.Entity.create({
            'name': 'X', 'code': 'parent_test',
            'parent_company_id': self.company.id,
        })
        with self.assertRaises(ValidationError):
            self.Member.create({
                'entity_id': e.id,
                'company_id': self.company.id,  # same as parent
                'ownership_pct': 100.0,
            })

    def test_ownership_bounds(self):
        e = self.Entity.create({
            'name': 'X', 'code': 'ownership_test',
            'parent_company_id': self.company.id,
        })
        # Need a different company to be a member.
        sub = self.env['res.company'].create({
            'name': 'Sub Co', 'currency_id': self.company.currency_id.id,
        })
        with self.assertRaises(ValidationError):
            self.Member.create({
                'entity_id': e.id, 'company_id': sub.id,
                'ownership_pct': 150.0,
            })
        with self.assertRaises(ValidationError):
            self.Member.create({
                'entity_id': e.id, 'company_id': sub.id,
                'ownership_pct': -10.0,
            })

    def test_member_accounting_inputs_must_be_finite(self):
        self.env.user.group_ids |= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        entity = self.Entity.create({
            'name': 'Finite Group', 'code': 'finite_member_group',
            'parent_company_id': self.company.id,
        })
        sub = self.env['res.company'].create({
            'name': 'Finite Sub', 'currency_id': self.company.currency_id.id,
        })
        for field_name in (
            'ownership_pct', 'investment_amount', 'acquisition_equity',
            'historical_rate', 'nci_fair_value', 'disposal_pct',
        ):
            for invalid in (float('nan'), float('inf'), float('-inf')):
                with self.assertRaises(ValidationError):
                    self.Member.create({
                        'entity_id': entity.id,
                        'company_id': sub.id,
                        field_name: invalid,
                    })

    def test_posting_account_roles_are_server_enforced(self):
        entity = self.Entity.create({
            'name': 'Role Group', 'code': 'role_group',
            'parent_company_id': self.company.id,
        })
        with self.assertRaises(ValidationError):
            entity.write({'cta_account_id': self.account_revenue.id})

        sub = self.env['res.company'].create({
            'name': 'Role Sub', 'currency_id': self.company.currency_id.id,
        })
        with self.assertRaises(ValidationError):
            self.Member.create({
                'entity_id': entity.id,
                'company_id': sub.id,
                'investment_account_id': self.account_revenue.id,
            })


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolRun(EhAccountIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.manager_group = self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        self.env.user.group_ids |= self.manager_group
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'Test Group',
            'code': 'test_group_run',
            'parent_company_id': self.company.id,
            'presentation_currency_id': self.company.currency_id.id,
        })

    def _make_run(self):
        return self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })

    def test_run_sequence_assigned(self):
        run = self._make_run()
        self.assertNotEqual(run.name, '/')
        self.assertTrue(run.name.startswith('CONS/'))

    def test_run_identity_is_server_owned(self):
        with self.assertRaises(AccessError):
            self.env['eh.consol.run'].create({
                'name': 'FORGED/RUN',
                'entity_id': self.entity.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
            })
        run = self._make_run()
        with self.assertRaises(AccessError):
            run.sudo().write({'name': 'RENAMED/AFTER/CREATE'})

    def test_period_must_be_chronological(self):
        with self.assertRaises(UserError):
            self.env['eh.consol.run'].create({
                'entity_id': self.entity.id,
                'period_from': '2026-12-31',
                'period_to': '2026-01-01',
            })
        run = self._make_run()
        with self.assertRaises(UserError):
            run.write({'period_from': '2027-01-01'})

        # Prove the locked action gate catches a pre-control/raw-SQL row too.
        self.env.cr.execute(
            "UPDATE eh_consol_run SET period_from = %s WHERE id = %s",
            ('2027-01-01', run.id),
        )
        run.invalidate_recordset(['period_from'])
        with self.assertRaises(UserError):
            run.action_compute()

    def test_run_impairment_account_role_is_server_enforced(self):
        with self.assertRaises(UserError):
            self.env['eh.consol.run'].create({
                'entity_id': self.entity.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
                'goodwill_impairment_account_id': self.account_revenue.id,
            })

    def test_run_state_default_draft(self):
        run = self._make_run()
        self.assertEqual(run.state, 'draft')

    def test_goodwill_recoverable_amount_must_be_finite(self):
        for invalid in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(UserError):
                self.env['eh.consol.run'].create({
                    'entity_id': self.entity.id,
                    'period_from': '2026-01-01',
                    'period_to': '2026-12-31',
                    'goodwill_recoverable_amount': invalid,
                })

    def test_compute_only_from_draft(self):
        run = self._make_run()
        run.action_compute()
        # Second compute call refused because state is now computed.
        with self.assertRaises(UserError):
            run.action_compute()

    def test_trial_balance_aggregates_accounts_and_drills_to_sources(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25.0},
                {'account': self.account_revenue, 'credit': 25.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        trial_balance = self.env['eh.consol.trial.balance'].search([
            ('run_id', '=', run.id),
        ])
        self.assertEqual(len(trial_balance), 2)
        cash = trial_balance.filtered(
            lambda row: row.account_id == self.account_cash)
        revenue = trial_balance.filtered(
            lambda row: row.account_id == self.account_revenue)
        self.assertEqual(cash.parent_amount, 25.0)
        self.assertEqual(cash.total_amount, 25.0)
        self.assertEqual(revenue.parent_amount, -25.0)
        self.assertEqual(revenue.total_amount, -25.0)
        action = cash.action_drilldown()
        self.assertEqual(action['res_model'], 'eh.consol.run.line')
        self.assertIn(('run_id', '=', run.id), action['domain'])
        self.assertIn(
            ('account_id', '=', self.account_cash.id), action['domain'])
        overview = run.action_view_trial_balance()
        self.assertEqual(overview['res_model'], 'eh.consol.trial.balance')
        self.assertEqual(overview['domain'], [('run_id', '=', run.id)])

    def test_company_closure_access_has_no_elevated_aml_count_probe(self):
        run = self._make_run()
        seen = []
        original_execute = self.env.cr.execute

        def record_execute(query, params=None, *args, **kwargs):
            seen.append(str(query))
            return original_execute(query, params, *args, **kwargs)

        with patch.object(self.env.cr, 'execute', record_execute):
            run._eh_assert_company_closure()
        count_queries = [
            query for query in seen
            if 'account_move_line' in query.lower()
            and 'count(' in query.lower()
        ]
        self.assertFalse(
            count_queries,
            "company closure must not compare visible and sudo AML counts",
        )

    def test_source_change_after_compute_requires_recompute(self):
        self.env.user.group_ids |= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        run = self._make_run()
        run.action_compute()
        stamped = run.source_move_version
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25.0},
                {'account': self.account_revenue, 'credit': 25.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        self.company.invalidate_recordset(['eh_move_version'])
        self.assertGreater(self.company.eh_move_version, stamped)
        with self.assertRaises(UserError):
            run.action_review()
        # Staleness must not strand the correction corridor.
        run.action_reset_to_draft()
        self.assertEqual(run.source_move_version, 0)
        run.action_compute()
        self.assertEqual(
            run.source_move_version, self.company.eh_move_version,
        )

    def test_source_company_lock_serializes_concurrent_version_writer(self):
        run = self._make_run()
        seen = []
        original_execute = self.env.cr.execute

        def record_execute(query, params=None, *args, **kwargs):
            seen.append(str(query))
            return original_execute(query, params, *args, **kwargs)

        with patch.object(self.env.cr, 'execute', record_execute):
            run._eh_lock_for_transition()
        company_lock = next(
            index for index, query in enumerate(seen)
            if 'FROM res_company' in query and 'FOR UPDATE' in query
        )
        run_lock = next(
            index for index, query in enumerate(seen)
            if 'FROM eh_consol_run' in query and 'FOR UPDATE' in query
        )
        self.assertLess(company_lock, run_lock)

        # A real independent backend cannot enter the source-version writer
        # while the compute/review transition owns the company row.  Base's
        # eh_move_version bump takes this same row lock, so either ordering is
        # safe: the later transaction sees the earlier committed graph/version.
        with self.env.registry.cursor() as other_cr:
            with self.assertRaises(OperationalError) as caught:
                other_cr.execute(
                    "SELECT id FROM res_company WHERE id = %s "
                    "FOR UPDATE NOWAIT",
                    (self.company.id,),
                )
            self.assertEqual(
                caught.exception.pgcode,
                errorcodes.LOCK_NOT_AVAILABLE,
            )
            other_cr.rollback()

    def test_review_only_from_computed(self):
        run = self._make_run()
        with self.assertRaises(UserError):
            run.action_review()

    def test_review_snapshots_exact_period_nci_profit_and_oci(self):
        sub = self.env['res.company'].create({
            'name': 'NCI Flow Sub',
            'currency_id': self.company.currency_id.id,
        })
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': sub.id,
            'ownership_pct': 80.0,
        })
        run = self._make_run()
        run.action_compute()
        run.action_review()
        self.assertFalse(run.nci_period_flow_available)
        run.action_reset_to_draft()
        run.action_compute()
        run.write({
            'nci_period_profit_input': 1250.0,
            'nci_period_oci_input': -50.0,
            'nci_period_flow_confirmed': True,
            'nci_period_flow_basis': 'NCI allocation schedule NCI-2026',
        })
        run.action_review()
        self.assertTrue(run.nci_period_flow_available)
        self.assertAlmostEqual(run.nci_period_profit_amount, 1250.0, places=2)
        self.assertAlmostEqual(run.nci_period_oci_amount, -50.0, places=2)
        self.assertAlmostEqual(
            run.nci_period_comprehensive_income_amount, 1200.0, places=2)
        with self.assertRaisesRegex(UserError, 'frozen after Review'):
            run.write({'nci_period_profit_input': 9999.0})

    def test_nci_period_flow_outputs_are_server_owned(self):
        run = self._make_run()
        for field_name in (
            'nci_period_profit_amount', 'nci_period_oci_amount',
            'nci_period_comprehensive_income_amount',
            'nci_period_flow_available',
        ):
            with self.subTest(field=field_name), self.assertRaises(AccessError):
                run.write({field_name: 1})

    def test_close_only_from_reviewed(self):
        run = self._make_run()
        run.action_compute()
        with self.assertRaises(UserError):
            run.action_close()
        run.action_review()
        run.action_close()
        self.assertEqual(run.state, 'closed')

    def test_non_manager_cannot_self_review_or_close_by_rpc(self):
        clerk = self.env['res.users'].create({
            'name': 'Consolidation preparer',
            'login': 'consolidation_preparer_review_gate',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        self.assertFalse(clerk.has_group(
            'eh_account_base.group_eh_manager',
        ))
        run = self._make_run()
        run.action_compute()
        with self.assertRaises(UserError):
            run.with_user(clerk).action_review()
        run.invalidate_recordset()
        self.assertEqual(run.state, 'computed')
        self.assertFalse(run.reviewed_by_id)

        run.action_review()
        with self.assertRaises(UserError):
            run.with_user(clerk).action_close()
        run.invalidate_recordset()
        self.assertEqual(run.state, 'reviewed')
        self.assertFalse(run.closed_by_id)

    def test_multi_run_review_failure_rolls_back_first_signoff(self):
        first = self._make_run()
        second = self._make_run()
        runs = first | second
        runs.action_compute()
        self.env.cr.execute(
            "UPDATE eh_consol_run SET cta_amount = 777.0 WHERE id = %s",
            (first.id,),
        )
        first.invalidate_recordset(['cta_amount'])
        self.assertEqual(first.cta_amount, 777.0)
        RunClass = type(first)
        original_write = RunClass.write

        def fail_second_signoff(records, vals):
            if vals.get('state') == 'reviewed' and second in records:
                raise UserError("injected second-run review failure")
            return original_write(records, vals)

        with patch.object(RunClass, 'write', fail_second_signoff):
            try:
                runs.action_review()
            except UserError:
                pass
            else:
                self.fail("Injected second-run review failure was not raised")
        runs.invalidate_recordset()
        self.assertEqual(set(runs.mapped('state')), {'computed'})
        self.assertFalse(first.reviewed_at)
        self.assertFalse(first.reviewed_by_id)
        self.assertFalse(second.reviewed_at)
        self.assertFalse(second.reviewed_by_id)
        self.assertEqual(
            first.cta_amount, 777.0,
            "Prepare-time restamping must roll back with a late action error.",
        )

    def test_multi_run_close_failure_rolls_back_first_close(self):
        first = self._make_run()
        second = self._make_run()
        runs = first | second
        runs.action_compute()
        runs.action_review()
        RunClass = type(first)
        original_write = RunClass.write

        def fail_second_close(records, vals):
            if vals.get('state') == 'closed' and second in records:
                raise UserError("injected second-run close failure")
            return original_write(records, vals)

        with patch.object(RunClass, 'write', fail_second_close):
            try:
                runs.action_close()
            except UserError:
                pass
            else:
                self.fail("Injected second-run close failure was not raised")
        runs.invalidate_recordset()
        self.assertEqual(set(runs.mapped('state')), {'reviewed'})
        self.assertFalse(first.closed_at)
        self.assertFalse(first.closed_by_id)
        self.assertFalse(second.closed_at)
        self.assertFalse(second.closed_by_id)

    def test_compute_picks_up_parent_balances(self):
        """A balanced move on the parent surfaces as parent_balance lines."""
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        parent_lines = run.line_ids.filtered(
            lambda l: l.kind == 'parent_balance',
        )
        self.assertGreater(len(parent_lines), 0)
        # Cash account parent line should equal 1000 (debit positive).
        cash_line = parent_lines.filtered(
            lambda l: l.account_id == self.account_cash,
        )
        self.assertAlmostEqual(cash_line.amount, 1000.0, places=2)


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolClosedRunFreeze(EhAccountIntegrationTestCase):
    """A closed consolidation run is signed and cited in audit, so it must be
    locked and reproducible: its lines are frozen (no write / unlink), it
    cannot be reset or recomputed in place, and only a manager can reopen it
    (which is recorded). After a manager reopen, reset and recompute work
    again.
    """

    def setUp(self):
        super().setUp()
        self.manager_group = self.env.ref('eh_account_base.group_eh_manager')
        self.env.user.group_ids |= self.manager_group
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'Freeze Group',
            'code': 'freeze_group_run',
            'parent_company_id': self.company.id,
            'presentation_currency_id': self.company.currency_id.id,
        })

    def _closed_run(self):
        """Create a run, compute it (parent balance present), review and
        close it, so it carries at least one frozen line."""
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        run.action_compute()
        run.action_review()
        run.action_close()
        self.assertEqual(run.state, 'closed')
        self.assertTrue(run.line_ids, "closed run must carry lines to freeze")
        return run

    def test_closed_run_lines_are_frozen_against_write(self):
        run = self._closed_run()
        line = run.line_ids[0]
        with self.assertRaises(UserError):
            line.write({'amount': line.amount + 999.0})

    def test_closed_run_lines_are_frozen_against_unlink(self):
        run = self._closed_run()
        with self.assertRaises(UserError):
            run.line_ids[0].unlink()

    def test_closed_run_line_cannot_be_appended_by_hand(self):
        """Create-append negative test: a user (here a manager, who holds
        create rights on the line model) must not be able to append a run line
        to a closed run. Without the create() guard the append succeeds and
        silently moves the consolidated totals, defeating the frozen-run
        control.
        """
        self.env.user.group_ids |= self.manager_group
        run = self._closed_run()
        before = len(run.line_ids)
        with self.assertRaises(UserError):
            self.env['eh.consol.run.line'].create({
                'run_id': run.id,
                'account_id': self.account_cash.id,
                'kind': 'parent_balance',
                'amount': 999999.0,
            })
        self.assertEqual(
            len(run.line_ids), before,
            "no line may be appended to a closed run",
        )

    def test_computed_run_line_cannot_be_appended_by_hand(self):
        """A computed (not yet closed) run is already settled: its lines are
        engine generated, so a hand-added line would move the totals. The
        create() guard blocks the append in 'computed' and 'reviewed' too, not
        only 'closed'.
        """
        self.env.user.group_ids |= self.manager_group
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        before = len(run.line_ids)
        with self.assertRaises(UserError):
            self.env['eh.consol.run.line'].create({
                'run_id': run.id,
                'account_id': self.account_cash.id,
                'kind': 'parent_balance',
                'amount': 999999.0,
            })
        self.assertEqual(len(run.line_ids), before)
        # reviewed too
        run.action_review()
        with self.assertRaises(UserError):
            self.env['eh.consol.run.line'].create({
                'run_id': run.id,
                'account_id': self.account_cash.id,
                'kind': 'parent_balance',
                'amount': 999999.0,
            })
        self.assertEqual(len(run.line_ids), before)

    def test_engine_build_path_still_creates_lines_on_settled_run(self):
        """The create() guard must exempt the engine's own superuser build
        path: action_compute rebuilds lines while the run is draft and the
        transition to computed only happens afterwards, so a normal compute is
        unaffected and the settled run still carries its engine lines.
        """
        run = self._closed_run()
        self.assertTrue(
            run.line_ids,
            "the engine build path must still populate a settled run's lines",
        )

    def test_sudo_alone_does_not_bypass_the_append_guard(self):
        """sudo / superuser is NOT an engine signal: even a superuser create
        against a settled run is blocked. Only the engine's controlled build
        context is exempt, so the freeze cannot be defeated by escalating to
        sudo.
        """
        run = self._closed_run()
        before = len(run.line_ids)
        with self.assertRaises(UserError):
            self.env['eh.consol.run.line'].sudo().create({
                'run_id': run.id,
                'account_id': self.account_cash.id,
                'kind': 'parent_balance',
                'amount': 1.0,
            })
        self.assertEqual(len(run.line_ids), before)

    def test_engine_context_is_exempt_from_the_append_guard(self):
        """The engine's controlled build path flags itself with the engine
        context key, so its own line creation is allowed even on a settled
        run (this is the exact path action_compute / action_impair_goodwill
        use). Default/unset behaviour for the engine is therefore unchanged.
        """
        from odoo.addons.eh_account_consolidation.models.consol_run_line \
            import CONSOL_ENGINE_CAPABILITY, CONSOL_ENGINE_CTX
        run = self._closed_run()
        before = len(run.line_ids)
        # A JSON-RPC client can forge a truthy context value, so truthiness is
        # deliberately insufficient even for an su caller.
        with self.assertRaises(UserError):
            self.env['eh.consol.run.line'].sudo().with_context(
                **{CONSOL_ENGINE_CTX: True}).create({
                    'run_id': run.id,
                    'account_id': self.account_cash.id,
                    'kind': 'parent_balance',
                    'amount': 1.0,
                })
        self.env['eh.consol.run.line'].sudo().with_context(
            **{CONSOL_ENGINE_CTX: CONSOL_ENGINE_CAPABILITY}).create({
                'run_id': run.id,
                'account_id': self.account_cash.id,
                'kind': 'parent_balance',
                'amount': 1.0,
            })
        self.assertEqual(len(run.line_ids), before + 1)

    def test_closed_run_cannot_be_reset_even_by_manager(self):
        self.env.user.group_ids |= self.manager_group
        run = self._closed_run()
        with self.assertRaises(UserError):
            run.action_reset_to_draft()
        self.assertEqual(run.state, 'closed')
        self.assertTrue(run.line_ids, "lines must survive a refused reset")

    def test_closed_run_cannot_be_recomputed(self):
        self.env.user.group_ids |= self.manager_group
        run = self._closed_run()
        with self.assertRaises(UserError):
            run.action_compute()
        self.assertEqual(run.state, 'closed')

    def test_non_manager_cannot_reopen(self):
        run = self._closed_run()
        # A user who is explicitly NOT a consolidation manager.
        non_manager = self.env['res.users'].create({
            'name': 'Consol Clerk',
            'login': 'consol_clerk_reopen',
            'group_ids': [(6, 0, [
                self.env.ref('account.group_account_user').id,
            ])],
        })
        self.assertFalse(
            non_manager.has_group('eh_account_base.group_eh_manager'))
        with self.assertRaises(UserError):
            run.with_user(non_manager).action_reopen()
        self.assertEqual(run.state, 'closed')

    def test_manager_reopen_then_reset_then_recompute(self):
        self.env.user.group_ids |= self.manager_group
        run = self._closed_run()
        original_line_count = len(run.line_ids)
        self.assertGreater(original_line_count, 0)
        # Reopen: back to reviewed, lines preserved, close audit fields cleared.
        run.action_reopen()
        self.assertEqual(run.state, 'reviewed')
        self.assertFalse(run.closed_at)
        self.assertFalse(run.closed_by_id)
        self.assertEqual(len(run.line_ids), original_line_count,
                         "reopen preserves lines until reset")
        # After reopen the lines are no longer frozen: reset drops them and
        # recompute rebuilds a fresh, reproducible set.
        run.action_reset_to_draft()
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.line_ids, "reset drops lines once reopened")
        self.assertFalse(run.computed_at)
        self.assertFalse(run.computed_by_id)
        self.assertFalse(run.reviewed_at)
        self.assertFalse(run.reviewed_by_id)
        self.assertFalse(run.consolidation_warning)
        self.assertEqual(run.goodwill_impairment_amount, 0.0)
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        cash_line = run.line_ids.filtered(
            lambda l: l.kind == 'parent_balance'
            and l.account_id == self.account_cash,
        )
        self.assertAlmostEqual(cash_line.amount, 1000.0, places=2)

    def test_reopen_only_from_closed(self):
        self.env.user.group_ids |= self.manager_group
        run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        with self.assertRaises(UserError):
            run.action_reopen()


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolElimination(EhAccountIntegrationTestCase):

    def setUp(self):
        super().setUp()
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'X', 'code': 'elim_test',
            'parent_company_id': self.company.id,
        })
        self.run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        self.Elim = self.env['eh.consol.elimination']

    def test_post_blocked_when_unbalanced(self):
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Unbalanced test',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 50.0,
                }),
            ],
        })
        with self.assertRaises(UserError):
            elim.action_post()

    def test_post_balanced_succeeds(self):
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Balanced test',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                }),
            ],
        })
        elim.action_post()
        self.assertEqual(elim.state, 'posted')

    def test_post_prepare_restamp_rolls_back_on_second_failure(self):
        eliminations = self.Elim.create([
            {
                'run_id': self.run.id,
                'name': name,
                'line_ids': [
                    (0, 0, {
                        'account_id': self.account_cash.id,
                        'debit': 100.0,
                    }),
                    (0, 0, {
                        'account_id': self.account_revenue.id,
                        'credit': 100.0,
                    }),
                ],
            }
            for name in ('Atomic elimination first', 'Atomic elimination second')
        ])
        first, second = eliminations.sorted('id')
        first_line = first.line_ids.sorted('id')[0]
        # Drain the stored-compute queue before seeding the deliberately stale
        # SQL value. Otherwise a later ORM read may first persist the pending
        # debit-credit computation and overwrite the test fixture itself.
        first_line.flush_recordset(['debit', 'credit', 'amount'])
        self.env.cr.execute(
            "UPDATE eh_consol_elimination_line SET amount = 777.0 "
            "WHERE id = %s",
            (first_line.id,),
        )
        first_line.invalidate_recordset(['amount'])
        self.assertEqual(first_line.amount, 777.0)
        EliminationClass = type(first)
        original_write = EliminationClass.write

        def fail_second_post(records, vals):
            if vals.get('state') == 'posted' and second.id in records.ids:
                raise UserError('Injected second elimination failure')
            return original_write(records, vals)

        with patch.object(EliminationClass, 'write', fail_second_post):
            try:
                eliminations.action_post()
            except UserError:
                pass
            else:
                self.fail("Injected second elimination failure was not raised")
        eliminations.invalidate_recordset()
        first_line.invalidate_recordset(['amount'])
        self.assertEqual(set(eliminations.mapped('state')), {'draft'})
        self.assertEqual(
            first_line.amount, 777.0,
            "Prepare-time elimination restamp must roll back with action.",
        )

    def test_reset_clears_current_cycle_posting_stamps(self):
        self.env.user.group_ids |= self.env.ref(
            'eh_account_base.group_eh_manager',
        )
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Reset stamps',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                }),
            ],
        })
        elim.action_post()
        self.assertTrue(elim.posted_at)
        elim.action_cancel()
        elim.action_reset_to_draft()
        self.assertEqual(elim.state, 'draft')
        self.assertFalse(elim.posted_at)
        self.assertFalse(elim.posted_by_id)

    def test_run_unlink_cannot_cascade_posted_elimination_evidence(self):
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Retained elimination',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                }),
            ],
        })
        elim.action_post()
        with self.assertRaises(UserError):
            self.run.unlink()
        self.assertTrue(elim.exists())
        self.assertEqual(elim.state, 'posted')

    def test_posted_elimination_freezes_run_entity_and_member_basis(self):
        sub = self.env['res.company'].create({
            'name': 'Elimination member',
            'currency_id': self.company.currency_id.id,
        })
        member = self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': sub.id,
        })
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Freeze complete basis',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                }),
            ],
        })
        elim.action_post()
        with self.assertRaises(UserError):
            self.run.write({'period_from': '2026-02-01'})
        alternate = self.env['res.currency'].create({
            'name': 'ELF', 'symbol': 'E', 'rounding': 0.01, 'active': True,
        })
        with self.assertRaises(UserError):
            self.entity.write({'presentation_currency_id': alternate.id})
        with self.assertRaises(UserError):
            member.write({'ownership_pct': 80.0})

    def test_post_blocked_with_no_lines(self):
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Empty test',
        })
        with self.assertRaises(UserError):
            elim.action_post()

    def test_line_cannot_have_both_debit_and_credit(self):
        with self.assertRaises(ValidationError):
            self.Elim.create({
                'run_id': self.run.id,
                'name': 'Both sides',
                'line_ids': [
                    (0, 0, {
                        'account_id': self.account_cash.id,
                        'debit': 100.0,
                        'credit': 100.0,
                    }),
                ],
            })

    def test_line_signed_amount(self):
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Signed amount',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                }),
            ],
        })
        debits = elim.line_ids.filtered(lambda l: l.debit > 0)
        credits = elim.line_ids.filtered(lambda l: l.credit > 0)
        self.assertAlmostEqual(debits.amount, 100.0, places=2)
        self.assertAlmostEqual(credits.amount, -100.0, places=2)

    def test_elimination_calculated_outputs_are_server_owned_and_repaired(self):
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Output ownership',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 100.0,
                }),
            ],
        })
        line = elim.line_ids[0]
        with self.assertRaises(AccessError):
            line.sudo().write({'amount': 999.0})
        with self.assertRaises(AccessError):
            self.env['eh.consol.elimination.line'].sudo().create({
                'elimination_id': elim.id,
                'account_id': self.account_cash.id,
                'debit': 1.0,
                'amount': 1.0,
            })
        self.env.cr.execute(
            "UPDATE eh_consol_elimination_line "
            "SET amount = 999.0, presentation_currency_id = NULL "
            "WHERE elimination_id = %s",
            (elim.id,),
        )
        elim.line_ids.invalidate_recordset([
            'amount', 'presentation_currency_id',
        ])
        elim.action_post()
        self.assertEqual(
            set(elim.line_ids.mapped('amount')), {100.0, -100.0},
        )
        self.assertEqual(
            elim.line_ids.mapped('presentation_currency_id'),
            self.run.presentation_currency_id,
        )

    def test_elimination_nonfinite_amounts_are_rejected(self):
        for invalid in (float('nan'), float('inf'), float('-inf')):
            with self.assertRaises(ValidationError):
                self.env['eh.consol.elimination.line'].create({
                    'elimination_id': self.Elim.create({
                        'run_id': self.run.id,
                        'name': 'Nonfinite elimination',
                    }).id,
                    'account_id': self.account_cash.id,
                    'debit': invalid,
                })

    def test_elimination_aggregate_overflow_is_rejected(self):
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Aggregate overflow',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 1.0,
                }),
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 1.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 1.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 1.0,
                }),
            ],
        })
        # Seed a representationally valid legacy row shape past ORM
        # conversion. Summing individually finite values overflows to Inf;
        # action_post must still fail closed rather than accepting NaN
        # from ``Inf - Inf``.
        debit_ids = elim.line_ids.filtered('debit').ids
        credit_ids = elim.line_ids.filtered('credit').ids
        self.env.cr.execute(
            "UPDATE eh_consol_elimination_line SET debit = %s "
            "WHERE id IN %s",
            (1e308, tuple(debit_ids)),
        )
        self.env.cr.execute(
            "UPDATE eh_consol_elimination_line SET credit = %s "
            "WHERE id IN %s",
            (1e308, tuple(credit_ids)),
        )
        elim.line_ids.invalidate_recordset(['debit', 'credit', 'amount'])
        with self.assertRaises(UserError):
            elim.action_post()

    def test_elimination_balance_uses_presentation_currency_precision(self):
        Currency = self.env['res.currency']
        precise = Currency.create({
            'name': 'K3D', 'symbol': 'K', 'rounding': 0.001, 'active': True,
        })
        whole = Currency.create({
            'name': 'J0D', 'symbol': 'J', 'rounding': 1.0, 'active': True,
        })

        def make_elimination(currency, suffix, credit):
            entity = self.env['eh.consol.entity'].create({
                'name': 'Precision %s' % suffix,
                'code': 'precision_%s' % suffix,
                'parent_company_id': self.company.id,
                'presentation_currency_id': currency.id,
            })
            run = self.env['eh.consol.run'].create({
                'entity_id': entity.id,
                'period_from': '2026-01-01',
                'period_to': '2026-12-31',
            })
            return self.Elim.create({
                'run_id': run.id,
                'name': 'Precision elimination %s' % suffix,
                'line_ids': [
                    (0, 0, {
                        'account_id': self.account_cash.id,
                        'debit': 100.0,
                    }),
                    (0, 0, {
                        'account_id': self.account_revenue.id,
                        'credit': credit,
                    }),
                ],
            })

        with self.assertRaises(UserError):
            make_elimination(precise, 'k3d', 99.991).action_post()
        rounded = make_elimination(whole, 'j0d', 99.6)
        credit_line = rounded.line_ids.filtered('credit')
        self.env.cr.execute(
            "UPDATE eh_consol_elimination_line SET credit = 99.6 "
            "WHERE id = %s",
            (credit_line.id,),
        )
        credit_line.invalidate_recordset(['credit', 'amount'])
        with self.assertRaises(UserError):
            rounded.action_post()
        exact = make_elimination(whole, 'j0d_exact', 100.0)
        exact.action_post()
        self.assertEqual(exact.state, 'posted')

    def test_elimination_graph_freezes_with_parent_run(self):
        elim = self.Elim.create({
            'run_id': self.run.id,
            'name': 'Frozen source',
            'line_ids': [
                (0, 0, {
                    'account_id': self.account_cash.id,
                    'debit': 10.0,
                }),
                (0, 0, {
                    'account_id': self.account_revenue.id,
                    'credit': 10.0,
                }),
            ],
        })
        elim.action_post()
        self.run.action_compute()
        with self.assertRaises(UserError):
            self.Elim.create({
                'run_id': self.run.id,
                'name': 'Late elimination',
            })
        with self.assertRaises(UserError):
            elim.write({'description': 'Rewritten source'})
        with self.assertRaises(UserError):
            elim.line_ids[0].write({'debit': 20.0})
        with self.assertRaises(UserError):
            elim.line_ids[0].unlink()
        with self.assertRaises(UserError):
            elim.action_cancel()


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolTranslation(EhAccountIntegrationTestCase):
    """IAS 21 translation math: average rate (P&L) versus closing rate
    (B/S), the resulting CTA, NCI carve-out, and equity-method skipping.

    The seeded company is used as the foreign subsidiary (it has a chart
    and can post moves). The consolidation parent is a separate company in
    a different presentation currency, so translation actually applies.
    """

    def setUp(self):
        super().setUp()
        Currency = self.env['res.currency']
        Rate = self.env['res.currency.rate']
        # Presentation currency, distinct from the subsidiary's currency,
        # with a rate that moves across the period so the average and the
        # closing rates genuinely differ.
        self.pres_ccy = Currency.create({
            'name': 'TCX',
            'symbol': 'T',
            'rounding': 0.01,
            'active': True,
        })
        self.pres_ccy.rate_ids.unlink()
        Rate.create({
            'currency_id': self.pres_ccy.id,
            'name': '2026-01-01',
            'rate': 2.0,
            'company_id': self.company.id,
        })
        Rate.create({
            'currency_id': self.pres_ccy.id,
            'name': '2026-12-31',
            'rate': 3.0,
            'company_id': self.company.id,
        })
        # A separate parent company (a member cannot be the parent).
        self.parent_company = self.env['res.company'].create({
            'name': 'Consol Parent Co',
            'currency_id': self.pres_ccy.id,
        })
        # The new parent company must be allowed and active, otherwise the
        # multi-company record rules this module ships would hide the
        # consolidation records from a non-superuser test user.
        self.env.user.write({'company_ids': [(4, self.parent_company.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[self.company.id, self.parent_company.id],
        ))
        # Explicit CTA + NCI equity accounts on the parent chart so the run
        # resolves them by config rather than by the name heuristic, and never
        # posts a CTA / NCI line with account_id False.
        self.cta_account = _make_account(
            self.env, self.parent_company, '3900',
            'Currency Translation Reserve', 'equity')
        self.nci_account = _make_account(
            self.env, self.parent_company, '3200',
            'Non-Controlling Interest', 'equity')
        self.re_account = _make_account(
            self.env, self.parent_company, '3100',
            'Consolidated Retained Earnings', 'equity_unaffected')
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'FX Group',
            'code': 'fx_group_translation',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
            'cta_account_id': self.cta_account.id,
            'nci_account_id': self.nci_account.id,
            'parent_retained_earnings_account_id': self.re_account.id,
        })

    def _make_run(self):
        run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        for member in self.entity.member_ids.filtered(
            lambda candidate: candidate.method == 'equity'
            and not candidate.fv_option
            and candidate.functional_currency_id != self.pres_ccy
        ):
            self.env['eh.consol.equity.rollforward'].create({
                'run_id': run.id,
                'member_id': member.id,
                'foreign_exchange_confirmed': True,
                'basis': 'Test fixture: explicit zero IAS 21 adjustment.',
            })
        return run

    def _closing_rate(self):
        return self.company.currency_id._convert(
            1.0, self.pres_ccy, self.company,
            fields.Date.from_string('2026-12-31'),
        )

    def test_translation_produces_nonzero_cta_with_fx_movement(self):
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0,
            'method': 'full',
        })
        # One balanced move: a balance-sheet asset (closing rate) against a
        # P&L income line (average rate). Because the average and closing
        # rates differ, the translated trial balance no longer nets to
        # zero, so a non-zero CTA must be booked.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        cta_lines = run.line_ids.filtered(lambda l: l.kind == 'cta')
        self.assertEqual(len(cta_lines), 1)
        cta_total = sum(cta_lines.mapped('amount'))
        # 364 days at 2.0 and one day at 3.0 gives average 2.0027397;
        # cash closes at 3,000 while revenue translates to -2,002.74.
        self.assertEqual(cta_total, -997.26)

    def test_cta_is_zero_without_fx_movement(self):
        # Flatten the rate so the average equals the closing rate; a
        # balanced subsidiary then translates uniformly and produces no
        # CTA. This pins the fix to genuine rate movement rather than an
        # unconditional offset.
        self.pres_ccy.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': self.pres_ccy.id,
            'name': '2026-01-01',
            'rate': 2.5,
            'company_id': self.company.id,
        })
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0,
            'method': 'full',
        })
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        cta_total = sum(
            run.line_ids.filtered(lambda l: l.kind == 'cta').mapped('amount')
        )
        self.assertAlmostEqual(cta_total, 0.0, places=2)

    def test_nci_carved_for_fractional_ownership(self):
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 80.0,
            'method': 'full',
        })
        # Equity in the subsidiary so there is something to carve. Issued
        # capital translates at its transaction-date historical rate.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        nci_lines = run.line_ids.filtered(lambda l: l.kind == 'nci')
        core_nci = nci_lines.filtered(
            lambda line: 'exchange differences' not in (line.notes or ''),
        )
        fx_nci = nci_lines - core_nci
        self.assertEqual(len(core_nci), 1, "one core NCI carve for the 80% sub")
        self.assertEqual(len(fx_nci), 1, "one IAS 21.41 NCI allocation expected")
        # The equity account balance is -5000 (credit). NCI share is 20%.
        historical_rate = self.company.currency_id._convert(
            1.0, self.pres_ccy, self.company,
            fields.Date.from_string('2026-06-01'), round=False,
        )
        expected = self.pres_ccy.round(
            self.pres_ccy.round(-5000.0 * historical_rate) * (1.0 - 0.80),
        )
        self.assertEqual(core_nci.amount, expected)
        closing_rate = self.company.currency_id._convert(
            1.0, self.pres_ccy, self.company,
            fields.Date.from_string('2026-12-31'), round=False,
        )
        member_plug = -self.pres_ccy.round(
            5000.0 * closing_rate - 5000.0 * historical_rate,
        )
        self.assertEqual(
            fx_nci.amount,
            self.pres_ccy.round(member_plug * (1.0 - 0.80)),
        )

    def test_nci_reclass_fails_closed_when_parent_retained_is_ambiguous(self):
        self.entity.parent_retained_earnings_account_id = False
        parent_branch = self.env['res.company'].create({
            'name': 'Consol Parent Branch',
            'parent_id': self.parent_company.id,
            'currency_id': self.parent_company.currency_id.id,
        })
        self.env.user.write({'company_ids': [(4, parent_branch.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=(
                self.env.context.get('allowed_company_ids', [])
                + [parent_branch.id]
            ),
        ))
        self.entity = self.entity.with_env(self.env)
        _make_account(
            self.env, parent_branch, '3101',
            'Second Consolidated Retained Earnings', 'equity_unaffected',
        )
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 80.0,
            'method': 'full',
        })
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_equity, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        with self.assertRaisesRegex(UserError, 'Found 2'):
            run.action_compute()
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.line_ids)

    def test_nci_carve_does_not_contaminate_cta(self):
        """The NCI carve-out is a balanced two-leg reclass within equity, so
        it must NOT leak into the CTA plug.

        Regression: the carve-out used to book a single credit to NCI equity
        with no offsetting debit, and _compute_cta summed that lone leg into
        the total it negates, so the missing counterparty was silently
        absorbed into the line labelled CTA (IAS 21), overstating the reported
        translation adjustment by the NCI amount. With the fix the two legs
        net to zero, so the CTA line equals the genuine translation adjustment
        (the CTA a 100%-owned, NCI-free run of the same books produces) and
        the run still balances.
        """
        # Genuine CTA reference: the SAME seeded company at 100% ownership
        # carves NO NCI, so its CTA is purely the average/closing rate gap with
        # nothing to contaminate it. Post the moves once, then run twice with
        # different ownership members on two entities over the same books.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        # A P&L move so the average/closing rate gap produces a genuine,
        # non-zero CTA (revenue is translated at the average rate, the cash
        # asset at the closing rate).
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 2000.0},
                {'account': self.account_revenue, 'credit': 2000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        # Reference entity: 100% ownership -> no NCI carve-out.
        ref_entity = self.env['eh.consol.entity'].create({
            'name': 'CTA Ref Group',
            'code': 'cta_ref_group',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
            'cta_account_id': self.cta_account.id,
            'nci_account_id': self.nci_account.id,
        })
        self.env['eh.consol.member'].create({
            'entity_id': ref_entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0,
            'method': 'full',
        })
        ref_run = self.env['eh.consol.run'].create({
            'entity_id': ref_entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        ref_run.action_compute()
        genuine_cta = sum(
            ref_run.line_ids.filtered(lambda l: l.kind == 'cta')
            .mapped('amount')
        )
        self.assertGreater(
            abs(genuine_cta), 1.0,
            "the reference run must produce a materially non-zero genuine CTA",
        )
        self.assertFalse(
            ref_run.line_ids.filtered(lambda l: l.kind == 'nci'),
            "the 100% reference run must carve no NCI",
        )

        # NCI entity: 80% ownership -> a real NCI carve-out on the same books.
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 80.0,
            'method': 'full',
        })
        run = self._make_run()
        run.action_compute()
        nci_lines = run.line_ids.filtered(lambda l: l.kind == 'nci')
        core_nci = nci_lines.filtered(
            lambda line: 'exchange differences' not in (line.notes or ''),
        )
        fx_nci = nci_lines - core_nci
        self.assertEqual(len(core_nci), 1, "one core NCI carve expected")
        self.assertEqual(len(fx_nci), 1, "one IAS 21.41 NCI allocation expected")
        nci_credit = core_nci.amount
        self.assertFalse(
            float(round(nci_credit, 2)) == 0.0,
            "the NCI carve-out must be materially non-zero for this test to "
            "prove the CTA is no longer contaminated",
        )
        cta = sum(
            run.line_ids.filtered(lambda l: l.kind == 'cta').mapped('amount')
        )
        # IAS 21.41 splits the genuine translation difference between parent
        # CTA and NCI while preserving the exact total and keeping the NCI
        # equity carve out of the plug basis.
        self.assertEqual(cta, self.pres_ccy.round(genuine_cta * 0.80))
        self.assertEqual(fx_nci.amount, self.pres_ccy.round(genuine_cta * 0.20))
        self.assertEqual(
            self.pres_ccy.round(cta + fx_nci.amount),
            self.pres_ccy.round(genuine_cta),
        )
        self.assertNotEqual(nci_credit, 0.0)
        # The whole run must still balance: every kind that carries a real
        # journal amount nets to zero once the CTA plug is included.
        run_total = sum(
            run.line_ids.filtered(
                lambda l: l.kind in (
                    'subsidiary_balance', 'parent_balance', 'elimination',
                    'equity_pickup', 'nci', 'cta',
                ),
            ).mapped('amount')
        )
        self.assertAlmostEqual(
            run_total, 0.0, places=2,
            msg="the consolidation run must balance after the NCI carve-out",
        )

    def test_full_ownership_produces_no_nci(self):
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0,
            'method': 'full',
        })
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.kind == 'nci'),
            "100% ownership carves no NCI",
        )

    def test_equity_method_member_not_rolled_up(self):
        # Equity-method configuration is mandatory (a missing investment or
        # share-of-profit account refuses the compute), so the member is
        # configured here; the test's point is unchanged: no line-by-line
        # rollup and no NCI for an equity-method associate.
        investment = _make_account(
            self.env, self.parent_company, '1500',
            'Investment in Associates', 'asset_non_current')
        sop = _make_account(
            self.env, self.parent_company, '4100',
            'Share of Profit of Associates', 'income')
        member = self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 40.0,
            'method': 'equity',
            'investment_account_id': investment.id,
            'share_of_profit_account_id': sop.id,
        })
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        pickup_snapshots = {
            (line.account_code, line.account_name)
            for line in run.line_ids.filtered(
                lambda line: line.kind == 'equity_pickup'
                and line.member_id == member
            )
        }
        self.assertEqual(
            pickup_snapshots,
            {
                ('1500', 'Investment in Associates'),
                ('4100', 'Share of Profit of Associates'),
            },
            "engine-owned account snapshots must retain the parent-company "
            "metadata outside that company's ambient context",
        )
        sub_lines = run.line_ids.filtered(
            lambda l: l.kind == 'subsidiary_balance'
            and l.member_id == member,
        )
        self.assertFalse(
            sub_lines,
            "equity-method members must not roll up line by line",
        )
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.kind == 'nci'),
            "equity-method members produce no NCI",
        )


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolAcquisitionElimination(EhAccountIntegrationTestCase):
    """IFRS 3 auto-elimination and IAS 28 equity pick-up.

    Self-contained (does NOT inherit TestConsolTranslation's tests): a distinct
    parent company in a presentation currency with a single flat rate, so the
    average and closing rates coincide and the elimination / pick-up arithmetic
    is a clean scalar with no CTA noise.
    """

    def setUp(self):
        super().setUp()
        Currency = self.env['res.currency']
        self.pres_ccy = Currency.create({
            'name': 'TAE', 'symbol': 'A', 'rounding': 0.01, 'active': True,
        })
        self.pres_ccy.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': self.pres_ccy.id,
            'name': '2026-01-01',
            'rate': 1.0,
            'company_id': self.company.id,
        })
        self.rate = 1.0
        self.parent_company = self.env['res.company'].create({
            'name': 'Consol Parent AE',
            'currency_id': self.pres_ccy.id,
        })
        self.env.user.write({'company_ids': [(4, self.parent_company.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[self.company.id, self.parent_company.id],
        ))
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'Acq Group',
            'code': 'acq_group_elim',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
        })
        # Consolidated-chart accounts live on the parent company.
        self.parent_investment = self._acc(
            '1500', 'Investment in Sub', 'asset_non_current')
        self.parent_equity_elim = self._acc(
            '3100', 'Pre-Acq Equity Elimination', 'equity')
        self.parent_goodwill = self._acc(
            '1600', 'Goodwill', 'asset_non_current')
        self.parent_nci = self._acc(
            '3200', 'Non-Controlling Interest', 'equity')
        self.parent_sop = self._acc(
            '4100', 'Share of Profit of Associates', 'income')

    def _acc(self, code, name, account_type):
        """Create/return an account owned by the parent company."""
        Account = self.env['account.account']
        multi = 'company_ids' in Account._fields
        company_field = 'company_ids' if multi else 'company_id'
        company_value = (
            [(6, 0, self.parent_company.ids)] if multi
            else self.parent_company.id)
        existing = Account.search([
            ('code', '=', code),
            (company_field, 'in', self.parent_company.ids),
        ], limit=1)
        if existing:
            return existing
        return Account.create({
            'code': code, 'name': name, 'account_type': account_type,
            company_field: company_value,
        })

    def _make_run(self):
        run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        for member in self.entity.member_ids.filtered(
            lambda candidate: candidate.method == 'equity'
            and not candidate.fv_option
            and candidate.functional_currency_id != self.pres_ccy
        ):
            self.env['eh.consol.equity.rollforward'].create({
                'run_id': run.id,
                'member_id': member.id,
                'foreign_exchange_confirmed': True,
                'basis': 'Test fixture: explicit zero IAS 21 adjustment.',
            })
        return run

    def test_auto_elimination_legs_net_to_zero(self):
        """A fully-configured full member auto-generates elimination legs
        that net to zero, and no separate NCI line is booked."""
        A = 5000.0   # subsidiary pre-acquisition equity (presentation ccy)
        I = 4500.0   # parent's investment cost
        o = 0.80
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': o * 100.0,
            'method': 'full',
            'investment_account_id': self.parent_investment.id,
            'investment_amount': I,
            'acquisition_equity': A,
            'acquisition_component_ids': [(0, 0, {
                'source_account_id': self.account_equity.id,
                'amount': A,
            })],
            'equity_elimination_account_id': self.parent_equity_elim.id,
            'goodwill_account_id': self.parent_goodwill.id,
            'nci_account_id': self.parent_nci.id,
        })
        # Subsidiary equity on the books (closing-rate translated at 1.0).
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        # The acquisition-date minority leg is tagged 'nci' (so it feeds the
        # nci_amount KPI) and the goodwill residual is tagged 'goodwill' (so
        # recognised goodwill is queryable by kind); the other two legs stay
        # 'elimination'.
        acq_lines = run.line_ids.filtered(
            lambda l: l.kind in ('elimination', 'nci', 'goodwill')
            and l.member_id)
        self.assertEqual(
            len(acq_lines), 4,
            "four IFRS 3 acquisition-elimination legs expected",
        )
        # The four legs must net to zero (balanced by construction).
        self.assertAlmostEqual(
            sum(acq_lines.mapped('amount')), 0.0, places=2)
        # Individual legs.
        equity_leg = acq_lines.filtered(
            lambda l: l.account_id == self.account_equity)
        inv_leg = acq_lines.filtered(
            lambda l: l.account_id == self.parent_investment)
        nci_leg = acq_lines.filtered(
            lambda l: l.account_id == self.parent_nci)
        gw_leg = acq_lines.filtered(
            lambda l: l.account_id == self.parent_goodwill)
        self.assertAlmostEqual(equity_leg.amount, A, places=2)
        self.assertAlmostEqual(inv_leg.amount, -I, places=2)
        self.assertAlmostEqual(nci_leg.amount, -(1.0 - o) * A, places=2)
        self.assertAlmostEqual(gw_leg.amount, I - o * A, places=2)
        # The acquisition-date minority leg carries kind='nci'.
        self.assertEqual(nci_leg.kind, 'nci')
        # No post-acquisition NCI carve-out here (no post-acq movement), so
        # the only 'nci' line is the acquisition-date one.
        self.assertEqual(
            run.line_ids.filtered(lambda l: l.kind == 'nci'), nci_leg,
            "acquisition NCI is the sole nci-tagged line for this member",
        )

    def test_auto_elimination_removes_double_count(self):
        """After compute, the parent's investment asset and the sub's
        pre-acquisition equity are both removed, so consolidated equity is
        not double-counted."""
        A = 5000.0
        I = 5000.0   # goodwill-free acquisition at 100% for a clean net
        o = 1.0
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0,
            'method': 'full',
            'investment_account_id': self.parent_investment.id,
            'investment_amount': I,
            'acquisition_equity': A,
            'acquisition_component_ids': [(0, 0, {
                'source_account_id': self.account_equity.id,
                'amount': A,
            })],
            'equity_elimination_account_id': self.parent_equity_elim.id,
            'goodwill_account_id': self.parent_goodwill.id,
            'nci_account_id': self.parent_nci.id,
        })
        # Parent carries the investment asset in its own books.
        parent_move = self.env['account.move'].create({
            'company_id': self.parent_company.id,
            'move_type': 'entry',
            'date': fields.Date.from_string('2026-06-01'),
            'journal_id': self._parent_journal().id,
            'line_ids': [
                (0, 0, {
                    'account_id': self.parent_investment.id,
                    'debit': 5000.0, 'credit': 0.0,
                }),
                (0, 0, {
                    'account_id': self._acc(
                        '3300', 'Parent Equity', 'equity').id,
                    'debit': 0.0, 'credit': 5000.0,
                }),
            ],
        })
        parent_move.action_post()
        # Subsidiary equity on its own books.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        # Net movement on the investment account across ALL run lines: the
        # parent_balance debit (+5000) is cancelled by the elimination credit
        # (-5000), so consolidated investment nets to zero.
        inv_total = sum(
            run.line_ids.filtered(
                lambda l: l.account_id == self.parent_investment,
            ).mapped('amount'),
        )
        self.assertAlmostEqual(inv_total, 0.0, places=2)
        # Net movement on the subsidiary's equity account: the translated
        # subsidiary_balance (-5000) is offset by the equity-elimination
        # debit (+5000) sitting on the elimination account, so the group's
        # pre-acquisition equity is removed and not double-counted.
        equity_elim_total = sum(
            run.line_ids.filtered(
                lambda l: l.account_id == self.account_equity
                and l.kind == 'elimination',
            ).mapped('amount'),
        )
        self.assertAlmostEqual(equity_elim_total, A, places=2)
        sub_equity_total = sum(
            run.line_ids.filtered(
                lambda l: l.account_id == self.account_equity
                and l.kind == 'subsidiary_balance',
            ).mapped('amount'),
        )
        # sub equity (-5000) + elimination of pre-acq equity (+5000) == 0.
        self.assertAlmostEqual(
            sub_equity_total + equity_elim_total, 0.0, places=2)

    def _parent_journal(self):
        Journal = self.env['account.journal']
        j = Journal.search([
            ('company_id', '=', self.parent_company.id),
            ('type', '=', 'general'),
        ], limit=1)
        if j:
            return j
        return Journal.create({
            'name': 'Parent Misc', 'code': 'PMISC', 'type': 'general',
            'company_id': self.parent_company.id,
        })

    def test_equity_method_pickup_produces_share_of_profit_lines(self):
        """An equity-method member with investment + share-of-profit accounts
        picks up the parent's share of the associate's period profit as two
        balanced lines."""
        o = 0.40
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': o * 100.0,
            'method': 'equity',
            'investment_account_id': self.parent_investment.id,
            'share_of_profit_account_id': self.parent_sop.id,
        })
        # Associate makes a 1000 profit (revenue over no expense).
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        pickup_lines = run.line_ids.filtered(
            lambda l: l.kind == 'equity_pickup')
        self.assertEqual(
            len(pickup_lines), 2,
            "two balanced equity pick-up legs expected",
        )
        self.assertAlmostEqual(
            sum(pickup_lines.mapped('amount')), 0.0, places=2)
        expected_share = o * 1000.0 * self.rate
        inv_leg = pickup_lines.filtered(
            lambda l: l.account_id == self.parent_investment)
        sop_leg = pickup_lines.filtered(
            lambda l: l.account_id == self.parent_sop)
        # Investment carrying value increased by the share (debit, +).
        self.assertAlmostEqual(inv_leg.amount, expected_share, places=2)
        # Share-of-profit income recognised (credit, -).
        self.assertAlmostEqual(sop_leg.amount, -expected_share, places=2)
        # No stale warning about this member.
        self.assertFalse(run.consolidation_warning)
        # And it still does not roll up line by line.
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.kind == 'subsidiary_balance'),
            "equity-method members must not roll up line by line",
        )


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolIntragroupProfitAndPostAcqNci(EhAccountIntegrationTestCase):
    """Intra-group unrealised-profit elimination (IFRS 10 / IAS 27) and
    post-acquisition NCI attribution.

    Standalone (does NOT inherit any other consolidation test class): a distinct
    parent company in a presentation currency with a single flat rate, so the
    average and closing rates coincide and the arithmetic is a clean scalar
    with no CTA noise.
    """

    def setUp(self):
        super().setUp()
        Currency = self.env['res.currency']
        self.pres_ccy = Currency.create({
            'name': 'TUP', 'symbol': 'U', 'rounding': 0.01, 'active': True,
        })
        self.pres_ccy.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': self.pres_ccy.id,
            'name': '2026-01-01',
            'rate': 1.0,
            'company_id': self.company.id,
        })
        self.rate = 1.0
        self.parent_company = self.env['res.company'].create({
            'name': 'Consol Parent UP',
            'currency_id': self.pres_ccy.id,
        })
        self.env.user.write({'company_ids': [(4, self.parent_company.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[self.company.id, self.parent_company.id],
        ))
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'IG Group',
            'code': 'ig_group_up',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
        })
        # Consolidated-chart accounts on the parent company.
        self.parent_inventory = self._acc(
            '1400', 'Consolidated Inventory', 'asset_current')
        self.parent_cogs = self._acc(
            '5100', 'Consolidated COGS', 'expense')
        self.parent_investment = self._acc(
            '1500', 'Investment in Sub', 'asset_non_current')
        self.parent_equity_elim = self._acc(
            '3100', 'Pre-Acq Equity Elimination', 'equity')
        self.parent_goodwill = self._acc(
            '1600', 'Goodwill', 'asset_non_current')
        self.parent_nci = self._acc(
            '3200', 'Non-Controlling Interest', 'equity')
        self.parent_re = _make_unaffected_account(
            self.env, self.parent_company, '3000',
            'Consolidated Retained Earnings')
        self.entity.write({
            'nci_account_id': self.parent_nci.id,
            'parent_retained_earnings_account_id': self.parent_re.id,
        })

    def _acc(self, code, name, account_type):
        Account = self.env['account.account']
        multi = 'company_ids' in Account._fields
        company_field = 'company_ids' if multi else 'company_id'
        company_value = (
            [(6, 0, self.parent_company.ids)] if multi
            else self.parent_company.id)
        existing = Account.search([
            ('code', '=', code),
            (company_field, 'in', self.parent_company.ids),
        ], limit=1)
        if existing:
            return existing
        return Account.create({
            'code': code, 'name': name, 'account_type': account_type,
            company_field: company_value,
        })

    def _make_run(self):
        return self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })

    def test_unrealised_profit_elimination_removes_margin_and_balances(self):
        """An unrealised-profit record emits two balanced elimination legs
        that debit COGS/RE and credit inventory by the margin, netting to
        zero."""
        run = self._make_run()
        margin = 800.0
        self.env['eh.consol.unrealised.profit'].create({
            'run_id': run.id,
            'name': 'IC stock margin',
            'unrealised_amount': margin,
            'inventory_account_id': self.parent_inventory.id,
            'cogs_or_re_account_id': self.parent_cogs.id,
        })
        run.action_compute()
        up_lines = run.line_ids.filtered(
            lambda l: l.kind == 'elimination'
            and l.account_id in (self.parent_inventory | self.parent_cogs)
        )
        self.assertEqual(
            len(up_lines), 2,
            "two unrealised-profit elimination legs expected",
        )
        # Net to zero (balanced by construction).
        self.assertAlmostEqual(sum(up_lines.mapped('amount')), 0.0, places=2)
        cogs_leg = up_lines.filtered(
            lambda l: l.account_id == self.parent_cogs)
        inv_leg = up_lines.filtered(
            lambda l: l.account_id == self.parent_inventory)
        # Dr COGS/RE +margin, Cr inventory -margin.
        self.assertAlmostEqual(cogs_leg.amount, margin, places=2)
        self.assertAlmostEqual(inv_leg.amount, -margin, places=2)

    def test_upstream_unrealised_profit_is_split_between_parent_and_nci(self):
        """The minority shares an upstream seller's eliminated profit."""
        seller = self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 80.0,
            'method': 'full',
            'nci_account_id': self.parent_nci.id,
        })
        run = self._make_run()
        self.env['eh.consol.unrealised.profit'].create({
            'run_id': run.id,
            'name': 'Upstream stock margin',
            'unrealised_amount': 100.0,
            'inventory_account_id': self.parent_inventory.id,
            'cogs_or_re_account_id': self.parent_cogs.id,
            'sale_direction': 'upstream',
            'seller_member_id': seller.id,
        })
        run.action_compute()
        lines = run.line_ids.filtered(
            lambda line: line.member_id == seller
            and 'unrealised' in (line.notes or '').lower()
        )
        by_account = {
            line.account_id: line.amount for line in lines
        }
        self.assertEqual(by_account[self.parent_nci], 20.0)
        self.assertEqual(by_account[self.parent_re], -20.0)
        core = run.line_ids.filtered(
            lambda line: line.account_id in (
                self.parent_cogs | self.parent_inventory
            )
        )
        self.assertEqual(
            {line.account_id: line.amount for line in core},
            {self.parent_cogs: 100.0, self.parent_inventory: -100.0},
        )
        self.assertEqual(sum((lines | core).mapped('amount')), 0.0)

    def test_nonzero_unrealised_profit_requires_complete_accounts(self):
        run = self._make_run()
        with self.assertRaisesRegex(
            ValidationError, 'requires complete posting accounts',
        ):
            self.env['eh.consol.unrealised.profit'].create({
                'run_id': run.id,
                'name': 'Incomplete current basis',
                'unrealised_amount': 100.0,
                'inventory_account_id': self.parent_inventory.id,
            })

        legacy = self.env['eh.consol.unrealised.profit'].create({
            'run_id': run.id,
            'name': 'Raw legacy incomplete basis',
            'unrealised_amount': 0.0,
        })
        self.env.cr.execute(
            "UPDATE eh_consol_unrealised_profit "
            "SET unrealised_amount = 100.0 WHERE id = %s",
            (legacy.id,),
        )
        legacy.invalidate_recordset(['unrealised_amount'])
        with self.assertRaisesRegex(
            ValidationError, 'requires complete posting accounts',
        ):
            run.action_compute()
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.line_ids)

    def test_no_unrealised_profit_records_leaves_run_unchanged(self):
        """A run with no unrealised-profit records books no such elimination
        legs (existing behaviour preserved)."""
        run = self._make_run()
        run.action_compute()
        up_lines = run.line_ids.filtered(
            lambda l: l.account_id in (self.parent_inventory | self.parent_cogs)
        )
        self.assertFalse(up_lines, "no unrealised-profit legs without records")

    def test_post_acquisition_profit_attributed_to_nci(self):
        """An investment-configured full member with post-acquisition profit
        produces a post-acq NCI line, so total NCI exceeds the acquisition
        NCI booked by the elimination."""
        A = 5000.0   # acquisition-date equity
        I = 4500.0   # investment cost
        o = 0.80
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': o * 100.0,
            'method': 'full',
            'investment_account_id': self.parent_investment.id,
            'investment_amount': I,
            'acquisition_equity': A,
            'acquisition_component_ids': [(0, 0, {
                'source_account_id': self.account_equity.id,
                'amount': A,
            })],
            'equity_elimination_account_id': self.parent_equity_elim.id,
            'goodwill_account_id': self.parent_goodwill.id,
            'nci_account_id': self.parent_nci.id,
        })
        # Subsidiary equity 5000 (= A) plus a 1000 post-acquisition profit
        # (revenue over no expense). Reporting net-asset base is therefore
        # 6000, so the post-acquisition movement is 1000.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        # Acquisition-date NCI (booked by the elimination) and the post-acq NCI
        # line are BOTH tagged 'nci', so the KPI sees the full minority share.
        acq_nci = -(1.0 - o) * A
        nci_lines = run.line_ids.filtered(lambda l: l.kind == 'nci')
        self.assertEqual(
            len(nci_lines), 2,
            "acquisition-date NCI and post-acq NCI lines both expected",
        )
        # Post-acq NCI = (1-o) * post-acq movement (1000), credit-negative.
        expected_post_acq = -(1.0 - o) * 1000.0 * self.rate
        post_acq_line = nci_lines.filtered(
            lambda l: abs(l.amount - expected_post_acq) < 0.5)
        self.assertTrue(post_acq_line, "post-acq NCI line present")
        post_acq_nci = sum(post_acq_line.mapped('amount'))
        self.assertEqual(post_acq_nci, self.pres_ccy.round(expected_post_acq))
        # The nci_amount KPI must equal acquisition NCI + post-acq NCI, and
        # strictly exceed the acquisition NCI in magnitude.
        total_nci = acq_nci + post_acq_nci
        self.assertEqual(run.nci_amount, self.pres_ccy.round(total_nci))
        self.assertGreater(
            abs(run.nci_amount), abs(acq_nci),
            "total NCI must exceed the acquisition NCI once post-acquisition "
            "profit is attributed to the minority",
        )

    def test_no_post_acq_movement_produces_no_extra_nci(self):
        """When the reporting equity equals the acquisition equity (no
        post-acquisition movement), no post-acq NCI line is booked, so the
        acquisition-only behaviour is preserved."""
        A = 5000.0
        I = 4500.0
        o = 0.80
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': o * 100.0,
            'method': 'full',
            'investment_account_id': self.parent_investment.id,
            'investment_amount': I,
            'acquisition_equity': A,
            'acquisition_component_ids': [(0, 0, {
                'source_account_id': self.account_equity.id,
                'amount': A,
            })],
            'equity_elimination_account_id': self.parent_equity_elim.id,
            'goodwill_account_id': self.parent_goodwill.id,
            'nci_account_id': self.parent_nci.id,
        })
        # Subsidiary equity exactly equals A, no P&L movement.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        # Only the acquisition-date NCI leg is present (now tagged 'nci'); no
        # post-acq NCI line when reporting equity equals acquisition equity.
        nci_lines = run.line_ids.filtered(lambda l: l.kind == 'nci')
        self.assertEqual(
            len(nci_lines), 1,
            "only the acquisition-date NCI line when there is no "
            "post-acquisition movement",
        )
        # The KPI equals the acquisition-date minority share.
        self.assertAlmostEqual(
            run.nci_amount, -(1.0 - o) * A, places=2)

    def test_nci_amount_kpi_includes_acquisition_minority_share(self):
        """Defect B: the nci_amount KPI must include the acquisition-date
        minority share even when there is no post-acquisition movement.

        Before the fix the acquisition NCI leg was tagged 'elimination', so
        _compute_totals (which sums only kind=='nci') reported nci_amount == 0
        and contradicted the field's help text.
        """
        A = 5000.0
        I = 4500.0
        o = 0.80
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': o * 100.0,
            'method': 'full',
            'investment_account_id': self.parent_investment.id,
            'investment_amount': I,
            'acquisition_equity': A,
            'acquisition_component_ids': [(0, 0, {
                'source_account_id': self.account_equity.id,
                'amount': A,
            })],
            'equity_elimination_account_id': self.parent_equity_elim.id,
            'goodwill_account_id': self.parent_goodwill.id,
            'nci_account_id': self.parent_nci.id,
        })
        # Subsidiary equity exactly equals A: only the acquisition-date NCI
        # exists, no post-acquisition movement to muddy the KPI.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        # The KPI must equal the acquisition-date minority share, not zero.
        expected_acq_nci = -(1.0 - o) * A
        self.assertAlmostEqual(run.nci_amount, expected_acq_nci, places=2)
        self.assertNotAlmostEqual(
            run.nci_amount, 0.0, places=2,
            msg="nci_amount must include the acquisition-date minority share",
        )

    def test_three_dp_presentation_currency_run_nets_to_zero(self):
        """Defect A: with a 3-dp presentation currency the CTA balancing plug
        must be rounded in that currency so sum(run lines) still nets to zero.

        Before the fix _compute_cta hard-coded round(total, 2), leaving a
        residual at the third decimal for a 3-dp currency, so the run did not
        balance.
        """
        Currency = self.env['res.currency']
        Rate = self.env['res.currency.rate']
        # A 3-dp presentation currency (KWD/BHD-style).
        pres3 = Currency.create({
            'name': 'TK3', 'symbol': 'K', 'rounding': 0.001, 'active': True,
        })
        pres3.rate_ids.unlink()
        # Average (period-open) and closing rates differ, so translating a
        # balanced subsidiary produces a genuine, fractional CTA.
        Rate.create({
            'currency_id': pres3.id, 'name': '2026-01-01',
            'rate': 0.307, 'company_id': self.company.id,
        })
        Rate.create({
            'currency_id': pres3.id, 'name': '2026-12-31',
            'rate': 0.313, 'company_id': self.company.id,
        })
        parent3 = self.env['res.company'].create({
            'name': 'Consol Parent 3DP', 'currency_id': pres3.id,
        })
        self.env.user.write({'company_ids': [(4, parent3.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[
                self.company.id, self.parent_company.id, parent3.id],
        ))
        cta3 = _make_account(
            self.env, parent3, '3900',
            'Currency Translation Reserve', 'equity')
        entity3 = self.env['eh.consol.entity'].create({
            'name': '3DP Group', 'code': 'three_dp_group',
            'parent_company_id': parent3.id,
            'presentation_currency_id': pres3.id,
            'cta_account_id': cta3.id,
        })
        self.env['eh.consol.member'].create({
            'entity_id': entity3.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0,
            'method': 'full',
        })
        # One FX-sensitive balanced move: an asset (closing rate) against a
        # P&L income line (average rate). The rate spread yields a fractional
        # CTA that exercises 3-dp rounding.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1234.0},
                {'account': self.account_revenue, 'credit': 1234.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self.env['eh.consol.run'].create({
            'entity_id': entity3.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        run.action_compute()
        cta_lines = run.line_ids.filtered(lambda l: l.kind == 'cta')
        self.assertTrue(cta_lines, "a CTA line must be produced")
        # The CTA plug must carry 3-dp precision: it equals the negated total
        # rounded in the presentation currency.
        self.assertEqual(
            cta_lines.amount, pres3.round(cta_lines.amount),
            "CTA plug must be rounded in the 3-dp presentation currency",
        )
        # The whole run must net to exactly zero at 3-dp precision.
        grand_total = sum(run.line_ids.mapped('amount'))
        self.assertTrue(
            float_is_zero(grand_total, precision_rounding=pres3.rounding),
            "sum(run lines) must net to zero in the presentation currency; a "
            "non-zero residual here is the hard-coded 2-dp rounding bug",
        )


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolSettledRunControls(EhAccountIntegrationTestCase):
    """ORM-level control integrity on a settled (computed / reviewed / closed)
    consolidation run, independent of the action methods.

    A settled run backs the consolidated figures (and, once posted, a GL move),
    so at the raw write / unlink layer:

      (a) its measurement / input fields are frozen;
      (b) it cannot be deleted;
      (c) a plain user cannot raw-reset its state out of the settled set to
          lift the freeze (manager-gated via the eh_consol_state_change flag);

    while the normal compute / review / close / reset flow keeps working.
    """

    def setUp(self):
        super().setUp()
        self.manager_group = self.env.ref('eh_account_base.group_eh_manager')
        self.env.user.group_ids |= self.manager_group
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'Controls Group',
            'code': 'controls_group_run',
            'parent_company_id': self.company.id,
            'presentation_currency_id': self.company.currency_id.id,
        })

    def _computed_run(self):
        """A run computed from one balanced parent move, so it carries settled
        figures and at least one line, in state 'computed'."""
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        self.assertTrue(run.line_ids)
        return run

    def test_settled_run_input_frozen_at_write(self):
        """(a) A measurement / input field on a settled run is frozen at the
        raw ORM write layer, even for a manager and even via sudo."""
        run = self._computed_run()
        with self.assertRaises(UserError):
            run.write({'period_to': '2027-12-31'})
        # sudo does not lift the freeze either.
        with self.assertRaises(UserError):
            run.sudo().write({'period_from': '2025-01-01'})
        # The frozen field is unchanged.
        self.assertEqual(
            fields.Date.to_string(run.period_to), '2026-12-31')

    def test_pure_state_write_is_not_frozen(self):
        """A state-only forward transition carries no frozen field and passes:
        the normal review / close flow still works on a settled run."""
        run = self._computed_run()
        run.action_review()
        self.assertEqual(run.state, 'reviewed')
        run.action_close()
        self.assertEqual(run.state, 'closed')

    def test_settled_run_cannot_be_unlinked(self):
        """(b) A settled run carries settled figures / a potential GL move, so
        it cannot be deleted; reset to draft first."""
        run = self._computed_run()
        with self.assertRaises(UserError):
            run.unlink()
        self.assertTrue(run.exists(), "settled run must survive the refused "
                        "unlink")
        # A draft run, by contrast, deletes cleanly.
        draft = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-03-31',
        })
        draft.unlink()
        self.assertFalse(draft.exists())

    def test_plain_user_cannot_raw_reset_settled_state(self):
        """(c) A raw ORM state write moving a settled run OUT of the settled
        set (here computed -> draft) is manager-gated: a plain user without the
        sanctioned context flag is refused, so it cannot un-freeze the run by
        resetting it."""
        run = self._computed_run()
        non_manager = self.env['res.users'].create({
            'name': 'Consol Clerk 2',
            'login': 'consol_clerk_reset',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('account.group_account_user').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        self.assertFalse(
            non_manager.has_group('eh_account_base.group_eh_manager'))
        actor = run.with_user(non_manager)
        if hasattr(actor, 'check_access'):
            actor.check_access('write')
        else:
            actor.check_access_rights('write')
            actor.check_access_rule('write')
        with self.assertRaises(UserError):
            actor.write({'state': 'draft'})
        self.assertEqual(run.state, 'computed')

    def test_sanctioned_reset_flow_still_works(self):
        """(d) The manager-gated action_reset_to_draft path sets the
        eh_consol_state_change flag, so the raw state write it performs passes
        the gate and the run returns to draft with its lines dropped, ready for
        a fresh recompute."""
        self.env.user.group_ids |= self.manager_group
        run = self._computed_run()
        run.action_reset_to_draft()
        self.assertEqual(run.state, 'draft')
        self.assertFalse(run.line_ids, "reset drops the settled lines")
        # And recompute rebuilds a fresh, reproducible set.
        run.action_compute()
        self.assertEqual(run.state, 'computed')
        cash_line = run.line_ids.filtered(
            lambda l: l.kind == 'parent_balance'
            and l.account_id == self.account_cash,
        )
        self.assertAlmostEqual(cash_line.amount, 1000.0, places=2)

    def test_settled_run_line_frozen_against_hand_write_and_unlink(self):
        """A settled (computed) run's line cannot be edited or deleted by hand,
        not only when closed: the line guard blocks the full settled set,
        matching the create-append guard."""
        run = self._computed_run()
        line = run.line_ids[0]
        with self.assertRaises(UserError):
            line.write({'amount': line.amount + 500.0})
        with self.assertRaises(UserError):
            line.unlink()


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolPostedMove(EhAccountIntegrationTestCase):
    """BUILD 1: immutable posted consolidation move into a dedicated
    consolidation ledger company (IFRS 10 auditability).

    Opt-in via entity.consolidation_company_id. When unset the run stays a
    memo-only set (covered by the pre-existing tests); the tests here exercise
    the configured path.
    """

    def setUp(self):
        super().setUp()
        self.manager_group = self.env.ref('eh_account_base.group_eh_manager')
        self.env.user.group_ids |= self.manager_group
        Currency = self.env['res.currency']
        self.pres_ccy = Currency.create({
            'name': 'TPM', 'symbol': 'P', 'rounding': 0.01, 'active': True,
        })
        self.pres_ccy.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': self.pres_ccy.id, 'name': '2026-01-01',
            'rate': 1.0, 'company_id': self.company.id,
        })
        # Parent + a dedicated consolidation ledger company, both in the
        # presentation currency so translation is identity.
        self.parent_company = self.env['res.company'].create({
            'name': 'PM Parent', 'currency_id': self.pres_ccy.id,
        })
        self.consol_company = self.env['res.company'].create({
            'name': 'PM Consolidation Ledger',
            'currency_id': self.pres_ccy.id,
        })
        self.env.user.write({'company_ids': [
            (4, self.parent_company.id), (4, self.consol_company.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[
                self.company.id, self.parent_company.id,
                self.consol_company.id],
        ))
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'PM Group', 'code': 'pm_group',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
            'consolidation_company_id': self.consol_company.id,
        })
        self.member = self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0,
            'method': 'full',
        })
        # Consolidation ledger must carry, by code, every account the run
        # references. The subsidiary posts to cash (1000) and revenue (4000);
        # the CTA plug is zero at a flat rate, so only those two are needed.
        _make_account(
            self.env, self.consol_company, '1000', 'Cash', 'asset_cash')
        _make_account(
            self.env, self.consol_company, '4000', 'Revenue', 'income')
        self.source_retained = _make_unaffected_account(
            self.env, self.company, '3998',
            'Posted Snapshot Retained Earnings',
        )
        self.member.retained_earnings_account_id = self.source_retained.id
        self.target_retained = _make_unaffected_account(
            self.env, self.consol_company, self.source_retained.code,
            'Posted Snapshot Retained Earnings',
        )
        _make_general_journal(self.env, self.consol_company)

    def _make_run(self):
        return self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-12-31',
        })

    def test_ledger_company_cannot_be_added_as_direct_member(self):
        with self.assertRaises(ValidationError):
            self.env['eh.consol.member'].create({
                'entity_id': self.entity.id,
                'company_id': self.consol_company.id,
            })
        member = self.entity.member_ids[0]
        with self.assertRaises(ValidationError):
            member.write({'company_id': self.consol_company.id})

    def test_parent_company_cannot_be_rehomed_onto_existing_member(self):
        member = self.entity.member_ids[0]
        with self.assertRaises(ValidationError):
            self.entity.write({'parent_company_id': member.company_id.id})
        self.assertEqual(self.entity.parent_company_id, self.parent_company)

    def test_post_move_is_balanced_and_immutable_and_close_requires_it(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        run.action_review()
        # Close is refused until the consolidation move is posted.
        with self.assertRaises(UserError):
            run.action_close()
        run.action_post_move()
        move = run.move_id
        self.assertTrue(move, "a consolidation move must be created")
        self.assertEqual(move.state, 'posted')
        self.assertEqual(move.company_id, self.consol_company)
        # The move is balanced by construction: debits == credits, net zero.
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('debit')),
            sum(move.line_ids.mapped('credit')), places=2)
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('balance')), 0.0, places=2)
        by_code = {}
        for line in move.line_ids:
            code = line.account_id.with_company(self.consol_company).code
            by_code[code] = by_code.get(code, 0.0) + line.balance
        self.assertEqual(by_code, {'1000': 1000.0, '4000': -1000.0})
        # Each posted line maps into the consolidation company's chart.
        field = _acc_company_field(self.env)
        for line in move.line_ids:
            if field == 'company_ids':
                self.assertIn(
                    self.consol_company, line.account_id.company_ids)
            else:
                self.assertEqual(
                    line.account_id.company_id, self.consol_company)
        # Immutable: a posted move cannot be re-posted.
        with self.assertRaises(UserError):
            run.action_post_move()
        # Now close succeeds.
        run.action_close()
        self.assertEqual(run.state, 'closed')

    def test_locked_snapshot_period_is_refused_without_date_shift(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        run.action_review()
        Move = self.env['account.move'].with_company(self.consol_company)
        domain = [('company_id', '=', self.consol_company.id)]
        before = Move.search_count(domain)
        self.consol_company.fiscalyear_lock_date = run.period_to
        with self.assertRaisesRegex(UserError, 'will not shift period evidence'):
            run.action_post_move()
        self.assertEqual(Move.search_count(domain), before)
        self.assertFalse(run.move_id)
        self.assertFalse(run.move_history_ids)
        self.assertEqual(run.state, 'reviewed')

    def test_two_period_moves_form_latest_snapshot_and_overlap_is_blocked(self):
        # January snapshot: cash +100 / revenue -100.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 100.0},
                {'account': self.account_revenue, 'credit': 100.0},
            ],
            date=fields.Date.from_string('2026-01-10'),
        )
        first = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01',
            'period_to': '2026-01-31',
        })
        first.action_compute()
        first.action_review()
        first.action_post_move()
        first.action_close()

        # February adds cash/revenue 50. The full February snapshot is cash
        # +150, February revenue -50 and opening retained earnings -100. Its
        # posted move must be the delta from January, not another full copy.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 50.0},
                {'account': self.account_revenue, 'credit': 50.0},
            ],
            date=fields.Date.from_string('2026-02-10'),
        )
        second = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-02-01',
            'period_to': '2026-02-28',
        })
        second.action_compute()
        second.action_review()
        second.action_post_move()
        self.assertEqual(second.posting_predecessor_run_id, first)

        def signed_by_code(moves):
            totals = {}
            for line in moves.line_ids:
                code = line.account_id.with_company(self.consol_company).code
                totals[code] = totals.get(code, 0.0) + line.balance
            return totals

        delta = signed_by_code(second.move_id)
        self.assertAlmostEqual(delta['1000'], 50.0, places=2)
        self.assertAlmostEqual(delta['4000'], 50.0, places=2)
        self.assertAlmostEqual(
            delta[self.source_retained.code], -100.0, places=2,
        )
        cumulative = signed_by_code(first.move_id | second.move_id)
        self.assertAlmostEqual(cumulative['1000'], 150.0, places=2)
        self.assertAlmostEqual(cumulative['4000'], -50.0, places=2)
        self.assertAlmostEqual(
            cumulative[self.source_retained.code], -100.0, places=2,
        )

        overlap = self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-02-15',
            'period_to': '2026-03-15',
        })
        overlap.action_compute()
        overlap.action_review()
        with self.assertRaisesRegex(UserError, 'overlap'):
            overlap.action_post_move()
        self.assertFalse(overlap.move_id)

        # A predecessor cannot be corrected underneath an active delta chain.
        with self.assertRaisesRegex(UserError, 'newest-first'):
            first.action_reopen()
        self.assertEqual(first.state, 'closed')

    def test_non_superuser_manager_runs_compute_review_post_and_close(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 25.0},
                {'account': self.account_revenue, 'credit': 25.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        manager = self.env['res.users'].create({
            'name': 'Real Consolidation Manager',
            'login': 'real_consolidation_manager',
            'company_id': self.company.id,
            'company_ids': [(6, 0, (
                self.company | self.parent_company | self.consol_company
            ).ids)],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })
        run = self._make_run()
        actor = run.with_user(manager).with_context(allowed_company_ids=(
            self.company | self.parent_company | self.consol_company
        ).ids)
        self.assertFalse(actor.env.su)
        actor.action_compute()
        actor.action_review()
        actor.action_post_move()
        actor.action_close()
        run.invalidate_recordset()
        self.assertEqual(run.state, 'closed')
        self.assertEqual(run.move_id.state, 'posted')

    def test_reopen_reverses_and_retains_the_move_evidence(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        run.action_review()
        run.action_post_move()
        self.assertTrue(run.move_id)
        run.action_close()
        original = run.move_id
        # Reopen posts a counter-entry and retains both sides of the GL trail.
        run.action_reopen()
        self.assertEqual(run.state, 'reviewed')
        self.assertFalse(
            run.move_id,
            "a reversed entry must never remain the active close pointer",
        )
        self.assertTrue(run.reversal_move_id)
        self.assertEqual(run.reversal_move_id.state, 'posted')
        self.assertIn(original, run.move_history_ids)
        self.assertIn(run.reversal_move_id, run.move_history_ids)
        self.assertAlmostEqual(
            sum((original | run.reversal_move_id).line_ids.mapped('balance')),
            0.0,
            places=2,
        )

    def test_reopen_uses_posting_rounding_snapshot_after_currency_drift(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 0.4},
                {'account': self.account_revenue, 'credit': 0.4},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        run.action_review()
        run.action_post_move()
        run.action_close()
        original = run.move_id
        self.assertEqual(run.posting_currency_rounding, 0.01)

        currency = self.consol_company.currency_id
        self.env.cr.execute(
            "UPDATE res_currency SET rounding = 1.0 WHERE id = %s",
            (currency.id,),
        )
        currency.invalidate_recordset(['rounding'])
        run.action_reopen()
        self.assertFalse(run.move_id)
        self.assertTrue(run.reversal_move_id)
        self.assertEqual(run.reversal_move_id.reversed_entry_id, original)

    def test_posting_snapshot_survives_live_account_metadata_drift(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        cash_line = run.line_ids.filtered(
            lambda line: line.account_id == self.account_cash
            and line.kind == 'subsidiary_balance',
        )[:1]
        self.assertTrue(cash_line.posting_account_id)
        source_type = cash_line.account_type
        posting_id = cash_line.posting_account_id.id
        with self.assertRaises(AccessError):
            cash_line.sudo().write({
                'posting_account_id': self.account_cash.id,
            })
        run.action_review()
        run.action_post_move()

        # External account maintenance must not rewrite the settled snapshots
        # or make close/reversal re-resolve by a mutable source/target code.
        self.env.cr.execute(
            "UPDATE account_account SET account_type = %s WHERE id = %s",
            ('asset_current', self.account_cash.id),
        )
        target = cash_line.posting_account_id
        target.with_company(self.consol_company).code = '1999'
        self.account_cash.invalidate_recordset(['account_type'])
        target.invalidate_recordset(['code'])
        cash_line.invalidate_recordset()
        self.assertEqual(cash_line.account_type, source_type)
        self.assertEqual(cash_line.posting_account_id.id, posting_id)
        run.action_close()
        run.action_reopen()
        self.assertTrue(run.reversal_move_id)

    def test_posting_snapshot_target_cannot_be_deleted(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        target = run.line_ids.filtered('posting_account_id')[0].posting_account_id
        with self.assertRaises(errors.RestrictViolation):
            target.unlink()

    def test_post_revalidates_external_ledger_currency_change(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        other_currency = self.env['res.currency'].create({
            'name': 'PMX', 'symbol': 'X', 'rounding': 0.01, 'active': True,
        })
        self.consol_company.currency_id = other_currency.id
        with self.assertRaises(ValidationError):
            run.action_post_move()

    def test_reopen_reset_recompute_requires_a_new_active_move(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        run.action_review()
        run.action_post_move()
        original = run.move_id
        run.action_close()
        run.action_reopen()
        reversal = run.reversal_move_id
        run.action_reset_to_draft()
        self.assertEqual(run.engine_provenance_version, 0)
        run.action_compute()
        self.assertEqual(run.engine_provenance_version, 2)
        run.action_review()
        with self.assertRaises(UserError):
            run.action_close()
        run.action_post_move()
        replacement = run.move_id
        self.assertTrue(replacement)
        self.assertNotEqual(replacement, original)
        self.assertFalse(run.reversal_move_id)
        self.assertTrue((original | reversal | replacement) <= run.move_history_ids)
        run.action_close()
        self.assertEqual(run.state, 'closed')

    def test_post_move_late_anchor_failure_rolls_back_the_gl(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        run.action_review()
        Move = self.env['account.move']
        before = set(Move.search([]).ids)
        RunClass = type(run)
        original_write = RunClass.write

        def fail_anchor(records, vals):
            if 'move_id' in vals:
                raise UserError("injected consolidation anchor failure")
            return original_write(records, vals)

        with patch.object(RunClass, 'write', fail_anchor):
            try:
                run.action_post_move()
            except UserError:
                pass
            else:
                self.fail("Injected consolidation anchor failure was not raised")
        Move.invalidate_model()
        run.invalidate_recordset()
        self.assertEqual(set(Move.search([]).ids), before)
        self.assertFalse(run.move_id)
        self.assertFalse(run.move_history_ids)

    def test_post_move_late_chatter_failure_rolls_back_the_gl(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        run.action_review()
        Move = self.env['account.move']
        before = set(Move.search([]).ids)
        RunClass = type(run)

        def fail_message(_records, **_kwargs):
            raise UserError("injected consolidation chatter failure")

        with patch.object(RunClass, 'message_post', fail_message):
            try:
                run.action_post_move()
            except UserError:
                pass
            else:
                self.fail("Injected consolidation chatter failure was not raised")
        Move.invalidate_model()
        run.invalidate_recordset()
        self.assertEqual(set(Move.search([]).ids), before)
        self.assertFalse(run.move_id)
        self.assertFalse(run.move_history_ids)

    def test_multi_run_post_failure_rolls_back_the_first_prefix(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        first = self._make_run()
        second = self._make_run()
        runs = first | second
        runs.action_compute()
        runs.action_review()
        Move = self.env['account.move']
        before = set(Move.search([]).ids)
        RunClass = type(first)
        original_write = RunClass.write

        def fail_second_anchor(records, vals):
            if 'move_id' in vals and second in records:
                raise UserError("injected second-run anchor failure")
            return original_write(records, vals)

        with patch.object(RunClass, 'write', fail_second_anchor):
            try:
                runs.action_post_move()
            except UserError:
                pass
            else:
                self.fail("Injected second-run anchor failure was not raised")
        Move.invalidate_model()
        runs.invalidate_recordset()
        self.assertEqual(set(Move.search([]).ids), before)
        self.assertFalse(first.move_id)
        self.assertFalse(second.move_id)

    def test_reversal_late_anchor_failure_preserves_active_graph(self):
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        run.action_review()
        run.action_post_move()
        run.action_close()
        original = run.move_id
        Move = self.env['account.move']
        before = set(Move.search([]).ids)
        RunClass = type(run)
        original_write = RunClass.write

        def fail_reversal_anchor(records, vals):
            if 'reversal_move_id' in vals:
                raise UserError("injected reversal anchor failure")
            return original_write(records, vals)

        with patch.object(RunClass, 'write', fail_reversal_anchor):
            try:
                run.action_reopen()
            except UserError:
                pass
            else:
                self.fail("Injected reversal anchor failure was not raised")
        Move.invalidate_model()
        run.invalidate_recordset()
        self.assertEqual(set(Move.search([]).ids), before)
        self.assertEqual(run.state, 'closed')
        self.assertEqual(run.move_id, original)
        self.assertFalse(run.reversal_move_id)
        self.assertEqual(run.move_history_ids, original)

    def test_missing_account_in_consol_company_raises_named_error(self):
        # Drop the revenue account from the consolidation company so the run
        # references an account the ledger does not carry.
        rev = self.env['account.account'].with_company(
            self.consol_company).search([
                ('code', '=', '4000'),
                (_acc_company_field(self.env), 'in', self.consol_company.ids),
            ], limit=1)
        self.assertTrue(rev, "test setup: revenue account must exist to drop")
        rev.unlink()
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        with self.assertRaises(UserError):
            run.action_compute()

    def test_memo_only_when_no_consolidation_company(self):
        """Defaults-off: an entity with no consolidation company behaves
        exactly as today. Close needs no move, and no move is created."""
        entity = self.env['eh.consol.entity'].create({
            'name': 'Memo Group', 'code': 'memo_group',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
        })
        self.env['eh.consol.member'].create({
            'entity_id': entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0, 'method': 'full',
        })
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self.env['eh.consol.run'].create({
            'entity_id': entity.id,
            'period_from': '2026-01-01', 'period_to': '2026-12-31',
        })
        run.action_compute()
        run.action_review()
        run.action_close()
        self.assertEqual(run.state, 'closed')
        self.assertFalse(run.move_id, "memo-only run posts no move")

    def test_consolidation_company_currency_must_equal_presentation(self):
        """IAS 21 presentation currency: a consolidation ledger company whose
        currency differs from the entity's presentation currency is rejected.

        Run-line amounts are in the presentation currency and are booked
        directly as debit/credit in the ledger company's currency without
        conversion. If the two differ the posted move would be in the wrong
        currency scale, so the mismatched configuration must not be allowed.
        Without the constraint this create succeeds and mis-scaled moves post.
        """
        other_ccy = self.env['res.currency'].create({
            'name': 'TPX', 'symbol': 'X', 'rounding': 0.01, 'active': True,
        })
        mismatched_ledger = self.env['res.company'].create({
            'name': 'PM Mismatched Ledger', 'currency_id': other_ccy.id,
        })
        with self.assertRaises(ValidationError):
            self.env['eh.consol.entity'].create({
                'name': 'Mismatch Group', 'code': 'mismatch_group',
                'parent_company_id': self.parent_company.id,
                'presentation_currency_id': self.pres_ccy.id,
                'consolidation_company_id': mismatched_ledger.id,
            })


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolAutoIntragroup(EhAccountIntegrationTestCase):
    """BUILD 2: automatic IFRS 10.B86 intragroup elimination between member
    companies. Opt-in via entity.auto_eliminate_intragroup (default False)."""

    def setUp(self):
        super().setUp()
        Currency = self.env['res.currency']
        self.pres_ccy = Currency.create({
            'name': 'TIG', 'symbol': 'G', 'rounding': 0.01, 'active': True,
        })
        self.pres_ccy.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': self.pres_ccy.id, 'name': '2026-01-01',
            'rate': 1.0, 'company_id': self.company.id,
        })
        self.parent_company = self.env['res.company'].create({
            'name': 'IG Parent', 'currency_id': self.pres_ccy.id,
        })
        # Two subsidiary companies in the presentation currency (identity
        # translation), each with their own chart.
        self.sub_a = self.env['res.company'].create({
            'name': 'IG Sub A', 'currency_id': self.pres_ccy.id,
        })
        self.sub_b = self.env['res.company'].create({
            'name': 'IG Sub B', 'currency_id': self.pres_ccy.id,
        })
        self.branch_a = self.env['res.company'].create({
            'name': 'IG Sub A Branch',
            'currency_id': self.pres_ccy.id,
            'parent_id': self.sub_a.id,
        })
        self.env.user.write({'company_ids': [
            (4, self.parent_company.id),
            (4, self.sub_a.id), (4, self.sub_b.id),
            (4, self.branch_a.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[
                self.company.id, self.parent_company.id,
                self.sub_a.id, self.sub_b.id, self.branch_a.id],
        ))
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'IG Auto Group', 'code': 'ig_auto_group',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
            'auto_eliminate_intragroup': True,
        })
        for company in (self.sub_a, self.sub_b):
            self.env['eh.consol.member'].create({
                'entity_id': self.entity.id,
                'company_id': company.id,
                'ownership_pct': 100.0, 'method': 'full',
            })
        # Per-company charts.
        self.ar_a = _make_account(
            self.env, self.sub_a, '1100', 'AR A', 'asset_receivable',
            reconcile=True)
        self.ap_a = _make_account(
            self.env, self.sub_a, '2100', 'AP A', 'liability_payable',
            reconcile=True)
        self.rev_a = _make_account(
            self.env, self.sub_a, '4000', 'Rev A', 'income')
        self.exp_a = _make_account(
            self.env, self.sub_a, '5000', 'Exp A', 'expense')
        self.ar_b = _make_account(
            self.env, self.sub_b, '1100', 'AR B', 'asset_receivable',
            reconcile=True)
        self.ap_b = _make_account(
            self.env, self.sub_b, '2100', 'AP B', 'liability_payable',
            reconcile=True)
        self.rev_b = _make_account(
            self.env, self.sub_b, '4000', 'Rev B', 'income')
        self.exp_b = _make_account(
            self.env, self.sub_b, '5000', 'Exp B', 'expense')
        self.j_a = _make_general_journal(self.env, self.sub_a)
        self.j_b = _make_general_journal(self.env, self.sub_b)
        self.branch_cash = _make_account(
            self.env, self.branch_a, '1010', 'Branch Cash', 'asset_current')
        self.branch_revenue = _make_account(
            self.env, self.branch_a, '4010', 'Branch Revenue', 'income')
        self.branch_journal = _make_general_journal(self.env, self.branch_a)
        self.partner_a = self.sub_a.partner_id
        self.partner_b = self.sub_b.partner_id

    def _make_run(self):
        return self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01', 'period_to': '2026-12-31',
        })

    def test_reciprocal_ar_ap_auto_eliminated_and_run_balances(self):
        d = fields.Date.from_string('2026-06-01')
        # A sells to B on credit: A has an AR to B (debit), B has an AP to A
        # (credit). Reciprocal 1000 each. Book against the counterparty's
        # company commercial partner so the engine can match them.
        _post_move(self.env, self.sub_a, self.j_a, d, [
            (self.ar_a, 1000.0, 0.0, self.partner_b),
            (self.rev_a, 0.0, 1000.0, self.partner_b),
        ])
        _post_move(self.env, self.sub_b, self.j_b, d, [
            (self.exp_b, 1000.0, 0.0, self.partner_a),
            (self.ap_b, 0.0, 1000.0, self.partner_a),
        ])
        run = self._make_run()
        run.action_compute()
        # AR account nets to zero at group level (subsidiary_balance +1000 plus
        # auto-elimination -1000).
        ar_total = sum(run.line_ids.filtered(
            lambda l: l.account_id == self.ar_a).mapped('amount'))
        self.assertAlmostEqual(ar_total, 0.0, places=2)
        # AP account nets to zero at group level (subsidiary_balance -1000 plus
        # auto-elimination +1000).
        ap_total = sum(run.line_ids.filtered(
            lambda l: l.account_id == self.ap_b).mapped('amount'))
        self.assertAlmostEqual(ap_total, 0.0, places=2)
        # Auto-elimination legs present and net to zero.
        auto = run.line_ids.filtered(
            lambda l: l.kind == 'elimination'
            and l.account_id in (self.ar_a | self.ap_b))
        self.assertEqual(len(auto), 2, "one balanced AR/AP elimination pair")
        self.assertAlmostEqual(sum(auto.mapped('amount')), 0.0, places=2)
        # Whole run still balances (CTA unaffected).
        run_total = sum(run.line_ids.mapped('amount'))
        self.assertTrue(float_is_zero(
            run_total, precision_rounding=self.pres_ccy.rounding))
        # Reciprocals agree, so no mismatch diagnostic is surfaced (other,
        # orthogonal warnings about investment config may still appear).
        self.assertNotIn(
            'does not agree', run.consolidation_warning or '')

    def test_reciprocal_sales_purchases_auto_eliminated(self):
        d = fields.Date.from_string('2026-06-01')
        # A recognises 2000 sales to B; B recognises 2000 purchases from A.
        _post_move(self.env, self.sub_a, self.j_a, d, [
            (self.ar_a, 2000.0, 0.0, self.partner_b),
            (self.rev_a, 0.0, 2000.0, self.partner_b),
        ])
        _post_move(self.env, self.sub_b, self.j_b, d, [
            (self.exp_b, 2000.0, 0.0, self.partner_a),
            (self.ap_b, 0.0, 2000.0, self.partner_a),
        ])
        run = self._make_run()
        run.action_compute()
        # Sales income nets to zero at group level.
        rev_total = sum(run.line_ids.filtered(
            lambda l: l.account_id == self.rev_a).mapped('amount'))
        self.assertAlmostEqual(rev_total, 0.0, places=2)
        # Purchase / COGS nets to zero at group level.
        exp_total = sum(run.line_ids.filtered(
            lambda l: l.account_id == self.exp_b).mapped('amount'))
        self.assertAlmostEqual(exp_total, 0.0, places=2)
        sales_elim = run.line_ids.filtered(
            lambda l: l.kind == 'elimination'
            and l.account_id in (self.rev_a | self.exp_b))
        self.assertEqual(len(sales_elim), 2)
        self.assertAlmostEqual(sum(sales_elim.mapped('amount')), 0.0, places=2)

    def test_member_branch_balances_are_included_in_exact_scope(self):
        _post_move(
            self.env, self.branch_a, self.branch_journal,
            fields.Date.from_string('2026-06-01'), [
                (self.branch_cash, 300.0, 0.0, False),
                (self.branch_revenue, 0.0, 300.0, False),
            ],
        )
        run = self._make_run()
        run.action_compute()
        branch_lines = run.line_ids.filtered(
            lambda line: line.kind == 'subsidiary_balance'
            and line.account_id in (self.branch_cash | self.branch_revenue),
        )
        self.assertEqual(len(branch_lines), 2)
        self.assertEqual(
            sorted(branch_lines.mapped('amount')),
            [-300.0, 300.0],
        )
        self.assertEqual(branch_lines.mapped('member_id').company_id, self.sub_a)

    def test_account_level_matching_and_member_map_preserve_provenance(self):
        ar_a_2 = _make_account(
            self.env, self.sub_a, '1101', 'AR A second',
            'asset_receivable', reconcile=True)
        ap_b_2 = _make_account(
            self.env, self.sub_b, '2101', 'AP B second',
            'liability_payable', reconcile=True)
        group_ar = _make_account(
            self.env, self.parent_company, '1110', 'Group AR',
            'asset_receivable', reconcile=True)
        group_ap = _make_account(
            self.env, self.parent_company, '2110', 'Group AP',
            'liability_payable', reconcile=True)
        member_a = self.entity.member_ids.filtered(
            lambda member: member.company_id == self.sub_a)
        member_b = self.entity.member_ids.filtered(
            lambda member: member.company_id == self.sub_b)
        member_a.consolidation_account_map = '1100 -> 1110'
        member_b.consolidation_account_map = '2100 -> 2110'
        d = fields.Date.from_string('2026-06-01')
        _post_move(self.env, self.sub_a, self.j_a, d, [
            (self.ar_a, 600.0, 0.0, self.partner_b),
            (ar_a_2, 400.0, 0.0, self.partner_b),
            (self.rev_a, 0.0, 1000.0, self.partner_b),
        ])
        _post_move(self.env, self.sub_b, self.j_b, d, [
            (self.exp_b, 1000.0, 0.0, self.partner_a),
            (self.ap_b, 0.0, 600.0, self.partner_a),
            (ap_b_2, 0.0, 400.0, self.partner_a),
        ])
        run = self._make_run()
        run.action_compute()
        ar_ap_auto = run.line_ids.filtered(
            lambda line: line.kind == 'elimination'
            and 'Auto IC AR/AP' in (line.notes or ''),
        )
        by_account = {
            line.account_id: line.amount for line in ar_ap_auto
        }
        self.assertEqual(by_account[group_ar], -600.0)
        self.assertEqual(by_account[ar_a_2], -400.0)
        self.assertEqual(by_account[group_ap], 600.0)
        self.assertEqual(by_account[ap_b_2], 400.0)
        self.assertEqual(sum(ar_ap_auto.mapped('amount')), 0.0)
        self.assertEqual(sum(run.line_ids.filtered(
            lambda line: line.account_id == group_ar).mapped('amount')), 0.0)
        self.assertNotIn(
            'Unmatched intragroup AR/AP', run.consolidation_warning or '')

    def test_child_contacts_match_and_posted_manual_accounts_are_skipped(self):
        contact_b = self.env['res.partner'].create({
            'name': 'IG Sub B billing contact',
            'parent_id': self.partner_b.id,
            'is_company': False,
        })
        contact_a = self.env['res.partner'].create({
            'name': 'IG Sub A billing contact',
            'parent_id': self.partner_a.id,
            'is_company': False,
        })
        d = fields.Date.from_string('2026-06-01')
        _post_move(self.env, self.sub_a, self.j_a, d, [
            (self.ar_a, 700.0, 0.0, contact_b),
            (self.rev_a, 0.0, 700.0, contact_b),
        ])
        _post_move(self.env, self.sub_b, self.j_b, d, [
            (self.exp_b, 700.0, 0.0, contact_a),
            (self.ap_b, 0.0, 700.0, contact_a),
        ])
        run = self._make_run()
        manual = self.env['eh.consol.elimination'].create({
            'run_id': run.id,
            'name': 'Reviewer-approved AR/AP elimination',
            'line_ids': [
                (0, 0, {
                    'account_id': self.ar_a.id,
                    'credit': 700.0,
                }),
                (0, 0, {
                    'account_id': self.ap_b.id,
                    'debit': 700.0,
                }),
            ],
        })
        manual.action_post()
        run.action_compute()
        auto_ar_ap = run.line_ids.filtered(
            lambda line: line.kind == 'elimination'
            and 'Auto IC AR/AP' in (line.notes or ''),
        )
        self.assertFalse(auto_ar_ap)
        auto_sales = run.line_ids.filtered(
            lambda line: line.kind == 'elimination'
            and 'Auto IC sales/COGS' in (line.notes or ''),
        )
        self.assertEqual(len(auto_sales), 2)
        self.assertEqual(
            {line.account_id: line.amount for line in auto_sales},
            {self.rev_a: 700.0, self.exp_b: -700.0},
        )
        manual_lines = run.line_ids.filtered(
            lambda line: line.elimination_id == manual)
        self.assertEqual(len(manual_lines), 2)
        self.assertEqual(sum(manual_lines.mapped('amount')), 0.0)

    def test_functional_currency_override_uses_amount_currency_for_auto_ic(self):
        foreign = self.env['res.currency'].create({
            'name': 'TIF', 'symbol': 'F', 'rounding': 0.01, 'active': True,
        })
        foreign.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': foreign.id,
            'name': '2026-01-01',
            'rate': 0.5,
            'company_id': self.sub_a.id,
        })
        member_a = self.entity.member_ids.filtered(
            lambda member: member.company_id == self.sub_a)
        member_a.functional_currency_id = foreign
        d = fields.Date.from_string('2026-06-01')
        factor = foreign._convert(
            1.0, self.pres_ccy, self.sub_a, d, round=False)
        translated = self.pres_ccy.round(500.0 * factor)
        move_a = self.env['account.move'].with_company(self.sub_a).create({
            'company_id': self.sub_a.id,
            'move_type': 'entry',
            'date': d,
            'journal_id': self.j_a.id,
            'line_ids': [
                (0, 0, {
                    'account_id': self.ar_a.id,
                    'partner_id': self.partner_b.id,
                    'debit': 900.0,
                    'currency_id': foreign.id,
                    'amount_currency': 500.0,
                }),
                (0, 0, {
                    'account_id': self.rev_a.id,
                    'partner_id': self.partner_b.id,
                    'credit': 900.0,
                    'currency_id': foreign.id,
                    'amount_currency': -500.0,
                }),
            ],
        })
        move_a.action_post()
        _post_move(self.env, self.sub_b, self.j_b, d, [
            (self.exp_b, translated, 0.0, self.partner_a),
            (self.ap_b, 0.0, translated, self.partner_a),
        ])
        run = self._make_run()
        run.action_compute()
        ar_source = run.line_ids.filtered(
            lambda line: line.kind == 'subsidiary_balance'
            and line.account_id == self.ar_a)
        self.assertEqual(ar_source.amount, translated)
        self.assertNotEqual(ar_source.amount, 900.0)
        self.assertEqual(sum(run.line_ids.filtered(
            lambda line: line.account_id == self.ar_a).mapped('amount')), 0.0)
        self.assertNotIn(
            'Unmatched intragroup AR/AP', run.consolidation_warning or '')

    def test_mismatched_reciprocal_raises_warning(self):
        d = fields.Date.from_string('2026-06-01')
        # A's AR to B is 1000, but B's AP to A is only 900 (mismatch of 100).
        _post_move(self.env, self.sub_a, self.j_a, d, [
            (self.ar_a, 1000.0, 0.0, self.partner_b),
            (self.rev_a, 0.0, 1000.0, self.partner_b),
        ])
        _post_move(self.env, self.sub_b, self.j_b, d, [
            (self.exp_b, 900.0, 0.0, self.partner_a),
            (self.ap_b, 0.0, 900.0, self.partner_a),
        ])
        run = self._make_run()
        run.action_compute()
        self.assertTrue(
            run.consolidation_warning,
            "a mismatched reciprocal must surface a diagnostic")
        self.assertIn(
            'Unmatched intragroup AR/AP source residual',
            run.consolidation_warning,
        )
        # The run still balances: the elimination pair is balanced by
        # construction even though the underlying reciprocal does not agree.
        run_total = sum(run.line_ids.mapped('amount'))
        self.assertTrue(float_is_zero(
            run_total, precision_rounding=self.pres_ccy.rounding))

    def test_auto_off_leaves_run_unchanged(self):
        """Defaults-off: with auto_eliminate_intragroup False, no automatic
        elimination legs are produced (existing behaviour preserved)."""
        self.entity.auto_eliminate_intragroup = False
        d = fields.Date.from_string('2026-06-01')
        _post_move(self.env, self.sub_a, self.j_a, d, [
            (self.ar_a, 1000.0, 0.0, self.partner_b),
            (self.rev_a, 0.0, 1000.0, self.partner_b),
        ])
        _post_move(self.env, self.sub_b, self.j_b, d, [
            (self.exp_b, 1000.0, 0.0, self.partner_a),
            (self.ap_b, 0.0, 1000.0, self.partner_a),
        ])
        run = self._make_run()
        run.action_compute()
        # AR is NOT eliminated: it stays at its gross subsidiary balance.
        ar_total = sum(run.line_ids.filtered(
            lambda l: l.account_id == self.ar_a).mapped('amount'))
        self.assertAlmostEqual(ar_total, 1000.0, places=2)
        # No auto elimination legs.
        auto = run.line_ids.filtered(
            lambda l: l.kind == 'elimination' and l.member_id is False
            and l.elimination_id is False)
        self.assertFalse(auto, "no auto elimination when the flag is off")


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolUnresolvedAccountRaises(EhAccountIntegrationTestCase):
    """A CTA / NCI line that cannot resolve an account must refuse the compute
    with a clear UserError, never silently post a run line with account_id
    False (which would drop the translation reserve / minority interest from
    the consolidated set).
    """

    def setUp(self):
        super().setUp()
        Currency = self.env['res.currency']
        Rate = self.env['res.currency.rate']
        # Presentation currency with a genuine rate movement so a foreign
        # subsidiary produces a non-zero CTA that must be booked somewhere.
        self.pres_ccy = Currency.create({
            'name': 'TUR', 'symbol': 'R', 'rounding': 0.01, 'active': True,
        })
        self.pres_ccy.rate_ids.unlink()
        Rate.create({
            'currency_id': self.pres_ccy.id, 'name': '2026-01-01',
            'rate': 2.0, 'company_id': self.company.id,
        })
        Rate.create({
            'currency_id': self.pres_ccy.id, 'name': '2026-12-31',
            'rate': 3.0, 'company_id': self.company.id,
        })
        # A bare parent company: NO equity account named 'translation'/'CTA'
        # or 'non-controlling'/'minority', and no explicit config, so nothing
        # resolves by heuristic either.
        self.parent_company = self.env['res.company'].create({
            'name': 'Unresolved Parent', 'currency_id': self.pres_ccy.id,
        })
        self.env.user.write({'company_ids': [(4, self.parent_company.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[self.company.id, self.parent_company.id],
        ))
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'Unresolved Group', 'code': 'unresolved_group',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
        })

    def _make_run(self):
        return self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01', 'period_to': '2026-12-31',
        })

    def test_unresolved_cta_account_raises_on_compute(self):
        """A non-zero CTA with no resolvable account refuses the compute."""
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0, 'method': 'full',
        })
        # An FX-sensitive balanced move: asset (closing rate) vs income
        # (average rate). The rate spread produces a genuine non-zero CTA.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        with self.assertRaises(UserError):
            run.action_compute()

    def test_configuring_cta_account_lets_compute_succeed(self):
        """The same run computes cleanly once a CTA account is configured on
        the entity, and the CTA line carries that exact account (no False)."""
        cta_account = _make_account(
            self.env, self.parent_company, '3900',
            'FX Reserve', 'equity')
        self.entity.cta_account_id = cta_account.id
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 100.0, 'method': 'full',
        })
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 1000.0},
                {'account': self.account_revenue, 'credit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        cta_lines = run.line_ids.filtered(lambda l: l.kind == 'cta')
        self.assertTrue(cta_lines, "a CTA line must be produced")
        for line in cta_lines:
            self.assertEqual(
                line.account_id, cta_account,
                "the CTA line must carry the configured account, never False")

    def test_unresolved_nci_account_raises_on_compute(self):
        """A non-zero NCI carve-out with no resolvable NCI account refuses the
        compute (a CTA account is configured so the CTA path is not what
        raises)."""
        cta_account = _make_account(
            self.env, self.parent_company, '3900', 'FX Reserve', 'equity')
        # A retained-earnings account so the NCI reclass leg resolves; only the
        # NCI equity account itself is left unresolvable.
        _make_account(
            self.env, self.parent_company, '3100',
            'Consolidated Retained Earnings', 'equity_unaffected')
        self.entity.cta_account_id = cta_account.id
        self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': 80.0, 'method': 'full',
        })
        # Subsidiary equity so there is a minority interest to carve.
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        with self.assertRaises(UserError):
            run.action_compute()


@tagged('eh_account_consolidation', 'integration', 'post_install', '-at_install')
class TestConsolGoodwillImpairment(EhAccountIntegrationTestCase):
    """IAS 36 goodwill impairment: a manager-gated action tests recognised
    goodwill against a recoverable amount and, when impaired, books a balanced
    impairment run line that reduces goodwill.

    Flat single rate so the arithmetic is a clean scalar with no CTA noise.
    """

    def setUp(self):
        super().setUp()
        self.manager_group = self.env.ref('eh_account_base.group_eh_manager')
        self.env.user.group_ids |= self.manager_group
        Currency = self.env['res.currency']
        self.pres_ccy = Currency.create({
            'name': 'TGI', 'symbol': 'I', 'rounding': 0.01, 'active': True,
        })
        self.pres_ccy.rate_ids.unlink()
        self.env['res.currency.rate'].create({
            'currency_id': self.pres_ccy.id, 'name': '2026-01-01',
            'rate': 1.0, 'company_id': self.company.id,
        })
        self.parent_company = self.env['res.company'].create({
            'name': 'GI Parent', 'currency_id': self.pres_ccy.id,
        })
        self.env.user.write({'company_ids': [(4, self.parent_company.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[self.company.id, self.parent_company.id],
        ))
        self.entity = self.env['eh.consol.entity'].create({
            'name': 'GI Group', 'code': 'gi_group',
            'parent_company_id': self.parent_company.id,
            'presentation_currency_id': self.pres_ccy.id,
        })
        self.parent_investment = _make_account(
            self.env, self.parent_company, '1500',
            'Investment in Sub', 'asset_non_current')
        self.parent_equity_elim = _make_account(
            self.env, self.parent_company, '3100',
            'Pre-Acq Equity Elimination', 'equity')
        self.parent_goodwill = _make_account(
            self.env, self.parent_company, '1600', 'Goodwill',
            'asset_non_current')
        self.parent_nci = _make_account(
            self.env, self.parent_company, '3200',
            'Non-Controlling Interest', 'equity')
        self.parent_impairment = _make_account(
            self.env, self.parent_company, '5900',
            'Goodwill Impairment Loss', 'expense')

    def _make_member(self, A=5000.0, I=6000.0, o=1.0):
        """Full member whose IFRS 3 acquisition elimination books goodwill of
        I - o*A. At A=5000, I=6000, o=1.0 the goodwill is 1000."""
        return self.env['eh.consol.member'].create({
            'entity_id': self.entity.id,
            'company_id': self.company.id,
            'ownership_pct': o * 100.0,
            'method': 'full',
            'investment_account_id': self.parent_investment.id,
            'investment_amount': I,
            'acquisition_equity': A,
            'acquisition_component_ids': [(0, 0, {
                'source_account_id': self.account_equity.id,
                'amount': A,
            })],
            'equity_elimination_account_id': self.parent_equity_elim.id,
            'goodwill_account_id': self.parent_goodwill.id,
            'nci_account_id': self.parent_nci.id,
        })

    def _make_run(self):
        return self.env['eh.consol.run'].create({
            'entity_id': self.entity.id,
            'period_from': '2026-01-01', 'period_to': '2026-12-31',
        })

    def _computed_run_with_goodwill(self):
        """Compute a run that recognises 1000 of goodwill (I - o*A = 1000)."""
        self._make_member(A=5000.0, I=6000.0, o=1.0)
        self.post_balanced_move(
            [
                {'account': self.account_cash, 'debit': 5000.0},
                {'account': self.account_equity, 'credit': 5000.0},
            ],
            date=fields.Date.from_string('2026-06-01'),
        )
        run = self._make_run()
        run.action_compute()
        self.assertAlmostEqual(
            run._eh_recognised_goodwill(), 1000.0, places=2,
            msg="setup: the run must recognise 1000 of goodwill")
        return run

    def test_untouched_zero_recoverable_amount_cannot_write_off_goodwill(self):
        run = self._computed_run_with_goodwill()
        run.goodwill_impairment_account_id = self.parent_impairment.id
        self.assertEqual(run.goodwill_recoverable_amount, 0.0)
        self.assertFalse(run.goodwill_recoverable_confirmed)
        before = run.line_ids.ids
        with self.assertRaisesRegex(UserError, 'manager-confirmed'):
            run.action_impair_goodwill()
        self.assertEqual(run.line_ids.ids, before)
        self.assertEqual(run.goodwill_impairment_amount, 0.0)

        # Zero remains a legitimate IAS 36 conclusion, but only through the
        # explicit full-write-off confirmation rather than a field default.
        run.goodwill_recoverable_confirmed = True
        run.action_impair_goodwill()
        self.assertEqual(run.goodwill_impairment_amount, 1000.0)
        self.assertAlmostEqual(run._eh_recognised_goodwill(), 0.0, places=2)

    def test_impairment_reduces_goodwill_and_books_balanced_charge(self):
        self.env.user.group_ids |= self.manager_group
        run = self._computed_run_with_goodwill()
        # Recoverable amount 600 < recognised goodwill 1000 -> impair by 400.
        run.goodwill_recoverable_amount = 600.0
        run.goodwill_impairment_account_id = self.parent_impairment.id
        run.action_impair_goodwill()
        self.assertEqual(
            run.goodwill_test_recoverable_amount, 600.0,
        )
        self.assertEqual(
            run.goodwill_test_account_id, self.parent_impairment,
        )
        self.assertEqual(run.goodwill_test_currency_id, self.pres_ccy)
        # The impairment charge is recorded on the run.
        self.assertAlmostEqual(
            run.goodwill_impairment_amount, 400.0, places=2)
        # Two balanced impairment legs that net to zero.
        imp_lines = run.line_ids.filtered(lambda l: l.kind == 'impairment')
        self.assertEqual(len(imp_lines), 2, "two impairment legs expected")
        self.assertAlmostEqual(
            sum(imp_lines.mapped('amount')), 0.0, places=2)
        # Dr impairment expense +400, Cr goodwill -400.
        exp_leg = imp_lines.filtered(
            lambda l: l.account_id == self.parent_impairment)
        gw_leg = imp_lines.filtered(
            lambda l: l.account_id == self.parent_goodwill)
        self.assertAlmostEqual(exp_leg.amount, 400.0, places=2)
        self.assertAlmostEqual(gw_leg.amount, -400.0, places=2)
        # Net recognised goodwill after the charge is 1000 - 400 = 600, exactly
        # the recoverable amount.
        self.assertAlmostEqual(
            run._eh_recognised_goodwill(), 600.0, places=2)
        # The whole run still balances after the impairment.
        run_total = sum(run.line_ids.mapped('amount'))
        self.assertTrue(float_is_zero(
            run_total, precision_rounding=self.pres_ccy.rounding),
            "the run must stay balanced after the impairment charge")

    def test_no_impairment_when_recoverable_at_or_above_goodwill(self):
        self.env.user.group_ids |= self.manager_group
        run = self._computed_run_with_goodwill()
        # Recoverable amount 1000 == recognised goodwill -> no impairment.
        run.goodwill_recoverable_amount = 1000.0
        run.goodwill_impairment_account_id = self.parent_impairment.id
        run.action_impair_goodwill()
        self.assertAlmostEqual(run.goodwill_impairment_amount, 0.0, places=2)
        self.assertFalse(
            run.line_ids.filtered(lambda l: l.kind == 'impairment'),
            "no impairment line when goodwill is within recoverable amount")

    def test_impairment_is_idempotent_on_rerun(self):
        """Re-running the test reverses the prior charge first, so the
        impairment is never double-booked."""
        self.env.user.group_ids |= self.manager_group
        run = self._computed_run_with_goodwill()
        run.goodwill_recoverable_amount = 600.0
        run.goodwill_impairment_account_id = self.parent_impairment.id
        run.action_impair_goodwill()
        run.action_impair_goodwill()
        imp_lines = run.line_ids.filtered(lambda l: l.kind == 'impairment')
        self.assertEqual(
            len(imp_lines), 2,
            "re-running must not double-book the impairment legs")
        self.assertAlmostEqual(
            run.goodwill_impairment_amount, 400.0, places=2)

    def test_impairment_input_change_requires_retest(self):
        self.env.user.group_ids |= self.manager_group
        run = self._computed_run_with_goodwill()
        run.write({
            'goodwill_recoverable_amount': 600.0,
            'goodwill_impairment_account_id': self.parent_impairment.id,
        })
        run.action_impair_goodwill()
        run.write({'goodwill_recoverable_amount': 700.0})
        self.assertFalse(run.goodwill_test_currency_id)
        with self.assertRaises(UserError):
            run.action_review()
        run.action_impair_goodwill()
        run.action_review()
        self.assertEqual(run.state, 'reviewed')
        self.assertAlmostEqual(run.goodwill_impairment_amount, 300.0)

    def test_impairment_after_review_demotes_for_fresh_signoff(self):
        self.env.user.group_ids |= self.manager_group
        run = self._computed_run_with_goodwill()
        run.write({
            'goodwill_recoverable_amount': 600.0,
            'goodwill_impairment_account_id': self.parent_impairment.id,
        })
        run.action_impair_goodwill()
        run.action_review()
        self.assertTrue(run.reviewed_at)
        run.write({'goodwill_recoverable_amount': 700.0})
        run.action_impair_goodwill()
        self.assertEqual(run.state, 'computed')
        self.assertFalse(run.reviewed_at)
        self.assertFalse(run.reviewed_by_id)

    def test_impairment_late_failure_rolls_back_complete_graph(self):
        self.env.user.group_ids |= self.manager_group
        run = self._computed_run_with_goodwill()
        run.write({
            'goodwill_recoverable_amount': 600.0,
            'goodwill_impairment_account_id': self.parent_impairment.id,
        })
        before_ids = run.line_ids.ids
        with patch.object(
            type(run), 'message_post', autospec=True,
            side_effect=UserError('injected late chatter failure'),
        ):
            try:
                run.action_impair_goodwill()
            except UserError:
                pass
            else:
                self.fail("Injected impairment chatter failure was not raised")
        run.invalidate_recordset()
        self.assertEqual(run.line_ids.ids, before_ids)
        self.assertFalse(run.goodwill_test_currency_id)
        self.assertEqual(run.goodwill_impairment_amount, 0.0)

    def test_impairment_revalidates_raw_wrong_role_account(self):
        self.env.user.group_ids |= self.manager_group
        run = self._computed_run_with_goodwill()
        self.env.cr.execute(
            "UPDATE eh_consol_run SET goodwill_impairment_account_id = %s "
            "WHERE id = %s",
            (self.account_revenue.id, run.id),
        )
        run.invalidate_recordset(['goodwill_impairment_account_id'])
        with self.assertRaises(UserError):
            run.action_impair_goodwill()

    def test_impairment_is_manager_gated(self):
        run = self._computed_run_with_goodwill()
        run.goodwill_recoverable_amount = 600.0
        run.goodwill_impairment_account_id = self.parent_impairment.id
        non_manager = self.env['res.users'].create({
            'name': 'Consol Clerk GI',
            'login': 'consol_clerk_impair',
            'group_ids': [(6, 0, [
                self.env.ref('account.group_account_user').id,
            ])],
        })
        self.assertFalse(
            non_manager.has_group('eh_account_base.group_eh_manager'))
        with self.assertRaises(UserError):
            run.with_user(non_manager).action_impair_goodwill()

    def test_impairment_included_in_posted_move(self):
        """When a consolidation ledger company is configured the impairment
        legs feed the posted consolidation move like any other run line."""
        self.env.user.group_ids |= self.manager_group
        consol_company = self.env['res.company'].create({
            'name': 'GI Consolidation Ledger',
            'currency_id': self.pres_ccy.id,
        })
        self.env.user.write({'company_ids': [(4, consol_company.id)]})
        self.env = self.env(context=dict(
            self.env.context,
            allowed_company_ids=[
                self.company.id, self.parent_company.id, consol_company.id],
        ))
        self.entity.consolidation_company_id = consol_company.id
        # The ledger must carry, by code, every account the run references.
        for code, name, atype in (
            ('1000', 'Cash', 'asset_cash'),
            ('3000', 'Owner Equity', 'equity'),
            ('1500', 'Investment in Sub', 'asset_non_current'),
            ('3100', 'Pre-Acq Equity Elimination', 'equity'),
            ('1600', 'Goodwill', 'asset_non_current'),
            ('3200', 'Non-Controlling Interest', 'equity'),
            ('5900', 'Goodwill Impairment Loss', 'expense'),
        ):
            _make_account(self.env, consol_company, code, name, atype)
        _make_general_journal(self.env, consol_company)
        run = self._computed_run_with_goodwill()
        run.goodwill_recoverable_amount = 600.0
        run.goodwill_impairment_account_id = self.parent_impairment.id
        run.action_impair_goodwill()
        run.action_review()
        run.action_post_move()
        move = run.move_id
        self.assertEqual(move.state, 'posted')
        # The impairment expense account appears on the posted move (mapped by
        # code into the ledger company chart).
        ledger_impairment = self.env['account.account'].with_company(
            consol_company).search([
                ('code', '=', '5900'),
                (_acc_company_field(self.env), 'in', consol_company.ids),
            ], limit=1)
        move_accounts = move.line_ids.mapped('account_id')
        self.assertIn(
            ledger_impairment, move_accounts,
            "the impairment charge must be included in the posted move")
        # The posted move is balanced by construction.
        self.assertAlmostEqual(
            sum(move.line_ids.mapped('balance')), 0.0, places=2)
