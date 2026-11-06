# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Shared fixtures for PDC tests."""

from odoo import fields

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


def configure_pdc_journal(case_class, journal):
    """Provision four distinct control stages for deterministic PDC tests."""
    incoming_transit = case_class._ensure_account(
        case_class.env, '1096', 'Incoming Cheques in Transit',
        'asset_current',
    )
    outgoing_transit = case_class._ensure_account(
        case_class.env, '2096', 'Issued Cheques in Transit',
        'liability_current',
    )
    outstanding_receipts = case_class._ensure_account(
        case_class.env, '1097', 'Outstanding Receipts', 'asset_current',
    )
    outstanding_payments = case_class._ensure_account(
        case_class.env, '1098', 'Outstanding Payments', 'asset_current',
    )
    for account in (
        incoming_transit,
        outgoing_transit,
        outstanding_receipts,
        outstanding_payments,
    ):
        if not account.reconcile:
            account.sudo().reconcile = True

    inbound_lines = journal.inbound_payment_method_line_ids
    outbound_lines = journal.outbound_payment_method_line_ids
    inbound_line = inbound_lines.filtered(
        lambda line: line.payment_method_id.code == 'manual')[:1]
    outbound_line = outbound_lines.filtered(
        lambda line: line.payment_method_id.code == 'manual')[:1]
    inbound_line = inbound_line or inbound_lines[:1]
    outbound_line = outbound_line or outbound_lines[:1]
    if not inbound_line or not outbound_line:
        raise AssertionError(
            "Test bank journal needs inbound and outbound payment methods."
        )
    inbound_line.sudo().payment_account_id = outstanding_receipts
    outbound_line.sudo().payment_account_id = outstanding_payments
    journal.sudo().write({
        'eh_pdc_incoming_transit_account_id': incoming_transit.id,
        'eh_pdc_outgoing_transit_account_id': outgoing_transit.id,
        'eh_pdc_inbound_payment_method_line_id': inbound_line.id,
        'eh_pdc_outbound_payment_method_line_id': outbound_line.id,
    })
    return {
        'incoming_transit': incoming_transit,
        'outgoing_transit': outgoing_transit,
        'outstanding_receipts': outstanding_receipts,
        'outstanding_payments': outstanding_payments,
        'statement_suspense': journal.suspense_account_id,
    }


class EhPdcTestCase(EhAccountIntegrationTestCase):
    """Adds a bank journal and a cheque book on top of the base fixtures."""

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

        accounts = configure_pdc_journal(cls, cls.bank_journal)
        cls.incoming_transit = accounts['incoming_transit']
        cls.outgoing_transit = accounts['outgoing_transit']
        cls.outstanding_receipts = accounts['outstanding_receipts']
        cls.outstanding_payments = accounts['outstanding_payments']
        cls.statement_suspense = accounts['statement_suspense']

        cls.book = cls.env['eh.cheque.book'].create({
            'name': 'Test Book 0001-0010',
            'journal_id': cls.bank_journal.id,
            'start_number': 1,
            'end_number': 10,
            'next_number': 1,
        })
        cls.book.action_activate()

        cls.reason_funds = cls.env.ref(
            'eh_account_pdc.bounce_reason_insufficient_funds',
        )
        cls.reason_closed = cls.env.ref(
            'eh_account_pdc.bounce_reason_account_closed',
        )

        cls.today = fields.Date.context_today(cls.env['eh.cheque'])
