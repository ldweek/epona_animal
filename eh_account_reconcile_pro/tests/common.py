# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared test fixtures for eh_account_reconcile_pro.

Extends the base accounting test case with bank journal and statement
line helpers. The bank statement line API in Odoo varies subtly across
point releases; the helpers here pin a single creation pattern that the
tests can rely on.
"""

from odoo import fields
from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


class EhReconcileIntegrationTestCase(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_journal = cls.env['account.journal'].search(
            [
                ('company_id', '=', cls.company.id),
                ('type', '=', 'bank'),
            ],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Test Bank',
                'code': 'TBNK',
                'type': 'bank',
                'company_id': cls.company.id,
            })
        # The reclassification path clears a counter-leg against the
        # original suspense line, which is only possible when the journal's
        # suspense account is reconcilable. A correctly configured bank
        # journal's suspense account is reconcilable in real Odoo; ensure
        # the fixture mirrors that so tests exercise the normal path. The
        # dedicated config-immutability test overrides this locally to
        # reproduce the misconfiguration.
        suspense = cls.bank_journal.suspense_account_id
        if suspense and not suspense.reconcile:
            suspense.sudo().reconcile = True

    @classmethod
    def make_statement_line(cls, amount, partner=None, date=None,
                            payment_ref=None, ref=None, journal=None):
        """Create and return a posted account.bank.statement.line.

        Bank statement lines in Odoo 17+ can be created standalone
        (without a parent statement). The helper handles both shapes.
        """
        date = date or fields.Date.today()
        journal = journal or cls.bank_journal
        vals = {
            'date': date,
            'amount': amount,
            'partner_id': partner.id if partner else False,
            'payment_ref': payment_ref or 'Test payment',
            'journal_id': journal.id,
        }
        if ref:
            vals['ref'] = ref
        return cls.env['account.bank.statement.line'].create(vals)

    @classmethod
    def make_open_invoice_line(cls, partner, amount, date=None, ref=None):
        """Create and post a balanced journal entry that produces an open
        receivable AML for the given partner. Returns the receivable AML.
        """
        date = date or fields.Date.today()
        line_vals = [
            {'account': cls.account_receivable, 'debit': amount,
             'partner': partner},
            {'account': cls.account_revenue, 'credit': amount},
        ]
        move = cls.post_balanced_move(line_vals, date=date)
        if ref:
            move.ref = ref
        return move.line_ids.filtered(
            lambda l: l.account_id == cls.account_receivable
        )
