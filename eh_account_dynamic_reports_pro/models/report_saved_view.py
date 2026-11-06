# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Pro metadata for the viewer's canonical saved-view model.

The free report viewer reads and writes ``eh.account.report.saved_view``.
Pro extends that same model with pinning and usage telemetry; it deliberately
does not register a second saved-view model. Older ``eh.report.saved.view``
rows are copied by the versioned migration while their source table is kept
untouched for rollback.
"""

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .period_options import (
    PERIOD_PRESET_SELECTION,
    apply_period_preset,
    parse_options_json,
    validate_period_fields,
)


class EhAccountReportSavedView(models.Model):
    _inherit = 'eh.account.report.saved_view'
    _order = 'pinned desc, sequence asc, shared desc, name asc'

    sequence = fields.Integer(default=10)
    pinned = fields.Boolean(
        default=False,
        help=(
            "Surface this view before unpinned views in the report viewer."
        ),
    )
    created_on = fields.Datetime(
        readonly=True,
        default=fields.Datetime.now,
    )
    last_used_at = fields.Datetime(readonly=True)
    use_count = fields.Integer(default=0, readonly=True)
    period_preset = fields.Selection(
        PERIOD_PRESET_SELECTION,
        required=True,
        default='options',
        help=(
            "Structured date scope applied when this saved view loads. Use "
            "Saved Options preserves the date block in Advanced Options "
            "JSON."
        ),
    )
    period_date_from = fields.Date(string="Date From")
    period_date_to = fields.Date(string="Date To")

    @api.constrains('options_json')
    def _check_pro_options_json(self):
        for view in self:
            parse_options_json(
                view.options_json,
                "Saved view '%s'" % view.display_name,
                ValidationError,
            )

    @api.constrains(
        'period_preset', 'period_date_from', 'period_date_to',
    )
    def _check_period_fields(self):
        for view in self:
            validate_period_fields(
                view.period_preset,
                view.period_date_from,
                view.period_date_to,
                "Saved view '%s'" % view.display_name,
                ValidationError,
            )

    @api.model
    def save_view(self, name, report_code, options, shared=False, notes=None,
                  pinned=None, sequence=None):
        """Use viewer API, optionally applying Pro presentation metadata."""
        if not name:
            raise UserError(_("Saved view name is required."))
        if not report_code:
            raise UserError(_("Saved view report code is required."))
        view_id = super().save_view(
            name, report_code, options, shared=shared, notes=notes,
        )
        pro_vals = {}
        if pinned is not None:
            pro_vals['pinned'] = bool(pinned)
        if sequence is not None:
            pro_vals['sequence'] = int(sequence)
        if pro_vals:
            self.browse(view_id).write(pro_vals)
        return view_id

    @api.model
    def list_for(self, report_code):
        """Return viewer rows ordered by Pro pinning metadata."""
        allowed_company_ids = (
            self.env.context.get('allowed_company_ids')
            or [self.env.company.id]
        )
        domain = [
            ('report_code', '=', report_code),
            '|',
            ('user_id', '=', self.env.user.id),
            '&',
            ('shared', '=', True),
            ('company_id', 'in', list(allowed_company_ids)),
        ]
        records = self.search(
            domain,
            order='pinned desc, sequence asc, shared desc, name asc',
        )
        return [{
            'id': rec.id,
            'name': rec.name,
            'shared': rec.shared,
            'owned': rec.user_id.id == self.env.user.id,
            'notes': rec.notes or '',
            'pinned': rec.pinned,
            'sequence': rec.sequence,
            'use_count': rec.use_count,
            'last_used_at': (
                rec.last_used_at.isoformat() if rec.last_used_at else None
            ),
        } for rec in records]

    def load_options(self):
        """Load through viewer API, then atomically record successful use."""
        self.ensure_one()
        self._eh_check_access('read')
        payload = apply_period_preset(
            super().load_options(),
            self.period_preset,
            self.period_date_from,
            self.period_date_to,
        )
        payload = self._eh_resolve_relative_dates(payload)
        self.env.cr.execute(
            "UPDATE eh_account_report_saved_view "
            "SET use_count = COALESCE(use_count, 0) + 1, "
            "    last_used_at = %s "
            "WHERE id = %s",
            (fields.Datetime.now(), self.id),
        )
        self.invalidate_recordset(['use_count', 'last_used_at'])
        return payload

    def _eh_resolve_relative_dates(self, payload):
        """Resolve legacy Pro date tokens when viewer loads saved options."""
        date_options = payload.get('date')
        if not isinstance(date_options, dict):
            return payload
        viewer = self.with_context(tz=self.env.user.tz or 'UTC')
        today = fields.Date.context_today(viewer)
        previous_month_end = today.replace(day=1) - relativedelta(days=1)
        company = self.company_id or self.env.company
        fiscal_year = company.compute_fiscalyear_dates(today)
        fiscal_year_start = (
            fiscal_year.get('date_from')
            if isinstance(fiscal_year, dict)
            else None
        ) or today.replace(month=1, day=1)
        token_dates = {
            'today': today,
            'auto_month_start': today.replace(day=1),
            'auto_month_end': (
                today.replace(day=1) + relativedelta(months=1, days=-1)
            ),
            'auto_qtd': today.replace(
                month=((today.month - 1) // 3) * 3 + 1,
                day=1,
            ),
            'auto_ytd': fiscal_year_start,
            'auto_prev_month_start': previous_month_end.replace(day=1),
            'auto_prev_month_end': previous_month_end,
        }
        resolved_date = dict(date_options)
        for key in ('date_from', 'date_to'):
            value = resolved_date.get(key)
            if isinstance(value, str) and value in token_dates:
                resolved_date[key] = fields.Date.to_string(
                    token_dates[value],
                )
            elif isinstance(value, str) and value.startswith('auto_'):
                raise UserError(_(
                    "Saved view '%(name)s' uses unsupported date token "
                    "'%(token)s'.",
                    name=self.display_name,
                    token=value,
                ))
        resolved_payload = dict(payload)
        resolved_payload['date'] = resolved_date
        return resolved_payload

    def action_toggle_pinned(self):
        """Let owners promote or demote a view without changing options."""
        self._check_owner_mutation()
        for rec in self:
            rec.pinned = not rec.pinned
        return True
