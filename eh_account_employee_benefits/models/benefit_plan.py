# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.benefit.plan: one defined benefit plan register (IAS 19).

Scope statement (also in the manifest): actuarial results are IMPORTED
INPUTS, never computed. The plan register holds the accounts and policy
choices the ledger mechanics need; the per-period actuarial figures live on
eh.benefit.valuation records.

Net-presentation decision (documented for the golden tests): the DBO and
plan-asset movement fields must point to the SAME balance-sheet account.
They remain separate configuration fields so each journal leg and each IAS
19.140-141 disclosure schedule keeps its own label, while the financial
statement account itself can only carry the net defined benefit position
required by IAS 19.63.

Contribution routing decision (contributions_posted_elsewhere):

* True (default): payroll normally pays the fund and books
  Dr contribution clearing / Cr bank when the cash leaves. The valuation
  entry then books Dr plan assets / Cr contribution clearing, closing the
  clearing loop without touching cash twice.
* False: no other system posts the payment, so the valuation entry credits
  the bank/clearing account directly (the "Cr cash/bank only if
  contributions posted here" case).

Funding status decision: a funded plan pays benefits and settlement
payments OUT OF PLAN ASSETS (no employer cash leg; the amounts cancel
inside the two movement deltas). An unfunded plan holds no assets at all
(opening assets, contributions and excess return are constrained to zero)
and the employer pays benefits and settlements directly, credited to the
benefit payment account.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_NET_POSITION_ACCOUNT_TYPES = frozenset({
    'asset_current', 'asset_non_current',
    'liability_current', 'liability_non_current',
})


_BENEFIT_ACCOUNT_TYPES = {
    'service_cost_account_id': frozenset({'expense', 'expense_direct_cost'}),
    'net_interest_account_id': frozenset({'expense', 'expense_direct_cost'}),
    'oci_account_id': frozenset({'equity'}),
    'dbo_account_id': _NET_POSITION_ACCOUNT_TYPES,
    'plan_asset_account_id': _NET_POSITION_ACCOUNT_TYPES,
    'contribution_account_id': frozenset({
        'asset_cash', 'asset_current', 'liability_current',
    }),
    'contribution_cash_account_id': frozenset({'asset_cash'}),
    'benefit_payment_account_id': frozenset({
        'asset_cash', 'asset_current', 'liability_current',
    }),
}


class EhBenefitPlan(models.Model):
    _name = 'eh.benefit.plan'
    _description = "Defined benefit plan (IAS 19)"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'name, id'
    _check_company_auto = True
    # Block a direct RPC write of state that would skip the activation /
    # closing gates (a closed plan cannot take new valuations; a plan must be
    # active before valuations post; closing refuses outstanding drafts). Only
    # the record's own actions, which run under sudo, may move state.
    _eh_guarded_fields = ('state', 'currency_id')

    name = fields.Char(required=True, tracking=True)
    state = fields.Selection(
        [('draft', "Draft"), ('active', "Active"), ('closed', "Closed")],
        default='draft', required=True, tracking=True, index=True)

    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        index=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True)

    country_note = fields.Char(
        string="Country / Jurisdiction Note", tracking=True,
        help="Where the plan operates and under which law (free note; the "
             "module carries no regional logic).")
    funded = fields.Boolean(
        default=True, tracking=True, string="Funded Plan",
        help="Funded: the plan holds assets, and benefits and settlement "
             "payments are paid out of those assets. Unfunded: the plan "
             "holds no assets and the employer pays benefits directly "
             "(credited to the benefit payment account).")
    contributions_posted_elsewhere = fields.Boolean(
        default=True, tracking=True,
        string="Contributions Posted Elsewhere",
        help="Default True: payroll pays the fund and posts the cash leg "
             "(Dr contribution clearing / Cr bank); the valuation entry "
             "credits the contribution clearing account. Untick only when "
             "nothing else posts the payment, in which case the valuation "
             "entry credits the bank/clearing account directly.")

    # ---- accounts ----
    service_cost_account_id = fields.Many2one(
        'account.account', string="Service Cost Expense Account",
        tracking=True, check_company=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]",
        help="P&L. Carries current service cost, past service cost "
             "(IAS 19.103) and settlement gains/losses (IAS 19.8 includes "
             "them in service cost).")
    net_interest_account_id = fields.Many2one(
        'account.account', string="Net Interest Expense Account",
        tracking=True, check_company=True,
        domain="[('account_type', 'in', ['expense', 'expense_direct_cost'])]",
        help="P&L. Net interest on the opening net defined benefit "
             "liability/(asset) at the discount rate (IAS 19.123). A net "
             "asset position produces net interest income (credit).")
    oci_account_id = fields.Many2one(
        'account.account', string="Remeasurement OCI Account", tracking=True,
        check_company=True,
        domain="[('account_type', '=', 'equity')]",
        help="Equity/OCI. Remeasurements (actuarial gains and losses on the "
             "obligation, excess return on plan assets, asset ceiling "
             "changes) go here and are NEVER reclassified to profit or "
             "loss (IAS 19.122).")
    dbo_account_id = fields.Many2one(
        'account.account', string="Net Position Account (DBO Leg)",
        tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_current', 'asset_non_current', "
               "'liability_current', 'liability_non_current'])]",
        help="Balance-sheet account for the obligation leg. For net "
             "presentation under IAS 19.63 this must be the same account "
             "as the Plan Asset Leg field.")
    plan_asset_account_id = fields.Many2one(
        'account.account', string="Net Position Account (Plan Asset Leg)",
        tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_current', 'asset_non_current', "
               "'liability_current', 'liability_non_current'])]",
        help="Balance-sheet account for the plan-asset and asset-ceiling "
             "legs. It must equal the DBO Leg account so the statement of "
             "financial position carries one net plan position.")
    contribution_account_id = fields.Many2one(
        'account.account', string="Contribution Clearing Account",
        tracking=True, check_company=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_current', 'liability_current'])]",
        help="Credited for employer contributions only when payroll or "
             "another system already posted the cash payment.")
    contribution_cash_account_id = fields.Many2one(
        'account.account', string="Contribution Bank Account",
        tracking=True, check_company=True,
        domain="[('account_type', '=', 'asset_cash')]",
        help="Credited when this valuation owns the contribution cash leg "
             "(Contributions Posted Elsewhere is unticked).")
    benefit_payment_account_id = fields.Many2one(
        'account.account', string="Benefit Payment Account", tracking=True,
        check_company=True,
        domain="[('account_type', 'in', "
               "['asset_cash', 'asset_current', 'liability_current'])]",
        help="Unfunded plans only: credited when the employer pays benefits "
             "and settlement amounts directly (cash or a payment clearing "
             "account).")
    journal_id = fields.Many2one(
        'account.journal', string="Journal", tracking=True,
        check_company=True,
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]")

    valuation_ids = fields.One2many(
        'eh.benefit.valuation', 'plan_id', string="Valuations")
    valuation_count = fields.Integer(compute='_compute_valuation_count')

    # Latest posted position, for the register list and the disclosure feed.
    latest_closing_dbo = fields.Monetary(
        compute='_compute_latest_position', currency_field='currency_id',
        string="Closing DBO")
    latest_closing_assets = fields.Monetary(
        compute='_compute_latest_position', currency_field='currency_id',
        string="Closing Plan Assets")
    latest_net_liability = fields.Monetary(
        compute='_compute_latest_position', currency_field='currency_id',
        string="Net Liability / (Asset)")
    latest_recognised_position = fields.Monetary(
        compute='_compute_latest_position', currency_field='currency_id',
        string="Recognised Net Position",
        help="Net liability, or the recognised asset after the asset "
             "ceiling (negative = asset).")

    notes = fields.Text()

    # Accounts and policy flags freeze once a valuation has posted: changing
    # them mid-stream would break the account-level rollforward the
    # disclosures read from the ledger.
    _PLAN_FROZEN_FIELDS = (
        'funded', 'contributions_posted_elsewhere', 'company_id',
        'service_cost_account_id', 'net_interest_account_id',
        'oci_account_id', 'dbo_account_id', 'plan_asset_account_id',
        'contribution_account_id', 'contribution_cash_account_id',
        'benefit_payment_account_id',
        'journal_id',
    )

    @api.constrains(
        'journal_id', 'service_cost_account_id',
        'net_interest_account_id', 'oci_account_id', 'dbo_account_id',
        'plan_asset_account_id', 'contribution_account_id',
        'contribution_cash_account_id',
        'benefit_payment_account_id',
    )
    def _check_accounting_configuration(self):
        """Enforce IAS 19 posting classifications beyond UI domains."""
        self._check_account_classifications()
        self._check_net_presentation()

    def _check_account_classifications(self):
        """Validate account and journal types without changing evidence."""
        for plan in self:
            if plan.journal_id and plan.journal_id.type != 'general':
                raise ValidationError(_(
                    "Employee-benefit valuations must use a general journal."
                ))
            for field_name, allowed_types in _BENEFIT_ACCOUNT_TYPES.items():
                account = plan[field_name]
                if account and account.account_type not in allowed_types:
                    raise ValidationError(_(
                        "%(account)s has classification %(actual)s, which is "
                        "not permitted for %(field)s.",
                        account=account.display_name,
                        actual=account.account_type,
                        field=plan._fields[field_name].string,
                    ))

    def _check_net_presentation(self):
        """Require one balance-sheet account for each funded plan."""
        for plan in self.filtered('funded'):
            if (plan.dbo_account_id and plan.plan_asset_account_id
                    and plan.dbo_account_id != plan.plan_asset_account_id):
                raise ValidationError(_(
                    "Plan %(plan)s must use one identical net-position "
                    "account for its DBO and plan-asset legs (IAS 19.63). "
                    "Separate gross balance-sheet accounts are not "
                    "permitted for new postings.",
                    plan=plan.display_name,
                ))

    def _eh_lock_integrity(self):
        """Serialize configuration edits with valuation transitions.

        Valuation posting locks the plan before the valuation.  Plan edits
        must take the same first lock so an update that started while a post
        was waiting cannot commit against a stale ``no posted valuations``
        decision after that post completes.
        """
        if not self.env.su:
            self._eh_check_access('write')
        ids = sorted(self.ids)
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_benefit_plan WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(ids),),
            )
            self.invalidate_recordset()
            self._check_company()
        return self

    def _compute_valuation_count(self):
        for plan in self:
            plan.valuation_count = len(plan.valuation_ids)

    def _posted_valuations(self):
        self.ensure_one()
        return self.valuation_ids.filtered(
            lambda v: v.state == 'posted').sorted(
            key=lambda v: (v.period_end, v.id))

    def _compute_latest_position(self):
        for plan in self:
            posted = plan._posted_valuations()
            latest = posted[-1] if posted else None
            plan.latest_closing_dbo = latest.closing_dbo if latest else 0.0
            plan.latest_closing_assets = (
                latest.closing_assets if latest else 0.0)
            plan.latest_net_liability = (
                latest.net_liability if latest else 0.0)
            plan.latest_recognised_position = (
                latest.recognised_net_position if latest else 0.0)

    def write(self, vals):
        self._eh_lock_integrity()
        frozen = [f for f in self._PLAN_FROZEN_FIELDS if f in vals]
        if frozen:
            locked = self.filtered(
                lambda p: any(v.state in ('posted', 'reversed')
                              for v in p.valuation_ids))
            if locked:
                raise UserError(_(
                    "Plan settings (%(fields)s) are frozen once a valuation "
                    "has posted on %(plans)s: the disclosure rollforward "
                    "reads these accounts off the ledger. Close the plan "
                    "and open a new one to change its structure.",
                    fields=', '.join(frozen),
                    plans=', '.join(locked.mapped('display_name'))))
        return super().write(vals)

    def unlink(self):
        self._eh_lock_integrity()
        with_valuations = self.filtered(lambda p: p.valuation_ids)
        if with_valuations:
            raise UserError(_(
                "A plan with valuations cannot be deleted (%s); its ledger "
                "chain would be orphaned. Close it instead.",
                ', '.join(with_valuations.mapped('display_name'))))
        return super().unlink()

    # ---- state machine ----

    def _eh_prepare_action(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can change a benefit plan's "
                "lifecycle."
            ))
        self._eh_lock_integrity()
        return self._eh_workflow_action()

    def action_activate(self):
        self = self._eh_prepare_action()
        for plan in self:
            if plan.state != 'draft':
                raise UserError(_(
                    "Only a draft plan can be activated."))
            plan._check_accounting_configuration()
            plan.state = 'active'
        return True

    def action_close(self):
        self = self._eh_prepare_action()
        for plan in self:
            if plan.state != 'active':
                raise UserError(_("Only an active plan can be closed."))
            open_valuations = plan.valuation_ids.filtered(
                lambda v: v.state == 'draft')
            if open_valuations:
                raise UserError(_(
                    "Post or delete the draft valuations of %s before "
                    "closing it.", plan.display_name))
            plan.state = 'closed'
        return True

    def action_reopen(self):
        self = self._eh_prepare_action()
        for plan in self:
            if plan.state != 'closed':
                raise UserError(_("Only a closed plan can be reopened."))
            plan._check_accounting_configuration()
            plan.state = 'active'
        return True

    def action_view_valuations(self):
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'name': _("Valuations"),
            'res_model': 'eh.benefit.valuation',
            'view_mode': 'list,form',
            'domain': [('plan_id', '=', self.id)],
            'context': {'default_plan_id': self.id},
        }

    # ---- disclosure feed (IAS 19.140-141) ----

    def get_rollforward(self):
        """Movement schedules built straight from the posted valuations.

        Returns {'dbo': [...], 'assets': [...], 'ceiling': [...]} where each
        row is one posted period in chronological order. Because every
        posted valuation is (a) tie-checked against its own movement
        analysis and (b) opening-chained to the prior posted valuation, the
        schedule reconciles opening to closing per IAS 19.140-141 by
        construction; nothing here is typed in.
        """
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('read')
        dbo_rows, asset_rows, ceiling_rows = [], [], []
        for v in self._posted_valuations():
            dbo_rows.append({
                'period_end': v.period_end,
                'opening': v.opening_dbo,
                'current_service_cost': v.current_service_cost,
                'past_service_cost': v.past_service_cost,
                'interest_cost': v.interest_cost,
                'benefits_paid': v.benefits_paid,
                'actuarial_loss_gain': v.actuarial_gain_loss_dbo,
                'settlements': v.settlement_dbo_released,
                'closing': v.closing_dbo,
            })
            asset_rows.append({
                'period_end': v.period_end,
                'opening': v.opening_assets,
                'interest_income': v.interest_income,
                'contributions_employer': v.contributions_employer,
                'benefits_paid': v.benefits_paid if v.plan_id.funded else 0.0,
                'return_excess': v.return_on_assets_excess,
                'settlements_paid': (
                    v.settlement_payment if v.plan_id.funded else 0.0),
                'closing': v.closing_assets,
            })
            ceiling_rows.append({
                'period_end': v.period_end,
                'opening_effect': v.opening_ceiling_effect,
                'change_in_effect': v.ceiling_effect_delta,
                'closing_effect': v.ceiling_effect,
                'recognised_asset': v.recognised_asset,
                'net_liability': v.net_liability,
                'recognised_net_position': v.recognised_net_position,
            })
        return {'dbo': dbo_rows, 'assets': asset_rows,
                'ceiling': ceiling_rows}

    def get_disclosure_values(self):
        """Return the rendered IAS 19.140-141 schedule input.

        Gross obligation and plan-asset movements remain visible as required
        disclosure reconciliations.  The statement-of-financial-position
        amount is deliberately one *net* recognised liability or asset per
        plan, after the asset ceiling, as required by IAS 19.63.
        """
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('read')
        periods = []
        for valuation in self._posted_valuations():
            position = valuation.recognised_net_position
            if self.currency_id.is_zero(position):
                presentation_label = _("Nil net defined benefit position")
                presentation_amount = 0.0
            elif position > 0:
                presentation_label = _("Net defined benefit liability")
                presentation_amount = position
            else:
                presentation_label = _("Net defined benefit asset")
                presentation_amount = -position
            periods.append({
                'period_end': valuation.period_end,
                'opening_dbo': valuation.opening_dbo,
                'service_cost': valuation.service_cost_total,
                'interest_cost': valuation.interest_cost,
                'benefits_paid': valuation.benefits_paid,
                'actuarial_loss_gain': valuation.actuarial_gain_loss_dbo,
                'settlement_dbo_released':
                    valuation.settlement_dbo_released,
                'closing_dbo': valuation.closing_dbo,
                'opening_assets': valuation.opening_assets,
                'interest_income': valuation.interest_income,
                'contributions_employer':
                    valuation.contributions_employer,
                'return_excess': valuation.return_on_assets_excess,
                'settlement_payment': valuation.settlement_payment,
                'closing_assets': valuation.closing_assets,
                'ceiling_effect': valuation.ceiling_effect,
                'recognised_net_position': position,
                'presentation_label': presentation_label,
                'presentation_amount': presentation_amount,
            })
        return {
            'plan': self,
            'currency': self.currency_id,
            'periods': periods,
        }

    def action_print_disclosure(self):
        """Render the user-facing IAS 19 movement and net-position report."""
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('read')
        return self.env.ref(
            'eh_account_employee_benefits.action_report_benefit_disclosure'
        ).report_action(self, config=False)
