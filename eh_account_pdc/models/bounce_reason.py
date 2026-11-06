# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Configurable cheque bounce reasons.

Bank specific reason codes vary by jurisdiction. We keep the list as
configurable records rather than a Python enum so a customer can add
local reason codes without forking.
"""

from odoo import fields, models


class EhChequeBounceReason(models.Model):
    _name = 'eh.cheque.bounce.reason'
    _description = "Cheque Bounce Reason"
    _order = 'sequence, code'

    sequence = fields.Integer(default=10)
    code = fields.Char(required=True)
    name = fields.Char(required=True, translate=True)
    description = fields.Text(translate=True)
    is_recoverable = fields.Boolean(
        default=True,
        help="If true, the cheque can typically be re-banked or replaced. "
             "If false (e.g. account closed), expect direct write-off.",
    )
    active = fields.Boolean(default=True)

    _uniq_code = models.Constraint(
        'unique(code)',
        'Bounce reason code must be unique.',
    )
