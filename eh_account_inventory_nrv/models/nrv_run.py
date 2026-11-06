# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.nrv.run: a period-end inventory net-realisable-value assessment.

Compute totals the required write-down; post recognises only the movement
from the opening position. An increase debits the write-down expense and
credits the write-down allowance; a recovery reverses it, capped at the
amount previously recognised because each line's write-down is floored at
zero (IAS 2.33).

Assessment basis (IAS 2.29). Write-downs are usually computed item by item,
but similar or related items may be grouped and assessed together. The run
carries an explicit assessment basis:

* item: each line is measured on its own; the write-down is the excess of
  the line's cost over its NRV, floored at zero per line.
* category: each product category is one unit of assessment. Surpluses (NRV
  above cost) and deficits inside the category are netted BEFORE the floor
  at zero is applied, so the category requirement is
  max(total cost - total NRV, 0). The category is therefore never carried
  above its aggregate cost, and because the requirement is allocated only
  over the lines with an item-level deficit (pro-rata by deficit), no
  individual line ever carries a negative write-down (never above its own
  cost) or more than its own deficit (never below its own NRV).

The basis is locked once the run is posted, tracked in the chatter, and
disclosed on the posted movement entry.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class EhNrvRun(models.Model):
    _name = 'eh.nrv.run'
    _description = "Inventory NRV run"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard', 'eh.gl.reversal']
    _order = 'reporting_date desc, revision desc, id desc'
    _rec_name = 'name'

    # eh.workflow.guard: state only ever changes through this model's own
    # action_* methods (which flag the write via _eh_workflow_action). A plain
    # user cannot RPC write({'state': 'posted'}) straight past action_post and
    # its manager gate, account validation and journal entry.
    _eh_guarded_fields = (
        'state', 'move_id', 'reversal_move_id',
        'computed_at', 'computed_by_id', 'posted_at', 'posted_by_id',
    )

    name = fields.Char(required=True, copy=False, default='/', tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('computed', "Computed"),
         ('posted', "Posted"), ('reversed', "Reversed"),
         ('cancelled', "Cancelled")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)
    reporting_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True)
    revision = fields.Integer(
        required=True, default=1, readonly=True, copy=False, index=True,
        help="Immutable revision of this company/reporting-date assessment. "
             "A new revision is permitted only after the prior revision has "
             "been reversed.",
    )
    supersedes_run_id = fields.Many2one(
        'eh.nrv.run', readonly=True, copy=False, ondelete='restrict',
        string="Supersedes run",
    )
    reversal_date = fields.Date(
        copy=False, tracking=True,
        help="Accounting date requested for reversal. Enter an open-period "
             "date before using Reverse; automatic next-day dating is not "
             "used because it can cross a fiscal lock or reporting period.",
    )
    assessment_basis = fields.Selection(
        [('item', "Item by item"), ('category', "Category (grouped)")],
        required=True, default='item', tracking=True,
        help="Unit of assessment for the lower-of-cost-and-NRV test "
             "(IAS 2.29). Item by item: each line is floored at zero on its "
             "own. Category (grouped): similar or related items sharing a "
             "product category are assessed as one unit, so surpluses and "
             "deficits inside the category are netted before the write-down "
             "is floored at zero; the category is never carried above its "
             "aggregate cost and no line is ever written up above its own "
             "cost. The choice is locked once the run is posted and is "
             "disclosed on the movement entry.")

    line_ids = fields.One2many('eh.nrv.line', 'run_id', copy=True)

    total_cost = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    closing_writedown = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id')
    gross_writedown = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Gross increases in required allowance for IAS 2.36(d).")
    gross_reversal = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Gross releases of prior allowance for IAS 2.36(e).")
    movement = fields.Monetary(
        compute='_compute_totals', store=True, currency_field='currency_id',
        help="Net movement to post; positive = charge to P&L.")

    writedown_expense_account_id = fields.Many2one(
        'account.account', string="Write-down Expense Account", tracking=True,
        domain="[('account_type', 'in', "
               "['expense', 'expense_direct_cost', 'expense_other']), "
               "('company_ids', 'parent_of', company_id)]")
    allowance_account_id = fields.Many2one(
        'account.account', string="Write-down Allowance Account",
        tracking=True,
        domain="[('account_type', 'in', ['asset_current']), "
               "('company_ids', 'parent_of', company_id)]",
        help="Contra-asset that carries the accumulated inventory "
             "write-down.")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        domain="[('type', '=', 'general'), "
               "('company_id', 'parent_of', company_id)]")

    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')
    reversal_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict')

    computed_at = fields.Datetime(readonly=True, tracking=True)
    computed_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)

    notes = fields.Text()

    _unique_company_date_revision = models.Constraint(
        'unique(company_id, reporting_date, revision)',
        'This inventory NRV run revision already exists for the company and '
        'reporting date.')

    # Measurement / input fields frozen once the run is posted or reversed. A
    # posted run has recognised a balanced ledger movement (IAS 2.34); its
    # inputs must not silently drift from what was posted. The state-transition
    # writes performed by action_compute / action_post / action_reverse /
    # action_cancel touch only state + audit stamps + move links, none of which
    # appear here, so those flows are never blocked. State itself is never
    # frozen. Reverse the run (manager-gated) to reopen it for editing.
    _FROZEN_AFTER_COMPUTE = (
        'reporting_date', 'company_id', 'writedown_expense_account_id',
        'allowance_account_id', 'journal_id', 'line_ids',
        'assessment_basis',
    )

    @api.depends('line_ids.required_writedown', 'line_ids.movement',
                 'line_ids.cost')
    def _compute_totals(self):
        for run in self:
            run.total_cost = sum(run.line_ids.mapped('cost'))
            run.closing_writedown = sum(
                run.line_ids.mapped('required_writedown'))
            movements = run.line_ids.mapped('movement')
            run.gross_writedown = sum(
                movement for movement in movements if movement > 0.0
            )
            run.gross_reversal = -sum(
                movement for movement in movements if movement < 0.0
            )
            run.movement = run.gross_writedown - run.gross_reversal

    @api.constrains(
        'writedown_expense_account_id', 'allowance_account_id', 'journal_id',
    )
    def _check_account_configuration(self):
        expense_types = {'expense', 'expense_direct_cost', 'expense_other'}
        for run in self:
            expense = run.writedown_expense_account_id
            allowance = run.allowance_account_id
            if expense and allowance and expense == allowance:
                raise ValidationError(_(
                    "The write-down expense and allowance accounts must be "
                    "different posting legs."
                ))
            if expense and expense.account_type not in expense_types:
                raise ValidationError(_(
                    "The NRV write-down account must be an expense, direct "
                    "cost or other-expense account."
                ))
            if allowance and allowance.account_type != 'asset_current':
                raise ValidationError(_(
                    "The NRV allowance account must be a current-asset "
                    "contra account."
                ))
            if run.journal_id and run.journal_id.type != 'general':
                raise ValidationError(_(
                    "Inventory NRV movements require a general journal."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        if any(
            'revision' in vals or 'supersedes_run_id' in vals
            for vals in vals_list
        ):
            raise AccessError(_(
                "NRV revision provenance is assigned only by the rerun engine."
            ))
        company_ids = []
        for vals in vals_list:
            if vals.get('reversal_date'):
                raise UserError(_(
                    "Set the reversal accounting date only after the NRV "
                    "movement has been posted."
                ))
            company_id = vals.get('company_id') or self.env.company.id
            self._eh_assert_company_allowed(company_id)
            company_ids.append(company_id)
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'eh.nrv.run') or '/'
        self._eh_lock_for_transition(extra_company_ids=company_ids)
        planned_keys = set()
        for vals in vals_list:
            company_id = vals.get('company_id') or self.env.company.id
            reporting_date = fields.Date.to_date(
                vals.get('reporting_date') or fields.Date.context_today(self)
            )
            key = (company_id, reporting_date)
            if key in planned_keys:
                raise UserError(_(
                    "Create only one NRV assessment revision per company and "
                    "reporting date at a time."
                ))
            planned_keys.add(key)
            latest = self.sudo().search([
                ('company_id', '=', company_id),
                ('reporting_date', '=', reporting_date),
            ], order='revision desc, id desc', limit=1)
            if latest and latest.state != 'reversed':
                raise UserError(_(
                    "NRV run %(run)s already owns this company/reporting date. "
                    "Post or cancel that draft, or reverse its posted ledger "
                    "entry before creating a replacement revision.",
                    run=latest.display_name,
                ))
            vals['revision'] = (latest.revision + 1) if latest else 1
            if latest:
                vals['supersedes_run_id'] = latest.id
        runs = super().create(vals_list)
        runs._eh_validate_references()
        return runs

    @api.model
    def _eh_assert_company_allowed(self, company_id):
        if (
            not self.env.su
            and company_id
            and int(company_id) not in self.env.companies.ids
        ):
            raise AccessError(_(
                "An NRV run may only use a company enabled in the caller's "
                "current company context."
            ))

    @api.model
    def _eh_account_company_ids(self, account):
        if 'company_ids' in account._fields:
            return set(account.company_ids.ids)
        return set(account.company_id.ids)

    @api.model
    def _eh_company_accounting_scope_ids(self, company):
        """O19 branches may use accounting objects owned by an ancestor.

        Supported series without the branch ``parent_ids`` semantic fall back
        to direct ownership; feature detection keeps the rule version-safe.
        """
        if 'parent_ids' in company._fields:
            return set(company.sudo().parent_ids.ids)
        return {company.id}

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        if not self.env.su:
            self._eh_check_access('write')
        for run in self:
            company_id = vals.get('company_id', run.company_id.id)
            self._eh_assert_company_allowed(company_id)
            company = self.env['res.company'].browse(company_id)
            journal_id = vals.get('journal_id', run.journal_id.id)
            journal = self.env['account.journal'].browse(journal_id)
            accounting_scope_ids = self._eh_company_accounting_scope_ids(
                company,
            )
            if journal:
                if not self.env.su:
                    journal._eh_check_access('read')
                if journal.company_id.id not in accounting_scope_ids:
                    raise UserError(_(
                        "The NRV journal must belong to the run company or an "
                        "accounting ancestor available to that branch."
                    ))
            for field_name in (
                'writedown_expense_account_id', 'allowance_account_id',
            ):
                account_id = vals.get(field_name, run[field_name].id)
                account = self.env['account.account'].browse(account_id)
                if not account:
                    continue
                if not self.env.su:
                    account._eh_check_access('read')
                if not (
                    accounting_scope_ids
                    & self._eh_account_company_ids(account)
                ):
                    raise UserError(_(
                        "NRV posting accounts must belong to the run company "
                        "or an accounting ancestor available to that branch."
                    ))
        return self

    def _eh_lock_company_rows(self, company_ids):
        company_ids = tuple(sorted(set(company_ids)))
        if company_ids:
            self.env.cr.execute(
                "SELECT id FROM res_company WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (company_ids,),
            )

    def _eh_lock_run_rows(self):
        ids = tuple(sorted(self.ids))
        if ids:
            self.env.cr.execute(
                "SELECT id FROM eh_nrv_run WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            self.invalidate_recordset()

    def _eh_lock_for_transition(
            self, extra_company_ids=(), lock_companies=True):
        company_ids = tuple(sorted(
            set(self.mapped('company_id').ids) | {
                int(company_id) for company_id in extra_company_ids
                if company_id
            }
        ))
        if lock_companies:
            self._eh_lock_company_rows(company_ids)
        self._eh_lock_run_rows()
        return self

    def _eh_prepare_transition(self):
        caller = self
        caller._eh_validate_references()
        caller._eh_lock_for_transition()
        caller._eh_validate_references()
        return caller._eh_workflow_action()

    def write(self, vals):
        if {'revision', 'supersedes_run_id'} & set(vals):
            raise AccessError(_(
                "NRV revision provenance is immutable and server-owned."
            ))
        if 'reversal_date' in vals:
            self._check_manager()
            self._eh_validate_references()
            self._eh_lock_for_transition(lock_companies=False)
            self._eh_validate_references()
            if self.filtered(lambda run: run.state != 'posted'):
                raise UserError(_(
                    "The reversal accounting date can only be set on a "
                    "posted NRV run awaiting reversal."
                ))
        reference_fields = {
            'company_id', 'journal_id', 'writedown_expense_account_id',
            'allowance_account_id',
        }
        frozen = [f for f in self._FROZEN_AFTER_COMPUTE if f in vals]
        if frozen or 'state' in vals:
            self._eh_validate_references(vals)
            self._eh_lock_for_transition(
                extra_company_ids=[vals.get('company_id')],
                lock_companies=(
                    'state' in vals or 'company_id' in vals
                ),
            )
            self._eh_validate_references(vals)
        elif reference_fields & set(vals):
            self._eh_validate_references(vals)
        posted = self.filtered(
            lambda r: r.state in ('posted', 'reversed'))
        # A pure state-transition write (action_compute / action_post /
        # action_reverse / action_cancel write only state + audit stamps +
        # move links) carries no frozen field and passes. A write touching a
        # measurement / input field while any record is posted or reversed is
        # always blocked, so a raw ORM write cannot drift a posted run's
        # figures from the recognised ledger movement.
        if frozen and posted:
            raise UserError(_(
                "Inputs on a posted NRV run are frozen (%(fields)s). "
                "Reverse it first (EH Accounting Manager only) to change it "
                "(IAS 2.34).",
                fields=', '.join(frozen)))
        # A posted / reversed run's state is a control point: resetting it to
        # draft would lift the freeze above. A raw ORM state write without the
        # sanctioned-transition context flag is manager-gated so a plain user
        # cannot un-freeze a GL-backed run.
        if 'state' in vals and not (
                self.env.su and self.env.context.get('eh_nrv_state_change')):
            crossing = posted.filtered(lambda r: r.state != vals['state'])
            if crossing:
                crossing._check_manager()
        return super().write(vals)

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager may post, reverse or change "
                "a controlled NRV workflow state."))

    def unlink(self):
        self._eh_validate_references()
        self._eh_lock_for_transition()
        self._eh_validate_references()
        posted = self.filtered(
            lambda r: r.state in ('posted', 'reversed')
            or r.move_id or r.reversal_move_id)
        if posted:
            raise UserError(_(
                "A posted NRV run cannot be deleted; reverse it first "
                "(EH Accounting Manager only)."))
        return super().unlink()

    # ---- transitions ----

    def action_compute(self):
        self = self._eh_prepare_transition()
        for run in self:
            if run.state not in ('draft', 'computed'):
                raise UserError(_(
                    "Compute is only available in draft or computed state."))
            if not run.line_ids:
                raise UserError(_("Add at least one inventory line."))
            run._eh_refresh_automatic_openings()
            run._eh_validate_measurements()
            run._validate_basis()
            run.write({
                'state': 'computed',
                'computed_at': fields.Datetime.now(),
                'computed_by_id': self.env.user.id,
            })
        return True

    def action_post(self):
        self = self._eh_prepare_transition()
        for run in self:
            run._check_manager()
            if run.state != 'computed':
                raise UserError(_("Run must be computed before posting."))
            run._eh_assert_no_later_posted_run(_("post"))
            run._eh_assert_automatic_openings_current()
            run._eh_assert_manual_openings_authorized()
            run._eh_validate_measurements()
            # Re-validate at post: lines stay editable between compute and
            # post, so a category could have been cleared in the meantime.
            run._validate_basis()
            run._validate_accounts()
            movement = run.currency_id.round(run.movement)
            move = self.env['account.move']
            if not run.currency_id.is_zero(movement):
                move = run._build_move()
            run.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
                'move_id': move.id,
            })
            if not move:
                run.message_post(body=_(
                    "NRV closing position posted with zero ledger movement; "
                    "the run remains the audited roll-forward anchor for the "
                    "next reporting date."
                ))
        return True

    def action_reverse(self):
        self = self._eh_prepare_transition()
        for run in self:
            run._check_manager()
            if run.state != 'posted':
                raise UserError(_("Only a posted NRV run can reverse."))
            run._eh_assert_no_later_posted_run(_("reverse"))
            if not run.move_id:
                run.with_context(eh_nrv_state_change=True).write({
                    'state': 'reversed',
                    'reversal_move_id': False,
                })
                run.message_post(body=_(
                    "Zero-movement NRV closing anchor reversed; no ledger "
                    "entry was required."
                ))
                continue
            if not run.reversal_date:
                raise UserError(_(
                    "Enter an open-period reversal accounting date before "
                    "reversing this NRV run."
                ))
            if run.reversal_date < run.reporting_date:
                raise UserError(_(
                    "The reversal accounting date cannot precede the NRV "
                    "reporting date."
                ))
            if (
                run.move_id.company_id != run.company_id
                or run.move_id.state != 'posted'
                or run.move_id.move_type != 'entry'
                or not run.move_id.eh_sealed
            ):
                raise UserError(_(
                    "The linked NRV move failed its sealed provenance check "
                    "and will not be reversed."
                ))
            reversal = run.move_id._eh_reverse_with_verified_capability([{
                'date': run.reversal_date,
                'journal_id': run.journal_id.id,
                'ref': _("NRV reversal %s", run.name),
            }], cancel=False)
            reversal._eh_post_verified_reversal()
            reversal.invalidate_recordset(['date', 'state'])
            if (
                reversal.state != 'posted'
                or reversal.date != run.reversal_date
            ):
                raise UserError(_(
                    "Odoo did not post the NRV reversal on the requested "
                    "accounting date. Choose an open-period date; no shifted "
                    "entry was accepted."
                ))
            run._eh_seal_reversal(reversal)
            run.with_context(eh_nrv_state_change=True).write({
                'state': 'reversed',
                'reversal_move_id': reversal.id,
            })
        return True

    def action_cancel(self):
        self = self._eh_prepare_transition()
        for run in self:
            if run.state in ('posted', 'reversed'):
                raise UserError(_("Cannot cancel a posted or reversed run."))
            run.state = 'cancelled'

    def action_set_to_draft(self):
        self = self._eh_prepare_transition()
        for run in self:
            if run.state != 'cancelled':
                raise UserError(_("Only cancelled runs can return to draft."))
            run.state = 'draft'

    def action_view_move(self):
        self.ensure_one()
        if not self.move_id:
            raise UserError(_("No movement entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move', 'res_id': self.move_id.id,
            'view_mode': 'form', 'views': [(False, 'form')],
        }

    def action_view_reversal_move(self):
        self.ensure_one()
        if not self.reversal_move_id:
            raise UserError(_("No reversal entry has been posted yet."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.reversal_move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    def action_view_superseded_run(self):
        self.ensure_one()
        if not self.supersedes_run_id:
            raise UserError(_("This is the first revision for its period."))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.nrv.run',
            'res_id': self.supersedes_run_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    # ---- helpers ----

    def _eh_assert_no_later_posted_run(self, action):
        self.ensure_one()
        later = self.sudo().search([
            ('company_id', '=', self.company_id.id),
            ('state', '=', 'posted'),
            ('reporting_date', '>', self.reporting_date),
        ], order='reporting_date desc, revision desc, id desc', limit=1)
        if later:
            raise UserError(_(
                "Cannot %(action)s %(run)s while later posted NRV run "
                "%(later)s owns the roll-forward chain. Reverse the chain "
                "from the latest reporting date first.",
                action=action,
                run=self.display_name,
                later=later.display_name,
            ))

    def _eh_refresh_automatic_openings(self):
        self.ensure_one()
        automatic = self.line_ids.filtered(
            lambda candidate: not candidate.opening_writedown_manual
        )
        if automatic:
            expected_by_line = automatic._prior_closing_writedown_map()
            for line in automatic:
                line.with_context(eh_nrv_auto_opening=True).write({
                    'prior_closing_writedown': expected_by_line.get(
                        line.id, 0.0,
                    ),
                    'opening_writedown': expected_by_line.get(line.id, 0.0),
                })

    def _eh_assert_automatic_openings_current(self):
        self.ensure_one()
        stale = self.env['eh.nrv.line']
        automatic = self.line_ids.filtered(
            lambda candidate: not candidate.opening_writedown_manual
        )
        expected_by_line = automatic._prior_closing_writedown_map()
        for line in automatic:
            expected = expected_by_line.get(line.id, 0.0)
            if not self.currency_id.is_zero(
                line.opening_writedown - expected
            ):
                stale |= line
        if stale:
            raise UserError(_(
                "The automatic opening write-down changed after this run was "
                "computed (%(lines)s). Compute again before posting so the "
                "movement follows the latest posted close.",
                lines=', '.join(stale.mapped('display_name')),
            ))

    def _eh_assert_manual_openings_authorized(self):
        self.ensure_one()
        incomplete = self.line_ids.filtered(
            lambda line: line.opening_writedown_manual and (
                not (line.opening_override_reason or '').strip()
                or not line.opening_override_by_id
                or not line.opening_override_at
            )
        )
        if incomplete:
            raise UserError(_(
                "Every manual opening override needs an EH Accounting "
                "Manager approval stamp and a reason before posting: %s.",
                ', '.join(incomplete.mapped('display_name')),
            ))

    def _eh_validate_measurements(self):
        self.ensure_one()
        invalid = []
        for line in self.line_ids:
            values = {
                'cost': line.cost,
                'NRV': line.net_realisable_value,
                'opening': line.opening_writedown,
                'required': line.required_writedown,
            }
            if any(not math.isfinite(float(value)) for value in values.values()):
                invalid.append(_("%s has a non-finite amount", line.name))
                continue
            if line.cost < 0.0 or line.net_realisable_value < 0.0:
                invalid.append(_("%s has negative cost or NRV", line.name))
            if not 0.0 <= line.opening_writedown <= line.cost:
                invalid.append(_("%s has an opening outside cost", line.name))
            if not 0.0 <= line.required_writedown <= line.cost:
                invalid.append(_("%s has a requirement outside cost", line.name))
        if invalid:
            raise UserError(_(
                "Inventory NRV measurements must be finite, non-negative and "
                "bounded by cost: %s.", '; '.join(invalid),
            ))

    def _validate_basis(self):
        """Category assessment needs a category on every line: IAS 2.29
        groups similar or related items, and an uncategorised line has no
        group to be assessed in."""
        self.ensure_one()
        seen_product_ids = set()
        duplicates = set()
        for line in self.line_ids.filtered('product_id'):
            product_id = line.product_id.id
            if product_id in seen_product_ids:
                duplicates.add(product_id)
            seen_product_ids.add(product_id)
        if duplicates:
            names = self.env['product.product'].browse(
                sorted(duplicates)
            ).mapped('display_name')
            raise UserError(_(
                "Each product may appear only once in an NRV run so its "
                "closing allowance has one deterministic roll-forward: %s.",
                ', '.join(names),
            ))
        if self.assessment_basis != 'category':
            return
        missing = self.line_ids.filtered(lambda ln: not ln.product_category_id)
        if missing:
            raise UserError(_(
                "The category assessment basis needs a product category on "
                "every line, because IAS 2.29 groups similar or related "
                "items and assesses each group as one unit. Missing on: "
                "%s.", ', '.join(missing.mapped('name'))))

    def _category_allocation(self):
        """Per-line closing write-down under the category basis (IAS 2.29).

        Each product category is one unit of assessment. The category
        requirement is max(total cost - total NRV, 0): surpluses and
        deficits inside the category are netted BEFORE the floor at zero,
        so the category as a whole is never carried above its aggregate
        cost. The requirement is then allocated over the lines carrying an
        item-level deficit, pro-rata by deficit, which guarantees:

        * a surplus line gets no allocation (it is never written up, so no
          item is ever carried above its own cost); and
        * a deficit line never gets more than its own deficit (it is never
          carried below its own NRV), because the netted requirement is at
          most the sum of the deficits.

        Rounding: each share is rounded to company currency; the last
        deficit line takes the residual so the shares tie exactly to the
        category requirement.

        Returns {line id: allocated closing write-down} covering every line
        of the run (zero for surplus lines).
        """
        self.ensure_one()
        currency = self.currency_id

        def rnd(value):
            return currency.round(value) if currency else round(value, 2)

        groups = {}
        for line in self.line_ids:
            groups.setdefault(line.product_category_id, []).append(line)
        alloc = {}
        for lines in groups.values():
            total_cost = max(rnd(sum(ln.cost for ln in lines)), 0.0)
            required = min(max(rnd(
                sum(ln.cost for ln in lines)
                - sum(ln.net_realisable_value for ln in lines)), 0.0),
                total_cost)
            deficits = [
                (ln, max(ln.cost - ln.net_realisable_value, 0.0))
                for ln in lines]
            for ln, _deficit in deficits:
                alloc[ln.id] = 0.0
            deficit_total = sum(d for _ln, d in deficits)
            if not required or not deficit_total:
                continue
            carriers = [(ln, d) for ln, d in deficits if d > 0.0]
            allocated = 0.0
            for ln, deficit in carriers[:-1]:
                share = rnd(required * deficit / deficit_total)
                alloc[ln.id] = share
                allocated += share
            alloc[carriers[-1][0].id] = rnd(required - allocated)
        return alloc

    def _basis_label(self):
        self.ensure_one()
        labels = dict(self._fields['assessment_basis']
                      ._description_selection(self.env))
        return labels.get(self.assessment_basis, self.assessment_basis)

    def _validate_accounts(self):
        self.ensure_one()
        missing = []
        if not self.journal_id:
            missing.append(_("journal"))
        if not self.writedown_expense_account_id:
            missing.append(_("write-down expense account"))
        if not self.allowance_account_id:
            missing.append(_("write-down allowance account"))
        if missing:
            raise UserError(_(
                "Configure the %s on run %s before posting.",
                ', '.join(missing), self.display_name))
        self._check_account_configuration()

    def _build_move(self):
        self.ensure_one()
        currency = self.currency_id
        movement = currency.round(self.movement)
        if currency.is_zero(movement):
            return self.env['account.move']
        if movement > 0:
            lines = [
                (0, 0, {
                    'name': _("Inventory write-down %s", self.name),
                    'account_id': self.writedown_expense_account_id.id,
                    'debit': movement, 'credit': 0.0}),
                (0, 0, {
                    'name': _("Write-down allowance %s", self.name),
                    'account_id': self.allowance_account_id.id,
                    'debit': 0.0, 'credit': movement}),
            ]
        else:
            amount = -movement
            lines = [
                (0, 0, {
                    'name': _("Write-down allowance release %s", self.name),
                    'account_id': self.allowance_account_id.id,
                    'debit': amount, 'credit': 0.0}),
                (0, 0, {
                    'name': _("Inventory write-down recovery %s", self.name),
                    'account_id': self.writedown_expense_account_id.id,
                    'debit': 0.0, 'credit': amount}),
            ]
        move = self.env['account.move'].with_company(
            self.company_id
        )._eh_create_sealed({
            'move_type': 'entry',
            'company_id': self.company_id.id,
            'date': self.reporting_date,
            'journal_id': self.journal_id.id,
            # IAS 2.36(a): the assessment basis is part of the measurement
            # policy applied, so it is disclosed on the ledger entry itself.
            'ref': _("Inventory NRV %(name)s (%(basis)s assessment)",
                     name=self.name, basis=self._basis_label()),
            'line_ids': lines,
            'eh_sealed': True,
        })
        move.action_post()
        return move
