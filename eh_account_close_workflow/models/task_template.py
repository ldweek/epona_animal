# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.close.task.template: task on a checklist template.

Task templates are pure definitions. They get copied into eh.close.task
records when a run is created from the checklist. Editing a template
does not affect runs already in flight.
"""

from odoo import fields, models


class EhCloseTaskTemplate(models.Model):
    _name = 'eh.close.task.template'
    _description = "Close checklist task template"
    _order = 'sequence, id'

    checklist_id = fields.Many2one(
        'eh.close.checklist',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)

    name = fields.Char(required=True)
    description = fields.Html()

    responsible_role = fields.Selection(
        [
            ('accountant', "Accountant"),
            ('manager', "Manager"),
            ('both', "Both"),
        ],
        default='accountant',
        required=True,
        help="Indicative; does not enforce permissions.",
    )

    is_required = fields.Boolean(
        default=True,
        help=(
            "When True, the task must reach 'done' or 'not applicable' "
            "before its run can be closed. Optional tasks are advisory."
        ),
    )
