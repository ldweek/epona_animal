# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.ecl.transfer: the stage-transfer audit trail of a run.

Every time the stage engine re-stages a bucket it records where the exposure
came from, where it went, why (days-past-due backstop, qualitative SICR
flag, cure after probation, or POCI pinning), and the allowance the bucket
carried in the prior run. The log feeds the transfers-in / transfers-out
columns of the IFRS 7.35H loss-allowance reconciliation.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# The transfer log is engine-owned evidence: only the stage engine (which
# stamps this context flag) may create, rewrite or clear it, so the audit
# trail cannot be hand-edited into agreement.
_ENGINE_FLAG = 'eh_ecl_stage_engine'


class EhEclTransfer(models.Model):
    _name = 'eh.ecl.transfer'
    _description = "ECL stage transfer log"
    _order = 'run_id, id'
    _rec_name = 'bucket_name'

    run_id = fields.Many2one(
        'eh.ecl.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True)

    bucket_id = fields.Many2one('eh.ecl.bucket', ondelete='set null')
    bucket_name = fields.Char(
        help="Snapshot of the bucket label at transfer time, kept even if "
             "the bucket is later removed from a draft run.")
    segment_id = fields.Many2one('eh.ecl.segment', ondelete='set null')

    from_stage = fields.Selection(
        [('1', "Stage 1"), ('2', "Stage 2"), ('3', "Stage 3")], required=True)
    to_stage = fields.Selection(
        [('1', "Stage 1"), ('2', "Stage 2"), ('3', "Stage 3")], required=True)
    reason = fields.Selection(
        [('backstop_30', "30+ DPD backstop"),
         ('backstop_90', "90+ DPD backstop"),
         ('sicr_flag', "Qualitative SICR flag"),
         ('credit_impaired', "Credit-impaired override"),
         ('cure', "Cure after probation"),
         ('poci', "POCI (pinned to lifetime)")],
        required=True,
        help="30+ DPD: rebuttable SICR presumption (IFRS 9.5.5.11). 90+ "
             "DPD: default backstop (IFRS 9.B5.5.37). Qualitative SICR flag: "
             "manual significant-increase-in-credit-risk move to Stage 2. "
             "Credit-impaired override: manual impairment move to Stage 3 "
             "(IFRS 9.B5.5.37). Cure: reverted after the probation period. "
             "POCI: excluded from staging, always lifetime ECL.")
    amount = fields.Monetary(
        currency_field='currency_id',
        help="Loss allowance the bucket carried in the prior run: the "
             "amount transferred between stages for the IFRS 7.35H "
             "reconciliation.")

    def _guard_engine_only(self):
        if not (self.env.su and self.env.context.get(_ENGINE_FLAG)):
            raise UserError(_(
                "Stage-transfer log entries are written by the stage engine "
                "only; they cannot be created or edited by hand."))

    @api.model_create_multi
    def create(self, vals_list):
        self._guard_engine_only()
        return super().create(vals_list)

    def write(self, vals):
        self._guard_engine_only()
        return super().write(vals)

    def unlink(self):
        self._guard_engine_only()
        return super().unlink()
