# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: state is a state machine, not a free field.

Every disclosure register (financial risk, maturity run, credit-risk note,
entity interest, sensitivity analysis, segment report, related party) drives
its state only through the manager-gated Finalise / Reopen actions, which run
under sudo. The inherited eh.workflow.guard mixin blocks a low-privilege user
from RPC-writing state directly, which would otherwise skip the action and
its lock / freeze.

The default test environment runs as SUPERUSER (env.su is True), for which
the mixin deliberately abstains (trusted, server-initiated code). The bypass
this closes is an interactive, low-privilege user, so the guarded write must
be attempted with_user(a normal user) for the guard to fire.
"""

from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.eh_account_disclosures.models.integrity import (
    DISCLOSURE_ENGINE_CAPABILITY,
    DISCLOSURE_ENGINE_CONTEXT,
)


@tagged('eh_account_disclosures', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):
    """A direct RPC write to the guarded state field is refused for a
    low-privilege user; the sanctioned sudo path is still allowed."""

    # model -> minimal create vals for a draft record.
    GUARDED_MODELS = {
        'eh.fin.risk': {'name': 'Trade receivables'},
        'eh.fin.maturity.run': {},
        'eh.fin.credit.note': {},
        'eh.entity.interest': {'name': 'Subsidiary Co'},
        'eh.fin.sensitivity': {},
        'eh.segment.report': {},
        'eh.related.party': {'name': 'Parent Co'},
    }

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A normal internal user in the EH user group (read/write/create on the
        # models) but NOT superuser, so the mixin guard is exercised rather
        # than the ACL layer.
        try:
            eh_user = cls.env.ref('eh_account_base.group_eh_user')
            cls.normal_user = cls.env['res.users'].with_context(
                mail_create_nosubscribe=True,
                no_reset_password=True,
            ).create({
                'name': 'Disclosure Guard Tester',
                'login': 'disclosure_guard_tester',
                'group_ids': [(6, 0, [eh_user.id])],
            })
        except Exception as exc:  # pragma: no cover - env cascade quirk
            cls.normal_user = None
            cls._user_error = exc

    def _require_user(self):
        if not self.normal_user:
            self.skipTest(
                "environment cannot create a test user: %s"
                % getattr(self, '_user_error', 'unknown'))

    def test_state_write_refused_for_low_privilege_user(self):
        self._require_user()
        for model, vals in self.GUARDED_MODELS.items():
            with self.subTest(model=model):
                rec = self.env[model].create(dict(vals))
                self.assertEqual(
                    rec.state, 'draft',
                    "%s must be born in draft" % model)
                # Direct RPC re-key of the state machine is blocked by the
                # mixin: a plain user cannot flip state past the action.
                with self.assertRaises(AccessError):
                    rec.with_user(self.normal_user).write(
                        {'state': 'finalised'})

    def test_sudo_action_path_still_transitions(self):
        # The sanctioned action path runs under sudo (env.su True), for which
        # the mixin abstains, so a state transition still goes through.
        for model, vals in self.GUARDED_MODELS.items():
            with self.subTest(model=model):
                rec = self.env[model].create(dict(vals))
                rec.sudo().write({'state': 'finalised'})
                self.assertEqual(
                    rec.state, 'finalised',
                    "%s: a sudo (action) write must still transition" % model)

    def test_manager_cannot_reopen_hidden_other_company_record(self):
        """Manager group membership must not bypass global company rules.

        Reopen used to elevate before touching the record, so call_kw could
        browse a guessed id hidden by the company rule and mutate it under
        sudo.  Every workflow action now proves write access first.
        """
        other_company = self.env['res.company'].create({
            'name': 'Hidden Disclosure Company',
        })
        manager = new_test_user(
            self.env,
            login='disclosure_cross_company_manager',
            groups='eh_account_base.group_eh_manager',
        )
        self.assertNotIn(other_company, manager.company_ids)

        for model, base_vals in self.GUARDED_MODELS.items():
            with self.subTest(model=model):
                vals = dict(base_vals, company_id=other_company.id)
                record = self.env[model].create(vals)
                record.sudo().write({'state': 'finalised'})
                with self.assertRaises(AccessError):
                    record.with_user(manager).action_reopen()
                self.assertEqual(record.state, 'finalised')

    def test_computed_disclosure_rows_are_engine_owned(self):
        self._require_user()
        run = self.env['eh.fin.sensitivity'].create({})
        Line = self.env['eh.fin.sensitivity.line']
        with self.assertRaises(AccessError):
            Line.with_user(self.normal_user).with_context(
                **{DISCLOSURE_ENGINE_CONTEXT: True}
            ).create({
                'run_id': run.id,
                'origin': 'computed',
                'kind': 'fx',
                'name': 'forged engine evidence',
            })
        computed = Line.with_context(**{
            DISCLOSURE_ENGINE_CONTEXT: DISCLOSURE_ENGINE_CAPABILITY,
        }).create({
            'run_id': run.id,
            'origin': 'computed',
            'kind': 'fx',
            'name': 'real engine evidence',
            'exposure': 100.0,
            'shock': '+10%',
            'pnl_impact': 10.0,
        })
        with self.assertRaises(AccessError):
            computed.with_context(**{
                DISCLOSURE_ENGINE_CONTEXT: None,
            }).with_user(self.normal_user).write({
                'pnl_impact': 999999.0,
            })
        with self.assertRaises(AccessError):
            computed.with_context(**{
                DISCLOSURE_ENGINE_CONTEXT: None,
            }).with_user(self.normal_user).unlink()

    def test_parent_edit_takes_finalisation_row_lock(self):
        run = self.env['eh.fin.sensitivity'].create({})
        queries = []
        real_execute = self.env.cr.execute

        def _record_execute(*args, **kwargs):
            queries.append(str(args[0]))
            return real_execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', _record_execute):
            run.write({'notes': 'lock regression'})
        self.assertTrue(any(
            'eh_fin_sensitivity' in query and 'FOR UPDATE' in query
            for query in queries
        ))
