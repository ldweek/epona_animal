# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


BENEFIT_MOVE_CONTEXT = 'eh_benefit_move_capability'
BENEFIT_MOVE_CAPABILITY = object()
BENEFIT_PROVENANCE_VERSION = 1


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_benefit_valuation_id = fields.Many2one(
        'eh.benefit.valuation', string="Benefit Valuation", readonly=True,
        index=True, ondelete='restrict', copy=False, check_company=True)
    eh_benefit_dc_accrual_id = fields.Many2one(
        'eh.benefit.dc.accrual', string="DC Accrual", readonly=True,
        index=True, ondelete='restrict', copy=False, check_company=True)
    eh_benefit_provenance_version = fields.Integer(
        string="Benefit Provenance Version", readonly=True, copy=False,
        help="Engine-owned durable version stamp. Zero denotes untrusted "
             "legacy journal evidence.")

    @api.model
    def _eh_validate_benefit_provenance(self, vals):
        valuation_id = vals.get('eh_benefit_valuation_id')
        accrual_id = vals.get('eh_benefit_dc_accrual_id')
        if valuation_id and accrual_id:
            raise UserError(_(
                "A benefit journal entry cannot belong to both a defined-"
                "benefit valuation and a defined-contribution accrual."
            ))
        source = (
            self.env['eh.benefit.valuation'].browse(valuation_id).exists()
            if valuation_id
            else self.env['eh.benefit.dc.accrual'].browse(accrual_id).exists()
        )
        if not source:
            if vals.get('eh_benefit_provenance_version'):
                raise UserError(_(
                    "An employee-benefit provenance stamp requires an "
                    "employee-benefit source."
                ))
            raise UserError(_(
                "The employee-benefit journal-entry source no longer exists."
            ))
        if vals.get('eh_benefit_provenance_version') \
                != BENEFIT_PROVENANCE_VERSION:
            raise AccessError(_(
                "An employee-benefit journal entry must carry the current "
                "engine-owned provenance version."
            ))
        if not self.env.su:
            source._eh_check_access('write')
        company = self.env['res.company'].browse(
            vals.get('company_id')
        ).exists()
        journal = self.env['account.journal'].browse(
            vals.get('journal_id')
        ).exists()
        company = company or journal.company_id
        if company and company != source.company_id:
            raise AccessError(_(
                "An employee-benefit journal entry and its source must "
                "belong to the same company."
            ))
        original = self.browse(vals.get('reversed_entry_id')).exists()
        if original:
            same_source = (
                valuation_id
                and original.eh_benefit_valuation_id == source
                and not original.eh_benefit_dc_accrual_id
            ) or (
                accrual_id
                and original.eh_benefit_dc_accrual_id == source
                and not original.eh_benefit_valuation_id
            )
            reviewed_legacy_source = bool(
                self.env.context.get(BENEFIT_MOVE_CONTEXT)
                is BENEFIT_MOVE_CAPABILITY
                and source.provenance_quarantined
                and original == source.move_id
                and original.eh_benefit_provenance_version == 0
            )
            if (
                not same_source
                or original.company_id != source.company_id
                or (
                    original.eh_benefit_provenance_version
                    != BENEFIT_PROVENANCE_VERSION
                    and not reviewed_legacy_source
                )
            ):
                raise AccessError(_(
                    "A benefit reversal must point to the exact original "
                    "entry of the same source and company."
                ))
        return source

    @api.model_create_multi
    def create(self, vals_list):
        protected = {
            'eh_benefit_valuation_id', 'eh_benefit_dc_accrual_id',
            'eh_benefit_provenance_version',
        }
        if any(
            any(vals.get(field_name) for field_name in protected)
            for vals in vals_list
        ) and not (
            self.env.context.get(BENEFIT_MOVE_CONTEXT)
            is BENEFIT_MOVE_CAPABILITY
        ):
            raise AccessError(_(
                "Employee-benefit journal-entry provenance is engine-owned."
            ))
        for vals in vals_list:
            if any(vals.get(field_name) for field_name in protected):
                self._eh_validate_benefit_provenance(vals)
        return super().create(vals_list)

    def write(self, vals):
        protected = {
            'eh_benefit_valuation_id', 'eh_benefit_dc_accrual_id',
            'eh_benefit_provenance_version',
        }
        if 'reversed_entry_id' in vals and any(
            move.eh_benefit_valuation_id or move.eh_benefit_dc_accrual_id
            for move in self
        ):
            raise AccessError(_(
                "Employee-benefit reversal provenance is immutable after "
                "entry creation."
            ))
        if protected.intersection(vals) and not (
            self.env.context.get(BENEFIT_MOVE_CONTEXT)
            is BENEFIT_MOVE_CAPABILITY
        ):
            raise AccessError(_(
                "Employee-benefit journal-entry provenance is immutable."
            ))
        if protected.intersection(vals):
            for move in self:
                if (
                    ('eh_benefit_valuation_id' in vals
                     and move.eh_benefit_valuation_id.id
                         != (vals.get('eh_benefit_valuation_id') or False))
                    or ('eh_benefit_dc_accrual_id' in vals
                        and move.eh_benefit_dc_accrual_id.id
                            != (vals.get('eh_benefit_dc_accrual_id') or False))
                    or ('eh_benefit_provenance_version' in vals
                        and move.eh_benefit_provenance_version
                            != vals.get('eh_benefit_provenance_version'))
                ):
                    raise AccessError(_(
                        "Employee-benefit journal-entry provenance is "
                        "immutable after creation."
                    ))
                check_vals = dict(vals)
                check_vals.setdefault(
                    'eh_benefit_valuation_id',
                    move.eh_benefit_valuation_id.id,
                )
                check_vals.setdefault(
                    'eh_benefit_dc_accrual_id',
                    move.eh_benefit_dc_accrual_id.id,
                )
                check_vals.setdefault(
                    'eh_benefit_provenance_version',
                    move.eh_benefit_provenance_version,
                )
                check_vals.setdefault('company_id', move.company_id.id)
                check_vals.setdefault('journal_id', move.journal_id.id)
                move._eh_validate_benefit_provenance(check_vals)
        return super().write(vals)
