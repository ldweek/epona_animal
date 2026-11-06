# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
One revaluation line per (account, partner, foreign currency) group.

Lines are produced by the run's compute step and frozen on post.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

# Run states in which the revaluation lines are frozen: once the run has
# posted (or been reversed after an auto-reverse), the lines are the audit
# record behind a posted journal entry and must not change.
_FROZEN_RUN_STATES = ('posted', 'reversed')


class EhFxRevaluationLine(models.Model):
    _name = 'eh.fx.revaluation.line'
    _description = "FX Revaluation Line"
    _order = 'run_id, account_id, partner_id, foreign_currency_id'

    run_id = fields.Many2one(
        'eh.fx.revaluation.run', required=True, ondelete='cascade', index=True,
    )
    account_id = fields.Many2one(
        'account.account', required=True, ondelete='restrict',
    )
    partner_id = fields.Many2one(
        'res.partner', ondelete='restrict',
    )
    foreign_currency_id = fields.Many2one(
        'res.currency', required=True, ondelete='restrict',
    )

    balance_company = fields.Monetary(
        help="Carrying amount in company currency before revaluation.",
    )
    balance_foreign = fields.Float(
        digits=(16, 6),
        help="Open balance in the foreign currency.",
    )
    closing_rate = fields.Float(
        digits=(16, 8),
        help="Units of company currency per 1 unit of foreign currency "
             "at the revaluation date.",
    )
    new_balance_company = fields.Monetary(
        help="Carrying amount restated at the closing rate.",
    )
    adjustment = fields.Monetary(
        help="new_balance_company minus balance_company. Positive moves "
             "the account up; sign is interpreted in light of the "
             "account nature to assign the gain or loss leg.",
    )
    nature = fields.Selection([
        ('gain', "Gain"),
        ('loss', "Loss"),
        ('flat', "No change"),
    ], default='flat')

    source_line_count = fields.Integer(
        help="Number of journal items aggregated into this line.",
    )
    source_move_line_ids = fields.Many2many(
        'account.move.line', 'eh_fx_reval_line_source_rel',
        'reval_line_id', 'move_line_id',
        string="Source Journal Items", copy=False,
        help=(
            "Exact journal items aggregated into this line at compute "
            "time. They drive the settled / open split: once "
            "every source item has been fully reconciled (settled), "
            "the line is classified as sourced from a settled exposure."
        ),
    )
    is_realized = fields.Boolean(
        compute='_compute_is_realized',
        search='_search_is_realized',
        string="Sources Settled",
        help=(
            "True when every source journal item behind this line has "
            "since been fully reconciled: the exposure was settled, so "
            "the exposure is settled. This is a settlement-status view of "
            "the revaluation-date adjustment, not a new realization entry."
        ),
    )

    currency_id = fields.Many2one(
        related='run_id.currency_id', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='run_id.company_id', store=True, readonly=True,
    )

    @api.depends('source_move_line_ids.reconciled')
    def _compute_is_realized(self):
        # Realized = the whole aggregated exposure has settled. A line
        # with no stored source items (runs computed before this field
        # existed, or non-reconcilable accounts such as bank balances)
        # stays unrealized: FX on an unsettled or standing balance is
        # not realized until the item clears.
        for line in self:
            sources = line.source_move_line_ids
            line.is_realized = bool(sources) and all(
                sources.mapped('reconciled'))

    def _search_is_realized(self, operator, value):
        # Some ORM paths normalise boolean leaves to in/not in with a
        # one-element list; fold those back to =/!= first.
        if operator in ('in', 'not in') \
                and isinstance(value, (list, tuple)) and len(value) == 1:
            operator = '=' if operator == 'in' else '!='
            value = value[0]
        if operator not in ('=', '!='):
            raise UserError(_(
                "Unsupported operator %s for Sources Settled filter.",
            ) % operator)
        want = bool(value) if operator == '=' else not bool(value)
        # Resolve the relational predicate in PostgreSQL rather than
        # materialising every revaluation line and its source items.
        # BOOL_AND mirrors the compute above; a line with no relation row
        # deliberately remains open.
        self.env.cr.execute("""
            SELECT relation.reval_line_id
              FROM eh_fx_reval_line_source_rel AS relation
              JOIN account_move_line AS source
                ON source.id = relation.move_line_id
             GROUP BY relation.reval_line_id
            HAVING BOOL_AND(source.reconciled)
        """)
        settled_ids = [row[0] for row in self.env.cr.fetchall()]
        return [('id', 'in' if want else 'not in', settled_ids)]

    def _eh_check_not_frozen(self):
        """Block edits/deletes of lines whose run has posted.

        A posted run's lines are the audited support for a posted journal
        entry; the docstring on this model promises they are frozen on
        post. The compute step (which rebuilds lines) only runs while the
        run is in draft or computed, so this never blocks a legitimate
        recompute.
        """
        for line in self:
            if line.run_id.state in _FROZEN_RUN_STATES:
                raise UserError(_(
                    "Revaluation line for %(account)s is frozen: its run "
                    "%(run)s has posted. Reverse or cancel the run to make "
                    "changes.",
                    account=line.account_id.display_name,
                    run=line.run_id.name or line.run_id.id,
                ))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "FX revaluation lines are engine output and can only be "
                "created by Compute."))
        # Adding a line to a posted / reversed run would recompute that run's
        # stored totals (net_adjustment, total_gain/loss, line_count and the
        # realized/unrealized split, all @api.depends('line_ids.adjustment')),
        # silently restating the audited FX result away from the sealed
        # journal entry - the very hole write()/unlink() close, but which they
        # cannot reach because the line ACL grants a plain user create only
        # (no write, no unlink). Block create when the target run is frozen;
        # the compute step (which rebuilds lines) runs only in draft/computed,
        # so this never blocks a legitimate recompute.
        run_ids = {vals['run_id'] for vals in vals_list if vals.get('run_id')}
        if run_ids:
            frozen = self.env['eh.fx.revaluation.run'].browse(run_ids).filtered(
                lambda r: r.state in _FROZEN_RUN_STATES)
            if frozen:
                raise UserError(_(
                    "Revaluation lines cannot be added to a posted FX "
                    "revaluation run; its lines are frozen. Reverse or cancel "
                    "the run to make changes."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(_(
                "FX revaluation lines are engine output and cannot be "
                "edited directly."))
        self._eh_check_not_frozen()
        # Re-pointing a line INTO a posted / reversed run would recompute that
        # target run's stored totals. _eh_check_not_frozen inspects only the
        # current (source) parent, so guard the destination run explicitly.
        if vals.get('run_id'):
            target = self.env['eh.fx.revaluation.run'].browse(vals['run_id'])
            if target.state in _FROZEN_RUN_STATES:
                raise UserError(_(
                    "Revaluation lines cannot be moved into a posted FX "
                    "revaluation run; its lines are frozen. Reverse or cancel "
                    "the run to make changes."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(_(
                "FX revaluation lines are engine output and can only be "
                "replaced by Compute."))
        self._eh_check_not_frozen()
        return super().unlink()
