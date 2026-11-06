# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move extension: linked-cheque counter for invoice forms.

Surfaces a stat-button on the invoice form: "Linked cheques: N", which
opens the filtered cheque list when there are any post-dated cheques
recorded against this invoice. Without this, users have to navigate
through the menu to confirm whether a customer has lodged a PDC
against their open invoice.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat


_PDC_MOVE_PROVENANCE_FIELDS = {
    'eh_pdc_cheque_id',
    'eh_pdc_transition',
}
_PDC_MOVE_PROVENANCE_INTERNAL = 'eh_pdc_move_provenance_internal'
_PDC_MOVE_PROVENANCE_CAPABILITY = object()

_PDC_MOVE_PROTECTED_FIELDS = _PDC_MOVE_PROVENANCE_FIELDS | {
    'auto_post',
    'company_id',
    'currency_id',
    'date',
    'journal_id',
    'line_ids',
    'move_type',
    'name',
    'partner_id',
    'ref',
    'reversed_entry_id',
    'state',
}


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_pdc_cheque_id = fields.Many2one(
        'eh.cheque',
        string="PDC Cheque",
        readonly=True,
        copy=False,
        index=True,
        ondelete='restrict',
        help="Server-only provenance link to the cheque that generated this "
             "entry.",
    )
    eh_pdc_transition = fields.Selection(
        [
            ('present', "Presentation"),
            ('clear', "Clearance"),
            ('bounce', "Bounce reversal"),
            ('bounce_charge', "Bounce charge"),
        ],
        string="PDC Transition",
        readonly=True,
        copy=False,
        index=True,
        help="Server-only lifecycle transition that generated this entry.",
    )

    eh_cheque_count = fields.Integer(
        compute='_compute_eh_cheque_count',
        groups='eh_account_base.group_eh_user',
        help="Number of post-dated cheques linked to this invoice.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        # Provenance is established only by the cheque workflow through an
        # in-process object capability. Client-supplied context is forgeable,
        # so an ordinary context value can never authorise these fields.
        provenance_authorised = (
            self.env.context.get(_PDC_MOVE_PROVENANCE_INTERNAL)
            is _PDC_MOVE_PROVENANCE_CAPABILITY
        )
        if not self.env.su and not provenance_authorised:
            vals_list = [
                {
                    key: value
                    for key, value in (vals or {}).items()
                    if key not in _PDC_MOVE_PROVENANCE_FIELDS
                }
                for vals in vals_list
            ]
        return super().create(vals_list)

    @api.model
    @api.private
    def _eh_create_pdc_sealed(self, cheque, transition, vals):
        """Create one sealed PDC move as actor, with exact provenance.

        Base deliberately restores caller ACLs before creating a sealed move.
        PDC provenance therefore needs its own in-process capability so this
        module's create guard can distinguish sanctioned workflow metadata
        from client-supplied fields.  Actor cheque access is proved before
        that capability is added; only seal/provenance stamping elevates.
        """
        cheque.ensure_one()
        actor_cheque = cheque.sudo(False)
        actor_cheque._eh_check_access('write')
        if not actor_cheque.env.su and not actor_cheque.env.user.has_group(
            'eh_account_base.group_eh_manager'
        ):
            raise AccessError(_(
                "Only an EH Accounting Manager can create PDC accounting "
                "entries."
            ))
        if transition not in {
            'present', 'clear', 'bounce_charge',
        }:
            raise UserError(_("Unsupported PDC provenance transition."))

        vals = dict(vals or {})
        supplied_cheque_id = vals.pop('eh_pdc_cheque_id', False)
        supplied_transition = vals.pop('eh_pdc_transition', False)
        if supplied_cheque_id not in (False, cheque.id) or (
            supplied_transition not in (False, transition)
        ):
            raise UserError(_(
                "PDC move provenance does not match its owning cheque "
                "transition."
            ))
        company_id = vals.get('company_id') or self.env.company.id
        if company_id != cheque.company_id.id:
            raise UserError(_(
                "PDC journal-entry provenance must stay in the cheque "
                "company."
            ))
        vals.update({
            'eh_pdc_cheque_id': cheque.id,
            'eh_pdc_transition': transition,
        })
        guarded = self.sudo(False).with_context(**{
            _PDC_MOVE_PROVENANCE_INTERNAL:
                _PDC_MOVE_PROVENANCE_CAPABILITY,
        })
        move = guarded._eh_create_sealed(vals)
        clean_context = dict(move.env.context)
        clean_context.pop(_PDC_MOVE_PROVENANCE_INTERNAL, None)
        return move.with_context(clean_context)

    def write(self, vals):
        provenance_fields = set(vals) & _PDC_MOVE_PROVENANCE_FIELDS
        provenance_authorised = (
            self.env.context.get(_PDC_MOVE_PROVENANCE_INTERNAL)
            is _PDC_MOVE_PROVENANCE_CAPABILITY
        )
        if provenance_fields and not provenance_authorised:
            raise AccessError(_(
                "PDC journal-entry provenance is server-owned. Fields "
                "%(fields)s may only be changed by the cheque workflow.",
                fields=', '.join(sorted(provenance_fields)),
            ))
        if not self.env.su:
            protected_moves = self.filtered('eh_pdc_cheque_id')
            protected_fields = (
                set(vals) & _PDC_MOVE_PROTECTED_FIELDS
                if protected_moves else set()
            )
            provenance_only = (
                provenance_authorised
                and protected_fields.issubset(_PDC_MOVE_PROVENANCE_FIELDS)
            )
            if (
                protected_fields
                and not provenance_only
                and not self._eh_has_sealed_mutation_capability()
            ):
                raise AccessError(_(
                    "PDC-generated journal entries are immutable. Fields "
                    "%(fields)s may only be changed by the cheque workflow.",
                    fields=', '.join(sorted(protected_fields)),
                ))
        return super().write(vals)

    @api.private
    def _eh_set_pdc_provenance(self, cheque, transition):
        """Attach exact cheque provenance through Base's sealed corridor."""
        self.ensure_one()
        cheque.ensure_one()
        cheque.sudo(False)._eh_check_access('write')
        if transition not in {
            'present', 'clear', 'bounce', 'bounce_charge',
        }:
            raise UserError(_("Unsupported PDC provenance transition."))
        if self.company_id != cheque.company_id:
            raise UserError(_(
                "PDC journal-entry provenance must stay in the cheque "
                "company."
            ))
        if (
            self.eh_pdc_cheque_id
            and self.eh_pdc_cheque_id != cheque
        ) or (
            self.eh_pdc_transition
            and self.eh_pdc_transition != transition
        ):
            raise UserError(_(
                "Existing PDC journal-entry provenance cannot be re-keyed."
            ))
        guarded = self.sudo().with_context(**{
            _PDC_MOVE_PROVENANCE_INTERNAL:
                _PDC_MOVE_PROVENANCE_CAPABILITY,
        })
        return guarded._eh_write_sealed_metadata(
            {
                'eh_pdc_cheque_id': cheque.id,
                'eh_pdc_transition': transition,
            },
            _PDC_MOVE_PROVENANCE_FIELDS,
        )

    def unlink(self):
        if not self.env.su and self.filtered('eh_pdc_cheque_id'):
            raise AccessError(_(
                "PDC-generated journal entries cannot be deleted directly. "
                "Use the cheque workflow so the accounting trail remains "
                "intact.",
            ))
        return super().unlink()

    def _reverse_moves(self, default_values_list=None, cancel=False):
        if (
            not self.env.su
            and self.filtered('eh_pdc_cheque_id')
            and not self._eh_has_verified_reversal_capability()
        ):
            raise AccessError(_(
                "PDC-generated journal entries cannot be reversed directly. "
                "Bounce the originating cheque instead.",
            ))
        return super()._reverse_moves(default_values_list, cancel=cancel)

    @api.depends_context('uid')
    def _compute_eh_cheque_count(self):
        Cheque = self.env['eh.cheque']
        groups = read_group_compat(Cheque, 
            [
                ('invoice_id', 'in', self.ids),
                ('company_id', 'in', self.env.companies.ids),
            ],
            groupby=['invoice_id'],
            aggregates=['__count'],
        )
        counts = {m.id: c for m, c in groups}
        for move in self:
            move.eh_cheque_count = counts.get(move.id, 0)

    def action_view_eh_cheques(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Linked cheques',
            'res_model': 'eh.cheque',
            'view_mode': 'kanban,list,form',
            'views': [(False, 'kanban'), (False, 'list'), (False, 'form')],
            'domain': [('invoice_id', '=', self.id)],
        }


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def _eh_pdc_line_mutation_authorised(self):
        """True only inside PDC creation or Base's sealed engine."""
        return (
            self.env.context.get(_PDC_MOVE_PROVENANCE_INTERNAL)
            is _PDC_MOVE_PROVENANCE_CAPABILITY
            or self.env['account.move']
                ._eh_has_sealed_mutation_capability()
        )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not self._eh_pdc_line_mutation_authorised():
            move_ids = {
                vals.get('move_id')
                for vals in vals_list
                if vals and vals.get('move_id')
            }
            protected = self.env['account.move'].sudo().search_count([
                ('id', 'in', list(move_ids)),
                ('eh_pdc_cheque_id', '!=', False),
            ]) if move_ids else 0
            if protected:
                raise AccessError(_(
                    "Lines cannot be added directly to a PDC-generated "
                    "journal entry.",
                ))
        return super().create(vals_list)

    def write(self, vals):
        if (
            not self.env.su
            and not self._eh_pdc_line_mutation_authorised()
        ):
            if self.filtered('move_id.eh_pdc_cheque_id'):
                raise AccessError(_(
                    "Lines on a PDC-generated journal entry are immutable.",
                ))
            destination_id = vals.get('move_id')
            if destination_id:
                # Authorize the destination before inspecting its protected
                # provenance. Otherwise an ordinary line could be reparented
                # into a PDC move because only its current parent was checked.
                destination = self.env['account.move'].browse(destination_id)
                destination._eh_check_access('write')
                if destination.eh_pdc_cheque_id:
                    raise AccessError(_(
                        "Lines cannot be moved into a PDC-generated journal "
                        "entry.",
                    ))
        return super().write(vals)

    def unlink(self):
        if (
            not self.env.su
            and not self._eh_pdc_line_mutation_authorised()
            and self.filtered('move_id.eh_pdc_cheque_id')
        ):
            raise AccessError(_(
                "Lines cannot be removed directly from a PDC-generated "
                "journal entry.",
            ))
        return super().unlink()
