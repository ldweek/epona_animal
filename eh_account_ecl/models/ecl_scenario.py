# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.ecl.scenario: one probability-weighted forward-looking scenario of a run.

IFRS 9.5.5.17(a) requires the ECL to be an unbiased, probability-weighted
amount evaluated over a range of possible outcomes, and IFRS 9.5.5.17(c)
requires reasonable and supportable forward-looking information. A scenario
scales the buckets' PD and LGD by macro adjustment factors and carries the
scenario probability; the run's general-approach ECL is the weighted sum over
its scenarios. A run without scenarios measures on a single implicit
base scenario (weight 1, factors 1).
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_FROZEN = frozenset({'computed', 'posted', 'reversed'})

class EhEclScenario(models.Model):
    _name = 'eh.ecl.scenario'
    _description = "ECL forward-looking scenario"
    _order = 'run_id, id'

    run_id = fields.Many2one(
        'eh.ecl.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    name = fields.Char(
        required=True, help="Label, e.g. 'Base', 'Upside', 'Downside'.")
    weight = fields.Float(
        digits=(7, 4), required=True, default=1.0,
        help="Probability of this scenario. The weights of a run's scenarios "
             "must sum to 1 (IFRS 9.5.5.17(a)).")
    pd_factor = fields.Float(
        digits=(7, 4), default=1.0,
        help="Macro adjustment multiplier applied to each bucket's PD under "
             "this scenario; the adjusted PD is capped at 100%.")
    lgd_factor = fields.Float(
        digits=(7, 4), default=1.0,
        help="Macro adjustment multiplier applied to each bucket's LGD under "
             "this scenario; the adjusted LGD is capped at 100%.")

    _check_weight = models.Constraint(
        'CHECK (weight >= 0 AND weight <= 1)',
        'A scenario weight must be between 0 and 1.')
    _check_factors = models.Constraint(
        'CHECK (pd_factor >= 0 AND lgd_factor >= 0)',
        'Scenario PD and LGD factors cannot be negative.')

    def _check_run_not_posted(self):
        # A scenario reweights the posted allowance the same way a bucket
        # edit would, so the posted-run freeze extends here (IFRS 9.5.5.8).
        if any(r.state in _FROZEN for r in self.run_id):
            raise UserError(_(
                "This ECL run is posted; its scenarios can no longer change. "
                "Reverse it to reopen (EH Accounting Manager only)."))

    def _eh_lock_parent_runs(self, extra_run_ids=()):
        runs = self.mapped('run_id') | self.env['eh.ecl.run'].browse(
            [run_id for run_id in extra_run_ids if run_id])
        runs._eh_validate_references()
        runs._eh_lock_for_transition()
        runs._eh_validate_references()
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_ecl_scenario WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (ids,))
            self.invalidate_recordset()
        return runs

    @api.model_create_multi
    def create(self, vals_list):
        run_ids = {v.get('run_id') for v in vals_list if v.get('run_id')}
        runs = self.env['eh.ecl.run'].browse(run_ids)
        runs._eh_validate_references()
        runs._eh_lock_for_transition()
        runs._eh_validate_references()
        if run_ids:
            frozen = runs.filtered(
                lambda r: r.state in _FROZEN)
            if frozen:
                raise UserError(_(
                    "Scenarios cannot be added to a posted ECL run; reverse "
                    "the run to reopen it (EH Accounting Manager only)."))
        return super().create(vals_list)

    def write(self, vals):
        target_ids = (vals.get('run_id'),) if vals.get('run_id') else ()
        self._eh_lock_parent_runs(target_ids)
        self._check_run_not_posted()
        if vals.get('run_id'):
            target = self.env['eh.ecl.run'].browse(vals['run_id'])
            if target.state in _FROZEN:
                raise UserError(_(
                    "Scenarios cannot be moved into a posted ECL run; "
                    "reverse the run to reopen it (EH Accounting Manager "
                    "only)."))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_parent_runs()
        self._check_run_not_posted()
        return super().unlink()
