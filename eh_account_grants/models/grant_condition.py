# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.gov.grant.condition: one condition attached to a government grant.

IAS 20.7/8: a grant is recognised only when there is reasonable assurance
that the entity will comply with the conditions attaching to it. The
register tracks each condition open / fulfilled / breached. When the grant
defers income until its conditions are met, an open or breached condition
blocks the release to income. Breaching a condition on a received
deferred-income grant accrues the clawback per IAS 20.32 (reverse
unamortised deferred income first, excess to profit or loss, liability
until the cash is repaid).
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class EhGovGrantCondition(models.Model):
    _name = 'eh.gov.grant.condition'
    _description = "Government grant condition (IAS 20)"
    _inherit = ['eh.workflow.guard']
    _order = 'grant_id, due_date, id'

    # The condition state (open / fulfilled / breached) gates grant income
    # release and triggers the breach clawback, so it may change only through
    # the record's own actions, never a direct RPC/ORM write.
    _eh_guarded_fields = ('state',)

    grant_id = fields.Many2one(
        'eh.gov.grant', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(
        related='grant_id.company_id', store=True, readonly=True)
    name = fields.Char(
        string="Condition", required=True,
        help="Condition attaching to the grant (IAS 20.7), e.g. 'employ 20 "
             "apprentices for 3 years' or 'complete the facility by "
             "2027-06-30'.")
    due_date = fields.Date(
        help="Date by which the condition must be met.")
    state = fields.Selection(
        [('open', "Open"), ('fulfilled', "Fulfilled"),
         ('breached', "Breached")],
        default='open', required=True, index=True, copy=False)
    fulfilled_date = fields.Date(readonly=True, copy=False)
    can_breach = fields.Boolean(
        compute='_compute_can_breach',
        help="Reviewer/manager capability for the current grant lifecycle. A "
             "breach that must post a clawback remains manager-only.")

    _SERVER_FIELDS = ('company_id', 'fulfilled_date')
    _SIGNED_FIELDS = ('grant_id', 'name', 'due_date')

    @api.depends(
        'state', 'grant_id.state', 'grant_id.grant_type',
        'grant_id.asset_approach', 'grant_id.clawback_accrued',
        'grant_id.clawback_settled',
    )
    @api.depends_context('uid')
    def _compute_can_breach(self):
        manager = self.env.user.has_group(
            'eh_account_base.group_eh_manager'
        )
        reviewer = self.env.user.has_group(
            'eh_account_grants.group_eh_grant_reviewer'
        )
        for condition in self:
            grant = condition.grant_id
            posting_required = (
                grant.state in ('received', 'closed')
                and not grant._is_netting
                and not grant.clawback_accrued
                and not grant.clawback_settled
            )
            condition.can_breach = (
                condition.state == 'open'
                and (manager or (reviewer and not posting_required))
            )

    @api.model
    def _eh_prepare_grants(self, grant_ids):
        grants = self.env['eh.gov.grant'].browse(sorted(set(grant_ids)))
        grants._eh_check_caller_scope(
            'read', require_active_company=False,
        )
        grants._eh_lock()
        return grants

    def _check_reviewer(self):
        if not (
                self.env.user.has_group(
                    'eh_account_grants.group_eh_grant_reviewer')
                or self.env.user.has_group(
                    'eh_account_base.group_eh_manager')):
            raise AccessError(_(
                "Only a Government Grant Reviewer or EH Accounting Manager "
                "can transition grant conditions."))

    @api.model_create_multi
    def create(self, vals_list):
        if (set().union(*(set(vals) for vals in vals_list or [{}]))
                & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_grant_engine'))):
            raise AccessError(_(
                "Condition company and review evidence are server-owned."))
        grants = self._eh_prepare_grants([
            vals.get('grant_id') for vals in vals_list if vals.get('grant_id')])
        if any(grant.state in grant._POSTED_STATES for grant in grants):
            raise UserError(_(
                "Condition rows are signed and frozen once the grant is "
                "received."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        grant_ids = self.mapped('grant_id').ids
        if vals.get('grant_id'):
            grant_ids.append(vals['grant_id'])
        grants = self._eh_prepare_grants(grant_ids)
        if (set(vals) & set(self._SERVER_FIELDS)
                and not (self.env.su
                         and self.env.context.get('eh_grant_engine'))):
            raise AccessError(_(
                "Condition company and review evidence are server-owned."))
        if (set(vals) & set(self._SIGNED_FIELDS)
                and any(grant.state in grant._POSTED_STATES
                        for grant in grants)):
            raise UserError(_(
                "Condition policy and rows are signed and frozen once the "
                "grant is received."))
        return super().write(vals)

    def unlink(self):
        grants = self._eh_prepare_grants(self.mapped('grant_id').ids)
        if any(grant.state in grant._POSTED_STATES for grant in grants):
            raise UserError(_(
                "Condition rows are signed and cannot be deleted after "
                "receipt."))
        return super().unlink()

    # ---- actions ----

    def action_fulfil(self):
        self._check_reviewer()
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_prepare_grants(self.mapped('grant_id').ids)
        # The grant row is the shared lock for receipt, release, clawback and
        # condition review. Drop a condition state prefetched before waiting.
        self.invalidate_recordset()
        self = self._eh_workflow_action().with_context(eh_grant_engine=True)
        for cond in self:
            if cond.state == 'breached':
                raise UserError(_(
                    "Condition '%(name)s' is breached; a breached condition "
                    "cannot be marked fulfilled. The clawback flow governs "
                    "it now (IAS 20.32).", name=cond.name))
            cond.write({
                'state': 'fulfilled',
                'fulfilled_date': fields.Date.context_today(cond),
            })
            cond.grant_id.message_post(body=_(
                "Grant condition fulfilled: %(name)s.", name=cond.name))
        return True

    def action_reopen(self):
        self._check_reviewer()
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_prepare_grants(self.mapped('grant_id').ids)
        self.invalidate_recordset()
        self = self._eh_workflow_action().with_context(eh_grant_engine=True)
        for cond in self:
            if cond.state != 'fulfilled':
                raise UserError(_(
                    "Only a fulfilled condition can be reopened."))
            cond.write({'state': 'open', 'fulfilled_date': False})
            cond.grant_id.message_post(body=_(
                "Grant condition reopened: %(name)s.", name=cond.name))
        return True

    def action_breach(self):
        """Mark the condition breached and accrue the clawback (IAS 20.32).

        On a received or fully amortised deferred-income grant the breach
        immediately accrues
        the repayment obligation through eh.gov.grant.action_accrue_clawback
        (manager-gated, loud on missing inputs, atomic with the breach
        mark). On a draft, netting, settled, or reversed grant the breach is
        recorded in the register and chatter without a new posting.
        """
        self._check_reviewer()
        if not self.env.su:
            self._eh_check_access('write')
        grants = self.mapped('grant_id')
        grants._eh_check_caller_scope(
            'read', require_active_company=False,
        )
        grants._eh_lock()
        self.invalidate_recordset()
        # Breaching a received grant posts a clawback immediately. Keep that
        # accounting transition manager-only; reviewers may transition
        # conditions only while no posting is required. Evaluate this after
        # the shared lock, against the current grant state.
        if (any(grant.state in ('received', 'closed') and not grant._is_netting
                and not grant.clawback_accrued
                and not grant.clawback_settled for grant in grants)
                and not self.env.user.has_group(
                    'eh_account_base.group_eh_manager')):
            raise AccessError(_(
                "An EH Accounting Manager must record a breach that triggers "
                "a clawback entry."))
        self = self._eh_workflow_action().with_context(eh_grant_engine=True)
        for cond in self:
            if cond.state == 'breached':
                raise UserError(_(
                    "Condition '%(name)s' is already breached.",
                    name=cond.name))
            cond.write({'state': 'breached', 'fulfilled_date': False})
            grant = cond.grant_id
            grant.message_post(body=_(
                "Grant condition breached: %(name)s.", name=cond.name))
            if (grant.state in ('received', 'closed') and not grant._is_netting
                    and not grant.clawback_accrued
                    and not grant.clawback_settled):
                grant.action_accrue_clawback()
        return True
