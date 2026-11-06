# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Workflow-guard regression tests.

A state machine protected only by a readonly widget is not protected:
a plain user with write access to the model can RPC
``write({'state': 'active'})`` straight past the activation action and
its checks. The eh.workflow.guard mixin blocks any non-superuser write
to a guarded field; only the record's own actions (which run under
sudo) may move the state.

These tests assert the guard fires for a NON-superuser (the test env
itself runs as SUPERUSER, where the guard intentionally passes) and
that the sanctioned action path still works.
"""

from datetime import date

from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged


@tagged('eh_account_sepa_dd', 'post_install', '-at_install')
class TestWorkflowGuard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Mandate = cls.env['eh.sepa.mandate']
        cls.Creditor = cls.env['eh.sepa.creditor']

        cls.bank_journal = cls.env['account.journal'].search(
            [('type', '=', 'bank'),
             ('company_id', '=', cls.env.company.id)],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Test Bank',
                'code': 'TBKG',
                'type': 'bank',
                'company_id': cls.env.company.id,
            })

        existing = cls.Creditor.search(
            [('journal_id', '=', cls.bank_journal.id)], limit=1,
        )
        if existing:
            cls.creditor = existing
        else:
            cls.creditor = cls.Creditor.create({
                'name': 'Guard creditor',
                'journal_id': cls.bank_journal.id,
                'creditor_identifier': 'DE98ZZZ09999999999',
                'creditor_name': 'Guard Co',
                'iban': 'DE89370400440532013000',
            })
        cls.partner = cls.env['res.partner'].create({
            'name': 'Guard customer',
        })

        # A plain (non-superuser) user with write access to the mandate,
        # so the guard - not a missing ACL - is what blocks the state
        # write. group_eh_user grants read/write/create on the mandate.
        cls.plain_user = None
        try:
            group = cls.env.ref('eh_account_base.group_eh_user')
            cls.plain_user = cls.env['res.users'].create({
                'name': 'Guard plain user',
                'login': 'eh_sepa_dd_guard_user',
                'email': 'eh_sepa_dd_guard_user@example.com',
                'company_id': cls.env.company.id,
                'company_ids': [(6, 0, cls.env.company.ids)],
                # Odoo 19: res.users uses group_ids (not groups_id).
                'group_ids': [(4, group.id)],
            })
        except Exception:
            cls.plain_user = None

        manager_group = cls.env.ref('eh_account_base.group_eh_manager')
        cls.manager_user = cls.env['res.users'].create({
            'name': 'Mandate guard manager',
            'login': 'eh_sepa_dd_guard_manager',
            'email': 'eh_sepa_dd_guard_manager@example.com',
            'company_id': cls.env.company.id,
            'company_ids': [(6, 0, cls.env.company.ids)],
            'group_ids': [(6, 0, manager_group.ids)],
        })

        cls.other_company = cls.env['res.company'].create({
            'name': 'SEPA DD Guard Foreign Company',
            'currency_id': cls.env.company.currency_id.id,
        })
        foreign_journal = cls.env['account.journal'].sudo().with_company(
            cls.other_company,
        ).create({
            'name': 'SEPA DD Foreign Bank',
            'code': 'DDFJ',
            'type': 'bank',
            'company_id': cls.other_company.id,
        })
        foreign_creditor = cls.Creditor.sudo().with_company(
            cls.other_company,
        ).create({
            'name': 'Foreign guard creditor',
            'company_id': cls.other_company.id,
            'journal_id': foreign_journal.id,
            'creditor_identifier': 'DE48ZZZ08888888888',
            'creditor_name': 'Foreign Guard Co',
            'iban': 'DE89370400440532013000',
            'pre_notification_days': 0,
        })
        foreign_partner = cls.env['res.partner'].sudo().create({
            'name': 'Foreign guard customer',
        })
        ForeignMandate = cls.Mandate.sudo().with_company(cls.other_company)

        def _foreign_mandate(reference, state):
            return ForeignMandate.create({
                'mandate_id': reference,
                'creditor_id': foreign_creditor.id,
                'partner_id': foreign_partner.id,
                'debtor_iban': 'FR1420041010050500013M02606',
                'signature_date': date(2026, 1, 15),
                'state': state,
                'local_instrument': 'CORE',
            })

        cls.foreign_draft = _foreign_mandate(
            'MNDT-GUARD-FOREIGN-DRAFT', 'draft',
        )
        cls.foreign_active = _foreign_mandate(
            'MNDT-GUARD-FOREIGN-ACTIVE', 'active',
        )
        cls.foreign_revoked = _foreign_mandate(
            'MNDT-GUARD-FOREIGN-REVOKED', 'revoked',
        )

    def _make_draft_mandate(self):
        return self.Mandate.create({
            'mandate_id': 'MNDT-GUARD-001',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'draft',
            'local_instrument': 'CORE',
        })

    def test_plain_user_cannot_write_state(self):
        """A non-superuser direct write to the guarded state is refused."""
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        self.doc = self._make_draft_mandate()
        with self.assertRaises(AccessError):
            self.doc.with_user(self.plain_user).write({'state': 'active'})
        # State must be untouched by the blocked write.
        self.doc.invalidate_recordset(['state'])
        self.assertEqual(self.doc.state, 'draft')

    def test_sanctioned_action_still_activates(self):
        """The record's own action (runs under sudo) still moves state."""
        self.doc = self._make_draft_mandate()
        self.doc.action_activate()
        self.assertEqual(self.doc.state, 'active')

    def test_plain_user_cannot_invoke_mandate_actions(self):
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        draft = self._make_draft_mandate()
        active = self.Mandate.create({
            'mandate_id': 'MNDT-GUARD-ACTIONS-ACTIVE',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'active',
            'local_instrument': 'CORE',
        })
        revoked = self.Mandate.create({
            'mandate_id': 'MNDT-GUARD-ACTIONS-REVOKED',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'revoked',
            'local_instrument': 'CORE',
        })

        with self.assertRaises(AccessError):
            draft.with_user(self.plain_user).action_activate()
        with self.assertRaises(AccessError):
            active.with_user(self.plain_user).action_revoke()
        with self.assertRaises(AccessError):
            revoked.with_user(self.plain_user).action_set_to_draft()
        with self.assertRaises(AccessError):
            active.with_user(self.plain_user).consume_for_collection(
                date(2026, 4, 30),
            )

        draft.invalidate_recordset(['state'])
        active.invalidate_recordset(['state', 'collection_count'])
        revoked.invalidate_recordset(['state'])
        self.assertEqual(draft.state, 'draft')
        self.assertEqual(active.state, 'active')
        self.assertEqual(active.collection_count, 0)
        self.assertEqual(revoked.state, 'revoked')

    def test_manager_cannot_operate_guessed_cross_company_mandate(self):
        with self.assertRaises(AccessError):
            self.foreign_draft.with_user(self.manager_user).action_activate()
        with self.assertRaises(AccessError):
            self.foreign_active.with_user(self.manager_user).action_revoke()
        with self.assertRaises(AccessError):
            self.foreign_revoked.with_user(
                self.manager_user,
            ).action_set_to_draft()
        with self.assertRaises(AccessError):
            self.foreign_active.with_user(
                self.manager_user,
            ).consume_for_collection(date(2026, 4, 30))

        self.foreign_draft.invalidate_recordset(['state'])
        self.foreign_active.invalidate_recordset([
            'state', 'collection_count',
        ])
        self.foreign_revoked.invalidate_recordset(['state'])
        self.assertEqual(self.foreign_draft.state, 'draft')
        self.assertEqual(self.foreign_active.state, 'active')
        self.assertEqual(self.foreign_active.collection_count, 0)
        self.assertEqual(self.foreign_revoked.state, 'revoked')

    def test_plain_user_cannot_write_sequence_facts(self):
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        mandate = self.Mandate.create({
            'mandate_id': 'MNDT-GUARD-SEQUENCE-FACTS',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'active',
            'local_instrument': 'CORE',
        })

        with self.assertRaises(AccessError):
            mandate.with_user(self.plain_user).write({
                'collection_count': 99,
                'last_collection_date': date(2026, 4, 30),
            })

        mandate.invalidate_recordset([
            'collection_count', 'last_collection_date',
        ])
        self.assertEqual(mandate.collection_count, 0)
        self.assertFalse(mandate.last_collection_date)

    def test_amendments_are_engine_owned_append_only_and_trusted(self):
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        mandate = self.Mandate.create({
            'mandate_id': 'MNDT-GUARD-AMENDMENTS',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'active',
            'local_instrument': 'CORE',
        })
        Amendment = self.env['eh.sepa.mandate.amendment']

        with self.assertRaises(AccessError):
            Amendment.with_user(self.plain_user).create({
                'mandate_id': mandate.id,
                'amendment_type': 'agent',
                'old_value': 'forged',
                'new_value': 'forged',
            })

        original_iban = mandate.debtor_iban
        mandate.with_user(self.manager_user).write({
            'debtor_iban': 'DE89370400440532013000',
        })
        amendment = mandate.amendment_ids
        self.assertEqual(len(amendment), 1)
        self.assertTrue(amendment.engine_owned)
        self.assertEqual(amendment.amended_by_id, self.manager_user)

        with self.assertRaises(AccessError):
            amendment.with_user(self.plain_user).write({
                'old_value': 'forged',
            })
        with self.assertRaises(AccessError):
            amendment.with_user(self.manager_user).unlink()

        # Simulate an untrusted pre-hardening row newer than the genuine
        # engine row. It must be ignored by PAIN.008 rendering.
        untrusted = Amendment.sudo().create({
            'mandate_id': mandate.id,
            'amendment_type': 'agent',
            'old_value': 'forged',
            'new_value': 'forged',
        })
        # Reproduce a legacy pre-hardening row at storage level.  The public
        # ORM must remain append-only even for superuser code.
        self.env.cr.execute(
            "UPDATE eh_sepa_mandate_amendment "
            "SET engine_owned = FALSE WHERE id = %s",
            (untrusted.id,),
        )
        untrusted.invalidate_recordset(['engine_owned'])
        mandate.invalidate_recordset(['amendment_ids'])
        self.assertEqual(
            mandate._eh_latest_amendment_for_rendering(),
            {'original_iban': original_iban},
        )

    def test_signed_identity_is_frozen_and_active_amendment_is_manager_only(self):
        if not self.plain_user:
            self.skipTest("Could not create a non-superuser test user.")
        mandate = self.Mandate.create({
            'mandate_id': 'MNDT-GUARD-SIGNED-IDENTITY',
            'creditor_id': self.creditor.id,
            'partner_id': self.partner.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'state': 'active',
            'local_instrument': 'CORE',
        })
        other_partner = self.env['res.partner'].create({
            'name': 'Different signed debtor',
        })

        with self.assertRaises(AccessError):
            mandate.with_user(self.plain_user).write({
                'debtor_iban': 'DE89370400440532013000',
            })
        with self.assertRaises(AccessError):
            mandate.with_user(self.manager_user).write({
                'partner_id': other_partner.id,
            })

        mandate.invalidate_recordset(['partner_id', 'debtor_iban'])
        self.assertEqual(mandate.partner_id, self.partner)
        self.assertEqual(
            mandate.debtor_iban,
            'FR1420041010050500013M02606',
        )
