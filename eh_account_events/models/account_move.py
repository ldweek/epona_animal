# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Immutable provenance for IAS 8 / IAS 10 journal entries."""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


EVENT_MOVE_CONTEXT = 'eh_event_move_capability'
EVENT_MOVE_CAPABILITY = object()
EVENT_PROVENANCE_VERSION = 1


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_accounting_change_id = fields.Many2one(
        'eh.accounting.change', string="Accounting Change", readonly=True,
        index=True, ondelete='restrict', copy=False, check_company=True)
    eh_subsequent_event_id = fields.Many2one(
        'eh.subsequent.event', string="Subsequent Event", readonly=True,
        index=True, ondelete='restrict', copy=False, check_company=True)
    eh_event_provenance_version = fields.Integer(
        string="Event Provenance Version", readonly=True, copy=False,
        help="Engine-owned durable version stamp for IAS 8 / IAS 10 ledger "
             "evidence. A zero value is untrusted legacy evidence.")

    @api.model
    def _eh_validate_event_provenance(self, vals):
        change_id = vals.get('eh_accounting_change_id')
        event_id = vals.get('eh_subsequent_event_id')
        if change_id and event_id:
            raise UserError(_(
                "An event journal entry cannot belong to both an accounting "
                "change and a subsequent event."
            ))
        source = (
            self.env['eh.accounting.change'].browse(change_id).exists()
            if change_id
            else self.env['eh.subsequent.event'].browse(event_id).exists()
        )
        if not source:
            if vals.get('eh_event_provenance_version'):
                raise UserError(_(
                    "An event provenance stamp requires an IAS 8 / IAS 10 "
                    "source."
                ))
            raise UserError(_("The event journal-entry source does not exist."))
        if vals.get('eh_event_provenance_version') \
                != EVENT_PROVENANCE_VERSION:
            raise AccessError(_(
                "An event journal entry must carry the current engine-owned "
                "provenance version."
            ))
        if not self.env.su:
            source._eh_check_access('write')
        journal = self.env['account.journal'].browse(
            vals.get('journal_id')
        ).exists()
        company = self.env['res.company'].browse(
            vals.get('company_id')
        ).exists() or journal.company_id
        if company and company != source.company_id:
            raise AccessError(_(
                "An event journal entry and its source must belong to the "
                "same company."
            ))
        original = self.browse(vals.get('reversed_entry_id')).exists()
        if original:
            same_source = (
                change_id
                and original.eh_accounting_change_id == source
                and not original.eh_subsequent_event_id
            ) or (
                event_id
                and original.eh_subsequent_event_id == source
                and not original.eh_accounting_change_id
            )
            reviewed_legacy_source = bool(
                self.env.context.get(EVENT_MOVE_CONTEXT)
                is EVENT_MOVE_CAPABILITY
                and source.legacy_provenance_quarantined
                and original == source.move_id
                and (
                    (
                        change_id
                        and not original.eh_subsequent_event_id
                        and original.eh_accounting_change_id
                            in (source, self.env['eh.accounting.change'])
                    )
                    or (
                        event_id
                        and not original.eh_accounting_change_id
                        and original.eh_subsequent_event_id
                            in (source, self.env['eh.subsequent.event'])
                    )
                )
                and original.eh_event_provenance_version == 0
            )
            if (
                not (same_source or reviewed_legacy_source)
                or original.company_id != source.company_id
                or (
                    original.eh_event_provenance_version
                    != EVENT_PROVENANCE_VERSION
                    and not reviewed_legacy_source
                )
            ):
                raise AccessError(_(
                    "An event reversal must point to the exact stamped "
                    "original of the same source and company."
                ))
        return source

    @api.model_create_multi
    def create(self, vals_list):
        protected = {
            'eh_accounting_change_id', 'eh_subsequent_event_id',
            'eh_event_provenance_version',
        }
        if any(
            any(vals.get(field_name) for field_name in protected)
            for vals in vals_list
        ) and not (
            self.env.context.get(EVENT_MOVE_CONTEXT)
            is EVENT_MOVE_CAPABILITY
        ):
            raise AccessError(_(
                "Accounting-change and subsequent-event journal-entry "
                "provenance is engine-owned."
            ))
        for vals in vals_list:
            if any(vals.get(field_name) for field_name in protected):
                self._eh_validate_event_provenance(vals)
        return super().create(vals_list)

    def write(self, vals):
        provenance_fields = {
            'eh_accounting_change_id', 'eh_subsequent_event_id',
            'eh_event_provenance_version',
        }
        protected = bool(provenance_fields.intersection(vals))
        # Once a move belongs to this engine, its link to the entry it reverses
        # is part of the same immutable audit trail.  This guard deliberately
        # applies under sudo too; only the in-process object capability can
        # mutate provenance.
        if 'reversed_entry_id' in vals and any(
            move.eh_accounting_change_id or move.eh_subsequent_event_id
            for move in self
        ):
            protected = True
        if protected and not (
            self.env.context.get(EVENT_MOVE_CONTEXT)
            is EVENT_MOVE_CAPABILITY
        ):
            raise AccessError(_(
                "Accounting-change and subsequent-event journal-entry "
                "provenance is immutable."
            ))
        if protected:
            for move in self:
                if (
                    ('eh_accounting_change_id' in vals
                     and move.eh_accounting_change_id.id
                         != (vals.get('eh_accounting_change_id') or False)
                     and move.eh_accounting_change_id)
                    or ('eh_subsequent_event_id' in vals
                        and move.eh_subsequent_event_id.id
                            != (vals.get('eh_subsequent_event_id') or False)
                        and move.eh_subsequent_event_id)
                    or ('eh_event_provenance_version' in vals
                        and move.eh_event_provenance_version
                            != vals.get('eh_event_provenance_version')
                        and move.eh_event_provenance_version)
                ):
                    raise AccessError(_(
                        "Event journal-entry provenance is immutable after "
                        "creation."
                    ))
                check_vals = dict(vals)
                check_vals.setdefault(
                    'eh_accounting_change_id',
                    move.eh_accounting_change_id.id,
                )
                check_vals.setdefault(
                    'eh_subsequent_event_id',
                    move.eh_subsequent_event_id.id,
                )
                check_vals.setdefault(
                    'eh_event_provenance_version',
                    move.eh_event_provenance_version,
                )
                check_vals.setdefault('company_id', move.company_id.id)
                check_vals.setdefault('journal_id', move.journal_id.id)
                check_vals.setdefault(
                    'reversed_entry_id', move.reversed_entry_id.id,
                )
                move._eh_validate_event_provenance(check_vals)
        return super().write(vals)
