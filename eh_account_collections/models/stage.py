# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.collections.stage: kanban columns for the collections workbench.

Each stage is a column in the kanban view. Cases move between stages as
the collector progresses through contact, promise, escalation, and
resolution. Stages with is_resolved=True are terminal: cases there are
considered closed and the auto creator does not touch them.
"""

from odoo import fields, models


class EhCollectionsStage(models.Model):
    _name = 'eh.collections.stage'
    _description = "Collections case stage"
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    description = fields.Text(translate=True)
    fold = fields.Boolean(
        default=False,
        help="Fold this stage's column on the kanban view by default.",
    )
    is_default = fields.Boolean(
        default=False,
        help=(
            "Used by the auto creator to place new cases. Only one stage "
            "should carry this flag at any given time."
        ),
    )
    is_resolved = fields.Boolean(
        default=False,
        help=(
            "Terminal state. Cases here are considered closed; the auto "
            "creator does not refresh or duplicate them."
        ),
    )
    is_disputed = fields.Boolean(
        default=False,
        help="Mark cases in this stage as disputed for downstream filtering.",
    )
    is_escalated = fields.Boolean(
        default=False,
        help=(
            "Stage to which the broken-promise cron escalates cases. "
            "When no stage carries this flag, the cron leaves the "
            "stage unchanged and only bumps priority."
        ),
    )

    color = fields.Integer(
        help="Color tag used by the kanban card decoration.",
    )
