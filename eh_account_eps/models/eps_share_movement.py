# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.eps.share.movement: the number of ordinary shares outstanding from a date.

The weighted average number of shares is the sum of each period's shares
outstanding weighted by the fraction of the reporting period they were in
issue (IAS 33.20). Each movement records the total shares outstanding from
its effective date until the next movement.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class EhEpsShareMovement(models.Model):
    _name = 'eh.eps.share.movement'
    _inherit = 'eh.eps.child.mixin'
    _description = "EPS share movement"
    _order = 'run_id, effective_date, id'

    run_id = fields.Many2one(
        'eh.eps.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)

    effective_date = fields.Date(
        required=True,
        help="Date from which this number of shares was outstanding.")
    shares_outstanding = fields.Float(
        digits=(16, 2), required=True,
        help="Total ordinary shares outstanding from the effective date "
             "until the next movement.")
    note = fields.Char()

    _check_shares = models.Constraint(
        'CHECK (shares_outstanding >= 0)',
        'Shares outstanding cannot be negative.')

    _unique_run_date = models.Constraint(
        'UNIQUE (run_id, effective_date)',
        'Only one ordinary-share balance may be effective on a date.')

    @api.model_create_multi
    def create(self, vals_list):
        # Appending a movement to an already-computed run would silently swing
        # the weighted-average shares and EPS. Block create on a frozen run;
        # a write/unlink-only guard leaves this append hole open.
        if (any('company_id' in vals for vals in vals_list)
                and not (self.env.su
                         and self.env.context.get('eh_eps_internal'))):
            raise AccessError(_(
                "The movement company is owned by its EPS run."))
        Run = self.env['eh.eps.run']
        run_ids = [vals.get('run_id') for vals in vals_list
                   if vals.get('run_id')]
        self._eh_prepare_runs(run_ids)
        seen = set()
        for vals in vals_list:
            identity = (vals.get('run_id'), vals.get('effective_date'))
            if not all(identity):
                continue
            if identity in seen or self.search_count([
                    ('run_id', '=', identity[0]),
                    ('effective_date', '=', identity[1]),
            ]):
                raise ValidationError(_(
                    "Only one ordinary-share balance may be effective on "
                    "%(date)s for this EPS run.",
                    date=identity[1],
                ))
            seen.add(identity)
        for vals in vals_list:
            run = Run.browse(vals['run_id']) if vals.get('run_id') else None
            if run and run.state == 'computed':
                raise UserError(_(
                    "Share movements cannot be added to run %s once it is "
                    "computed. Set the run back to draft first (IAS 33).",
                    run.display_name))
        return super().create(vals_list)

    def write(self, vals):
        if ('company_id' in vals and not (
                self.env.su and self.env.context.get('eh_eps_internal'))):
            raise AccessError(_(
                "The movement company is owned by its EPS run."))
        run_ids = self.mapped('run_id').ids
        if vals.get('run_id'):
            run_ids.append(vals['run_id'])
        target_runs = self._eh_prepare_runs(run_ids)
        self._eh_check_run_frozen(_("Share movements"))
        if any(run.state != 'draft' for run in target_runs):
            raise UserError(_(
                "Share movements cannot be reparented to a computed EPS run."))
        if 'run_id' in vals or 'effective_date' in vals:
            for movement in self:
                run_id = vals.get('run_id', movement.run_id.id)
                effective_date = vals.get(
                    'effective_date', movement.effective_date,
                )
                if self.search_count([
                    ('run_id', '=', run_id),
                    ('effective_date', '=', effective_date),
                    ('id', '!=', movement.id),
                ]):
                    raise ValidationError(_(
                        "Only one ordinary-share balance may be effective "
                        "on %(date)s for this EPS run.",
                        date=effective_date,
                    ))
        return super().write(vals)

    def unlink(self):
        self._eh_prepare_runs(self.mapped('run_id').ids)
        self._eh_check_run_frozen(_("Share movements"))
        return super().unlink()
