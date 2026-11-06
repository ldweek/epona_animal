# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.fx.cta.position: CTA reserve registry (IAS 21).

A CTA position is the parent-books ledger anchor for the cumulative
foreign currency translation reserve of ONE foreign operation. It does
not translate the foreign operation itself (that lives in the
consolidation engine); it owns the equity reserve account slice on the
parent's books and the IAS 21.48 disposal mechanics:

* Net investment hedges (IFRS 9 6.5.13) park their effective portion
  in the position's CTA equity account, tagging the journal entry to
  the position, so the position balance is ledger-fed at all times.
* Consolidation exports can point their translation differences at a
  position the same way (any posted account.move carrying
  eh_cta_position_id feeds the balance).
* On disposal of the foreign operation the FULL accumulated balance,
  including NIH effective portions parked there, is reclassified from
  equity to profit or loss (IAS 21.48). A partial disposal of an
  associate or joint venture reclassifies the proportionate share
  (IAS 21.48A-C, simplified to pct of balance).

Balance sign convention: credit minus debit on the CTA account lines
of posted moves tagged to the position. Positive = accumulated net
translation GAIN sitting in equity; negative = accumulated net loss.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .fx_integrity import (
    FX_EQUITY_ACCOUNT_TYPES,
    FX_GAIN_ACCOUNT_TYPES,
    FX_LOSS_ACCOUNT_TYPES,
    FX_MOVE_LINK_CAPABILITY,
    assert_company_allowed,
    lock_report_input_versions,
    require_account_type,
    require_record_company,
    resolve_reference,
)

# Fields that define what the accumulated reserve balance means. Once the
# position is disposed its balance has been reclassified to P&L against
# these exact settings; changing them afterwards would orphan the audit
# trail behind the posted reclassification entry.
_EH_FROZEN_POSITION_FIELDS = (
    'name',
    'company_id',
    'cta_account_id',
    'foreign_operation_partner_id',
    'foreign_operation_company_id',
    'operation_currency_id',
    'operation_type',
    'nci_account_id',
    'journal_id',
    'gain_account_id',
    'loss_account_id',
    'disposal_pct',
    'disposal_date',
    'disposal_loses_control',
)
_EH_LEDGER_ANCHOR_FIELDS = (
    'name', 'company_id', 'cta_account_id',
    'foreign_operation_partner_id', 'foreign_operation_company_id',
    'operation_currency_id',
    'operation_type', 'nci_account_id',
)
_EH_POSITION_EVIDENCE_FIELDS = (
    'state', 'currency_id', 'disposal_move_id',
    'disposed_at', 'disposed_by_id',
)


