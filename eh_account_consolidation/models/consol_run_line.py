# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.consol.run.line: one row in the consolidated trial balance.

The consolidation run produces one row per (account, kind, source).
Kinds:

  parent_balance: parent company's own balance.
  subsidiary_balance: a subsidiary's translated balance.
  elimination: an elimination journal line.
  equity_pickup: IAS 28 share-of-profit pick-up for an associate.
  cta: currency translation adjustment under IAS 21, split per member.
  cta_recycle: IAS 21.48 disposal reclassification of a member's CTA.
  nci: non-controlling interest carve-out (proportionate or fair value).
  goodwill: IFRS 3 goodwill / bargain-purchase residual.
  impairment: IAS 36 goodwill impairment charge.
  disclosure: zero-amount memo row (e.g. an IAS 28.1A election).

Each row carries the amount in the consolidation entity's
presentation currency. The aggregation view sums these by account
to produce the consolidated trial balance.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


# States in which a run's consolidated figures are settled and must not be
# extended by a hand-added line. The engine rebuilds every line from scratch
# while the run is still 'draft' (action_compute sets 'computed' only AFTER
# _build_lines), and later engine paths (IAS 36 impairment) create lines in
# 'computed' / 'reviewed'. The guard exempts the engine's own build path,
# which flags itself with this context key, and only ever fires on a manual
# append.
_FROZEN_APPEND_STATES = ('computed', 'reviewed', 'closed')

_SERVER_OWNED_FIELDS = frozenset({
    'presentation_currency_id', 'account_code', 'account_name',
    'account_type', 'posting_account_id',
})

# Context key set by the run engine's controlled build path (see
# eh.consol.run._build_lines and action_impair_goodwill) so its own line
# creation is exempt from the frozen-append guard. env.su alone is not a
# reliable engine signal: the admin / superuser runs with su=True yet must
# still be blocked from hand-appending to a settled run.
CONSOL_ENGINE_CTX = 'eh_consol_engine_build'
# An object identity, unlike a truthy context value, cannot cross JSON-RPC.
# Every corridor that may mutate settled engine evidence must carry this exact
# in-process capability as well as ``env.su``.
CONSOL_ENGINE_CAPABILITY = object()


_KIND_CHOICES = [
    ('parent_balance', "Parent balance"),
    ('subsidiary_balance', "Subsidiary balance"),
    ('elimination', "Elimination"),
    ('equity_pickup', "Equity pick-up (IAS 28)"),
    ('cta', "CTA (IAS 21)"),
    ('cta_recycle', "CTA recycling (IAS 21.48)"),
    ('nci', "Non-controlling interest"),
    ('goodwill', "Goodwill (IFRS 3)"),
    ('bargain_purchase', "Bargain-purchase gain (IFRS 3.34)"),
    ('impairment', "Goodwill impairment (IAS 36)"),
    ('disclosure', "Disclosure (memo)"),
]


def _account_type_selection(recordset):
    return recordset.env['account.account']._fields[
        'account_type'
    ]._description_selection(recordset.env)


