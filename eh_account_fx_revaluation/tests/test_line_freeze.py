# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Regression: eh.fx.revaluation.line create()/write() freeze guard.

A posted FX revaluation run's lines are the audited support behind a sealed
journal entry; the run's totals (net_adjustment, total_gain/loss, the
realized/unrealized split) are STORED and recompute from
line_ids.adjustment. The model froze write() and unlink() but left create()
open - and the line ACL grants a plain accounting user create ONLY (no
write, no unlink), so injecting a line was the one mutation no guard
covered. A regular user could env['eh.fx.revaluation.line'].create(
{'run_id': posted_run.id, 'adjustment': -50000, ...}) and silently restate
the recognised FX gain/loss away from the posted move, with no state change.

These tests prove:
  * create() into a posted run is refused (exercised as a plain non-manager
    user, whose sole ORM mutation on the line is create), and
  * write() re-pointing a draft-run line INTO a posted run is refused (the
    write() guard alone inspected only the source parent).
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase

from .compat import add_user_groups, user_group_values


@tagged('eh_account_fx_revaluation', 'post_install', '-at_install')
class TestFxLineFreeze(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        add_user_groups(
            cls.env.user,
            cls.env.ref('account.group_account_manager'),
            cls.env.ref('eh_account_base.group_eh_manager'),
        )

        cls.eur = cls.env.ref('base.EUR')
        cls.eur.active = True

        cls.gain_account = cls._ensure_account(
            cls.env, '4922', 'Unrealised FX Gain (freeze)', 'income_other',
        )
        cls.loss_account = cls._ensure_account(
            cls.env, '5932', 'Unrealised FX Loss (freeze)', 'expense',
        )
        cls.account_receivable.eh_fx_revalue = True

        Rate = cls.env['res.currency.rate']
        Rate.search([('currency_id', '=', cls.eur.id)]).unlink()
        Rate.create({
            'currency_id': cls.eur.id,
            'name': '2026-01-01',
            'rate': 1.0,
            'company_id': cls.company.id,
        })

        # A plain accounting user: create=1 on the line, write=0/unlink=0
        # (per security/ir.model.access.csv). Their only line mutation is
        # create - the operation the freeze guard now has to cover.
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'FX Line Freeze Plain User',
                'login': 'fx_line_freeze_plain',
                'email': 'fx_line_freeze_plain@example.com',
                'company_id': cls.company.id,
                'company_ids': [(6, 0, cls.company.ids)],
                **user_group_values(
                    cls.env, cls.env.ref('eh_account_base.group_eh_user'),
                ),
            })
        except Exception:  # pragma: no cover - provisioning guard
            cls.plain_user = False

    # ---- fixtures ----

    def _posted_run(self):
        """A posted run carrying at least one non-zero revaluation line."""
        move = self.env['account.move'].create({
            'move_type': 'entry',
            'date': '2026-02-15',
            'journal_id': self.journal_misc.id,
            'line_ids': [
                (0, 0, {
                    'name': 'EUR receivable',
                    'account_id': self.account_receivable.id,
                    'partner_id': self.partner_a.id,
                    'currency_id': self.eur.id,
                    'amount_currency': 1000.0,
                    'debit': 1000.0,
                    'credit': 0.0,
                }),
                (0, 0, {
                    'name': 'EUR revenue (company ccy)',
                    'account_id': self.account_revenue.id,
                    'debit': 0.0,
                    'credit': 1000.0,
                }),
            ],
        })
        move.action_post()
        self.env['res.currency.rate'].create({
            'currency_id': self.eur.id,
            'name': '2026-03-31',
            'rate': 1.25,
            'company_id': self.company.id,
        })
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-03-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        run.action_compute()
        run.action_post()
        self.assertEqual(run.state, 'posted')
        self.assertTrue(run.line_ids)
        return run

    # ---- create guard ----

    def test_plain_user_cannot_inject_line_into_posted_run(self):
        """A regular user's create({'run_id': posted_run.id, ...}) is refused:
        it would recompute the posted run's stored net_adjustment away from
        the sealed journal entry."""
        if not self.plain_user:
            self.skipTest("No plain user could be provisioned.")
        run = self._posted_run()
        net_before = run.net_adjustment
        count_before = run.line_count
        with self.assertRaises(AccessError):
            self.env['eh.fx.revaluation.line'].with_user(self.plain_user).create({
                'run_id': run.id,
                'account_id': self.account_receivable.id,
                'foreign_currency_id': self.eur.id,
                'adjustment': -50000.0,
            })
        run.invalidate_recordset()
        self.assertEqual(
            run.net_adjustment, net_before,
            "Injected line must not restate the posted run's net adjustment.")
        self.assertEqual(
            run.line_count, count_before,
            "No line may be added to a posted run.")

    def test_engine_create_reaches_posted_parent_freeze(self):
        """Trusted engine context still cannot mutate sealed run evidence."""
        run = self._posted_run()
        with self.assertRaises(UserError):
            self.env['eh.fx.revaluation.line'].sudo().create({
                'run_id': run.id,
                'account_id': self.account_receivable.id,
                'foreign_currency_id': self.eur.id,
                'adjustment': -50000.0,
            })

    def test_create_line_on_draft_run_still_allowed(self):
        """The freeze must not block the ordinary path: a line can be created
        on a draft/computed run (this is exactly what the compute step does)."""
        run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-04-30',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        self.assertEqual(run.state, 'draft')
        line = self.env['eh.fx.revaluation.line'].create({
            'run_id': run.id,
            'account_id': self.account_receivable.id,
            'foreign_currency_id': self.eur.id,
            'adjustment': 10.0,
        })
        self.assertTrue(line.exists())

    # ---- write re-point guard ----

    def test_cannot_repoint_line_into_posted_run(self):
        """write({'run_id': posted_run.id}) on a draft-run line is refused: the
        source-parent-only guard would otherwise wave it through and recompute
        the posted target run's stored totals."""
        posted = self._posted_run()
        draft_run = self.env['eh.fx.revaluation.run'].create({
            'revaluation_date': '2026-05-31',
            'journal_id': self.journal_misc.id,
            'gain_account_id': self.gain_account.id,
            'loss_account_id': self.loss_account.id,
            'auto_reverse': False,
        })
        line = self.env['eh.fx.revaluation.line'].create({
            'run_id': draft_run.id,
            'account_id': self.account_receivable.id,
            'foreign_currency_id': self.eur.id,
            'adjustment': -25000.0,
        })
        with self.assertRaises(UserError):
            line.write({'run_id': posted.id})
        self.assertEqual(
            line.run_id, draft_run,
            "The line must stay on its draft run after the refused move.")
