# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.asset.cgu: IAS 36 cash-generating unit and recoverable-amount engine.

IAS 36 requires an entity, at each reporting date, to assess whether
there is any indication that an asset may be impaired (and, for goodwill
and certain intangibles, to test annually irrespective of indicators).
Where an individual asset does not generate cash inflows that are largely
independent of those from other assets, the recoverable amount is
determined for the cash-generating unit (CGU) to which the asset belongs
(IAS 36.66).

The recoverable amount is the HIGHER of:
  (a) value in use (VIU) -- the present value of the future cash flows
      expected to be derived from the CGU, computed here by discounting a
      projected cash-flow schedule at a pre-tax discount rate; and
  (b) fair value less costs of disposal (FVLCD) -- an entity input.

When the carrying amount of the CGU exceeds its recoverable amount, the
difference is an impairment loss. IAS 36.104 allocates that loss FIRST to
any goodwill allocated to the unit, then pro-rata to the other assets of
the unit on the basis of their carrying amounts.

This engine is an ADDITIONAL, opt-in way to DERIVE the impairment number.
It reuses the existing eh.asset.impairment posting path: the test creates
one draft impairment per allocated asset and posts it (a
segregation-of-duties control point), so every downstream figure -- NBV,
the ledger, the reversal ceiling -- stays consistent with a hand-keyed
impairment. Nothing here changes an asset that is not assigned to a CGU.
"""

import hashlib
import json
import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


_CGU_EVENT_CONTEXT_KEY = 'eh_cgu_test_event_capability'
_CGU_EVENT_CAPABILITY = object()


class EhAssetCgu(models.Model):
    _name = 'eh.asset.cgu'
    _description = "Asset cash-generating unit (IAS 36)"
    _order = 'name, id'
    _inherit = [
        'mail.thread', 'mail.activity.mixin', 'eh.workflow.guard',
    ]

    # A completed test stamps audit evidence consumed by the IAS 36 annual
    # test monitor. Readonly widgets do not protect these fields from RPC;
    # only action_test_now may set them.
    _eh_guarded_fields = (
        'last_test_date', 'last_test_result',
        'currency_mismatch_quarantined', 'currency_mismatch_note',
        'legacy_test_basis_quarantined', 'legacy_test_basis_note',
    )

    def _eh_lock_for_test(self):
        if not self.ids:
            return self
        if not self.env.su:
            self._eh_check_access('write')
        self.env.cr.execute(
            'SELECT id FROM eh_asset_cgu WHERE id IN %s '
            'ORDER BY id FOR UPDATE',
            (tuple(self.ids),),
        )
        self.invalidate_recordset([
            'company_id', 'currency_id', 'currency_mismatch_quarantined',
            'member_ids', 'cashflow_ids', 'discount_rate', 'fair_value',
            'costs_of_disposal', 'carrying_amount', 'value_in_use',
            'fair_value_less_costs', 'recoverable_amount',
            'impairment_shortfall', 'last_test_date', 'last_test_result',
        ])
        return self

    def write(self, vals):
        if vals and not self.env.su:
            self._eh_check_access('write')
        if vals:
            self._eh_lock_for_test()
        if vals and self.filtered('currency_mismatch_quarantined'):
            raise UserError(_(
                "A currency-quarantined CGU is read-only because changing "
                "its recoverable-amount evidence would reinterpret posted "
                "impairments. Recreate the CGU in company currency after "
                "preserving/reversing the linked evidence.",
            ))
        # A legacy mismatch without evidence is intentionally not
        # quarantined. Permit one corrective write that makes company and
        # currency agree; block every other material mutation meanwhile.
        for cgu in self.filtered(
                lambda record: (
                    record.currency_id != record.company_id.currency_id
                )):
            target_company = self.env['res.company'].browse(
                vals.get('company_id') or cgu.company_id.id,
            )
            target_currency = self.env['res.currency'].browse(
                vals.get('currency_id') or cgu.currency_id.id,
            )
            if target_currency != target_company.currency_id:
                raise UserError(_(
                    "Correct the CGU to its company currency before changing "
                    "any valuation input or membership.",
                ))
        if {'company_id', 'currency_id'} & set(vals):
            evidence = self.filtered(lambda cgu: (
                cgu.test_event_ids
                or
                cgu.last_test_date
                or cgu.last_test_result
                or any(
                    impairment.state == 'posted' or impairment.move_id
                    for impairment in cgu.impairment_ids
                )
            ))
            if evidence:
                raise UserError(_(
                    "CGU company and currency are frozen after IAS 36 test or "
                    "impairment evidence exists. Create a new CGU for a "
                    "different measurement scope."
                ))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_for_test()
        evidence = self.filtered(lambda cgu: (
            cgu.currency_mismatch_quarantined
            or cgu.test_event_ids
            or cgu.last_test_date
            or cgu.last_test_result
            or any(
                impairment.state == 'posted' or impairment.move_id
                for impairment in cgu.impairment_ids
            )
        ))
        if evidence:
            raise UserError(_(
                "A CGU with IAS 36 test or posted impairment evidence cannot "
                "be deleted. Archive it so the evidence remains traceable.",
            ))
        return super().unlink()

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
        help="Measurement currency for the recoverable-amount test.",
    )
    currency_mismatch_quarantined = fields.Boolean(
        readonly=True, copy=False, tracking=True,
        help=(
            "Set by the upgrade audit when a legacy CGU with test/posted "
            "impairment evidence uses a non-company currency even though the "
            "engine performed no FX conversion. The CGU remains visible but "
            "read-only."
        ),
    )
    currency_mismatch_note = fields.Text(readonly=True, copy=False)
    legacy_test_basis_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "A pre-upgrade test outcome or impairment exists without the "
            "immutable input/member/cash-flow snapshot introduced in 1.5.2. "
            "The outcome is retained but is not represented as a new event."
        ),
    )
    legacy_test_basis_note = fields.Text(readonly=True, copy=False)

    member_ids = fields.One2many(
        'eh.asset', 'cgu_id',
        string="Member Assets",
        help=(
            "Assets that make up this cash-generating unit. Their "
            "carrying amounts are summed to the CGU carrying amount and "
            "any impairment loss is allocated across them (goodwill "
            "first)."
        ),
    )
    member_count = fields.Integer(
        compute='_compute_member_totals', store=False,
    )
    carrying_amount = fields.Monetary(
        compute='_compute_member_totals', store=False,
        currency_field='currency_id',
        help=(
            "Sum of the net book value of every member asset. The "
            "impairment test compares this to the recoverable amount."
        ),
    )

    # ---- value in use (discounted cash flow) ----
    discount_rate = fields.Float(
        string="Discount Rate (%)",
        digits=(6, 4),
        help=(
            "Pre-tax discount rate applied to the projected cash flows "
            "to derive value in use, expressed as a percent per period "
            "(e.g. 10 for 10%). IAS 36.55 requires a rate that reflects "
            "current market assessments of the time value of money and "
            "the risks specific to the asset."
        ),
    )
    cashflow_ids = fields.One2many(
        'eh.asset.cgu.cashflow', 'cgu_id',
        string="Projected Cash Flows",
        help=(
            "Projected future net cash inflows attributable to the CGU, "
            "one row per period. Discounted at the discount rate to a "
            "present value that is the value in use."
        ),
    )
    value_in_use = fields.Monetary(
        compute='_compute_recoverable', store=False,
        currency_field='currency_id',
        help=(
            "Present value of the projected cash flows discounted at "
            "the discount rate. Zero when no cash flows are projected."
        ),
    )

    # ---- fair value less costs of disposal ----
    fair_value = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Fair value of the CGU (an entity input, e.g. a market or "
            "appraised value), before deducting the costs of disposal."
        ),
    )
    costs_of_disposal = fields.Monetary(
        currency_field='currency_id',
        help="Incremental costs directly attributable to the disposal.",
    )
    fair_value_less_costs = fields.Monetary(
        compute='_compute_recoverable', store=False,
        currency_field='currency_id',
        help="fair_value less costs_of_disposal, floored at zero.",
    )

    recoverable_amount = fields.Monetary(
        compute='_compute_recoverable', store=False,
        currency_field='currency_id',
        help=(
            "Higher of value in use and fair value less costs of "
            "disposal (IAS 36.18)."
        ),
    )
    impairment_shortfall = fields.Monetary(
        compute='_compute_recoverable', store=False,
        currency_field='currency_id',
        help=(
            "carrying_amount less recoverable_amount when positive; "
            "zero otherwise. The amount an impairment test would "
            "allocate across the member assets."
        ),
    )

    # ---- test governance ----
    annual_test_required = fields.Boolean(
        default=False, tracking=True,
        help=(
            "Flags a CGU that must be tested for impairment annually "
            "irrespective of indicators (IAS 36.10: a CGU to which "
            "goodwill has been allocated, or that contains an "
            "indefinite-life intangible). Informational; drives review "
            "filters and reminders."
        ),
    )
    last_test_date = fields.Date(
        readonly=True, tracking=True,
        help="Date the most recent impairment test was run.",
    )
    last_test_result = fields.Selection(
        [
            ('passed', "No impairment"),
            ('impaired', "Impairment recognised"),
        ],
        readonly=True, tracking=True,
        help="Outcome of the most recent impairment test.",
    )
    impairment_ids = fields.One2many(
        'eh.asset.impairment', 'cgu_id',
        string="Allocated Impairments",
        help="Impairment events created by this CGU's tests.",
    )
    test_event_ids = fields.One2many(
        'eh.asset.cgu.test.event', 'cgu_id', string="Immutable Test Evidence",
        readonly=True,
    )

    @api.constrains('currency_id', 'company_id')
    def _check_company_currency(self):
        for cgu in self:
            if (cgu.currency_id and cgu.company_id
                    and cgu.currency_id != cgu.company_id.currency_id):
                raise ValidationError(_(
                    "CGU %(cgu)s must use %(currency)s, the currency of "
                    "company %(company)s. The IAS 36 engine compares and "
                    "allocates its recoverable amount directly against "
                    "member assets; it does not implement FX conversion.",
                    cgu=cgu.display_name,
                    currency=cgu.company_id.currency_id.display_name,
                    company=cgu.company_id.display_name,
                ))
            invalid_members = cgu.member_ids.filtered(lambda asset: (
                asset.company_id != cgu.company_id
                or asset.currency_id != cgu.currency_id
            ))
            if invalid_members:
                raise ValidationError(_(
                    "Every asset in CGU %(cgu)s must use the CGU company and "
                    "currency.", cgu=cgu.display_name,
                ))

    @api.constrains('discount_rate', 'fair_value', 'costs_of_disposal')
    def _check_measurement_inputs(self):
        for cgu in self:
            if not math.isfinite(cgu.discount_rate) or cgu.discount_rate < 0:
                raise ValidationError(_(
                    "CGU discount rate must be finite and cannot be negative."
                ))
            if not all(math.isfinite(value) and value >= 0 for value in (
                    cgu.fair_value, cgu.costs_of_disposal)):
                raise ValidationError(_(
                    "CGU fair value and costs of disposal must be finite and "
                    "cannot be negative."
                ))

    def _eh_validate_company_currency(self):
        for cgu in self:
            if cgu.currency_mismatch_quarantined:
                raise UserError(_(
                    "CGU %(cgu)s is quarantined because legacy IAS 36 "
                    "evidence was calculated in %(source)s while member "
                    "assets post in %(company)s. Existing evidence was not "
                    "rewritten; recreate the CGU in company currency after "
                    "preserving/reversing its linked impairments.",
                    cgu=cgu.display_name,
                    source=cgu.currency_id.display_name,
                    company=cgu.company_id.currency_id.display_name,
                ))
            if cgu.currency_id != cgu.company_id.currency_id:
                raise UserError(_(
                    "CGU %(cgu)s uses %(source)s but company %(company)s "
                    "measures its assets in %(currency)s. Correct the CGU "
                    "currency before running an IAS 36 test.",
                    cgu=cgu.display_name,
                    source=cgu.currency_id.display_name,
                    company=cgu.company_id.display_name,
                    currency=cgu.company_id.currency_id.display_name,
                ))
        return True

    @api.depends(
        'member_ids', 'member_ids.net_book_value',
    )
    def _compute_member_totals(self):
        for cgu in self:
            cgu.member_count = len(cgu.member_ids)
            cgu.carrying_amount = sum(
                cgu.member_ids.mapped('net_book_value'),
            )

    @api.depends(
        'cashflow_ids.amount', 'cashflow_ids.period',
        'discount_rate', 'fair_value', 'costs_of_disposal',
        'member_ids', 'member_ids.net_book_value',
    )
    def _compute_recoverable(self):
        for cgu in self:
            viu = cgu._compute_value_in_use()
            fvlcd = max(
                0.0,
                (cgu.fair_value or 0.0) - (cgu.costs_of_disposal or 0.0),
            )
            fvlcd = cgu.currency_id.round(fvlcd)
            recoverable = max(viu, fvlcd)
            cgu.value_in_use = viu
            cgu.fair_value_less_costs = fvlcd
            cgu.recoverable_amount = recoverable
            shortfall = (cgu.carrying_amount or 0.0) - recoverable
            cgu.impairment_shortfall = cgu.currency_id.round(
                max(0.0, shortfall),
            )

    def _compute_value_in_use(self):
        """Present value of the projected cash flows.

        PV = sum over rows of amount / (1 + r) ** period, with r the
        per-period discount rate and period the (1-based) number of
        periods from the measurement date. Rounded in the CGU currency.
        """
        self.ensure_one()
        rate = (self.discount_rate or 0.0) / 100.0
        pv = 0.0
        for line in self.cashflow_ids:
            period = line.period or 0
            if period <= 0:
                # A period-0 (or unset) flow is undiscounted.
                pv += line.amount or 0.0
                continue
            pv += (line.amount or 0.0) / ((1.0 + rate) ** period)
        return self.currency_id.round(pv)

    # ---- allocation ----

    def _ias36_allocation(self):
        """Return a list of (asset, amount) tuples allocating the
        impairment shortfall across the CGU's member assets.

        IAS 36.104: reduce the carrying amount of any goodwill in the
        unit first; allocate the remainder pro-rata to the other assets
        on the basis of their carrying amount (net book value). IAS 36.105:
        no member may be reduced below its individually assessed floor (the
        highest of individually determinable FVLCD, VIU and zero). Amounts a
        member cannot absorb are redistributed until the exact currency-rounded
        shortfall is allocated, or the test is rejected before any posting when
        aggregate headroom is insufficient.
        """
        self.ensure_one()
        rnd = self.currency_id.round
        shortfall = self.impairment_shortfall
        if shortfall <= 0:
            return []

        allocation = []
        remaining = shortfall

        def headroom(asset):
            return rnd(max(
                0.0,
                asset.net_book_value - (asset.cgu_impairment_floor or 0.0),
            ))

        # Stage 1: goodwill absorbs the loss first, subject to the same
        # IAS 36.105 individual floor.
        goodwill = self.member_ids.filtered(
            lambda a: a.is_goodwill and headroom(a) > 0,
        )
        for asset in goodwill:
            if remaining <= 0:
                break
            take = rnd(min(headroom(asset), remaining))
            if take > 0:
                allocation.append((asset, take))
                remaining = rnd(remaining - take)

        # Stage 2: pro-rata across the remaining (non-goodwill) assets on
        # the basis of their carrying amount.
        if remaining > 0:
            others = self.member_ids.filtered(
                lambda a: not a.is_goodwill and headroom(a) > 0,
            )
            available = rnd(sum(headroom(asset) for asset in others))
            if self.currency_id.compare_amounts(available, remaining) < 0:
                raise UserError(_(
                    "The CGU %(name)s has an impairment shortfall of "
                    "%(amt).2f after goodwill, but its non-goodwill members "
                    "have only %(headroom).2f of carrying amount above their "
                    "IAS 36.105 individual floors. No entry was posted. "
                    "Reassess the member floors, membership, or recoverable-"
                    "amount inputs.",
                    name=self.display_name, amt=remaining, headroom=available,
                ))
            shares = {asset: 0.0 for asset in others}
            residual = remaining
            active = others
            while self.currency_id.compare_amounts(residual, 0.0) > 0:
                base = sum(active.mapped('net_book_value'))
                pass_allocated = 0.0
                for asset in active:
                    room = rnd(headroom(asset) - shares[asset])
                    raw = residual * (asset.net_book_value / base)
                    pass_remaining = rnd(residual - pass_allocated)
                    take = min(room, pass_remaining, max(0.0, rnd(raw)))
                    shares[asset] = rnd(shares[asset] + take)
                    pass_allocated = rnd(pass_allocated + take)
                if self.currency_id.is_zero(pass_allocated):
                    # Sub-cent proportional shares: assign one rounded residue
                    # to the member with most remaining headroom.
                    asset = max(
                        active,
                        key=lambda item: headroom(item) - shares[item],
                    )
                    room = rnd(headroom(asset) - shares[asset])
                    take = min(room, residual)
                    shares[asset] = rnd(shares[asset] + take)
                    pass_allocated = take
                residual = rnd(residual - pass_allocated)
                active = active.filtered(
                    lambda asset: self.currency_id.compare_amounts(
                        headroom(asset) - shares[asset], 0.0,
                    ) > 0,
                )
                if residual > 0 and not active:
                    raise UserError(_(
                        "The CGU loss cannot be allocated without breaching an "
                        "IAS 36.105 individual asset floor. No entry was posted."
                    ))
            for asset, amt in shares.items():
                if amt > 0:
                    allocation.append((asset, amt))

        return allocation

    def _eh_test_basis_payload(self, members, cashflows, test_date,
                               allocation=None):
        """Canonical pre-posting snapshot supporting one IAS 36 decision."""
        self.ensure_one()
        member_rows = []
        for asset in members.sorted('id'):
            posted_depreciation = asset.depreciation_line_ids.filtered(
                lambda line: line.is_posted and line.move_id,
            )
            posted_impairments = asset.impairment_ids.filtered(
                lambda event: event.state == 'posted' and event.move_id,
            )
            member_rows.append({
                'asset_id': asset.id,
                'asset_code': asset.code or '',
                'is_goodwill': bool(asset.is_goodwill),
                'acquisition_cost': asset.acquisition_cost,
                'revaluation_adjustment': asset.revaluation_adjustment,
                'posted_depreciation': sum(posted_depreciation.mapped('amount')),
                'posted_depreciation_move_ids': sorted(
                    posted_depreciation.mapped('move_id').ids,
                ),
                'posted_impairment_move_ids': sorted(
                    posted_impairments.mapped('move_id').ids,
                ),
                'net_book_value': asset.net_book_value,
                'ias36_individual_floor': asset.cgu_impairment_floor,
            })
        return {
            'schema': 'eh.asset.cgu.test.v1',
            'cgu_id': self.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'test_date': fields.Date.to_string(test_date),
            'discount_rate': self.discount_rate,
            'fair_value': self.fair_value,
            'costs_of_disposal': self.costs_of_disposal,
            'cashflows': [
                {
                    'cashflow_id': line.id,
                    'period': line.period,
                    'amount': line.amount,
                    'note': line.note or '',
                }
                for line in cashflows.sorted(lambda line: (line.period, line.id))
            ],
            'members': member_rows,
            'derived': {
                'carrying_amount': self.carrying_amount,
                'value_in_use': self.value_in_use,
                'fair_value_less_costs': self.fair_value_less_costs,
                'recoverable_amount': self.recoverable_amount,
                'impairment_shortfall': self.impairment_shortfall,
            },
            'allocation': [
                {'asset_id': asset.id, 'amount': amount}
                for asset, amount in (allocation or [])
            ],
        }

    def _eh_create_test_event(self, payload, result, impairment_ids=None):
        self.ensure_one()
        basis_json = json.dumps(
            payload, sort_keys=True, separators=(',', ':'),
            ensure_ascii=True,
        )
        return self.env['eh.asset.cgu.test.event'].sudo().with_context(**{
            _CGU_EVENT_CONTEXT_KEY: _CGU_EVENT_CAPABILITY,
        }).create({
            'cgu_id': self.id,
            'company_id': self.company_id.id,
            'currency_id': self.currency_id.id,
            'test_date': payload['test_date'],
            'tested_at': fields.Datetime.now(),
            'tested_by_id': self.env.user.id,
            'result': result,
            'carrying_amount': payload['derived']['carrying_amount'],
            'value_in_use': payload['derived']['value_in_use'],
            'fair_value_less_costs': payload['derived'][
                'fair_value_less_costs'
            ],
            'recoverable_amount': payload['derived']['recoverable_amount'],
            'impairment_shortfall': payload['derived'][
                'impairment_shortfall'
            ],
            'basis_json': basis_json,
            'basis_fingerprint': hashlib.sha256(
                basis_json.encode('utf-8'),
            ).hexdigest(),
            'impairment_ids': [(6, 0, getattr(impairment_ids, 'ids', []))],
        })

    # ---- actions ----

    def action_test_now(self):
        """Indicator-driven impairment test: compare carrying amount to
        recoverable amount and, when carrying exceeds recoverable, derive
        and post the impairment across the member assets.

        This is the opt-in engine. It funnels through the existing
        eh.asset.impairment path (draft create then Post), so posting is
        gated to accounting managers and every allocated entry balances
        by construction. Returns silently (records last_test_result =
        'passed') when the CGU is not impaired.
        """
        if not self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        ):
            raise UserError(_(
                "Only an accounting manager can run a CGU impairment "
                "test that posts impairment charges to the general "
                "ledger. This is a segregation-of-duties control point.",
            ))
        # Check rules on the exact CGUs before any carrying-amount read.
        # Server-owned stamps elevate only through the guarded helper after
        # this access proof, instead of sudoing the whole accounting action.
        self._eh_check_access('write')
        self._eh_lock_for_test()
        Impairment = self.env['eh.asset.impairment']
        today = fields.Date.context_today(self)
        for cgu in self:
            # Read the complete membership under elevation only after the
            # caller proved access to the owning CGU, then prove actor access
            # to every resolved member before measuring or posting anything.
            # Holding the CGU row serialises membership/cash-flow edits; member
            # asset locks serialise every competing carrying-value transition.
            members = self.env['eh.asset'].sudo().search([
                ('cgu_id', '=', cgu.id),
            ], order='id')
            members.sudo(False)._eh_check_access('write')
            members._eh_lock_for_transition()
            cashflows = self.env['eh.asset.cgu.cashflow'].sudo().search([
                ('cgu_id', '=', cgu.id),
            ], order='period, id')
            cashflows.sudo(False)._eh_check_access('read')
            tested_cgu = cgu.sudo()
            tested_cgu._eh_validate_company_currency()
            members._eh_validate_company_currency()
            members.invalidate_recordset(['net_book_value'])
            tested_cgu.invalidate_recordset([
                'carrying_amount', 'recoverable_amount',
                'impairment_shortfall', 'value_in_use',
                'fair_value_less_costs', 'member_ids', 'cashflow_ids',
            ])
            if not members:
                raise UserError(_(
                    "The CGU %s has no member assets to test.",
                ) % cgu.display_name)
            shortfall = tested_cgu.impairment_shortfall
            if shortfall <= 0:
                payload = tested_cgu._eh_test_basis_payload(
                    members, cashflows, today,
                )
                tested_cgu._eh_create_test_event(payload, 'passed')
                cgu._eh_workflow_write({
                    'last_test_date': today,
                    'last_test_result': 'passed',
                })
                # A completed test satisfies the IAS 36.10 annual
                # mandate for every member of the unit.
                members.filtered(
                    'annual_test_overdue',
                )._eh_workflow_write({
                    'annual_test_overdue': False,
                })
                cgu.message_post(body=_(
                    "IAS 36 test: carrying amount %(ca).2f does not "
                    "exceed recoverable amount %(ra).2f (VIU %(viu).2f, "
                    "FVLCD %(fv).2f). No impairment.",
                    ca=tested_cgu.carrying_amount,
                    ra=tested_cgu.recoverable_amount,
                    viu=tested_cgu.value_in_use,
                    fv=tested_cgu.fair_value_less_costs,
                ))
                continue
            allocation = tested_cgu._ias36_allocation()
            payload = tested_cgu._eh_test_basis_payload(
                members, cashflows, today, allocation=allocation,
            )
            reason = _(
                "IAS 36 CGU impairment test on %(name)s. Carrying "
                "amount %(ca).2f exceeds recoverable amount %(ra).2f "
                "(higher of value in use %(viu).2f and fair value less "
                "costs of disposal %(fv).2f). Shortfall %(sf).2f "
                "allocated across the unit (goodwill first, then "
                "pro-rata on carrying amount).",
                name=cgu.display_name, ca=tested_cgu.carrying_amount,
                ra=tested_cgu.recoverable_amount,
                viu=tested_cgu.value_in_use,
                fv=tested_cgu.fair_value_less_costs, sf=shortfall,
            )
            created = Impairment
            for asset, amount in allocation:
                imp = Impairment.create({
                    'asset_id': asset.id,
                    'cgu_id': cgu.id,
                    'impairment_date': today,
                    'amount': amount,
                    'is_reversal': False,
                    'reason': reason,
                })
                created |= imp
            created.action_post()
            tested_cgu._eh_create_test_event(
                payload, 'impaired', impairment_ids=created,
            )
            cgu._eh_workflow_write({
                'last_test_date': today,
                'last_test_result': 'impaired',
            })
            # The write-down brings the unit's carrying amount onto its
            # recoverable amount, so each member's post-test carrying
            # amount IS its allocated share of that recoverable amount
            # (IAS 36.104). Stamp it as the member's latest
            # recoverable-amount measurement (used by the revaluation
            # wizard's uplift cap), and clear the annual-test flag.
            for member in members:
                member.invalidate_recordset(['net_book_value'])
                member._eh_workflow_write({
                    'recoverable_amount_latest': member.net_book_value,
                    'recoverable_amount_date': today,
                    'annual_test_overdue': False,
                })
            cgu.message_post(body=reason)
        return True

    def action_view_impairments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Allocated impairments"),
            'res_model': 'eh.asset.impairment',
            'view_mode': 'list,form',
            'domain': [('cgu_id', '=', self.id)],
        }


class EhAssetCguTestEvent(models.Model):
    _name = 'eh.asset.cgu.test.event'
    _description = "Immutable IAS 36 CGU test evidence"
    _order = 'test_date desc, id desc'

    cgu_id = fields.Many2one(
        'eh.asset.cgu', required=True, ondelete='restrict', index=True,
        check_company=True,
    )
    company_id = fields.Many2one(
        'res.company', required=True, readonly=True, index=True,
    )
    currency_id = fields.Many2one(
        'res.currency', required=True, readonly=True,
    )
    test_date = fields.Date(required=True, readonly=True, index=True)
    tested_at = fields.Datetime(required=True, readonly=True)
    tested_by_id = fields.Many2one(
        'res.users', required=True, readonly=True, ondelete='restrict',
    )
    result = fields.Selection(
        [('passed', "No impairment"), ('impaired', "Impairment recognised")],
        required=True, readonly=True,
    )
    carrying_amount = fields.Monetary(required=True, readonly=True)
    value_in_use = fields.Monetary(required=True, readonly=True)
    fair_value_less_costs = fields.Monetary(required=True, readonly=True)
    recoverable_amount = fields.Monetary(required=True, readonly=True)
    impairment_shortfall = fields.Monetary(required=True, readonly=True)
    basis_json = fields.Text(required=True, readonly=True)
    basis_fingerprint = fields.Char(
        required=True, readonly=True, index=True, size=64,
    )
    impairment_ids = fields.Many2many(
        'eh.asset.impairment', 'eh_asset_cgu_test_impairment_rel',
        'test_event_id', 'impairment_id', readonly=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(_CGU_EVENT_CONTEXT_KEY) \
                is not _CGU_EVENT_CAPABILITY:
            raise AccessError(_(
                "IAS 36 test evidence can only be created by the CGU test "
                "engine."
            ))
        for vals in vals_list:
            payload = vals.get('basis_json') or ''
            expected = hashlib.sha256(payload.encode('utf-8')).hexdigest()
            if vals.get('basis_fingerprint') != expected:
                raise ValidationError(_(
                    "CGU test evidence fingerprint does not match its "
                    "canonical basis snapshot."
                ))
            cgu = self.env['eh.asset.cgu'].browse(vals.get('cgu_id')).exists()
            if not cgu or cgu.company_id.id != vals.get('company_id') \
                    or cgu.currency_id.id != vals.get('currency_id'):
                raise ValidationError(_(
                    "CGU test evidence must retain the exact CGU company and "
                    "currency."
                ))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_(
            "IAS 36 test evidence is append-only and cannot be edited."
        ))

    def unlink(self):
        raise AccessError(_(
            "IAS 36 test evidence is append-only and cannot be deleted."
        ))


class EhAssetCguCashflow(models.Model):
    _name = 'eh.asset.cgu.cashflow'
    _description = "CGU projected cash flow (IAS 36 value in use)"
    _order = 'cgu_id, period, id'

    cgu_id = fields.Many2one(
        'eh.asset.cgu', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='cgu_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='cgu_id.currency_id', store=True, readonly=True,
    )
    period = fields.Integer(
        required=True,
        help=(
            "Number of periods from the measurement date at which this "
            "cash flow occurs (1 = one period out). A period of 0 (or "
            "less) is treated as an undiscounted present-date flow."
        ),
    )
    amount = fields.Monetary(
        required=True, currency_field='currency_id',
        help=(
            "Projected net cash inflow for the period (positive) or "
            "outflow (negative), before discounting."
        ),
    )
    note = fields.Char(help="Optional label for this cash-flow row.")

    _check_period = models.Constraint(
        'CHECK (period >= 0)',
        'Cash-flow period cannot be negative.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        cgu_ids = {
            vals.get('cgu_id') for vals in vals_list if vals.get('cgu_id')
        }
        cgus = self.env['eh.asset.cgu'].browse(cgu_ids)
        if not self.env.su:
            cgus._eh_check_access('write')
        cgus._eh_lock_for_test()
        cgus._eh_validate_company_currency()
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            self._eh_check_access('write')
        cgus = self.mapped('cgu_id')
        if vals.get('cgu_id'):
            cgus |= self.env['eh.asset.cgu'].browse(vals['cgu_id'])
        if not self.env.su:
            cgus._eh_check_access('write')
        cgus._eh_lock_for_test()
        cgus._eh_validate_company_currency()
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        cgus = self.mapped('cgu_id')
        if not self.env.su:
            cgus._eh_check_access('write')
        cgus._eh_lock_for_test()
        cgus._eh_validate_company_currency()
        return super().unlink()