class EhFxCtaPosition(models.Model):
    _name = 'eh.fx.cta.position'
    _description = "CTA Reserve Position (IAS 21)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'company_id, name, id'

    # 'disposed' is reached only through action_dispose, which posts the IAS
    # 21.48 reclassification entry. Block any direct write to state that does
    # not originate from that action (which flags the write).
    _eh_guarded_fields = _EH_POSITION_EVIDENCE_FIELDS
    _eh_create_guarded_fields = _EH_POSITION_EVIDENCE_FIELDS

    name = fields.Char(
        required=True, tracking=True,
        help=(
            "Free label for the foreign operation this reserve slice "
            "belongs to, e.g. 'Net investment in DE subsidiary'."
        ),
    )
    state = fields.Selection(
        [
            ('open', "Open"),
            ('disposed', "Disposed"),
        ],
        default='open', required=True, tracking=True, index=True,
        help=(
            "open: the reserve accumulates translation differences and "
            "NIH effective portions. disposed: the foreign operation "
            "was disposed of and its full balance was reclassified to "
            "P&L after a full disposal/loss of control (IAS 21.48); the "
            "position is frozen. A partial disposal retaining control "
            "keeps the position open and reallocates CTA to NCI."
        ),
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
    )

    foreign_operation_partner_id = fields.Many2one(
        'res.partner', string="Foreign Operation (Partner)",
        ondelete='restrict',
        help=(
            "Partner record of the foreign operation, when the "
            "subsidiary / associate / JV is tracked as a partner."
        ),
    )
    foreign_operation_company_id = fields.Many2one(
        'res.company', string="Foreign Operation (Company)",
        ondelete='restrict',
        help=(
            "Company record of the foreign operation, when the "
            "subsidiary lives in this database (consolidation exports "
            "point here)."
        ),
    )
    operation_currency_id = fields.Many2one(
        'res.currency', string="Operation Currency",
        help=(
            "Functional currency of the foreign operation. "
            "Informational: the reserve itself is carried in the "
            "parent's presentation currency."
        ),
    )
    operation_type = fields.Selection(
        [
            ('subsidiary', "Subsidiary"),
            ('associate_joint_venture', "Associate or joint venture"),
        ],
        required=True, default='subsidiary', tracking=True,
        help=(
            "Controls IAS 21 partial-disposal treatment. A partial disposal "
            "of a subsidiary without loss of control reallocates CTA within "
            "equity to NCI (IAS 21.48B). An associate/JV partial disposal "
            "reclassifies the proportionate CTA to profit or loss."
        ),
    )

    cta_account_id = fields.Many2one(
        'account.account', string="CTA Equity Account", required=True,
        domain="[('account_type', 'in', ('equity', 'equity_unaffected'))]",
        check_company=True,
        help=(
            "Equity account carrying this position's slice of the "
            "foreign currency translation reserve. NIH effective "
            "portions and consolidation translation differences tagged "
            "to the position post here."
        ),
    )
    nci_account_id = fields.Many2one(
        'account.account', string="NCI Equity Account",
        domain="[('account_type', 'in', ('equity', 'equity_unaffected'))]",
        check_company=True,
        help=(
            "Non-controlling-interest equity account credited/debited for a "
            "partial disposal of a subsidiary that retains control. No "
            "profit-or-loss reclassification occurs in that IAS 21.48B path."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal', string="Journal",
        domain="[('type', '=', 'general')]",
        check_company=True,
        help="Journal used to post the disposal reclassification.",
    )
    gain_account_id = fields.Many2one(
        'account.account', string="Reclass Gain Account",
        domain="[('account_type', 'in', ('income', 'income_other'))]",
        check_company=True,
        help=(
            "P&L account credited when a positive (gain) reserve "
            "balance is reclassified on disposal."
        ),
    )
    loss_account_id = fields.Many2one(
        'account.account', string="Reclass Loss Account",
        domain="[('account_type', 'in', ('expense', 'expense_other'))]",
        check_company=True,
        help=(
            "P&L account debited when a negative (loss) reserve "
            "balance is reclassified on disposal."
        ),
    )

    balance = fields.Monetary(
        compute='_compute_balance', currency_field='currency_id',
        help=(
            "Ledger-fed accumulated reserve balance: credit minus "
            "debit on the CTA account lines of posted journal entries "
            "tagged to this position. Positive = accumulated net "
            "translation gain."
        ),
    )
    move_count = fields.Integer(compute='_compute_move_count')

    disposal_pct = fields.Float(
        string="Disposal %", default=100.0, digits=(5, 2),
        help=(
            "Share of the accumulated balance to reclassify on the "
            "next Dispose. 100 disposes the whole operation and closes "
            "the position (IAS 21.48). Below 100 reclassifies the "
            "proportionate share for a partial disposal of an "
            "associate or JV (IAS 21.48A-C) and keeps the position "
            "open."
        ),
    )
    disposal_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help="Accounting date for the next disposal entry.",
    )
    disposal_loses_control = fields.Boolean(
        string="Loss of control", default=False,
        help=(
            "For a subsidiary, select when the transaction loses control. "
            "The full CTA balance is then reclassified to profit or loss "
            "under IAS 21.48A even if the ownership percentage sold is below "
            "100. Leave clear when control is retained; the proportionate "
            "CTA is reattributed to NCI under IAS 21.48B."
        ),
    )
    disposal_move_id = fields.Many2one(
        'account.move', string="Disposal Entry", readonly=True,
        copy=False, ondelete='restrict',
        help="Reclassification entry of the final (full) disposal.",
    )
    disposed_at = fields.Datetime(readonly=True, tracking=True)
    disposed_by_id = fields.Many2one('res.users', readonly=True)

    notes = fields.Text()

    # ---- computes ----

    def _compute_balance(self):
        MoveLine = self.env['account.move.line']
        for pos in self:
            if not pos.ids or not pos.cta_account_id:
                pos.balance = 0.0
                continue
            lines = MoveLine.search([
                ('move_id.eh_cta_position_id', '=', pos.id),
                ('account_id', '=', pos.cta_account_id.id),
                ('move_id.state', '=', 'posted'),
            ])
            pos.balance = (
                sum(lines.mapped('credit')) - sum(lines.mapped('debit'))
            )

    def _compute_move_count(self):
        Move = self.env['account.move']
        for pos in self:
            pos.move_count = Move.search_count([
                ('eh_cta_position_id', '=', pos.id),
            ]) if pos.ids else 0

    # ---- constraints ----

    @api.constrains('cta_account_id')
    def _check_cta_account_equity(self):
        for pos in self:
            if pos.cta_account_id and pos.cta_account_id.account_type \
                    not in ('equity', 'equity_unaffected'):
                raise ValidationError(_(
                    "CTA position %(name)s: the CTA account must be an "
                    "equity account. The translation reserve is a "
                    "component of equity until disposal (IAS 21.39(c)).",
                    name=pos.display_name,
                ))

    @api.constrains('disposal_pct')
    def _check_disposal_pct(self):
        for pos in self:
            pct = float(pos.disposal_pct or 0.0)
            if (
                not math.isfinite(pct)
                or pct <= 0.0
                or pct > 100.0
            ):
                raise ValidationError(_(
                    "Disposal %% must be greater than 0 and at most "
                    "100. Got %.2f.",
                ) % pct)

    @api.model
    def _eh_validate_disposal_pct_values(self, vals):
        if 'disposal_pct' not in vals:
            return
        try:
            pct = float(vals.get('disposal_pct') or 0.0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(_(
                "Disposal percentage must be finite, greater than 0 and at "
                "most 100."
            )) from exc
        if not math.isfinite(pct) or pct <= 0.0 or pct > 100.0:
            raise ValidationError(_(
                "Disposal %% must be a finite number greater than 0 and at "
                "most 100. Got %s.",
                pct,
            ))

    # ---- ORM guards ----

    @api.model_create_multi
    def create(self, vals_list):
        # A position reaches 'disposed' only through action_dispose, which
        # posts the reclassification entry. Creating one directly disposed
        # would fabricate a closed reserve with no entry behind it.
        for vals in vals_list:
            self._eh_validate_disposal_pct_values(vals)
            assert_company_allowed(
                self.env,
                vals.get('company_id') or self.env.company.id,
                _("CTA position"),
            )
            if vals.get('state') and vals['state'] != 'open':
                raise UserError(_(
                    "A CTA position is created open and reaches the "
                    "disposed state only through the Dispose action, "
                    "which posts the IAS 21.48 reclassification entry.",
                ))
        positions = super().create(vals_list)
        positions._eh_validate_references()
        return positions

    def write(self, vals):
        self._eh_validate_disposal_pct_values(vals)
        if vals and not self.env.su:
            self._eh_check_access('write')
        self._eh_validate_references(vals)
        self._eh_lock_for_transition(
            extra_company_ids=(vals.get('company_id'),),
        )
        self._eh_validate_references(vals)
        touched = [f for f in _EH_FROZEN_POSITION_FIELDS if f in vals]
        anchor_touched = [f for f in _EH_LEDGER_ANCHOR_FIELDS if f in vals]
        if anchor_touched:
            ids = tuple(sorted(set(self.ids)))
            self.env.cr.execute(
                "SELECT DISTINCT eh_cta_position_id FROM account_move "
                "WHERE eh_cta_position_id IN %s",
                (ids,),
            )
            evidenced_ids = {row[0] for row in self.env.cr.fetchall()}
            evidenced = self.filtered(lambda pos: pos.id in evidenced_ids)
            if evidenced:
                raise UserError(_(
                    "CTA position %(name)s already has tagged journal "
                    "evidence. Its ledger identity is frozen (%(fields)s); "
                    "create a new position for a different operation or "
                    "reserve account.",
                    name=evidenced[0].display_name,
                    fields=', '.join(anchor_touched),
                ))
        if touched:
            disposed = self.filtered(lambda p: p.state == 'disposed')
            if disposed:
                raise UserError(_(
                    "CTA position %(name)s is disposed: its identity "
                    "fields are frozen (%(fields)s) because the full "
                    "reserve balance has been reclassified to P&L "
                    "against them (IAS 21.48).",
                    name=disposed[0].display_name,
                    fields=', '.join(touched),
                ))
        # The state of a disposed position is a control point: resetting it
        # to open would silently lift the freeze above and reopen a reserve
        # whose balance was already recycled to P&L. A raw ORM state write
        # without the sanctioned-transition flag is manager-gated.
        if 'state' in vals and not (
                self.env.su and self.env.context.get('eh_cta_state_change')):
            crossing = self.filtered(
                lambda p: p.state == 'disposed' and p.state != vals['state'])
            if crossing:
                crossing._check_manager()
        result = super().write(vals)
        self._eh_validate_references()
        return result

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of "
                "a disposed CTA position."))

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_for_transition()
        disposed = self.filtered(lambda p: p.state == 'disposed')
        if disposed:
            raise UserError(_(
                "A disposed CTA position cannot be deleted; it is the "
                "audit anchor of the posted IAS 21.48 reclassification "
                "entry."))
        Move = self.env['account.move']
        for pos in self:
            if Move.search_count([('eh_cta_position_id', '=', pos.id)]):
                raise UserError(_(
                    "CTA position %(name)s has journal entries tagged "
                    "to it and cannot be deleted; the position is the "
                    "ledger anchor of those entries.",
                    name=pos.display_name,
                ))
        return super().unlink()

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        for pos in self:
            company_id = vals.get('company_id', pos.company_id.id)
            assert_company_allowed(
                self.env, company_id, _("CTA position"))
            company = resolve_reference(
                self.env, 'res.company', company_id,
                _("CTA parent company"), access='read')
            journal_id = vals.get('journal_id', pos.journal_id.id)
            journal = resolve_reference(
                self.env, 'account.journal', journal_id,
                _("CTA journal"), access='read')
            require_record_company(
                journal, company, _("CTA journal"))
            if journal and journal.type != 'general':
                raise ValidationError(_(
                    "CTA disposal requires a general journal."
                ))
            for field_name, label, allowed_types in (
                ('cta_account_id', _("CTA equity account"),
                 FX_EQUITY_ACCOUNT_TYPES),
                ('nci_account_id', _("CTA NCI equity account"),
                 FX_EQUITY_ACCOUNT_TYPES),
                ('gain_account_id', _("CTA gain account"),
                 FX_GAIN_ACCOUNT_TYPES),
                ('loss_account_id', _("CTA loss account"),
                 FX_LOSS_ACCOUNT_TYPES),
            ):
                account_id = vals.get(field_name, pos[field_name].id)
                account = resolve_reference(
                    self.env, 'account.account', account_id,
                    label, access='read')
                require_record_company(account, company, label)
                require_account_type(account, allowed_types, label)
            resolve_reference(
                self.env, 'res.partner', vals.get(
                    'foreign_operation_partner_id',
                    pos.foreign_operation_partner_id.id),
                _("CTA foreign-operation partner"), access='read')
            resolve_reference(
                self.env, 'res.company', vals.get(
                    'foreign_operation_company_id',
                    pos.foreign_operation_company_id.id),
                _("CTA foreign-operation company"), access='read')
            resolve_reference(
                self.env, 'res.currency', vals.get(
                    'operation_currency_id', pos.operation_currency_id.id),
                _("CTA operation currency"), access='read')
        return self

    def _eh_lock_for_transition(self, extra_company_ids=()):
        """Lock source-version rows before positions across every FX path.

        The isolated input epochs are what ledger/rate writers update. Taking
        a position first would invert the FX transition order and permit
        deadlocks during a concurrent disposal/consolidation post.
        """
        ids = tuple(sorted(set(self.ids)))
        if not ids:
            return self
        expected_company_by_position = {
            position.id: position.company_id.id for position in self
        }
        company_ids = tuple(sorted({
            company_id
            for company_id in (
                *expected_company_by_position.values(),
                *(int(value) for value in extra_company_ids if value),
            )
            if company_id
        }))
        lock_report_input_versions(self.env, company_ids)
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id, company_id FROM eh_fx_cta_position WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (ids,),
        )
        locked_rows = self.env.cr.fetchall()
        if {row[0] for row in locked_rows} != set(ids):
            raise UserError(_(
                "A CTA position changed concurrently. Retry the operation."
            ))
        if any(
            expected_company_by_position.get(position_id) != company_id
            for position_id, company_id in locked_rows
        ):
            raise UserError(_(
                "A CTA position changed company concurrently. Retry the "
                "operation."
            ))
        self.invalidate_recordset()
        return self

    def _eh_prepare_action(self):
        if not self.env.su:
            self._eh_check_access('write')
        self._check_manager()
        self._eh_validate_references()
        self._eh_lock_for_transition()
        self._eh_validate_references()
        return self._eh_workflow_action()

    # ---- actions ----

    def action_dispose(self):
        """Apply full or partial CTA disposal accounting (IAS 21.48A-C).

        At disposal_pct == 100 the FULL accumulated balance, including
        NIH effective portions parked in the reserve, moves from the
        CTA equity account to profit or loss and the position closes.
        A subsidiary partial disposal that retains control reallocates the
        proportionate reserve to NCI within equity (48B). An associate/JV
        partial disposal reclassifies its proportionate reserve to P&L (48C).
        Losing control reclassifies the full reserve even when pct < 100.
        """
        with self.env.cr.savepoint():
            positions = self._eh_prepare_action()
            positions._eh_dispose_locked()
        return True

    def _eh_dispose_locked(self):
        for original in self.sorted('id'):
            pos = original
            if pos.state != 'open':
                raise UserError(_(
                    "CTA position %(name)s is already disposed; the "
                    "reserve was reclassified in full and cannot be "
                    "disposed again.",
                    name=pos.display_name,
                ))
            pct = float(pos.disposal_pct or 0.0)
            if not math.isfinite(pct) or pct <= 0.0 or pct > 100.0:
                raise UserError(_(
                    "Disposal %% must be greater than 0 and at most "
                    "100. Got %.2f.",
                ) % pct)
            if not pos.journal_id or not pos.gain_account_id \
                    or not pos.loss_account_id:
                raise UserError(_(
                    "CTA position %(name)s needs a journal, a reclass "
                    "gain account and a reclass loss account before "
                    "disposal.",
                    name=pos.display_name,
                ))
            company_ccy = pos.company_id.currency_id
            pos.invalidate_recordset(['balance'])
            balance = pos.balance
            if company_ccy.is_zero(balance):
                raise UserError(_(
                    "CTA position %(name)s has a zero accumulated "
                    "balance; there is nothing to reclassify.",
                    name=pos.display_name,
                ))
            subsidiary_partial_nci = (
                pos.operation_type == 'subsidiary'
                and pct < 100.0
                and not pos.disposal_loses_control
            )
            if subsidiary_partial_nci and not pos.nci_account_id:
                raise UserError(_(
                    "CTA position %(name)s retains control after a partial "
                    "subsidiary disposal. Configure an NCI Equity Account so "
                    "the proportionate reserve can be reattributed within "
                    "equity under IAS 21.48B.",
                    name=pos.display_name,
                ))
            # Percentage semantics are independent of currency rounding.
            # Loss of subsidiary control is a disposal under IAS 21.48A and
            # recycles the full reserve even if a residual interest remains.
            full = pct == 100.0 or (
                pos.operation_type == 'subsidiary'
                and pos.disposal_loses_control
            )
            amount = company_ccy.round(
                abs(balance) if full else abs(balance) * pct / 100.0,
            )
            if company_ccy.is_zero(amount):
                raise UserError(_(
                    "CTA position %(name)s has a disposal share that rounds "
                    "to zero in %(currency)s; increase the percentage or "
                    "record no disposal.",
                    name=pos.display_name,
                    currency=company_ccy.display_name,
                ))
            disposal_date = pos.disposal_date
            label = _("CTA disposal %(name)s (%(pct).2f%%)",
                      name=pos.name, pct=pct)
            # Balance sign is credit-positive on the equity reserve:
            #   positive balance = accumulated gain -> Dr CTA / Cr gain P&L
            #   negative balance = accumulated loss -> Dr loss P&L / Cr CTA
            if subsidiary_partial_nci and balance > 0:
                lines = [
                    (0, 0, {
                        'name': label,
                        'account_id': pos.cta_account_id.id,
                        'debit': amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': label,
                        'account_id': pos.nci_account_id.id,
                        'debit': 0.0,
                        'credit': amount,
                    }),
                ]
            elif subsidiary_partial_nci:
                lines = [
                    (0, 0, {
                        'name': label,
                        'account_id': pos.nci_account_id.id,
                        'debit': amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': label,
                        'account_id': pos.cta_account_id.id,
                        'debit': 0.0,
                        'credit': amount,
                    }),
                ]
            elif balance > 0:
                lines = [
                    (0, 0, {
                        'name': label,
                        'account_id': pos.cta_account_id.id,
                        'debit': amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': label,
                        'account_id': pos.gain_account_id.id,
                        'debit': 0.0,
                        'credit': amount,
                    }),
                ]
            else:
                lines = [
                    (0, 0, {
                        'name': label,
                        'account_id': pos.loss_account_id.id,
                        'debit': amount,
                        'credit': 0.0,
                    }),
                    (0, 0, {
                        'name': label,
                        'account_id': pos.cta_account_id.id,
                        'debit': 0.0,
                        'credit': amount,
                    }),
                ]
            move = self.env['account.move'].with_context(
                eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
            )._eh_create_sealed({
                'move_type': 'entry',
                'company_id': pos.company_id.id,
                'journal_id': pos.journal_id.id,
                'date': disposal_date,
                'ref': _("CTA disposal %s", pos.name),
                'line_ids': lines,
                'eh_sealed': True,
                'eh_cta_position_id': pos.id,
            })
            move.action_post()
            pos.invalidate_recordset(['balance'])
            if full:
                pos.with_context(eh_cta_state_change=True).write({
                    'state': 'disposed',
                    'disposed_at': fields.Datetime.now(),
                    'disposed_by_id': self.env.user.id,
                    'disposal_move_id': move.id,
                })
                pos.message_post(body=_(
                    "Foreign operation disposed: the full accumulated "
                    "CTA balance of %(amount).2f was reclassified from "
                    "equity to profit or loss (IAS 21.48). Entry "
                    "%(move)s.",
                    amount=abs(balance), move=move.name,
                ))
            elif subsidiary_partial_nci:
                pos.message_post(body=_(
                    "Partial subsidiary disposal retaining control: "
                    "%(pct).2f%% of accumulated CTA (%(amount).2f) was "
                    "reattributed to non-controlling interests within equity "
                    "under IAS 21.48B. No amount was recycled to profit or "
                    "loss. Entry %(move)s dated %(date)s.",
                    pct=pct, amount=amount, move=move.name,
                    date=disposal_date,
                ))
            else:
                pos.message_post(body=_(
                    "Partial disposal: %(pct).2f%% of the accumulated "
                    "CTA balance (%(amount).2f) was reclassified to "
                    "profit or loss (IAS 21.48C). The position stays "
                    "open for the retained interest. Entry %(move)s dated "
                    "%(date)s.",
                    pct=pct, amount=amount, move=move.name,
                    date=disposal_date,
                ))
        return self

    def action_view_moves(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("CTA Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('eh_cta_position_id', '=', self.id)],
            'context': {'create': False},
        }


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_cta_position_id = fields.Many2one(
        'eh.fx.cta.position', string="CTA Position",
        index='btree_not_null', copy=False, ondelete='restrict',
        help=(
            "CTA reserve position this entry feeds. NIH effective "
            "portions, consolidation translation exports and disposal "
            "reclassifications carry this tag so the position balance "
            "is computed straight from the ledger."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('eh_cta_position_id') for vals in vals_list) and not (
            self.env.context.get('eh_fx_move_link_capability')
            is FX_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "CTA journal provenance is engine-owned."))
        cta_ids = tuple(sorted({
            int(vals['eh_cta_position_id'])
            for vals in vals_list if vals.get('eh_cta_position_id')
        }))
        if cta_ids:
            positions = self.env['eh.fx.cta.position'].browse(cta_ids)
            positions._eh_lock_for_transition()
        return super().create(vals_list)

    def write(self, vals):
        if 'eh_cta_position_id' in vals and not (
            self.env.context.get('eh_fx_move_link_capability')
            is FX_MOVE_LINK_CAPABILITY
        ):
            raise AccessError(_(
                "CTA journal provenance is immutable."))
        return super().write(vals)

    def action_post(self):
        positions = self.mapped('eh_cta_position_id')
        if positions:
            if not self.env.su:
                positions._eh_check_access('read')
            positions._eh_validate_references()
            positions._eh_lock_for_transition()
            positions._eh_validate_references()
            disposed = positions.filtered(lambda position: position.state != 'open')
            if disposed:
                raise UserError(_(
                    "CTA position %(name)s is already disposed; no later "
                    "journal entry may feed its recycled reserve.",
                    name=disposed[0].display_name,
                ))
            for move in self.filtered('eh_cta_position_id'):
                position = move.eh_cta_position_id
                if move.company_id != position.company_id:
                    raise ValidationError(_(
                        "CTA-tagged entry %(move)s and position %(position)s "
                        "belong to different companies.",
                        move=move.display_name,
                        position=position.display_name,
                    ))
                if position.cta_account_id not in move.line_ids.account_id:
                    raise ValidationError(_(
                        "CTA-tagged entry %(move)s has no line on position "
                        "%(position)s's reserve account.",
                        move=move.display_name,
                        position=position.display_name,
                    ))
        return super().action_post()
