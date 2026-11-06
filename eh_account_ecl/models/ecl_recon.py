# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.ecl.recon: the per-stage loss-allowance reconciliation of a posted run.

IFRS 7.35H requires a reconciliation of the opening to the closing loss
allowance per measurement category (12-month, lifetime not credit-impaired,
lifetime credit-impaired, POCI). Each line rolls one stage:

    closing = opening + transfers in - transfers out
              + remeasurement - write-offs

Opening is the prior posted run's closing for the stage; transfers come from
the stage-engine log at the allowance the exposure carried in the prior run;
write-offs are the run's posted allowance write-offs; remeasurement is the
residual measurement change of the period. Lines are engine-generated at
posting time and rebuilt when a linked write-off posts.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# The reconciliation is derived evidence: only the rebuild routine (which
# stamps this context flag) may write it, so the disclosure feed can never
# disagree with the run it summarises.
_REBUILD_FLAG = 'eh_ecl_recon_rebuild'

STAGES = [
    ('1', "Stage 1 (12-month ECL)"),
    ('2', "Stage 2 (lifetime ECL)"),
    ('3', "Stage 3 (credit-impaired)"),
    ('poci', "POCI"),
]


class EhEclRecon(models.Model):
    _name = 'eh.ecl.recon'
    _description = "ECL loss-allowance reconciliation line"
    _order = 'run_id, stage'
    _rec_name = 'stage'

    run_id = fields.Many2one(
        'eh.ecl.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    stage = fields.Selection(STAGES, required=True)
    opening = fields.Monetary(
        currency_field='currency_id',
        help="Prior posted run's closing allowance for this stage.")
    transfers_in = fields.Monetary(
        currency_field='currency_id',
        help="Allowance transferred into this stage by the stage engine, at "
             "the amount the exposures carried in the prior run.")
    transfers_out = fields.Monetary(
        currency_field='currency_id',
        help="Allowance transferred out of this stage by the stage engine.")
    remeasurement = fields.Monetary(
        currency_field='currency_id',
        help="Net remeasurement of the period: new exposures, changes in "
             "PD/LGD/EAD or loss rates, and post-transfer remeasurement of "
             "moved exposures.")
    writeoffs = fields.Monetary(
        currency_field='currency_id',
        help="Allowance consumed by write-offs posted against this run.")
    closing = fields.Monetary(
        currency_field='currency_id',
        help="Closing allowance for the stage: opening + transfers in - "
             "transfers out + remeasurement - write-offs.")

    def _guard_rebuild_only(self):
        if not (self.env.su and self.env.context.get(_REBUILD_FLAG)):
            raise UserError(_(
                "Reconciliation lines are derived from the run and its "
                "write-offs; they cannot be created or edited by hand."))

    @api.model_create_multi
    def create(self, vals_list):
        self._guard_rebuild_only()
        return super().create(vals_list)

    def write(self, vals):
        self._guard_rebuild_only()
        return super().write(vals)

    def unlink(self):
        self._guard_rebuild_only()
        return super().unlink()
