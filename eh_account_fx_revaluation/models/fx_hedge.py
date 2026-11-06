# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.fx.hedge: IFRS 9 hedge designation and accounting.

Three hedge types per IFRS 9 / IAS 39:

* Cash flow hedge (CFH): hedges variability in cash flows of a
  forecast transaction or a recognised asset / liability with variable
  cash flows. Effective portion goes to OCI (other comprehensive
  income, an equity reserve); ineffective portion goes straight to
  P&L. When the forecast transaction occurs, the OCI reserve is
  reclassified to P&L.

* Fair value hedge (FVH): hedges exposure to changes in fair value of
  a recognised asset, liability, or unrecognised firm commitment. The
  hedging instrument's gain/loss is recognised, and the effective
  portion adjusts the hedged item's carrying amount for the hedged risk
  (booked to the hedged item account), so the net P&L effect in the
  period is the ineffective portion only.

* Net investment hedge (NIH): hedges the FX exposure of a net
  investment in a foreign operation (subsidiary). Effective portion
  goes to OCI under the foreign currency translation reserve (CTA);
  reclassified to P&L on disposal of the foreign operation.

Effectiveness testing per IFRS 9.B6.4:
* Economic relationship between hedged item and hedging instrument.
* Credit risk does not dominate value changes.
* Hedge ratio matches the actual quantities used.

This module ships two quantitative test methods:

* dollar_offset: the cumulative changes must have opposite signs and the
  absolute ratio of hedging-instrument change to hedged-item change must
  fall within 80-125% per the legacy IAS 39 test (still permitted under
  IFRS 9 as one acceptable method).
* regression: linear regression of changes; r-squared must exceed a
  configurable threshold (default 0.80).

