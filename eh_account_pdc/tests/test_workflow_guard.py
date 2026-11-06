# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Workflow-guard regression: cheque state cannot be RPC-written past actions.

A state machine protected only by a readonly statusbar is not protected: a
draft cheque's state is not frozen, so a plain user could
``write({'state': 'cleared'})`` straight past ``action_clear`` and its journal
entry. The eh.workflow.guard mixin blocks any direct non-superuser write to a
guarded field; only the record's own actions (which run under sudo) let the
write through.
"""

from datetime import timedelta

from odoo import SUPERUSER_ID
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'post_install', '-at_install')
class TestPdcWorkflowGuard(EhPdcTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A plain accounting user: read/write on the model but NOT a manager
        # and NOT superuser. This is the RPC vector the guard closes. The test
        # env itself runs as SUPERUSER, so the guard would (correctly) not
        # fire; we must exercise it as a genuine non-superuser user.
        group_xmlids = ['base.group_user']
        eh_user = cls.env.ref(
            'eh_account_base.group_eh_user', raise_if_not_found=False)
        if eh_user:
            group_xmlids.append('eh_account_base.group_eh_user')
        try:
            cls.plain_user = cls.env['res.users'].create({
                'name': 'Plain PDC User',
                'login': 'plain_pdc_user',
                'group_ids': [(6, 0, [
                    cls.env.ref(x).id for x in group_xmlids])],
            })
        except Exception:  # pragma: no cover - env cannot create a user
            cls.plain_user = False
        manager_group = cls.env.ref('eh_account_base.group_eh_manager')
        cls.manager_user = cls.env['res.users'].create({
            'name': 'PDC Guard Manager',
            'login': 'pdc_guard_manager',
            'email': 'pdc-guard-manager@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, manager_group.ids)],
        })
        # A partner created here rather than referenced from demo data
        # (base.res_partner_2), which is absent when the suite loads nodemo.
        cls.partner = cls.env['res.partner'].create({'name': 'PDC Guard Co'})

        cls.other_company = cls.env['res.company'].create({
            'name': 'PDC Guard Foreign Company',
            'currency_id': cls.company.currency_id.id,
        })
        cls.foreign_journal = cls.env['account.journal'].sudo().with_company(
            cls.other_company,
        ).create({
            'name': 'PDC Guard Foreign Bank',
            'code': 'PGFB',
            'type': 'bank',
            'company_id': cls.other_company.id,
        })
        cls.foreign_partner = cls.env['res.partner'].sudo().create({
            'name': 'PDC Guard Foreign Partner',
        })

        def _foreign_cheque(number, state):
            return cls.env['eh.cheque'].sudo().with_company(
                cls.other_company,
            ).create({
                'direction': 'incoming',
                'cheque_number': number,
                'journal_id': cls.foreign_journal.id,
                'partner_id': cls.foreign_partner.id,
                'amount': 50.0,
                'currency_id': cls.other_company.currency_id.id,
                'company_id': cls.other_company.id,
                'issue_date': cls.today,
                'value_date': cls.today,
                'state': state,
            })

        cls.foreign_draft_cheque = _foreign_cheque(
            'FOREIGN-DRAFT', 'draft',
        )
        cls.foreign_registered_cheque = _foreign_cheque(
            'FOREIGN-REGISTERED', 'registered',
        )
        cls.foreign_presented_cheque = _foreign_cheque(
            'FOREIGN-PRESENTED', 'presented',
        )
        cls.foreign_bounced_cheque = _foreign_cheque(
            'FOREIGN-BOUNCED', 'bounced',
        )

    def _presented_cheque(self, number):
        cheque = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'cheque_number': number,
            'journal_id': self.bank_journal.id,
            'partner_id': self.partner.id,
            'amount': 500.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        })
        cheque.action_register()
        cheque.action_present()
        return cheque

    def test_cheque_rejects_journal_from_unrelated_company(self):
        with self.assertRaises(UserError):
            self.env['eh.cheque'].sudo().create({
                'direction': 'incoming',
                'cheque_number': 'WRONG-COMPANY-JOURNAL',
                'journal_id': self.foreign_journal.id,
                'partner_id': self.partner.id,
                'amount': 50.0,
                'currency_id': self.company.currency_id.id,
                'company_id': self.company.id,
                'issue_date': self.today,
                'value_date': self.today,
            })

    def _raw_cheque(self, number, state='draft', value_date=None):
        return self.env['eh.cheque'].create({
            'direction': 'incoming',
            'cheque_number': number,
            'journal_id': self.bank_journal.id,
            'partner_id': self.partner.id,
            'amount': 50.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': value_date or self.today,
            'state': state,
        })

    def test_direct_cheque_state_write_is_blocked(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        rec = self.env['eh.cheque'].create({
            'direction': 'incoming',
            'cheque_number': '900001',
            'journal_id': self.bank_journal.id,
            'partner_id': self.partner.id,
            'amount': 500.0,
            'currency_id': self.company.currency_id.id,
            'value_date': self.today,
        })
        self.assertEqual(rec.state, 'draft')
        # The bypass: a plain user RPC-writing state past action_clear.
        with self.assertRaises(AccessError):
            rec.with_user(self.plain_user).write({'state': 'cleared'})
        # State is unchanged: the bypass did not take effect.
        rec.invalidate_recordset(['state'])
        self.assertEqual(rec.state, 'draft')

    def test_direct_book_state_write_is_blocked(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        book = self.env['eh.cheque.book'].create({
            'name': 'Guard Book 2000-2010',
            'journal_id': self.bank_journal.id,
            'start_number': 2000,
            'end_number': 2010,
            'next_number': 2000,
        })
        self.assertEqual(book.state, 'draft')
        with self.assertRaises(AccessError):
            book.with_user(self.plain_user).write({'state': 'in_use'})
        book.invalidate_recordset(['state'])
        self.assertEqual(book.state, 'draft')

    def test_payee_snapshot_is_server_owned_and_immutable(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        cheque = self.env['eh.cheque'].with_user(self.plain_user).create({
            'direction': 'incoming',
            'cheque_number': 'GUARD-PAYEE-SNAPSHOT',
            'journal_id': self.bank_journal.id,
            'partner_id': self.partner.id,
            'payee_name_snapshot': 'FORGED CLIENT PAYEE',
            'amount': 50.0,
            'currency_id': self.company.currency_id.id,
            'company_id': self.company.id,
            'issue_date': self.today,
            'value_date': self.today,
        })
        self.assertFalse(cheque.payee_name_snapshot)

        with self.assertRaises(AccessError):
            cheque.with_user(self.manager_user).write({
                'payee_name_snapshot': 'FORGED MANAGER PAYEE',
            })

        expected_name = self.partner.complete_name
        cheque.with_user(self.manager_user).action_register()
        cheque.invalidate_recordset(['payee_name_snapshot', 'state'])
        self.assertEqual(cheque.state, 'registered')
        self.assertEqual(cheque.payee_name_snapshot, expected_name)

        self.partner.name = 'PDC Guard Co Renamed Later'
        cheque.invalidate_recordset(['payee_name_snapshot'])
        self.assertEqual(cheque.payee_name_snapshot, expected_name)

    def test_direct_report_render_requires_manager_and_record_access(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        cheque = self._raw_cheque('GUARD-DIRECT-REPORT')
        manager_group = self.env.ref('eh_account_base.group_eh_manager')
        reports = (
            self.env.ref('eh_account_pdc.action_report_cheque_print'),
            self.env.ref('eh_account_pdc.action_report_cheque_register'),
        )
        for report in reports:
            self.assertEqual(report.group_ids, manager_group)
            with self.assertRaises(AccessError):
                self.env['ir.actions.report'].with_user(
                    self.plain_user,
                )._render_qweb_html(report.report_name, cheque.ids)
            with self.assertRaises(AccessError):
                self.env['ir.actions.report'].with_user(
                    self.plain_user,
                )._render_qweb_pdf(report.report_name, cheque.ids)

            html, output_type = self.env['ir.actions.report'].with_user(
                self.manager_user,
            )._render_qweb_html(report.report_name, cheque.ids)
            self.assertEqual(output_type, 'html')
            self.assertTrue(html)

            with self.assertRaises(AccessError):
                self.env['ir.actions.report'].with_user(
                    self.manager_user,
                )._render_qweb_html(
                    report.report_name,
                    self.foreign_draft_cheque.ids,
                )

    def test_presentation_link_and_move_provenance_are_immutable(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        cheque = self._presented_cheque('GUARD-PROV-1')
        other = self._presented_cheque('GUARD-PROV-2')
        original_move = cheque.present_move_id

        with self.assertRaises(AccessError):
            cheque.with_user(self.plain_user).write({
                'present_move_id': other.present_move_id.id,
                'presented_by_id': self.plain_user.id,
            })
        with self.assertRaises(AccessError):
            original_move.with_user(self.plain_user).write({
                'eh_pdc_cheque_id': other.id,
            })
        with self.assertRaises(AccessError):
            original_move.with_user(self.plain_user).write({
                'ref': 'forged presentation',
            })
        with self.assertRaises(AccessError):
            original_move.line_ids[:1].with_user(self.plain_user).write({
                'name': 'forged line',
            })
        with self.assertRaises(AccessError):
            original_move.with_user(self.plain_user)._reverse_moves(
                [{}], cancel=False,
            )

        cheque.invalidate_recordset(['present_move_id', 'presented_by_id'])
        self.assertEqual(cheque.present_move_id, original_move)
        self.assertEqual(original_move.eh_pdc_cheque_id, cheque)
        self.assertEqual(original_move.eh_pdc_transition, 'present')

    def test_rpc_context_cannot_forge_pdc_move_provenance(self):
        """String/bool context values cannot reproduce object capability."""
        cheque = self._raw_cheque('GUARD-PROV-CONTEXT')
        forged = self.env['account.move'].with_user(
            self.manager_user,
        ).with_context(
            eh_pdc_move_provenance_internal=True,
        ).create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': self.today,
            'eh_pdc_cheque_id': cheque.id,
            'eh_pdc_transition': 'present',
        })

        self.assertFalse(forged.eh_pdc_cheque_id)
        self.assertFalse(forged.eh_pdc_transition)

    def test_ordinary_line_cannot_be_reparented_into_pdc_move(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        cheque = self._presented_cheque('GUARD-REPARENT-1')
        ordinary_move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': self.journal_misc.id,
            'date': self.today,
            'line_ids': [
                (0, 0, {
                    'name': 'ordinary debit',
                    'account_id': self.account_cash.id,
                    'debit': 10.0,
                }),
                (0, 0, {
                    'name': 'ordinary credit',
                    'account_id': self.account_equity.id,
                    'credit': 10.0,
                }),
            ],
        })
        line = ordinary_move.line_ids[:1]

        with self.assertRaises(AccessError):
            line.with_user(self.plain_user).write({
                'move_id': cheque.present_move_id.id,
            })

        line.invalidate_recordset(['move_id'])
        self.assertEqual(line.move_id, ordinary_move)

    def test_bounce_rejects_another_cheques_posted_presentation(self):
        cheque = self._presented_cheque('GUARD-BOUNCE-1')
        other = self._presented_cheque('GUARD-BOUNCE-2')
        wrong_move = other.present_move_id

        # Simulate privileged legacy corruption. The ordinary RPC route is
        # blocked above; the bounce action must still distrust the pointer and
        # validate the unforgeable move-side provenance before reversing.
        cheque.sudo().write({'present_move_id': wrong_move.id})
        with self.assertRaises(UserError):
            cheque._mark_bounced(
                reason=self.reason_funds,
                dishonour_date=self.today,
            )

        self.assertEqual(cheque.state, 'presented')
        self.assertFalse(wrong_move._eh_reversal_moves())

    def test_normal_bounce_preserves_server_provenance(self):
        cheque = self._presented_cheque('GUARD-BOUNCE-OK')
        present_move = cheque.present_move_id
        self.assertEqual(present_move.eh_pdc_cheque_id, cheque)
        self.assertEqual(present_move.eh_pdc_transition, 'present')
        self.assertTrue(present_move.eh_sealed)

        cheque._mark_bounced(
            reason=self.reason_funds,
            dishonour_date=self.today,
        )

        self.assertEqual(cheque.state, 'bounced')
        self.assertEqual(cheque.bounce_move_id.eh_pdc_cheque_id, cheque)
        self.assertEqual(cheque.bounce_move_id.eh_pdc_transition, 'bounce')
        self.assertEqual(cheque.bounce_move_id.reversed_entry_id, present_move)
        self.assertTrue(cheque.bounce_move_id.eh_sealed)

    def test_real_manager_can_present_clear_and_bounce_with_provenance(self):
        """Sanctioned actions work without relying on test-runner sudo."""
        clear_cheque = self._raw_cheque('GUARD-MANAGER-CLEAR')
        manager_clear = clear_cheque.with_user(self.manager_user)
        self.assertFalse(manager_clear.env.su)
        manager_clear.action_register()
        manager_clear.action_present()
        clear_cheque.invalidate_recordset()
        self.assertEqual(clear_cheque.presented_by_id, self.manager_user)
        self.assertEqual(
            clear_cheque.present_move_id.eh_pdc_cheque_id, clear_cheque,
        )
        self.assertEqual(
            clear_cheque.present_move_id.eh_pdc_transition, 'present',
        )
        self.assertEqual(
            clear_cheque.present_move_id.create_uid, self.manager_user,
        )

        manager_clear.action_clear()
        clear_cheque.invalidate_recordset()
        self.assertEqual(clear_cheque.state, 'cleared')
        self.assertEqual(clear_cheque.cleared_by_id, self.manager_user)
        self.assertEqual(
            clear_cheque.clear_move_id.eh_pdc_cheque_id, clear_cheque,
        )
        self.assertEqual(clear_cheque.clear_move_id.eh_pdc_transition, 'clear')
        self.assertEqual(clear_cheque.clear_move_id.create_uid, self.manager_user)

        bounce_cheque = self._raw_cheque('GUARD-MANAGER-BOUNCE')
        manager_bounce = bounce_cheque.with_user(self.manager_user)
        manager_bounce.action_register()
        manager_bounce.action_present()
        manager_bounce._mark_bounced(
            reason=self.reason_funds,
            dishonour_date=self.today,
        )
        bounce_cheque.invalidate_recordset()
        self.assertEqual(bounce_cheque.state, 'bounced')
        self.assertEqual(bounce_cheque.bounced_by_id, self.manager_user)
        self.assertEqual(
            bounce_cheque.bounce_move_id.eh_pdc_cheque_id, bounce_cheque,
        )
        self.assertEqual(
            bounce_cheque.bounce_move_id.eh_pdc_transition, 'bounce',
        )
        self.assertEqual(
            bounce_cheque.bounce_move_id.create_uid, self.manager_user,
        )
        self.assertTrue(bounce_cheque.bounce_move_id.eh_sealed)

    def test_plain_user_cannot_invoke_any_cheque_workflow_entrypoint(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        draft = self._raw_cheque('GUARD-ACTION-DRAFT', 'draft')
        registered = self._raw_cheque(
            'GUARD-ACTION-REGISTERED', 'registered',
        )
        presented = self._raw_cheque(
            'GUARD-ACTION-PRESENTED', 'presented',
        )
        bounced = self._raw_cheque('GUARD-ACTION-BOUNCED', 'bounced')

        calls = [
            lambda: draft.with_user(self.plain_user).action_register(),
            lambda: draft.with_user(self.plain_user).action_print_cheque(),
            lambda: registered.with_user(self.plain_user).action_present(),
            lambda: registered.with_user(self.plain_user).action_cancel(),
            lambda: presented.with_user(self.plain_user).action_clear(),
            lambda: presented.with_user(
                self.plain_user,
            ).action_open_bounce_wizard(),
            lambda: presented.with_user(self.plain_user)._mark_bounced(
                self.reason_funds,
                dishonour_date=self.today,
            ),
            lambda: bounced.with_user(
                self.plain_user,
            ).action_open_replace_wizard(),
            lambda: bounced.with_user(self.plain_user)._mark_replaced_by(draft),
        ]
        for call in calls:
            with self.assertRaises(AccessError):
                call()

        self.assertEqual(draft.state, 'draft')
        self.assertEqual(registered.state, 'registered')
        self.assertEqual(presented.state, 'presented')
        self.assertEqual(bounced.state, 'bounced')

    def test_manager_cannot_operate_guessed_cross_company_cheque(self):
        calls = [
            lambda: self.foreign_draft_cheque.with_user(
                self.manager_user,
            ).action_register(),
            lambda: self.foreign_draft_cheque.with_user(
                self.manager_user,
            ).action_print_cheque(),
            lambda: self.foreign_registered_cheque.with_user(
                self.manager_user,
            ).action_present(),
            lambda: self.foreign_registered_cheque.with_user(
                self.manager_user,
            ).action_cancel(),
            lambda: self.foreign_presented_cheque.with_user(
                self.manager_user,
            ).action_clear(),
            lambda: self.foreign_presented_cheque.with_user(
                self.manager_user,
            )._mark_bounced(
                self.reason_funds,
                dishonour_date=self.today,
            ),
            lambda: self.foreign_bounced_cheque.with_user(
                self.manager_user,
            )._mark_replaced_by(self.foreign_draft_cheque),
        ]
        for call in calls:
            with self.assertRaises(AccessError):
                call()

        self.assertEqual(self.foreign_draft_cheque.state, 'draft')
        self.assertEqual(
            self.foreign_registered_cheque.state,
            'registered',
        )
        self.assertEqual(self.foreign_presented_cheque.state, 'presented')
        self.assertEqual(self.foreign_bounced_cheque.state, 'bounced')

    def test_rpc_context_cannot_force_early_presentation(self):
        cheque = self._raw_cheque(
            'GUARD-EARLY-CONTEXT',
            state='registered',
            value_date=self.today + timedelta(days=7),
        )
        with self.assertRaises(UserError):
            cheque.with_user(self.manager_user).with_context(
                eh_pdc_force_early=True,
            ).action_present()
        with self.assertRaises(UserError):
            cheque.with_user(SUPERUSER_ID).with_context(
                eh_pdc_force_early=True,
            ).action_present()
        self.assertEqual(cheque.state, 'registered')
        self.assertFalse(cheque.present_move_id)

    def test_registered_financial_identity_and_audit_row_are_retained(self):
        cheque = self._raw_cheque(
            'GUARD-FROZEN-FINANCIAL',
            state='registered',
        )
        with self.assertRaises(AccessError):
            cheque.with_user(self.manager_user).write({
                'amount': 999.0,
                'partner_id': self.partner_b.id,
            })
        with self.assertRaises(AccessError):
            cheque.with_user(self.manager_user).unlink()

        cheque.invalidate_recordset(['amount', 'partner_id', 'state'])
        self.assertEqual(cheque.amount, 50.0)
        self.assertEqual(cheque.partner_id, self.partner)
        self.assertEqual(cheque.state, 'registered')

    def test_wizards_authorize_client_supplied_cheque_before_sudo(self):
        if not self.plain_user:
            self.skipTest("No non-superuser user available in this env.")
        presented = self._raw_cheque(
            'GUARD-WIZARD-PRESENTED', 'presented',
        )
        bounced = self._raw_cheque('GUARD-WIZARD-BOUNCED', 'bounced')
        bounce_wizard = self.env['eh.cheque.bounce.wizard'].with_user(
            self.plain_user,
        ).create({
            'cheque_id': presented.id,
            'reason_id': self.reason_funds.id,
            'dishonour_date': self.today,
        })
        replace_wizard = self.env['eh.cheque.replace.wizard'].with_user(
            self.plain_user,
        ).create({
            'cheque_id': bounced.id,
            'new_cheque_number': 'GUARD-WIZARD-NEW',
            'new_value_date': self.today,
            'new_amount': 50.0,
        })

        with self.assertRaises(AccessError):
            bounce_wizard.action_confirm()
        with self.assertRaises(AccessError):
            replace_wizard.action_confirm()

        foreign_bounce = self.env['eh.cheque.bounce.wizard'].with_user(
            self.manager_user,
        ).create({
            'cheque_id': self.foreign_presented_cheque.id,
            'reason_id': self.reason_funds.id,
            'dishonour_date': self.today,
        })
        foreign_replace = self.env['eh.cheque.replace.wizard'].with_user(
            self.manager_user,
        ).create({
            'cheque_id': self.foreign_bounced_cheque.id,
            'new_cheque_number': 'FOREIGN-WIZARD-NEW',
            'new_value_date': self.today,
            'new_amount': 50.0,
        })
        with self.assertRaises(AccessError):
            foreign_bounce.action_confirm()
        with self.assertRaises(AccessError):
            foreign_replace.action_confirm()

        self.assertEqual(presented.state, 'presented')
        self.assertEqual(bounced.state, 'bounced')
        self.assertEqual(self.foreign_presented_cheque.state, 'presented')
        self.assertEqual(self.foreign_bounced_cheque.state, 'bounced')
