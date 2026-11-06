# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Period-selectable IAS 37.84-92 disclosure roll-forward."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class EhProvisionDisclosureWizard(models.TransientModel):
    _name = 'eh.provision.disclosure.wizard'
    _description = "IAS 37 disclosure period"

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)
    date_from = fields.Date(
        required=True,
        default=lambda self: fields.Date.context_today(self).replace(
            month=1, day=1))
    date_to = fields.Date(
        required=True, default=fields.Date.context_today)
    line_ids = fields.One2many(
        'eh.provision.disclosure.line', 'wizard_id', readonly=True)

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wizard in self:
            if wizard.date_from and wizard.date_to \
                    and wizard.date_from > wizard.date_to:
                raise ValidationError(_(
                    "Disclosure start date cannot follow the end date."))

    def _amount_on_account(self, moves, account, before=False):
        if not account:
            return 0.0
        lines = moves.line_ids.filtered(
            lambda line: line.account_id == account)
        amount = sum(lines.mapped('credit')) - sum(lines.mapped('debit'))
        return self.company_id.currency_id.round(
            -amount if before else amount)

    def action_generate(self):
        self.ensure_one()
        if not self.env.su and self.company_id not in self.env.companies:
            raise AccessError(_(
                "You can only report a company enabled in your current "
                "company context."))
        self.line_ids.unlink()
        provisions = self.env['eh.provision'].search([
            ('company_id', '=', self.company_id.id),
            ('state', '!=', 'cancelled'),
        ])
        quarantined = provisions.filtered('eh_integrity_review_required')
        if quarantined:
            raise UserError(_(
                "IAS 37 disclosure is blocked while provision %(provision)s "
                "is quarantined: %(reason)s",
                provision=quarantined[0].display_name,
                reason=(quarantined[0].eh_integrity_review_reason
                        or _("journal provenance requires review"))))
        moves = self.env['account.move'].search([
            ('eh_provision_id', 'in', provisions.ids),
            ('state', '=', 'posted'),
            ('date', '<=', self.date_to),
        ])
        Line = self.env['eh.provision.disclosure.line']
        created = Line
        for provision in provisions:
            provision_moves = moves.filtered(
                lambda move: move.eh_provision_id == provision)
            opening_moves = provision_moves.filtered(
                lambda move: move.date < self.date_from)
            period_moves = provision_moves.filtered(
                lambda move: move.date >= self.date_from)
            provision_lines = period_moves.line_ids.filtered(
                lambda line: line.account_id == provision.provision_account_id)

            def movement_amount(event_types, debit=False):
                event_moves = period_moves.filtered(
                    lambda move: move.eh_provision_event in event_types)
                event_lines = event_moves.line_ids.filtered(
                    lambda line: line.account_id
                    == provision.provision_account_id)
                amount = (
                    sum(event_lines.mapped('debit'))
                    if debit else sum(event_lines.mapped('credit'))
                )
                return provision.currency_id.round(amount)

            opening = provision.currency_id.round(sum(
                opening_moves.line_ids.filtered(
                    lambda line: line.account_id
                    == provision.provision_account_id
                ).mapped('credit')) - sum(
                    opening_moves.line_ids.filtered(
                        lambda line: line.account_id
                        == provision.provision_account_id
                    ).mapped('debit')))
            closing = provision.currency_id.round(
                opening
                + sum(provision_lines.mapped('credit'))
                - sum(provision_lines.mapped('debit')))
            reimbursement_lines = provision_moves.line_ids.filtered(
                lambda line: line.account_id
                == provision.reimbursement_account_id)
            reimbursement = provision.currency_id.round(
                sum(reimbursement_lines.mapped('debit'))
                - sum(reimbursement_lines.mapped('credit')))
            created |= Line.create({
                'wizard_id': self.id,
                'provision_id': provision.id,
                'name': provision.name,
                'classification': provision.classification,
                'provision_type': provision.provision_type,
                'opening_amount': max(opening, 0.0),
                'additions_amount': movement_amount(
                    ('recognition', 'remeasure_increase')),
                'utilised_amount': movement_amount(('utilisation',), debit=True),
                'released_amount': movement_amount(
                    ('remeasure_decrease', 'reversal'), debit=True),
                'unwind_amount': movement_amount(('unwind',)),
                'closing_amount': max(closing, 0.0),
                'contingent_estimate': (
                    provision.best_estimate
                    if provision.classification != 'provision' else 0.0
                ),
                'reimbursement_amount': max(reimbursement, 0.0),
                'expected_settlement_date':
                    provision.expected_settlement_date,
                'nature': provision.disclosure_nature or provision.notes,
                'uncertainties': provision.disclosure_uncertainties,
                'rare_exemption': provision.disclosure_rare_exemption,
                'exemption_reason': provision.disclosure_exemption_reason,
                'currency_id': provision.currency_id.id,
                'company_id': provision.company_id.id,
            })
        return {
            'type': 'ir.actions.act_window',
            'name': _(
                "IAS 37 Disclosure %(start)s – %(end)s",
                start=self.date_from, end=self.date_to),
            'res_model': 'eh.provision.disclosure.line',
            'view_mode': 'list,pivot,graph,form',
            'domain': [('wizard_id', '=', self.id)],
            'context': {'create': False, 'edit': False, 'delete': False},
        }


class EhProvisionDisclosureLine(models.TransientModel):
    _name = 'eh.provision.disclosure.line'
    _description = "IAS 37 disclosure line"
    _order = 'classification, provision_type, name'

    wizard_id = fields.Many2one(
        'eh.provision.disclosure.wizard', required=True, ondelete='cascade',
        index=True)
    provision_id = fields.Many2one('eh.provision', required=True, index=True)
    name = fields.Char(required=True)
    company_id = fields.Many2one('res.company', required=True)
    currency_id = fields.Many2one('res.currency', required=True)
    classification = fields.Selection(
        related='provision_id.classification', readonly=True)
    provision_type = fields.Selection(
        related='provision_id.provision_type', readonly=True)
    opening_amount = fields.Monetary(currency_field='currency_id')
    additions_amount = fields.Monetary(currency_field='currency_id')
    utilised_amount = fields.Monetary(currency_field='currency_id')
    released_amount = fields.Monetary(currency_field='currency_id')
    unwind_amount = fields.Monetary(currency_field='currency_id')
    closing_amount = fields.Monetary(currency_field='currency_id')
    contingent_estimate = fields.Monetary(currency_field='currency_id')
    reimbursement_amount = fields.Monetary(currency_field='currency_id')
    expected_settlement_date = fields.Date()
    nature = fields.Text()
    uncertainties = fields.Text()
    rare_exemption = fields.Boolean()
    exemption_reason = fields.Text()
