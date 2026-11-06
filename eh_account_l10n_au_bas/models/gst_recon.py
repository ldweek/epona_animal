# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.bas.gst.recon.result: transient holder for the 1A/1B vs GL
reconciliation produced by eh.bas.run.compute_gst_control_reconciliation.

Built as a transient model so the result lives only for the user's
current session and does not pollute the database with snapshots that
would go stale the next time the BAS is recomputed.
"""

from odoo import api, fields, models


class EhBasGstReconResult(models.TransientModel):
    _name = 'eh.bas.gst.recon.result'
    _description = "BAS GST control reconciliation result"

    run_id = fields.Many2one(
        'eh.bas.run', required=True, ondelete='cascade',
        help="BAS run this reconciliation snapshot was computed from.",
    )
    label_1a = fields.Monetary(
        currency_field='currency_id',
        help="GST on sales (1A) as computed on the BAS run.",
    )
    label_1b = fields.Monetary(
        currency_field='currency_id',
        help="GST on purchases (1B) as computed on the BAS run.",
    )
    gst_collected_movement = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Net credit movement on the company's tax control accounts "
            "during the BAS period. Should equal the 1A label."
        ),
    )
    gst_paid_movement = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Net debit movement on the company's tax control accounts "
            "during the BAS period. Should equal the 1B label."
        ),
    )
    collected_diff = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Difference between 1A and the GL movement. Non-zero "
            "indicates a missing tag on a journal item or a manual "
            "entry on the control account that bypassed the tax engine."
        ),
    )
    paid_diff = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Difference between 1B and the GL movement. Investigate "
            "non-zero values before lodging."
        ),
    )
    unclassified_movement = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Net debit movement on the selected tax control accounts that "
            "cannot be attributed to the GST-on-sales or GST-on-purchases "
            "tax grids. A non-zero amount identifies manual entries, "
            "non-GST taxes sharing the account, or incomplete tax tags."
        ),
    )
    detail_lines = fields.Text(
        help=(
            "List of tax control accounts considered in the "
            "reconciliation, plus any computation notes."
        ),
    )
    currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_currency',
        help="Currency of the run's company; AUD for AU BAS lodgers.",
    )

    @api.depends('run_id.company_id.currency_id')
    def _compute_currency(self):
        for rec in self:
            rec.currency_id = rec.run_id.company_id.currency_id
