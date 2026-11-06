# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
res.company extension: Peppol access-point selection.

Each company picks one access-point adapter; the inbound poll cron
reads the field to know which adapter to instantiate. Outbound
submission (when the deployment moves beyond file generation to live
transmission) reads the same field.
"""

from odoo import _, api, fields, models

from odoo.addons.eh_account_einvoice_peppol.tools import (
    access_point_registry,
)


class ResCompany(models.Model):
    _inherit = 'res.company'

    eh_peppol_access_point_key = fields.Selection(
        selection='_eh_peppol_available_adapters',
        string="Peppol access point",
        default='manual',
        help=(
            "Identifier of the access-point adapter the company uses "
            "for inbound poll and outbound submit. Defaults to "
            "'manual', which means the deployment routes XML by file "
            "drop / email / portal upload. Set this to one of the "
            "registered adapter keys to enable live AS4 transport "
            "(install the corresponding ERP Heritage adapter module "
            "first)."
        ),
    )
    eh_peppol_ap_config = fields.Text(
        string="Peppol access-point credentials (JSON)",
        groups="base.group_system",
        help=(
            "JSON credentials handed to the access-point adapter "
            "factory (api_key, base_url, sender id, signing cert path, "
            "etc.). Shape varies per adapter. Only read by live "
            "adapters; the manual file-drop default needs none. "
            "Restricted to system administrators because it holds the "
            "access-point API key and signing-cert path (a network "
            "impersonation credential); the inbound poll and outbound "
            "submit read it via sudo so transmission still works for "
            "ordinary users without exposing the secret to them."
        ),
    )
    eh_peppol_inbox_email = fields.Char(
        string="Peppol inbox email",
        help=(
            "Optional alias the access point forwards inbound XML to. "
            "Sites that use email-as-transport (rather than direct "
            "API delivery) configure this and route the alias to a "
            "mail-fetcher that creates eh.peppol.inbound records."
        ),
    )
    eh_peppol_payment_means_code = fields.Selection([
        ('1', "Instrument not defined"),
        ('30', "Credit transfer"),
        ('58', "SEPA credit transfer"),
    ], string="Peppol payment means", default='1', required=True, help=(
        "PaymentMeansCode rendered on outbound UBL. Credit-transfer codes "
        "30 and 58 require a payee bank account on the invoice; code 1 "
        "records that no more specific instrument is evidenced."
    ))
    eh_peppol_poll_cursor = fields.Datetime(
        string="Last completed Peppol poll",
        readonly=True,
        copy=False,
        groups="base.group_system",
        help=(
            "Server-owned lower bound for inbound access-point polling. "
            "It advances only after every returned message is persisted; "
            "failed or partial batches retry from the prior cursor."
        ),
    )

    @api.model
    def _eh_peppol_available_adapters(self):
        """Return [(key, label)] for the dashboard config dropdown."""
        return [
            (key, key.title())
            for key in access_point_registry.list_adapters()
        ]