class EhConsolRunLine(models.Model):
    _name = 'eh.consol.run.line'
    _inherit = 'eh.consol.company.scope.mixin'
    _description = "Consolidation run line"
    _order = 'run_id, account_id, kind'

    run_id = fields.Many2one(
        'eh.consol.run', required=True,
        ondelete='cascade', index=True,
    )
    presentation_currency_id = fields.Many2one(
        related='run_id.presentation_currency_id',
        store=True, readonly=True,
    )

    account_id = fields.Many2one(
        'account.account',
        ondelete='restrict',
        index=True,
        help=(
            "Account on the consolidated chart. May be empty for "
            "CTA / NCI lines when no matching account is configured."
        ),
    )
    account_code = fields.Char(
        string="Account Code", readonly=True, copy=False,
        help="Engine-owned snapshot of the source-company account code at "
             "run time. It is stored as a plain value because the live account "
             "code is company-dependent across supported Odoo versions.",
    )
    account_name = fields.Char(
        string="Account Name", readonly=True, copy=False,
        help="Engine-owned snapshot of the source-company account name at "
             "run time. Stored as a plain label so the consolidation run "
             "stays reproducible regardless of company or language context.",
    )
    account_type = fields.Selection(
        selection=_account_type_selection, readonly=True, copy=False,
    )
    posting_account_id = fields.Many2one(
        'account.account', readonly=True, copy=False, ondelete='restrict',
        index=True,
        help=(
            "Immutable engine snapshot of the exact account used if this "
            "line is posted to the dedicated consolidation ledger."
        ),
    )

    kind = fields.Selection(
        _KIND_CHOICES, required=True, index=True,
    )
    company_id = fields.Many2one(
        'res.company',
        help="Source company. Empty for CTA + multi-source rows.",
    )
    member_id = fields.Many2one(
        'eh.consol.member',
        help="Source member, when the row originates from a subsidiary.",
    )
    elimination_id = fields.Many2one(
        'eh.consol.elimination',
        help="Source elimination, when the row originates from one.",
    )
    cta_position_id = fields.Many2one(
        'eh.fx.cta.position', string="CTA Position",
        ondelete='restrict',
        help=(
            "CTA reserve position (eh_account_fx_revaluation) the row's "
            "translation reserve slice belongs to. Carried on kind='cta' "
            "and kind='cta_recycle' rows of members linked to a position, "
            "so the run-side reserve movement is traceable to the "
            "parent-books registry (IAS 21.48)."
        ),
    )
    disposal_event_id = fields.Many2one(
        'eh.consol.disposal.event', ondelete='cascade', readonly=True,
        help=(
            "Immutable disposal event that generated this CTA-recycling line."
        ),
    )

    amount = fields.Monetary(
        currency_field='presentation_currency_id',
        help="Amount in the consolidation presentation currency.",
    )

    notes = fields.Char()

    @api.model
    def _eh_validate_finite_amount(self, vals, current=None):
        amount = vals.get(
            'amount', current.amount if current else 0.0,
        )
        if not math.isfinite(float(amount or 0.0)):
            raise ValidationError(_(
                "A consolidation run-line amount must be finite."
            ))
        return True

    @api.constrains('amount')
    def _check_finite_amount(self):
        for line in self:
            self._eh_validate_finite_amount({}, current=line)

    def _check_run_not_frozen(self):
        """Block any hand edit or delete of a line whose run is settled
        (computed / reviewed / closed).

        A settled run's consolidated figures are engine generated and, once
        closed, signed and cited in audit; its lines must be frozen
        (reproducible). Editing or deleting a line by hand would silently move
        the consolidated totals, so it is refused for the full settled set, not
        only 'closed' (matching the create-append guard). The engine's own
        controlled paths (recompute drops lines while the run is draft; the IAS
        36 impairment test drops its prior impairment lines on a computed /
        reviewed run) flag themselves with CONSOL_ENGINE_CTX and are exempt, so
        this guard only ever fires on a manual tamper. env.su alone is not an
        engine signal: even the superuser is blocked unless the engine context
        is set.
        """
        if (
            self.env.su
            and self.env.context.get(CONSOL_ENGINE_CTX)
            is CONSOL_ENGINE_CAPABILITY
        ):
            return
        for line in self:
            if line.run_id.state in _FROZEN_APPEND_STATES:
                raise UserError(_(
                    "Run %(run)s is %(state)s. Its consolidation lines are "
                    "frozen and cannot be changed or deleted by hand; that "
                    "would silently move the consolidated totals. Reset the "
                    "run to draft and recompute instead.",
                    run=line.run_id.name, state=line.run_id.state,
                ))

    @api.model
    def _eh_validate_target(self, vals, current=None):
        run = self.env['eh.consol.run'].browse(
            vals.get('run_id', current.run_id.id if current else False))
        if not run:
            return run
        run._eh_assert_company_closure()
        scope = set(run.entity_id._eh_scope_companies().ids)
        account = self.env['account.account'].browse(vals.get(
            'account_id', current.account_id.id if current else False))
        if account:
            if not self.env.su:
                account._eh_check_access('read')
            company_ids = (
                set(account.company_ids.ids)
                if 'company_ids' in account._fields
                else set(account.company_id.ids)
            )
            if not (company_ids & scope):
                raise UserError(_(
                    "The consolidation line account is outside the run's "
                    "authorised company closure."
                ))
        company = self.env['res.company'].browse(vals.get(
            'company_id', current.company_id.id if current else False))
        if company and company.id not in scope:
            raise AccessError(_(
                "The consolidation line source company is outside the run's "
                "authorised company closure."
            ))
        member = self.env['eh.consol.member'].browse(vals.get(
            'member_id', current.member_id.id if current else False))
        if member:
            if not self.env.su:
                member._eh_check_access('read')
            if member.entity_id != run.entity_id:
                raise UserError(_(
                    "The consolidation line member must belong to its run "
                    "entity."
                ))
        elimination = self.env['eh.consol.elimination'].browse(vals.get(
            'elimination_id', current.elimination_id.id if current else False))
        if elimination:
            elimination._eh_assert_company_closure()
            if elimination.run_id != run:
                raise UserError(_(
                    "The consolidation line elimination must belong to its "
                    "run."
                ))
        position = self.env['eh.fx.cta.position'].browse(vals.get(
            'cta_position_id',
            current.cta_position_id.id if current else False))
        if position:
            if not self.env.su:
                position._eh_check_access('read')
            if position.company_id.id not in scope:
                raise UserError(_(
                    "The CTA position is outside the run's authorised company "
                    "closure."
                ))
        disposal_event = self.env['eh.consol.disposal.event'].browse(vals.get(
            'disposal_event_id',
            current.disposal_event_id.id if current else False,
        ))
        if disposal_event and (
            disposal_event.run_id != run
            or disposal_event.member_id != member
        ):
            raise UserError(_(
                "A CTA-recycling line's disposal event must belong to the same "
                "run and member."
            ))
        return run

    def _eh_lock_parent_runs(self, extra_run_ids=()):
        runs = self.mapped('run_id') | self.env['eh.consol.run'].browse(
            [run_id for run_id in extra_run_ids if run_id])
        runs._eh_lock_for_transition()
        ids = tuple(sorted(self.ids))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id, run_id FROM eh_consol_run_line WHERE id IN %s "
                "ORDER BY id FOR UPDATE", (ids,))
            locked_rows = self.env.cr.fetchall()
            if {row[0] for row in locked_rows} != set(ids):
                raise UserError(_(
                    "A consolidation run line changed concurrently. Retry "
                    "the operation."
                ))
            if not {row[1] for row in locked_rows}.issubset(set(runs.ids)):
                raise UserError(_(
                    "A consolidation run line changed run concurrently. "
                    "Retry the operation."
                ))
            self.invalidate_recordset()
        return runs

    @api.model_create_multi
    def create(self, vals_list):
        """Block a manual append to a settled run.

        A closed / reviewed / computed run's consolidated totals are settled
        and (once closed) signed and cited in audit. Its lines are engine
        generated: the engine rebuilds them from scratch while the run is
        still draft, and the IAS 36 impairment path adds lines afterwards. A
        user (even a manager, who holds create rights on this model, and even
        the superuser) appending a line to such a run would silently move the
        consolidated figures and defeat the frozen-run control, so create is
        refused for any caller whose target run is in a settled state. The
        engine's own build path flags itself with the CONSOL_ENGINE_CTX
        context key and is exempt.
        """
        forged = set().union(*(
            set(vals) & _SERVER_OWNED_FIELDS for vals in vals_list
        ))
        if forged:
            raise AccessError(_(
                "Consolidation line snapshots are server-owned and cannot be "
                "supplied directly: %s",
                ', '.join(sorted(forged)),
            ))
        event_linked = any(vals.get('disposal_event_id') for vals in vals_list)
        if event_linked and not (
            self.env.su
            and self.env.context.get(CONSOL_ENGINE_CTX)
            is CONSOL_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "CTA disposal-event links are immutable engine evidence."
            ))
        for vals in vals_list:
            self._eh_validate_finite_amount(vals)
            self._eh_validate_target(vals)
        run_ids = {vals.get('run_id') for vals in vals_list if vals.get('run_id')}
        self._eh_lock_parent_runs(run_ids)
        for vals in vals_list:
            self._eh_validate_finite_amount(vals)
            run = self._eh_validate_target(vals)
            if not (
                self.env.su
                and self.env.context.get(CONSOL_ENGINE_CTX)
                is CONSOL_ENGINE_CAPABILITY
            ):
                if run.state in _FROZEN_APPEND_STATES:
                    raise UserError(_(
                        "Run %s is %s. Its consolidation lines are engine "
                        "generated and cannot be added by hand; adding a line "
                        "would silently change the consolidated totals. Reset "
                        "the run to draft and recompute instead.",
                        run.name, run.state,
                    ))
        return super().create(vals_list)

    def write(self, vals):
        forged = set(vals) & _SERVER_OWNED_FIELDS
        if forged:
            raise AccessError(_(
                "Consolidation line snapshots are server-owned and cannot be "
                "changed directly: %s",
                ', '.join(sorted(forged)),
            ))
        for line in self:
            self._eh_validate_finite_amount(vals, current=line)
            self._eh_validate_target(vals, current=line)
        target_ids = (vals.get('run_id'),) if vals.get('run_id') else ()
        self._eh_lock_parent_runs(target_ids)
        for line in self:
            self._eh_validate_finite_amount(vals, current=line)
            self._eh_validate_target(vals, current=line)
        self._check_run_not_frozen()
        return super().write(vals)

    def unlink(self):
        for line in self:
            self._eh_validate_target({}, current=line)
        self._eh_lock_parent_runs()
        for line in self:
            self._eh_validate_target({}, current=line)
        self._check_run_not_frozen()
        return super().unlink()