The model is designation-only: actual valuation of the hedging
instrument and the hedged item is supplied via the periodic
movement records. Sites using treasury management systems pump the
fair-value changes into eh.fx.hedge.movement records via API.
"""

import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .fx_integrity import (
    FX_BALANCE_SHEET_ACCOUNT_TYPES,
    FX_EQUITY_ACCOUNT_TYPES,
    FX_MOVE_LINK_CAPABILITY,
    FX_PNL_ACCOUNT_TYPES,
    assert_company_allowed,
    lock_report_input_versions,
    require_account_type,
    require_record_company,
    resolve_reference,
)


_HEDGE_TYPES = [
    ('cash_flow', "Cash flow hedge (CFH)"),
    ('fair_value', "Fair value hedge (FVH)"),
    ('net_investment', "Net investment hedge (NIH)"),
]

_TEST_METHODS = [
    ('dollar_offset', "Dollar-offset (80-125%)"),
    ('regression', "Regression (R-squared threshold)"),
]

_HEDGE_FROZEN_FIELDS = frozenset({
    'name', 'hedge_type', 'company_id', 'designation_date',
    'hedged_item_description', 'hedging_instrument_description',
    'notional_amount', 'hedged_currency_id', 'test_method',
    'rsquared_threshold', 'oci_account_id', 'pl_account_id',
    'instrument_account_id', 'hedged_item_account_id', 'cta_position_id',
    'journal_id', 'notes',
})
_HEDGE_EVIDENCE_FIELDS = (
    'state', 'currency_id', 'termination_date',
    'last_test_date', 'last_test_effective',
)
_HEDGE_TEST_OUTPUT_FIELDS = frozenset({
    'company_id', 'currency_id', 'offset_ratio', 'rsquared',
    'slope', 'is_effective',
})
_HEDGE_TEST_INPUT_FIELDS = frozenset({
    'hedge_id', 'test_date', 'method', 'cumulative_instrument_change',
    'cumulative_hedged_change', 'regression_pairs', 'notes',
})


def _eh_finite_number(value, label, minimum=None, maximum=None):
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(_("%s must be a finite number.", label)) from exc
    if not math.isfinite(number):
        raise ValidationError(_("%s must be a finite number.", label))
    if minimum is not None and number < minimum:
        raise ValidationError(_("%(label)s must be at least %(minimum)s.",
                                label=label, minimum=minimum))
    if maximum is not None and number > maximum:
        raise ValidationError(_("%(label)s must not exceed %(maximum)s.",
                                label=label, maximum=maximum))
    return number


def _eh_currency_number(value, currency, label):
    number = _eh_finite_number(value, label)
    rounding = _eh_finite_number(
        currency.rounding if currency else 0.01,
        "Currency rounding",
        minimum=0.0,
    )
    if rounding <= 0.0 or not math.isfinite(number / rounding):
        raise ValidationError(_(
            "%s exceeds the currency's safe numeric range.", label,
        ))
    return number


def _eh_parse_regression_text(value):
    pairs = []
    for line in (value or '').splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(',')
        if len(parts) != 2:
            continue
        try:
            x = float(parts[0].strip())
            y = float(parts[1].strip())
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValidationError(_(
                "Regression pairs must contain finite numeric values."
            ))
        pairs.append((x, y))
    return pairs


class EhFxHedge(models.Model):
    _name = 'eh.fx.hedge'
    _description = "Hedge designation"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'designation_date desc, id desc'

    # The hedge lifecycle is driven by action_designate / action_dedesignate /
    # action_terminate and by _eh_apply_effectiveness (test-driven qualify /
    # suspend). Block any direct write to state that does not originate from
    # one of those (each flags its write).
    _eh_guarded_fields = _HEDGE_EVIDENCE_FIELDS
    _eh_create_guarded_fields = _HEDGE_EVIDENCE_FIELDS

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
        help=(
            "Display name for the hedge relationship. Convention: "
            "'<Type>: <Hedged item> via <Instrument>'."
        ),
    )
    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('designated', "Designated"),
            ('effective', "Effective"),
            ('dedesignated', "De-designated"),
            ('terminated', "Terminated"),
        ],
        default='draft', required=True, tracking=True, index=True,
        help=(
            "Hedge lifecycle. draft: parameters being entered. "
            "designated: formal IFRS 9 designation in place but no "
            "test performed yet. effective: most recent test passed. "
            "de-designated: management voluntarily ended the hedge. "
            "terminated: the underlying instrument expired or was "
            "sold."
        ),
    )
    hedge_type = fields.Selection(
        _HEDGE_TYPES, required=True, default='cash_flow',
        tracking=True,
        help=(
            "IFRS 9 hedge type. Determines whether the effective "
            "portion goes to OCI (CFH, NIH) or P&L (FVH)."
        ),
    )

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company, index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id', store=True, readonly=True,
    )

    designation_date = fields.Date(
        required=True, default=fields.Date.context_today,
        tracking=True,
        help=(
            "Formal designation date. IFRS 9 hedge accounting starts "
            "from this date forward; gains and losses before this "
            "date go to P&L unconditionally."
        ),
    )
    termination_date = fields.Date(
        tracking=True,
        help="Date the hedge relationship ended.",
    )

    # ---- hedged item / hedging instrument ----
    hedged_item_description = fields.Text(
        required=True,
        help=(
            "Free-form description of the hedged item: forecast "
            "USD revenue Q3 2026, EUR-denominated trade receivable "
            "from Acme GmbH, net investment in DE subsidiary, etc. "
            "IFRS 9 requires the hedged item to be reliably "
            "measurable; document the measurement basis here."
        ),
    )
    hedging_instrument_description = fields.Text(
        required=True,
        help=(
            "Free-form description of the hedging instrument: "
            "AUD/USD forward 2026-09-30 notional 1M, USD-denominated "
            "loan, etc."
        ),
    )
    notional_amount = fields.Monetary(
        currency_field='hedged_currency_id',
        help=(
            "Notional amount of the hedging instrument in the hedged "
            "item's currency. Drives the dollar-offset and regression "
            "calculations."
        ),
    )
    hedged_currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
        help=(
            "Currency of the hedged exposure. For CFH on a USD "
            "forecast sale this is USD; for NIH on a EUR subsidiary "
            "this is EUR."
        ),
    )

    # ---- effectiveness testing ----
    test_method = fields.Selection(
        _TEST_METHODS, required=True, default='dollar_offset',
        help=(
            "Quantitative effectiveness test. Dollar-offset checks "
            "the ratio of cumulative gain on the instrument to "
            "cumulative loss on the hedged item falls within "
            "80-125%. Regression performs a linear fit of period "
            "changes and requires R-squared above the threshold."
        ),
    )
    rsquared_threshold = fields.Float(
        default=0.80,
        help=(
            "Minimum R-squared for the regression test. Standard "
            "industry threshold is 0.80; some entities tighten to "
            "0.90 for material hedges."
        ),
    )
    test_ids = fields.One2many(
        'eh.fx.hedge.test', 'hedge_id', copy=False,
        help="Periodic effectiveness test results.",
    )
    last_test_date = fields.Date(
        compute='_compute_last_test', store=True,
        help="Date of the most recent effectiveness test.",
    )
    last_test_effective = fields.Boolean(
        compute='_compute_last_test', store=True,
        help=(
            "True when the most recent test passed. The state "
            "auto-flips to effective on a passing test and stays in "
            "designated otherwise."
        ),
    )

    # ---- accounts ----
    oci_account_id = fields.Many2one(
        'account.account',
        string="OCI / Equity Account",
        check_company=True,
        help=(
            "OCI reserve where the effective portion of CFH and NIH "
            "gains/losses accumulates. Typically an equity-class "
            "account named 'Cash Flow Hedge Reserve' or 'Foreign "
            "Currency Translation Reserve'. Required for CFH and "
            "NIH; ignored for FVH."
        ),
    )
    pl_account_id = fields.Many2one(
        'account.account',
        string="P&L Account",
        check_company=True,
        help=(
            "P&L account where the ineffective portion (and the "
            "entire FVH movement) is recognised. Typically the FX "
            "gain/loss account."
        ),
    )
    instrument_account_id = fields.Many2one(
        'account.account',
        string="Hedging Instrument Account",
        check_company=True,
        help=(
            "Balance-sheet account that carries the hedging "
            "instrument's fair value (e.g. derivative asset / "
            "liability for a forward, FX-loan account)."
        ),
    )
    hedged_item_account_id = fields.Many2one(
        'account.account',
        string="Hedged Item Account",
        check_company=True,
        help=(
            "Balance-sheet account carrying the hedged item (the "
            "recognised asset, liability, or firm commitment). For a "
            "fair-value hedge the effective portion adjusts this "
            "account's carrying amount for the hedged risk, so only "
            "the ineffective portion reaches P&L. Required for FVH; "
            "ignored for CFH and NIH."
        ),
    )
    cta_position_id = fields.Many2one(
        'eh.fx.cta.position', string="CTA Position",
        tracking=True,
        help=(
            "CTA reserve position of the hedged foreign operation "
            "(NIH only). When set, the effective portion posts to the "
            "position's CTA equity account and the entry is tagged to "
            "the position, so the position balance is ledger-fed and "
            "the parked amounts are recycled to P&L by disposing the "
            "position (IAS 21.48), not per movement."
        ),
    )
    journal_id = fields.Many2one(
        'account.journal', string="Journal",
        check_company=True,
        help="Journal used to post hedge movements.",
    )

    movement_ids = fields.One2many(
        'eh.fx.hedge.movement', 'hedge_id', copy=False,
        help="Periodic gain/loss recognition records.",
    )
    movement_count = fields.Integer(
        compute='_compute_counts', store=False,
    )
    test_count = fields.Integer(
        compute='_compute_counts', store=False,
    )

    notes = fields.Text(
        help=(
            "Hedge documentation: risk management objective, strategy, "
            "method of measuring effectiveness, sources of "
            "ineffectiveness. IFRS 9 requires this to be in place at "
            "designation."
        ),
    )

    @api.depends('test_ids.test_date', 'test_ids.is_effective')
    def _compute_last_test(self):
        for hedge in self:
            tests = hedge.test_ids.sorted(
                key=lambda test: (test.test_date, test.id), reverse=True,
            )
            if tests:
                hedge.last_test_date = tests[0].test_date
                hedge.last_test_effective = tests[0].is_effective
            else:
                hedge.last_test_date = False
                hedge.last_test_effective = False

    @api.depends('movement_ids', 'test_ids')
    def _compute_counts(self):
        for hedge in self:
            hedge.movement_count = len(hedge.movement_ids)
            hedge.test_count = len(hedge.test_ids)

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get([
            'notional_amount', 'rsquared_threshold', 'hedged_currency_id',
        ])
        for vals in vals_list:
            self._eh_validate_numeric_values(dict(defaults, **vals))
            assert_company_allowed(
                self.env,
                vals.get('company_id') or self.env.company.id,
                _("FX hedge"),
            )
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code(
                    'eh.fx.hedge',
                ) or '/'
                vals['name'] = seq
        hedges = super().create(vals_list)
        for hedge in hedges:
            hedge._eh_validate_numeric_values({}, current=hedge)
        hedges._eh_validate_references()
        return hedges

    def write(self, vals):
        if vals and not self.env.su:
            self._eh_check_access('write')
        for hedge in self:
            self._eh_validate_numeric_values(vals, current=hedge)
        self._eh_validate_references(vals)
        self._eh_lock_for_transition(vals)
        for hedge in self:
            self._eh_validate_numeric_values(vals, current=hedge)
        self._eh_validate_references(vals)
        if not self.env.su:
            frozen = self.filtered(lambda hedge: hedge.state != 'draft')
            touched = sorted(_HEDGE_FROZEN_FIELDS.intersection(vals))
            if frozen and touched:
                raise UserError(_(
                    "Designated hedge accounting basis is immutable "
                    "(%(fields)s). De-designate and create a documented new "
                    "designation instead of rewriting signed evidence.",
                    fields=', '.join(touched),
                ))
        result = super().write(vals)
        for hedge in self:
            self._eh_validate_numeric_values({}, current=hedge)
        self._eh_validate_references()
        return result

    @api.model
    def _eh_validate_numeric_values(self, vals, current=None):
        currency_id = vals.get(
            'hedged_currency_id',
            current.hedged_currency_id.id if current else False,
        )
        currency = self.env['res.currency'].browse(currency_id)
        notional = vals.get(
            'notional_amount', current.notional_amount if current else 0.0,
        )
        _eh_currency_number(notional, currency, _("Hedge notional amount"))
        if float(notional or 0.0) < 0.0:
            raise ValidationError(_(
                "Hedge notional amount must be non-negative."
            ))
        _eh_finite_number(
            vals.get(
                'rsquared_threshold',
                current.rsquared_threshold if current else 0.80,
            ),
            _("R-squared threshold"),
            minimum=0.0,
            maximum=1.0,
        )
        return True

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        evidence = self.filtered(
            lambda hedge: hedge.state != 'draft'
            or hedge.test_ids or hedge.movement_ids)
        if evidence:
            raise UserError(_(
                "A designated or evidenced hedge cannot be deleted; retain "
                "its audit trail and de-designate or terminate it."))
        return super().unlink()

    @api.constrains(
        'hedge_type', 'oci_account_id', 'pl_account_id',
        'hedged_item_account_id', 'cta_position_id',
    )
    def _check_account_setup(self):
        for hedge in self:
            if hedge.state == 'draft':
                continue
            if hedge.hedge_type in ('cash_flow', 'net_investment'):
                # NIH may source its OCI account from the linked CTA
                # position instead of a direct OCI account.
                has_reserve = hedge.oci_account_id or (
                    hedge.hedge_type == 'net_investment'
                    and hedge.cta_position_id
                )
                if not has_reserve:
                    raise ValidationError(_(
                        "Hedge %s requires an OCI / equity account "
                        "(or, for NIH, a CTA position) to receive the "
                        "effective portion of movements (CFH and NIH "
                        "route the effective portion through OCI per "
                        "IFRS 9).",
                    ) % hedge.display_name)
            if hedge.hedge_type == 'fair_value' \
                    and not hedge.hedged_item_account_id:
                raise ValidationError(_(
                    "Hedge %s is a fair-value hedge and requires a "
                    "Hedged Item Account: the effective portion adjusts "
                    "the hedged item's carrying amount, so only the "
                    "ineffective portion reaches P&L (IFRS 9 6.5.8).",
                ) % hedge.display_name)
            if not hedge.pl_account_id:
                raise ValidationError(_(
                    "Hedge %s requires a P&L account for the "
                    "ineffective portion (and for the full FVH "
                    "movement when applicable).",
                ) % hedge.display_name)

    @api.constrains('cta_position_id', 'company_id', 'hedge_type')
    def _check_cta_position(self):
        for hedge in self:
            if not hedge.cta_position_id:
                continue
            if hedge.hedge_type != 'net_investment':
                raise ValidationError(_(
                    "Hedge %s: only a net investment hedge can link a "
                    "CTA position. CFH and FVH effective portions do "
                    "not accumulate in the translation reserve.",
                ) % hedge.display_name)
            if hedge.cta_position_id.company_id != hedge.company_id:
                raise ValidationError(_(
                    "Hedge %(hedge)s and CTA position %(pos)s belong "
                    "to different companies.",
                    hedge=hedge.display_name,
                    pos=hedge.cta_position_id.display_name,
                ))

    def _eh_check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can approve hedge "
                "designation, testing, or lifecycle changes."))

    def _eh_validate_references(self, vals=None):
        vals = vals or {}
        for hedge in self:
            company_id = vals.get('company_id', hedge.company_id.id)
            assert_company_allowed(self.env, company_id, _("FX hedge"))
            company = resolve_reference(
                self.env, 'res.company', company_id,
                _("FX hedge company"), access='read')
            journal_id = vals.get('journal_id', hedge.journal_id.id)
            journal = resolve_reference(
                self.env, 'account.journal', journal_id,
                _("FX hedge journal"), access='read')
            require_record_company(
                journal, company, _("FX hedge journal"))
            if journal and journal.type != 'general':
                raise ValidationError(_(
                    "FX hedge movements require a general journal."
                ))
            for field_name, label, allowed_types in (
                ('oci_account_id', _("FX hedge OCI account"),
                 FX_EQUITY_ACCOUNT_TYPES),
                ('pl_account_id', _("FX hedge P&L account"),
                 FX_PNL_ACCOUNT_TYPES),
                ('instrument_account_id', _("FX hedge instrument account"),
                 FX_BALANCE_SHEET_ACCOUNT_TYPES),
                ('hedged_item_account_id', _("FX hedged-item account"),
                 FX_BALANCE_SHEET_ACCOUNT_TYPES),
            ):
                account_id = vals.get(field_name, hedge[field_name].id)
                account = resolve_reference(
                    self.env, 'account.account', account_id,
                    label, access='read')
                require_record_company(account, company, label)
                require_account_type(account, allowed_types, label)
            cta_id = vals.get('cta_position_id', hedge.cta_position_id.id)
            cta = resolve_reference(
                self.env, 'eh.fx.cta.position', cta_id,
                _("FX hedge CTA position"), access='write')
            require_record_company(
                cta, company, _("FX hedge CTA position"), exact=True)
            resolve_reference(
                self.env, 'res.currency', vals.get(
                    'hedged_currency_id', hedge.hedged_currency_id.id),
                _("FX hedge currency"), access='read')
        return self

    def _eh_lock_for_transition(self, vals=None):
        vals = vals or {}
        expected_links = {
            hedge.id: (
                hedge.company_id.id,
                hedge.cta_position_id.id or False,
            )
            for hedge in self
        }
        cta_ids = set(self.mapped('cta_position_id').ids)
        if vals.get('cta_position_id'):
            cta_ids.add(int(vals['cta_position_id']))
        company_ids = set(self.mapped('company_id').ids)
        if vals.get('company_id'):
            company_ids.add(int(vals['company_id']))
        if cta_ids:
            self.env['eh.fx.cta.position'].browse(
                sorted(cta_ids),
            )._eh_lock_for_transition(
                extra_company_ids=company_ids,
            )
        elif company_ids:
            lock_report_input_versions(self.env, company_ids)
        ids = tuple(sorted(set(self.ids)))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id, company_id, cta_position_id "
                "FROM eh_fx_hedge WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            locked_rows = self.env.cr.fetchall()
            if {row[0] for row in locked_rows} != set(ids):
                raise UserError(_(
                    "A hedge changed concurrently. Retry the operation."
                ))
            if any(
                expected_links.get(hedge_id) != (
                    company_id, cta_position_id or False,
                )
                for hedge_id, company_id, cta_position_id in locked_rows
            ):
                raise UserError(_(
                    "A hedge changed company or CTA position concurrently. "
                    "Retry the operation."
                ))
            self.invalidate_recordset()
        return self

    def _eh_prepare_action(self, manager=True):
        if not self.env.su:
            self._eh_check_access('write')
        if manager:
            self._eh_check_manager()
        self._eh_validate_references()
        self._eh_lock_for_transition()
        for hedge in self:
            hedge._eh_validate_numeric_values({}, current=hedge)
        self._eh_validate_references()
        return self._eh_workflow_action()

    def _eh_apply_effectiveness(self):
        """Sync the hedge state to its latest effectiveness test.

        A passing test on an active hedge qualifies it (state -> effective);
        a failing test suspends hedge accounting (effective -> designated).
        IFRS 9 permits the OCI / hedged-item deferral only while the hedge
        qualifies, so movement posting reads this state. Terminated and
        de-designated hedges are never auto-reactivated.
        """
        candidates = self.filtered(
            lambda hedge: hedge.state in ('designated', 'effective')
        )
        if not candidates:
            return
        hedges = candidates._eh_prepare_action(manager=True)
        for hedge in hedges.sorted('id'):
            if hedge.last_test_effective and hedge.state != 'effective':
                hedge.state = 'effective'
                hedge.message_post(body=_(
                    "Hedge qualifies: effectiveness test on %s passed.",
                ) % (hedge.last_test_date or ''))
            elif not hedge.last_test_effective and hedge.state == 'effective':
                hedge.state = 'designated'
                hedge.message_post(body=_(
                    "Hedge no longer qualifies: latest effectiveness test "
                    "failed. Hedge accounting is suspended until a passing "
                    "test is recorded; movements in the meantime recognise "
                    "the full change in P&L."
                ))

    # ---- actions ----

    def action_designate(self):
        with self.env.cr.savepoint():
            hedges = self._eh_prepare_action(manager=True)
            hedges._eh_action_designate_locked()
        return True

    def _eh_action_designate_locked(self):
        for hedge in self.sorted('id'):
            if hedge.state != 'draft':
                raise UserError(_(
                    "Only draft hedges can be designated.",
                ))
            if not hedge.notes:
                raise UserError(_(
                    "Hedge %s needs documented risk management "
                    "objective and strategy in the Notes field "
                    "before designation. IFRS 9 paragraph 6.4.1(b) "
                    "requires this at inception.",
                ) % hedge.display_name)
            if hedge.test_ids:
                raise UserError(_(
                    "Draft hedge %s already carries pre-designation "
                    "effectiveness evidence. Remove or remediate that legacy "
                    "evidence before designation; it cannot qualify a hedge "
                    "retroactively.", hedge.display_name))
            has_reserve = hedge.oci_account_id or (
                hedge.hedge_type == 'net_investment'
                and hedge.cta_position_id
            )
            if hedge.hedge_type in ('cash_flow', 'net_investment') \
                    and not has_reserve:
                raise ValidationError(_(
                    "Hedge %s requires an OCI / equity account (or, "
                    "for NIH, a CTA position) to receive the "
                    "effective portion of movements (CFH and NIH "
                    "route the effective portion through OCI per "
                    "IFRS 9).",
                ) % hedge.display_name)
            if hedge.hedge_type == 'fair_value' \
                    and not hedge.hedged_item_account_id:
                raise ValidationError(_(
                    "Hedge %s is a fair-value hedge and requires a "
                    "Hedged Item Account so the effective portion can "
                    "adjust the hedged item's carrying amount (IFRS 9 "
                    "6.5.8).",
                ) % hedge.display_name)
            if not hedge.pl_account_id:
                raise ValidationError(_(
                    "Hedge %s requires a P&L account for the "
                    "ineffective portion (and for the full FVH "
                    "movement when applicable).",
                ) % hedge.display_name)
            hedge.state = 'designated'
            hedge.message_post(body=_(
                "Hedge designated on %s.",
            ) % hedge.designation_date)

    def action_dedesignate(self):
        with self.env.cr.savepoint():
            hedges = self._eh_prepare_action(manager=True)
            hedges._eh_action_dedesignate_locked()
        return True

    def _eh_action_dedesignate_locked(self):
        for hedge in self.sorted('id'):
            if hedge.state not in ('designated', 'effective'):
                raise UserError(_(
                    "Only active hedges can be de-designated.",
                ))
            hedge.write({
                'state': 'dedesignated',
                'termination_date': fields.Date.context_today(self),
            })
            hedge.message_post(body=_(
                "Hedge de-designated. Subsequent movements on the "
                "hedging instrument go straight to P&L; the OCI "
                "reserve stays until the original hedged transaction "
                "occurs (CFH) or the foreign operation is disposed "
                "(NIH)."
            ))

    def action_terminate(self):
        with self.env.cr.savepoint():
            hedges = self._eh_prepare_action(manager=True)
            hedges._eh_action_terminate_locked()
        return True

    def _eh_action_terminate_locked(self):
        for hedge in self.sorted('id'):
            if hedge.state == 'terminated':
                continue
            hedge.write({
                'state': 'terminated',
                'termination_date': fields.Date.context_today(self),
            })

    def action_view_tests(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Effectiveness Tests"),
            'res_model': 'eh.fx.hedge.test',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('hedge_id', '=', self.id)],
            'context': {'default_hedge_id': self.id},
        }

    def action_view_movements(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Movements"),
            'res_model': 'eh.fx.hedge.movement',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': [('hedge_id', '=', self.id)],
            'context': {'default_hedge_id': self.id},
        }


class EhFxHedgeTest(models.Model):
    _name = 'eh.fx.hedge.test'
    _description = "Hedge effectiveness test"
    _order = 'hedge_id, test_date desc'

    hedge_id = fields.Many2one(
        'eh.fx.hedge', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='hedge_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='hedge_id.currency_id', store=True, readonly=True,
    )

    test_date = fields.Date(
        required=True, default=fields.Date.context_today,
    )
    method = fields.Selection(
        _TEST_METHODS, required=True, default='dollar_offset',
    )

    # Dollar-offset inputs
    cumulative_instrument_change = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Cumulative fair-value change of the hedging instrument "
            "since designation. Positive = gain on instrument."
        ),
    )
    cumulative_hedged_change = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Cumulative fair-value change of the hedged item since "
            "designation. Positive = gain on hedged item. For CFH "
            "this is the variability in expected future cash flows."
        ),
    )

    offset_ratio = fields.Float(
        compute='_compute_results', store=True, digits=(7, 4),
        help=(
            "Absolute ratio of |instrument change| to |hedged "
            "change|. The changes must also have opposite signs, and the "
            "ratio must fall within 0.80-1.25, to pass the dollar-offset "
            "test."
        ),
    )

    # Regression inputs (delimited list of paired changes)
    regression_pairs = fields.Text(
        help=(
            "Newline-delimited 'instrument_change,hedged_change' "
            "pairs across testing periods. Used by the regression "
            "method to compute R-squared."
        ),
    )
    rsquared = fields.Float(
        compute='_compute_results', store=True, digits=(7, 4),
        help="R-squared of the linear regression on regression_pairs.",
    )
    slope = fields.Float(
        compute='_compute_results', store=True, digits=(7, 4),
        help=(
            "Slope of the regression of hedged-item change on hedging-"
            "instrument change. An effective hedge offsets the hedged "
            "item, so the slope must be negative and close to -1 (its "
            "absolute value within 0.80 to 1.25). R-squared alone is "
            "sign-blind, so the slope guards against a positively "
            "correlated, non-hedging relationship."
        ),
    )

    is_effective = fields.Boolean(
        compute='_compute_results', store=True,
        help=(
            "True when the test passes. Dollar-offset: opposite-signed "
            "changes and ratio in [0.80, 1.25]. Regression: R-squared >= "
            "threshold on the parent hedge."
        ),
    )
    notes = fields.Text()

    @api.depends(
        'method', 'cumulative_instrument_change',
        'cumulative_hedged_change', 'regression_pairs',
        'hedge_id.rsquared_threshold',
    )
    def _compute_results(self):
        for test in self:
            if test.method == 'dollar_offset':
                hedged_change = test.cumulative_hedged_change or 0.0
                instrument_change = (
                    test.cumulative_instrument_change or 0.0
                )
                hedged = abs(hedged_change)
                inst = abs(instrument_change)
                test.slope = 0.0
                if hedged == 0:
                    test.offset_ratio = 0.0
                    test.rsquared = 0.0
                    test.is_effective = False
                    continue
                ratio = inst / hedged
                if not math.isfinite(ratio):
                    raise ValidationError(_(
                        "Dollar-offset inputs produce a non-finite ratio."
                    ))
                test.offset_ratio = round(ratio, 4)
                test.rsquared = 0.0
                test.is_effective = (
                    instrument_change * hedged_change < 0.0
                    and 0.80 <= ratio <= 1.25
                )
            else:
                pairs = test._parse_regression_pairs()
                test.offset_ratio = 0.0
                if len(pairs) < 2:
                    test.rsquared = 0.0
                    test.slope = 0.0
                    test.is_effective = False
                    continue
                rsq = test._compute_rsquared(pairs)
                slope = test._regression_slope(pairs)
                test.rsquared = round(rsq, 4)
                test.slope = round(slope, 4)
                threshold = (
                    test.hedge_id.rsquared_threshold or 0.80
                )
                # IFRS 9 regression effectiveness needs more than a high
                # R-squared: the slope must be negative and close to -1,
                # because the hedging instrument has to move opposite to
                # the hedged item. R-squared is sign-blind, so a positively
                # correlated (non-hedging) pair would otherwise pass at
                # R-squared near 1. The 0.80 to 1.25 band on the absolute
                # slope mirrors the dollar-offset band.
                test.is_effective = (
                    rsq >= threshold
                    and slope < 0.0
                    and 0.80 <= abs(slope) <= 1.25
                )

    @api.model_create_multi
    def create(self, vals_list):
        with self.env.cr.savepoint():
            return self._eh_create_locked(vals_list)

    @api.model
    def _eh_create_locked(self, vals_list):
        if any(_HEDGE_TEST_OUTPUT_FIELDS.intersection(vals)
               for vals in vals_list) and not self.env.su:
            raise AccessError(_(
                "Hedge effectiveness results are engine-owned."))
        defaults = self.default_get([
            'hedge_id', 'cumulative_instrument_change',
            'cumulative_hedged_change', 'regression_pairs',
        ])
        effective_vals = [dict(defaults, **vals) for vals in vals_list]
        hedges = self.env['eh.fx.hedge'].browse({
            vals.get('hedge_id') for vals in effective_vals
            if vals.get('hedge_id')
        })
        hedges._eh_check_access('write')
        hedges._eh_check_manager()
        hedges._eh_validate_references()
        hedges._eh_lock_for_transition()
        hedges.invalidate_recordset(['state', 'designation_date'])
        for vals in effective_vals:
            hedge = self.env['eh.fx.hedge'].browse(vals.get('hedge_id')).exists()
            if not hedge or hedge.state not in ('designated', 'effective'):
                raise UserError(_(
                    "Effectiveness evidence can only be recorded after the "
                    "hedge has been formally designated."))
            test_date = fields.Date.to_date(
                vals.get('test_date') or fields.Date.context_today(self))
            if test_date < hedge.designation_date:
                raise ValidationError(_(
                    "An effectiveness test cannot predate hedge designation "
                    "(%s).", hedge.designation_date))
            self._eh_validate_numeric_values(
                dict(defaults, **vals), hedge=hedge,
            )
        tests = super().create(vals_list)
        # Recording a test drives the parent hedge's qualification state so
        # movement posting can enforce that OCI / hedged-item deferral only
        # happens while a passing test is in force.
        tests.hedge_id._eh_apply_effectiveness()
        return tests

    def write(self, vals):
        with self.env.cr.savepoint():
            return self._eh_write_locked(vals)

    def _eh_write_locked(self, vals):
        if vals and not self.env.su:
            self._eh_check_access('write')
        if _HEDGE_TEST_OUTPUT_FIELDS.intersection(vals) and not self.env.su:
            raise AccessError(_(
                "Hedge effectiveness results are engine-owned."))
        hedges = self.mapped('hedge_id')
        if vals.get('hedge_id'):
            hedges |= self.env['eh.fx.hedge'].browse(vals['hedge_id'])
        hedges._eh_check_access('write')
        hedges._eh_check_manager()
        hedges._eh_validate_references()
        hedges._eh_lock_for_transition()
        target_hedge = (
            self.env['eh.fx.hedge'].browse(vals['hedge_id']).exists()
            if vals.get('hedge_id') else self.env['eh.fx.hedge']
        )
        for test in self:
            hedge = target_hedge or test.hedge_id
            self._eh_validate_numeric_values(vals, current=test, hedge=hedge)
            if hedge.state not in ('designated', 'effective'):
                raise UserError(_(
                    "Effectiveness evidence belongs to a formally designated "
                    "hedge and cannot be moved onto a draft or closed hedge."))
            test_date = fields.Date.to_date(
                vals.get('test_date') or test.test_date)
            if test_date < hedge.designation_date:
                raise ValidationError(_(
                    "An effectiveness test cannot predate hedge designation "
                    "(%s).", hedge.designation_date))
        if _HEDGE_TEST_INPUT_FIELDS.intersection(vals):
            if hedges.filtered(lambda hedge: hedge.state != 'draft'):
                raise UserError(_(
                    "An effectiveness test on a designated hedge is "
                    "append-only. Record a new test instead of rewriting "
                    "the qualification evidence."))
        res = super().write(vals)
        for test in self:
            self._eh_validate_numeric_values({}, current=test)
        self.hedge_id._eh_apply_effectiveness()
        return res

    @api.model
    def _eh_validate_numeric_values(self, vals, current=None, hedge=None):
        hedge = hedge or (current.hedge_id if current else None)
        currency = hedge.company_id.currency_id if hedge else None
        for field_name, label in (
            ('cumulative_instrument_change', _("Instrument change")),
            ('cumulative_hedged_change', _("Hedged-item change")),
        ):
            value = vals.get(
                field_name, current[field_name] if current else 0.0,
            )
            _eh_currency_number(value, currency, label)
        _eh_parse_regression_text(vals.get(
            'regression_pairs',
            current.regression_pairs if current else False,
        ))
        return True

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        hedges = self.mapped('hedge_id')
        hedges._eh_check_access('write')
        if hedges.filtered(lambda hedge: hedge.state != 'draft'):
            hedges._eh_check_manager()
        hedges._eh_lock_for_transition()
        if self.filtered(lambda test: test.hedge_id.state != 'draft'):
            raise UserError(_(
                "Effectiveness tests on designated hedges are immutable "
                "audit evidence and cannot be deleted."))
        return super().unlink()

    def _parse_regression_pairs(self):
        """Parse regression_pairs into a list of (x, y) tuples.

        Lines that do not parse cleanly are skipped silently so a
        stray blank line does not break the test.
        """
        self.ensure_one()
        return _eh_parse_regression_text(self.regression_pairs)

    @staticmethod
    def _compute_rsquared(pairs):
        """Linear regression coefficient of determination.

        Pure Python (no numpy dep). Returns 0.0 when the variance of
        either series is zero so the test fails closed.
        """
        n = len(pairs)
        if n < 2:
            return 0.0
        scale_x = max(abs(x) for x, _ in pairs)
        scale_y = max(abs(y) for _, y in pairs)
        if scale_x == 0 or scale_y == 0:
            return 0.0
        normalised = [(x / scale_x, y / scale_y) for x, y in pairs]
        mean_x = math.fsum(x for x, _ in normalised) / n
        mean_y = math.fsum(y for _, y in normalised) / n
        ss_xx = math.fsum((x - mean_x) ** 2 for x, _ in normalised)
        ss_yy = math.fsum((y - mean_y) ** 2 for _, y in normalised)
        ss_xy = math.fsum(
            (x - mean_x) * (y - mean_y) for x, y in normalised
        )
        if ss_xx == 0 or ss_yy == 0:
            return 0.0
        r = ss_xy / ((ss_xx * ss_yy) ** 0.5)
        return r * r

    @staticmethod
    def _regression_slope(pairs):
        """Slope of the linear regression of hedged-item change (y) on
        hedging-instrument change (x).

        For an effective hedge the instrument offsets the hedged item, so
        the slope must be negative. Pure Python (no numpy). Returns 0.0
        when the instrument series has zero variance.
        """
        n = len(pairs)
        if n < 2:
            return 0.0
        scale_x = max(abs(x) for x, _ in pairs)
        scale_y = max(abs(y) for _, y in pairs)
        if scale_x == 0 or scale_y == 0:
            return 0.0
        normalised = [(x / scale_x, y / scale_y) for x, y in pairs]
        mean_x = math.fsum(x for x, _ in normalised) / n
        mean_y = math.fsum(y for _, y in normalised) / n
        ss_xx = math.fsum((x - mean_x) ** 2 for x, _ in normalised)
        ss_xy = math.fsum(
            (x - mean_x) * (y - mean_y) for x, y in normalised
        )
        if ss_xx == 0:
            return 0.0
        slope = (ss_xy / ss_xx) * (scale_y / scale_x)
        if not math.isfinite(slope):
            raise ValidationError(_(
                "Regression inputs produce a non-finite slope."
            ))
        return slope


class EhFxHedgeMovement(models.Model):
    _name = 'eh.fx.hedge.movement'
    _description = "Hedge movement (period gain/loss recognition)"
    _order = 'hedge_id, movement_date desc, id desc'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']

    # A movement reaches 'posted' / 'reclassified' only through action_post /
    # action_reclassify_to_pl, which attach its journal entry. Block any direct
    # write to state that does not originate from those (which flag the write).
    _eh_guarded_fields = (
        'state', 'hedge_type', 'company_id', 'currency_id',
        'ineffective_portion', 'move_id', 'reclassification_move_id',
        'posted_at', 'posted_by_id',
    )
    _eh_create_guarded_fields = _eh_guarded_fields

    hedge_id = fields.Many2one(
        'eh.fx.hedge', required=True, ondelete='cascade', index=True,
    )
    # Surfaced as a direct related field so view modifiers can
    # reference it without a dotted path (Odoo 16 rejects composed
    # fields in attrs).
    hedge_type = fields.Selection(
        related='hedge_id.hedge_type', store=True, readonly=True,
    )
    company_id = fields.Many2one(
        related='hedge_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='hedge_id.currency_id', store=True, readonly=True,
    )

    movement_date = fields.Date(
        required=True, default=fields.Date.context_today,
        tracking=True,
    )
    total_change = fields.Monetary(
        required=True, currency_field='currency_id', tracking=True,
        help=(
            "Total fair-value change of the hedging instrument for "
            "this period (positive = gain on instrument)."
        ),
    )
    effective_portion = fields.Monetary(
        required=True, currency_field='currency_id', tracking=True,
        help=(
            "Portion considered effective per the most recent "
            "effectiveness test. For CFH and NIH, OCI is capped at the "
            "lower of instrument and hedged-item changes. For FVH, the "
            "separately measured hedged-item change supplies the basis "
            "adjustment and both measured changes are recognised in P&L."
        ),
    )
    hedged_item_change = fields.Monetary(
        required=True, default=0.0, currency_field='currency_id', tracking=True,
        help=(
            "Measured change in the hedged item attributable to the hedged "
            "risk for this period. Use the opposite sign to the instrument "
            "change. It caps CFH/NIH deferral under IFRS 9 6.5.11(a) and is "
            "posted as the FVH hedged-item carrying-amount basis adjustment."
        ),
    )
    ineffective_portion = fields.Monetary(
        compute='_compute_ineffective', store=True,
        currency_field='currency_id',
        help=(
            "CFH/NIH: total change minus effective portion. FVH: instrument "
            "change plus opposite-signed hedged-item basis adjustment."
        ),
    )

    state = fields.Selection(
        [
            ('draft', "Draft"),
            ('posted', "Posted"),
            ('reclassified', "Reclassified to P&L"),
        ],
        default='draft', required=True, tracking=True, index=True,
    )
    move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        help="JE posted for the period gain/loss.",
    )
    reclassification_move_id = fields.Many2one(
        'account.move', readonly=True, copy=False, ondelete='restrict',
        help=(
            "JE that reclassifies the OCI portion to P&L when the "
            "hedged transaction occurs (CFH) or the foreign "
            "operation is disposed (NIH)."
        ),
    )
    reclassification_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
        help=(
            "Accounting date to use if this OCI movement is later "
            "reclassified to profit or loss. It must not precede movement "
            "date and remains editable until reclassification."
        ),
    )

    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)

    notes = fields.Text()

    @api.depends(
        'total_change', 'effective_portion', 'hedged_item_change', 'hedge_type',
    )
    def _compute_ineffective(self):
        for mvt in self:
            if mvt.hedge_type == 'fair_value':
                # FVH recognises both measured changes in P&L while the hedged
                # item side also changes its carrying amount. Opposite signs
                # therefore net to actual ineffectiveness.
                mvt.ineffective_portion = (
                    (mvt.total_change or 0.0)
                    + (mvt.hedged_item_change or 0.0)
                )
            else:
                mvt.ineffective_portion = (
                    (mvt.total_change or 0.0)
                    - (mvt.effective_portion or 0.0)
                )

    @api.constrains(
        'effective_portion', 'total_change', 'hedged_item_change',
        'movement_date', 'reclassification_date',
    )
    def _check_effective_le_total(self):
        for mvt in self:
            currency = mvt.currency_id or mvt.company_id.currency_id
            effective = abs(mvt.effective_portion or 0.0)
            total = abs(mvt.total_change or 0.0)
            hedged = abs(mvt.hedged_item_change or 0.0)
            lower_of = min(total, hedged)
            exceeds = (
                currency.compare_amounts(effective, lower_of) > 0
                if currency else effective > lower_of + 0.01
            )
            if mvt.hedge_type != 'fair_value' and exceeds:
                raise ValidationError(_(
                    "Effective portion %.2f exceeds the IFRS 9 6.5.11(a) "
                    "lower-of cap %.2f (instrument %.2f; hedged item %.2f).",
                ) % (
                    mvt.effective_portion, lower_of,
                    mvt.total_change, mvt.hedged_item_change,
                ))
            if (
                mvt.reclassification_date and mvt.movement_date
                and mvt.reclassification_date < mvt.movement_date
            ):
                raise ValidationError(_(
                    "OCI reclassification date cannot precede movement date."
                ))

    @api.constrains(
        'effective_portion', 'total_change', 'hedged_item_change',
    )
    def _check_effective_sign(self):
        for mvt in self:
            eff = mvt.effective_portion or 0.0
            tot = mvt.total_change or 0.0
            if eff and tot and (eff > 0) != (tot > 0):
                raise ValidationError(_(
                    "The effective portion must have the same sign as the "
                    "total change: the effective part of an instrument gain "
                    "is a gain, of a loss a loss. Got effective %.2f against "
                    "total %.2f.",
                ) % (eff, tot))
            hedged = mvt.hedged_item_change or 0.0
            if hedged and tot and (hedged > 0) == (tot > 0):
                raise ValidationError(_(
                    "Hedged-item change must have the opposite sign to the "
                    "hedging-instrument change."
                ))

    def _eh_related_hedges(self, vals=None):
        vals = vals or {}
        hedges = self.mapped('hedge_id')
        if vals.get('hedge_id'):
            hedges |= self.env['eh.fx.hedge'].browse(vals['hedge_id'])
        return hedges

    @api.model
    def _eh_validate_numeric_values(self, vals, current=None, hedge=None):
        hedge = hedge or (current.hedge_id if current else None)
        currency = hedge.company_id.currency_id if hedge else None
        numbers = {}
        for field_name, label in (
            ('total_change', _("Total hedge change")),
            ('effective_portion', _("Effective hedge portion")),
            ('hedged_item_change', _("Hedged-item change")),
        ):
            numbers[field_name] = _eh_currency_number(
                vals.get(field_name, current[field_name] if current else 0.0),
                currency,
                label,
            )
        total = numbers['total_change']
        effective = numbers['effective_portion']
        hedged_item = numbers['hedged_item_change']
        if effective and total and (effective > 0.0) != (total > 0.0):
            raise ValidationError(_(
                "The effective portion must have the same sign as the total "
                "change."
            ))
        if hedged_item and total and (hedged_item > 0.0) == (total > 0.0):
            raise ValidationError(_(
                "The hedged-item change must have the opposite sign to the "
                "instrument change."
            ))
        lower_of = min(abs(total), abs(hedged_item))
        lower_of_applies = not hedge or hedge.hedge_type != 'fair_value'
        if (
            lower_of_applies
            and currency
            and currency.compare_amounts(abs(effective), lower_of) > 0
        ):
            raise ValidationError(_(
                "The effective portion exceeds the lower of the instrument "
                "and hedged-item changes (IFRS 9 6.5.11(a))."
            ))
        if (
            lower_of_applies
            and not currency
            and abs(effective) > lower_of + 0.01
        ):
            raise ValidationError(_(
                "The effective portion exceeds the IFRS 9 lower-of cap."
            ))
        return True

    def _eh_lock_for_transition(self, vals=None):
        expected_hedge_by_movement = {
            movement.id: movement.hedge_id.id for movement in self
        }
        hedges = self._eh_related_hedges(vals)
        hedges._eh_validate_references()
        hedges._eh_lock_for_transition()
        ids = tuple(sorted(set(self.ids)))
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id, hedge_id FROM eh_fx_hedge_movement "
                "WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (ids,),
            )
            locked_rows = self.env.cr.fetchall()
            if {row[0] for row in locked_rows} != set(ids):
                raise UserError(_(
                    "A hedge movement changed concurrently. Retry the "
                    "operation."
                ))
            if any(
                expected_hedge_by_movement.get(movement_id) != hedge_id
                for movement_id, hedge_id in locked_rows
            ):
                raise UserError(_(
                    "A hedge movement changed hedge concurrently. Retry the "
                    "operation."
                ))
            self.invalidate_recordset()
        return self

    def _eh_prepare_action(self):
        if not self.env.su:
            self._eh_check_access('write')
        self.hedge_id._eh_check_access('write')
        self.hedge_id._eh_check_manager()
        self.hedge_id._eh_validate_references()
        self._eh_lock_for_transition()
        for movement in self:
            movement._eh_validate_numeric_values({}, current=movement)
        self.hedge_id._eh_validate_references()
        return self._eh_workflow_action()

    # ---- post ----

    def action_post(self):
        with self.env.cr.savepoint():
            movements = self._eh_prepare_action()
            movements._eh_action_post_locked()
        return True

    def _eh_action_post_locked(self):
        for mvt in self.sorted('id'):
            if mvt.state != 'draft':
                raise UserError(_(
                    "Only draft movements can be posted.",
                ))
            hedge = mvt.hedge_id
            if not hedge.journal_id or not hedge.instrument_account_id:
                raise UserError(_(
                    "Hedge %s needs journal and instrument account "
                    "configured before posting movements.",
                ) % hedge.display_name)
            if hedge.hedge_type == 'fair_value' \
                    and not hedge.hedged_item_account_id:
                raise UserError(_(
                    "Fair-value hedge %s needs a Hedged Item Account before "
                    "posting: the effective portion adjusts the hedged "
                    "item's carrying amount.",
                ) % hedge.display_name)
            if hedge.hedge_type == 'net_investment' \
                    and hedge.cta_position_id \
                    and hedge.cta_position_id.state == 'disposed':
                raise UserError(_(
                    "NIH %(hedge)s points at CTA position %(pos)s, "
                    "which is disposed: its reserve was already "
                    "reclassified to P&L in full (IAS 21.48), so no "
                    "further effective portion can be parked there.",
                    hedge=hedge.display_name,
                    pos=hedge.cta_position_id.display_name,
                ))
            # IFRS 9: the OCI (CFH/NIH) or hedged-item (FVH) deferral of the
            # effective portion is only permitted while the hedge qualifies.
            # A non-zero effective portion therefore requires a passing
            # effectiveness test dated on or before this movement. Otherwise
            # the movement must recognise the full change in P&L (effective
            # portion 0), which is exactly the de-designated treatment.
            if mvt.effective_portion:
                tests_as_of = hedge.test_ids.filtered(
                    lambda test: (
                        hedge.designation_date <= test.test_date
                        <= mvt.movement_date
                    ),
                ).sorted(
                    key=lambda test: (test.test_date, test.id), reverse=True,
                )
                qualifying = tests_as_of[:1].filtered('is_effective')
                if (
                    hedge.state in ('draft', 'dedesignated', 'terminated')
                    or not qualifying
                ):
                    raise UserError(_(
                        "Hedge %(name)s does not currently qualify for hedge "
                        "accounting, so the effective portion cannot be "
                        "deferred to OCI or the hedged item. Record a passing "
                        "effectiveness test dated on or before %(date)s, or "
                        "set the effective portion to 0 to recognise the full "
                        "change in P&L.",
                        name=hedge.display_name, date=mvt.movement_date,
                    ))
            move = mvt._build_move()
            mvt.write({
                'state': 'posted',
                'move_id': move.id,
                'posted_at': fields.Datetime.now(),
                'posted_by_id': self.env.user.id,
            })

    def _build_move(self):
        """Post the period gain/loss with the effective / ineffective split.

        For CFH and NIH:
          Dr/Cr instrument (total_change)
          Cr/Dr OCI account (effective_portion)
          Cr/Dr P&L account (ineffective_portion)

        For FVH the independently measured hedged-item change adjusts its
        carrying amount and is recognised in P&L with instrument movement:
          Dr/Cr instrument (total_change)
          Dr/Cr hedged item account (hedged_item_change)
          balancing P&L = -(instrument + hedged-item change)
        This is the basis adjustment and resulting ineffectiveness required
        by IFRS 9 6.5.8, not an assumed mirror of instrument effectiveness.
        """
        self.ensure_one()
        hedge = self.hedge_id
        # Sign convention: total_change > 0 means we have a gain on
        # the hedging instrument. Debit the instrument balance-sheet
        # account; credit the gain across OCI + P&L per the type.
        amount = self.total_change
        is_gain = amount >= 0
        absamount = abs(amount)
        eff_abs = abs(self.effective_portion or 0.0)
        ineff_abs = abs(self.ineffective_portion or 0.0)
        label = _("Hedge movement %s on %s") % (
            hedge.name, self.movement_date,
        )
        lines = []
        # Hedging instrument leg.
        if is_gain:
            lines.append((0, 0, {
                'name': label,
                'account_id': hedge.instrument_account_id.id,
                'debit': absamount,
                'credit': 0.0,
            }))
        else:
            lines.append((0, 0, {
                'name': label,
                'account_id': hedge.instrument_account_id.id,
                'debit': 0.0,
                'credit': absamount,
            }))
        # Effective portion: OCI for CFH/NIH. FVH instead posts the measured
        # hedged-item change as a carrying-amount basis adjustment. A
        # position-linked NIH books its OCI leg on the position's CTA
        # equity account so the reserve balance is ledger-fed.
        cta_position = (
            hedge.cta_position_id
            if hedge.hedge_type == 'net_investment' else
            self.env['eh.fx.cta.position']
        )
        if cta_position:
            eff_account = cta_position.cta_account_id
        elif hedge.hedge_type in ('cash_flow', 'net_investment'):
            eff_account = hedge.oci_account_id
        else:
            eff_account = self.env['account.account']
        if eff_abs > 0 and hedge.hedge_type != 'fair_value':
            if is_gain:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': eff_account.id,
                    'debit': 0.0,
                    'credit': eff_abs,
                }))
            else:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': eff_account.id,
                    'debit': eff_abs,
                    'credit': 0.0,
                }))
        if hedge.hedge_type == 'fair_value':
            hedged_change = self.hedged_item_change or 0.0
            if hedged_change:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.hedged_item_account_id.id,
                    'debit': max(hedged_change, 0.0),
                    'credit': max(-hedged_change, 0.0),
                }))
            # Balance total signed instrument + hedged-item changes. P&L is
            # exact opposite, yielding period ineffectiveness.
            pnl_balance = -(
                (self.total_change or 0.0) + hedged_change
            )
            if pnl_balance:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.pl_account_id.id,
                    'debit': max(pnl_balance, 0.0),
                    'credit': max(-pnl_balance, 0.0),
                }))
        # CFH/NIH ineffectiveness: always P&L.
        elif ineff_abs > 0:
            if is_gain:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.pl_account_id.id,
                    'debit': 0.0,
                    'credit': ineff_abs,
                }))
            else:
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.pl_account_id.id,
                    'debit': ineff_abs,
                    'credit': 0.0,
                }))
        move_vals = {
            'move_type': 'entry',
            'company_id': hedge.company_id.id,
            'journal_id': hedge.journal_id.id,
            'date': self.movement_date,
            'ref': "%s / %s" % (hedge.name, self.id),
            'line_ids': lines,
            'eh_sealed': True,
            'eh_fx_hedge_movement_id': self.id,
        }
        if cta_position and eff_abs > 0:
            move_vals['eh_cta_position_id'] = cta_position.id
        move = self.env['account.move'].with_context(
            eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
        )._eh_create_sealed(move_vals)
        move.action_post()
        if cta_position and eff_abs > 0:
            # The position balance is a stored-free ledger compute; drop
            # the cached value so the freshly posted OCI leg is visible.
            cta_position.invalidate_recordset(['balance'])
        return move

    def action_reclassify_to_pl(self):
        """Reclassify the effective portion from OCI to P&L.

        Used when:
        * CFH: the forecast transaction occurs (sale recognised,
          purchase recognised). The OCI reserve flips to revenue or
          COGS.
        * NIH: the foreign operation is disposed of. The CTA reserve
          flips to gain/loss on disposal.

        Only applies to movements that originally posted to OCI.
        FVH movements never went to OCI so this action is a no-op.
        """
        with self.env.cr.savepoint():
            movements = self._eh_prepare_action()
            movements._eh_action_reclassify_to_pl_locked()
        return True

    def _eh_action_reclassify_to_pl_locked(self):
        for mvt in self.sorted('id'):
            if mvt.state != 'posted':
                raise UserError(_(
                    "Only posted movements can be reclassified.",
                ))
            if (
                not mvt.move_id
                or mvt.move_id.eh_fx_hedge_movement_id != mvt
                or mvt.move_id.company_id != mvt.company_id
                or mvt.move_id.state != 'posted'
                or not mvt.move_id.eh_sealed
            ):
                raise UserError(_(
                    "Hedge movement journal provenance is missing or "
                    "inconsistent; reclassification is blocked."))
            hedge = mvt.hedge_id
            if hedge.hedge_type == 'fair_value':
                raise UserError(_(
                    "Fair-value hedge movements never post to OCI; "
                    "no reclassification needed.",
                ))
            if hedge.hedge_type == 'net_investment' \
                    and hedge.cta_position_id:
                raise UserError(_(
                    "NIH %(hedge)s parks its effective portion in CTA "
                    "position %(pos)s. IAS 21.48 recycles the FULL "
                    "accumulated reserve (including these amounts) on "
                    "disposal of the foreign operation: use the "
                    "Dispose action on the position instead of "
                    "reclassifying per movement.",
                    hedge=hedge.display_name,
                    pos=hedge.cta_position_id.display_name,
                ))
            eff_abs = abs(mvt.effective_portion or 0.0)
            if eff_abs == 0:
                raise UserError(_(
                    "Movement %s has zero effective portion; "
                    "nothing to reclassify.",
                ) % mvt.id)
            reclassification_date = mvt.reclassification_date
            label = _("OCI reclassification %s on %s") % (
                hedge.name, reclassification_date,
            )
            # Reverse the OCI leg, recognise into P&L.
            is_gain = (mvt.total_change or 0.0) >= 0
            lines = []
            if is_gain:
                # Original posted: Cr OCI. Reclassification: Dr OCI,
                # Cr P&L (recognise as income).
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.oci_account_id.id,
                    'debit': eff_abs,
                    'credit': 0.0,
                }))
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.pl_account_id.id,
                    'debit': 0.0,
                    'credit': eff_abs,
                }))
            else:
                # Original posted: Dr OCI. Reclassification: Cr OCI,
                # Dr P&L (recognise as expense).
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.oci_account_id.id,
                    'debit': 0.0,
                    'credit': eff_abs,
                }))
                lines.append((0, 0, {
                    'name': label,
                    'account_id': hedge.pl_account_id.id,
                    'debit': eff_abs,
                    'credit': 0.0,
                }))
            recl_move = self.env['account.move'].with_context(
                eh_fx_move_link_capability=FX_MOVE_LINK_CAPABILITY,
            )._eh_create_sealed({
                'move_type': 'entry',
                'company_id': hedge.company_id.id,
                'journal_id': hedge.journal_id.id,
                'date': reclassification_date,
                'ref': "Reclass %s / %s" % (hedge.name, mvt.id),
                'line_ids': lines,
                'eh_sealed': True,
                'eh_fx_hedge_reclassification_movement_id': mvt.id,
            })
            recl_move.action_post()
            mvt.with_context(eh_hedge_state_change=True).write({
                'state': 'reclassified',
                'reclassification_move_id': recl_move.id,
            })

    # ---- freeze-after-post ----

    # Measurement inputs that determine the posted journal entry and the
    # audit figures. Once a movement has posted, its journal entry is live
    # and these figures are the recognised gain/loss split; a direct write
    # to any of them would silently desynchronise the audit record from the
    # posted JE. They are frozen from that point on.
    _EH_FROZEN_MOVEMENT_FIELDS = (
        'total_change',
        'effective_portion',
        'hedged_item_change',
        'movement_date',
        'hedge_id',
    )

    @api.model_create_multi
    def create(self, vals_list):
        # A movement is always born in draft and only reaches posted /
        # reclassified through action_post / action_reclassify_to_pl, which
        # attach its journal entry. Creating one already in a posted state
        # would fabricate a finalised figure with no journal entry behind it,
        # so refuse it at the ORM layer regardless of ACL create rights.
        if not (self.env.su
                and self.env.context.get('eh_hedge_state_change')):
            for v in vals_list:
                if v.get('state') and v['state'] != 'draft':
                    raise UserError(_(
                        "A hedge movement is created in draft and reaches a "
                        "posted state only through Post / Reclassify, which "
                        "attach its journal entry. It cannot be created "
                        "directly in the %s state.", v['state']))
        defaults = self.default_get([
            'hedge_id', 'total_change', 'effective_portion',
            'hedged_item_change', 'movement_date', 'reclassification_date',
        ])
        for vals in vals_list:
            if not vals.get('reclassification_date'):
                vals['reclassification_date'] = (
                    vals.get('movement_date')
                    or defaults.get('movement_date')
                    or fields.Date.context_today(self)
                )
        effective_vals = [dict(defaults, **vals) for vals in vals_list]
        hedges = self.env['eh.fx.hedge'].browse({
            vals.get('hedge_id') for vals in effective_vals
            if vals.get('hedge_id')
        })
        hedges._eh_check_access('write')
        hedges._eh_validate_references()
        hedges._eh_lock_for_transition()
        for vals in effective_vals:
            hedge = self.env['eh.fx.hedge'].browse(vals.get('hedge_id'))
            self._eh_validate_numeric_values(vals, hedge=hedge)
        movements = super().create(vals_list)
        movements.hedge_id._eh_validate_references()
        return movements

    def write(self, vals):
        # Block edits to posted measurement inputs on any movement that has
        # already left draft. The state-transition writes issued by
        # action_post / action_reclassify_to_pl move a still-draft record
        # forward and set only bookkeeping fields, so they are unaffected.
        if vals and not self.env.su:
            self._eh_check_access('write')
        hedges = self._eh_related_hedges(vals)
        hedges._eh_check_access('write')
        hedges._eh_validate_references()
        self._eh_lock_for_transition(vals)
        target_hedge = (
            self.env['eh.fx.hedge'].browse(vals['hedge_id'])
            if vals.get('hedge_id') else self.env['eh.fx.hedge']
        )
        for movement in self:
            self._eh_validate_numeric_values(
                vals,
                current=movement,
                hedge=target_hedge or movement.hedge_id,
            )
        touched = [f for f in self._EH_FROZEN_MOVEMENT_FIELDS if f in vals]
        if touched:
            target_signed = hedges.filtered(
                lambda hedge: hedge.state in ('dedesignated', 'terminated'))
            for mvt in self:
                if mvt.state in ('posted', 'reclassified') or target_signed:
                    raise UserError(_(
                        "Movement %(name)s is %(state)s: its measurement "
                        "inputs are frozen because they drive the posted "
                        "journal entry and the audit figures. Reverse the "
                        "posted entry and recreate the movement to correct "
                        "%(fields)s.",
                        name=mvt.hedge_id.name or mvt.id,
                        state=mvt.state,
                        fields=", ".join(touched),
                    ))
        if 'reclassification_date' in vals:
            reclassified = self.filtered(
                lambda movement: movement.state == 'reclassified')
            if reclassified:
                raise UserError(_(
                    "Reclassification date is frozen after OCI has been "
                    "reclassified to profit or loss."
                ))
        # The state of a posted / reclassified movement is itself a control
        # point: resetting it to draft would silently lift the figure freeze
        # above and orphan the posted journal entry. A raw ORM 'state' write
        # that moves such a movement OUT of its posted / reclassified state,
        # without the sanctioned-transition context flag, is manager-gated so a
        # plain user cannot un-freeze a GL-backed movement. The legitimate
        # transitions (action_post moves a draft record; action_reclassify_to_pl
        # sets the flag) are unaffected.
        if 'state' in vals and not (
                self.env.su
                and self.env.context.get('eh_hedge_state_change')):
            confirmed = self.filtered(
                lambda m: m.state in ('posted', 'reclassified'))
            crossing = confirmed.filtered(lambda m: m.state != vals['state'])
            if crossing:
                crossing._check_manager()
        result = super().write(vals)
        for movement in self:
            self._eh_validate_numeric_values({}, current=movement)
        self.hedge_id._eh_validate_references()
        return result

    def _check_manager(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may change the state of a "
                "posted or reclassified hedge movement."))

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_for_transition()
        evidenced = self.filtered(
            lambda mvt: mvt.state != 'draft'
            or mvt.move_id or mvt.reclassification_move_id)
        if evidenced:
            raise UserError(_(
                "A posted or reclassified hedge movement cannot be deleted; "
                "its journal evidence is permanent."))
        return super().unlink()
