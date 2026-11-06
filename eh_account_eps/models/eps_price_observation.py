# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.eps.price.observation: a dated market price of the ordinary share.

IAS 33.45 applies the treasury-stock method at the average market price of
the ordinary shares DURING the period, not a spot price. When observations
are recorded on a potential-share class, its average market price is the
arithmetic mean of the observations dated inside the run period; without
observations the manually entered scalar average stands, so existing runs
are unchanged.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class EhEpsPriceObservation(models.Model):
    _name = 'eh.eps.price.observation'
    _inherit = 'eh.eps.child.mixin'
    _description = "EPS market price observation"
    _order = 'potential_id, date, id'

    potential_id = fields.Many2one(
        'eh.eps.potential', required=True, ondelete='cascade', index=True)
    run_id = fields.Many2one(
        related='potential_id.run_id', store=True, readonly=True)
    company_id = fields.Many2one(
        related='potential_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='potential_id.currency_id', store=True, readonly=True)

    date = fields.Date(
        required=True,
        help="Observation date. Only observations dated inside the run "
             "period enter the period average (IAS 33.45).")
    price = fields.Monetary(
        required=True, currency_field='currency_id',
        help="Observed market price of one ordinary share.")

    _check_price = models.Constraint(
        'CHECK (price > 0)',
        'A market price observation must be positive.')

    _SERVER_FIELDS = ('run_id', 'company_id', 'currency_id')

    @api.model
    def _eh_prepare_potentials(self, potential_ids):
        potentials = self.env['eh.eps.potential'].browse(
            sorted(set(potential_ids)))
        potentials._eh_check_access('read')
        runs = potentials.mapped('run_id')
        self._eh_prepare_runs(runs.ids)
        return potentials, runs

    @api.model_create_multi
    def create(self, vals_list):
        # Appending an observation to an already-computed run would silently
        # move the resolved average market price and diluted EPS. Block
        # create on a frozen run; a write/unlink-only guard leaves this
        # append hole open.
        if (set().union(*(set(vals) for vals in vals_list or [{}]))
                & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_eps_internal'))):
            raise AccessError(_(
                "Observation run, company and currency are owned by its "
                "potential-share line."))
        Potential = self.env['eh.eps.potential']
        self._eh_prepare_potentials([
            vals.get('potential_id') for vals in vals_list
            if vals.get('potential_id')])
        for vals in vals_list:
            pot = (Potential.browse(vals['potential_id'])
                   if vals.get('potential_id') else None)
            if pot and pot.run_id.state == 'computed':
                raise UserError(_(
                    "Market price observations cannot be added to run %s "
                    "once it is computed. Set the run back to draft first "
                    "(IAS 33).", pot.run_id.display_name))
        return super().create(vals_list)

    def write(self, vals):
        if (set(vals) & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_eps_internal'))):
            raise AccessError(_(
                "Observation run, company and currency are owned by its "
                "potential-share line."))
        potential_ids = self.mapped('potential_id').ids
        if vals.get('potential_id'):
            potential_ids.append(vals['potential_id'])
        _potentials, target_runs = self._eh_prepare_potentials(potential_ids)
        self._eh_check_run_frozen(_("Market price observations"))
        if any(run.state != 'draft' for run in target_runs):
            raise UserError(_(
                "Market price observations cannot be reparented to a "
                "computed EPS run."))
        return super().write(vals)

    def unlink(self):
        self._eh_prepare_potentials(self.mapped('potential_id').ids)
        self._eh_check_run_frozen(_("Market price observations"))
        return super().unlink()
