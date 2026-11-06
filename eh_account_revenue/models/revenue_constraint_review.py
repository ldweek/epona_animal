# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.revenue.constraint.review: a periodic reassessment of the variable
consideration estimate and its constraint (IFRS 15.56, 15.59).

IFRS 15.56 requires the entity to update the estimated transaction price,
including the constrained variable consideration, at the end of each
reporting period. This model is the workflow and audit trail for that
reassessment: a draft review snapshots the obligation's current estimate and
constraint, the reviewer records the revised amounts and the rationale, and
applying the review writes the new values onto the obligation and trues
revenue up through the contract's normal recognition run, so the change
posts as a balanced catch-up (or reversal), never a silent restatement.
Applied reviews are frozen; together they are the period-by-period audit
trail the standard asks for.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import SQL


class EhRevenueConstraintReview(models.Model):
    _name = 'eh.revenue.constraint.review'
    _description = "Variable consideration constraint review (IFRS 15.56)"
    _inherit = ['mail.thread', 'eh.workflow.guard']
    _order = 'review_date desc, id desc'

    # Only action_apply may move the review from draft to applied (writing the
    # revised estimate onto the obligation and posting the balanced catch-up
    # first). A direct RPC write to state, skipping that, is refused by
    # eh.workflow.guard.write().
    _eh_guarded_fields = (
        'state', 'previous_estimate', 'previous_constraint',
        'company_id', 'currency_id', 'contract_id', 'obligation_id',
    )
    # create() below overwrites both snapshot values from the obligation;
    # keep them while still stripping workflow state and related scope fields.
    _eh_create_guarded_fields = ('state', 'company_id', 'currency_id')

    contract_id = fields.Many2one(
        'eh.revenue.contract', required=True, ondelete='cascade', index=True)
    obligation_id = fields.Many2one(
        'eh.revenue.obligation', string="Performance Obligation",
        required=True, ondelete='cascade',
        domain="[('contract_id', '=', contract_id),"
               " ('variable_consideration', '=', True)]")
    company_id = fields.Many2one(
        related='contract_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one(
        related='contract_id.currency_id', store=True, readonly=True)

    review_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Reporting date the reassessment is performed for "
             "(IFRS 15.56: at the end of each reporting period).")
    previous_estimate = fields.Monetary(
        readonly=True, currency_field='currency_id',
        help="Variable consideration estimate on the obligation when the "
             "review was opened (snapshot).")
    previous_constraint = fields.Monetary(
        readonly=True, currency_field='currency_id',
        help="Constraint cap on the obligation when the review was opened "
             "(snapshot).")
    new_estimate = fields.Monetary(
        currency_field='currency_id',
        help="Revised variable consideration estimate (expected value or "
             "most likely amount per the obligation's method, IFRS 15.53).")
    new_constraint = fields.Monetary(
        currency_field='currency_id',
        help="Revised constraint: the portion of the estimate that is "
             "highly probable not to reverse (IFRS 15.56).")
    rationale = fields.Text(
        help="Why the revised estimate and constraint are appropriate: the "
             "evidence that the included amount is highly probable not to "
             "reverse. Required before the review can be applied; the "
             "applied review is the IFRS 15.56 audit trail.")
    state = fields.Selection(
        [('draft', "Draft"), ('applied', "Applied")],
        default='draft', required=True, tracking=True, index=True)

    _check_new_estimate = models.Constraint(
        'CHECK (new_estimate >= 0)',
        'The revised estimate cannot be negative.')
    _check_new_constraint = models.Constraint(
        'CHECK (new_constraint >= 0)',
        'The revised constraint cannot be negative.')

    @api.constrains('contract_id', 'obligation_id')
    def _check_obligation_contract(self):
        for review in self:
            if review.obligation_id.contract_id != review.contract_id:
                raise ValidationError(_(
                    "The reviewed obligation must belong to the review's "
                    "contract."))

    @api.model_create_multi
    def create(self, vals_list):
        # Snapshot the obligation's current estimate and constraint at the
        # moment the review is opened, and seed the revised values from them
        # so the reviewer edits from the current position.
        Obligation = self.env['eh.revenue.obligation']
        obligations = Obligation.browse([
            vals['obligation_id']
            for vals in vals_list if vals.get('obligation_id')
        ])
        contracts = obligations.mapped('contract_id')
        if not self.env.su:
            obligations._eh_check_access('read')
            contracts._eh_check_access('write')
        self._eh_lock_contracts(contracts)
        obligations.invalidate_recordset([
            'contract_id', 'variable_estimate', 'variable_constraint',
        ])
        for vals in vals_list:
            if not vals.get('obligation_id'):
                continue
            ob = Obligation.browse(vals['obligation_id'])
            if not self.env.su:
                ob._eh_check_access('read')
                ob.contract_id._eh_check_access('write')
            vals['contract_id'] = ob.contract_id.id
            vals['previous_estimate'] = ob.variable_estimate
            vals['previous_constraint'] = ob.variable_constraint
            vals.setdefault('new_estimate', ob.variable_estimate)
            vals.setdefault('new_constraint', ob.variable_constraint)
        return super().create(vals_list)

    def write(self, vals):
        # An applied review is the audit trail behind a posted catch-up
        # entry; it is frozen. Only the apply action itself (which sets the
        # state under its own context) may touch it.
        if not self.env.su:
            self._eh_check_access('write')
        contracts = self.mapped('contract_id')
        if not self.env.su:
            contracts._eh_check_access('write')
        self._eh_lock_contracts(contracts)
        self._eh_lock_for_transition()
        applying = (
            self.env.su
            and self.env.context.get('eh_revenue_review_apply')
            and set(vals) <= {'state'}
        )
        if not applying and (
            any(r.state == 'applied' for r in self)
            or {'previous_estimate', 'previous_constraint', 'contract_id',
                'obligation_id'} & set(vals)
        ):
            raise UserError(_(
                "An applied constraint review is the IFRS 15.56 audit "
                "trail and can no longer be changed."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        contracts = self.mapped('contract_id')
        if not self.env.su:
            contracts._eh_check_access('write')
        self._eh_lock_contracts(contracts)
        self._eh_lock_for_transition()
        if any(r.state == 'applied' for r in self):
            raise UserError(_(
                "An applied constraint review is the IFRS 15.56 audit "
                "trail and cannot be deleted."))
        return super().unlink()

    def action_apply(self):
        """Write the revised estimate and constraint onto the obligation and
        true revenue up through the contract's normal recognition run.

        The obligation write runs under the review context, the one
        sanctioned path through the posted-revenue basis freeze. The
        reallocation itself flows through the module's existing
        variable-consideration mechanics (variable_included ->
        allocated_price -> target_recognised); when revenue has already
        posted, the recognition run posts the balanced delta. The review is
        then frozen as the audit record."""
        for original in self.sorted('id'):
            if not original.env.su:
                original._eh_check_access('write')
                original.obligation_id._eh_check_access('read')
            contract = original.contract_id._eh_prepare_action(manager=True)
            original._eh_lock_for_transition()
            review = original._eh_workflow_action()
            contract = review.contract_id
            if review.state != 'draft':
                raise UserError(_("Only a draft review can be applied."))
            if contract.state != 'active':
                raise UserError(_(
                    "Constraint reviews are applied on active contracts."))
            obligation = review.obligation_id
            if not obligation.variable_consideration:
                raise UserError(_(
                    "Obligation %s carries no variable consideration to "
                    "review.", obligation.display_name))
            if not (review.rationale or '').strip():
                raise UserError(_(
                    "Record the rationale for the revised estimate and "
                    "constraint before applying the review (IFRS 15.56)."))
            obligation.with_context(eh_revenue_constraint_review=True).write({
                'variable_estimate': review.new_estimate,
                'variable_constraint': review.new_constraint,
            })
            currency = contract.currency_id
            if (
                any(contract.obligation_ids.mapped('recognised_amount'))
                and any(
                    currency.compare_amounts(ob.to_recognise, 0.0) != 0
                    for ob in contract.obligation_ids
                )
            ):
                contract.action_recognise(accounting_date=review.review_date)
            review.with_context(eh_revenue_review_apply=True).write(
                {'state': 'applied'})
        return True

    def _eh_lock_for_transition(self):
        ids = tuple(sorted(set(self.ids)))
        if not ids:
            return
        self.flush_recordset()
        self.env.cr.execute(SQL(
            "SELECT id FROM eh_revenue_constraint_review "
            "WHERE id IN %s ORDER BY id FOR UPDATE",
            ids,
        ))
        self.invalidate_recordset()

    @api.model
    def _eh_lock_contracts(self, contracts):
        ids = tuple(sorted(set(contracts.ids)))
        if not ids:
            return
        contracts.flush_recordset()
        self.env.cr.execute(SQL(
            "SELECT id FROM eh_revenue_contract WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            ids,
        ))
        contracts.invalidate_recordset()
