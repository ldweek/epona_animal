# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Financial-analysis ratio layer for eh.account.dashboard.

Adds a real ratio engine on top of the operational tiles: liquidity
(current / quick / cash), efficiency (DSO / DIO / DPO / CCC),
profitability (gross and net margin, ROA, ROE, ROCE) and leverage
(debt-to-equity, interest cover), each computed per company from posted
balances grouped by account_type, guarded against zero denominators,
and paired with the same-length immediately-preceding period for a
delta badge.

Conventions (documented because the golden tests hand-derive against
them):

* Point-in-time ratios (liquidity, leverage) read cumulative balances
  as of period_date_to.
* Average balances = (opening + closing) / 2, where opening is the
  cumulative balance at the day before period_date_from and closing is
  the cumulative balance at period_date_to.
* Days in period = (period_date_to - period_date_from).days + 1
  (inclusive of both endpoints).
* Account-type sets: current assets = asset_receivable, asset_cash,
  asset_current, asset_prepayments; current liabilities =
  liability_payable, liability_current, liability_credit_card;
  equity = equity, equity_unaffected plus every unclosed P&L balance through
  the measurement date (so economic equity reconciles before year-end close);
  COGS = expense_direct_cost.
* EBIT = net income + interest expense + income tax expense.
* Inventory has no dedicated account_type in Odoo, so inventory
  accounts are detected by a documented name heuristic over
  asset_current accounts ("inventory", "stock", ...). The payload flags
  whether any were found; DIO/CCC null-mark when none carry balances.
* Interest expense: soft lookup of the IAS 7 interest-paid account tag
  shipped by eh_account_dynamic_reports first; if no expense account in
  the company carries the tag, a name heuristic over expense accounts
  ("interest", "finance cost", ...) is used. The payload flags which
  source resolved ('tag' / 'heuristic' / 'none').
* Income tax expense: name heuristic over expense accounts ("income
  tax", "tax expense", ...); accounts already claimed by the interest
  set are excluded so EBIT never double-adds a line.
* Every ratio guards its denominator: a zero (or absent) denominator
  yields value None plus a human-readable note, never an exception.
* Prior period = the window of identical length ending the day before
  period_date_from. delta_pct is None when either side is None or the
  prior value is zero (no comparable baseline).
* Payload values are rounded to 2dp; deltas are computed from the
  unrounded values, then rounded to 2dp.

Warn thresholds live on res.company (see res_company.py in this
module) and drive a per-tile status: 'na' (no value), 'warn'
(threshold breached), 'ok' (threshold respected), or 'info' (ratio
carries no threshold).
"""

from datetime import timedelta

from odoo import _, models
from odoo.tools import SQL

from odoo.addons.eh_account_base.tools.sql_builder import MoveLineQuery


RATIO_CURRENT_ASSET_TYPES = (
    'asset_receivable', 'asset_cash', 'asset_current', 'asset_prepayments',
)
RATIO_CURRENT_LIABILITY_TYPES = (
    'liability_payable', 'liability_current', 'liability_credit_card',
)
RATIO_NON_CURRENT_LIABILITY_TYPES = ('liability_non_current',)
RATIO_EQUITY_TYPES = ('equity', 'equity_unaffected')
RATIO_INCOME_TYPES = ('income', 'income_other')
RATIO_EXPENSE_TYPES = (
    'expense', 'expense_other', 'expense_depreciation', 'expense_direct_cost',
)
RATIO_COGS_TYPES = ('expense_direct_cost',)
RATIO_ASSET_TYPES = RATIO_CURRENT_ASSET_TYPES + (
    'asset_non_current', 'asset_fixed',
)
RATIO_ECONOMIC_EQUITY_TYPES = (
    RATIO_EQUITY_TYPES + RATIO_INCOME_TYPES + RATIO_EXPENSE_TYPES
)

# Documented name heuristics (lower-cased substring match on the
# account name in the user's language).
EH_INVENTORY_NAME_PATTERNS = (
    'inventory', 'stock', 'merchandise', 'raw material',
    'finished goods', 'work in progress', 'goods held',
)
EH_INTEREST_NAME_PATTERNS = (
    'interest', 'finance cost', 'finance charge', 'borrowing cost',
)
EH_INCOME_TAX_PATTERNS = (
    'income tax', 'tax expense', 'corporate tax', 'corporation tax',
    'company tax', 'current tax', 'deferred tax',
)

# IAS 7 interest-paid tag shipped by eh_account_dynamic_reports (a hard
# dependency of this module, but resolved softly so a partial registry
# never breaks the board).
_EH_INTEREST_TAG_XMLID = 'eh_account_dynamic_reports.account_tag_interest_paid'

# ratio key -> (company threshold field, breach direction).
# 'below': warn when value < threshold; 'above': warn when value > it.
EH_RATIO_THRESHOLD_RULES = {
    'current_ratio': ('eh_ratio_warn_current', 'below'),
    'quick_ratio': ('eh_ratio_warn_quick', 'below'),
    'cash_ratio': ('eh_ratio_warn_cash', 'below'),
    'interest_cover': ('eh_ratio_warn_interest_cover', 'below'),
    'net_margin_pct': ('eh_ratio_warn_net_margin_pct', 'below'),
    'debt_to_equity': ('eh_ratio_warn_debt_equity', 'above'),
    'ccc': ('eh_ratio_warn_ccc_days', 'above'),
}

_ZERO = 1e-9


class EhAccountDashboardRatios(models.Model):
    _inherit = 'eh.account.dashboard'

    # ------------------------------------------------------------------
    # snapshot extension
    # ------------------------------------------------------------------

    def get_dashboard_snapshot(self):
        snapshot = super().get_dashboard_snapshot()
        snapshot['ratios'] = self._eh_ratio_payload()
        return snapshot

    # ------------------------------------------------------------------
    # payload
    # ------------------------------------------------------------------

    def _eh_ratio_catalog(self):
        """Category / ratio catalogue. A method (not a class attr) so
        the labels translate against the active language."""
        return (
            ('liquidity', _("Liquidity"), (
                ('current_ratio', _("Current ratio"), 'x'),
                ('quick_ratio', _("Quick ratio"), 'x'),
                ('cash_ratio', _("Cash ratio"), 'x'),
            )),
            ('efficiency', _("Efficiency"), (
                ('dso', _("DSO"), 'days'),
                ('dio', _("DIO"), 'days'),
                ('dpo', _("DPO"), 'days'),
                ('ccc', _("Cash conversion cycle"), 'days'),
            )),
            ('profitability', _("Profitability"), (
                ('gross_margin_pct', _("Gross margin"), 'pct'),
                ('net_margin_pct', _("Net margin"), 'pct'),
                ('roa', _("Return on assets"), 'pct'),
                ('roe', _("Return on equity"), 'pct'),
                ('roce', _("ROCE"), 'pct'),
            )),
            ('leverage', _("Leverage"), (
                ('debt_to_equity', _("Debt to equity"), 'x'),
                ('interest_cover', _("Interest cover"), 'x'),
            )),
        )

    def _eh_ratio_payload(self):
        """Full 'ratios' payload block for the Owl board snapshot.

        Grouped by category; every entry carries value / prior /
        delta_pct / note / status. Never raises on missing data: the
        guards downgrade to value None + note.
        """
        self.ensure_one()
        date_from = self.period_date_from
        date_to = self.period_date_to
        if not date_from or not date_to or date_from > date_to:
            return {
                'available': False,
                'window': {},
                'flags': {},
                'categories': [],
            }
        sources = self._eh_ratio_sources()
        current = self._eh_ratio_values(date_from, date_to, sources)
        days = (date_to - date_from).days + 1
        prior_to = date_from - timedelta(days=1)
        prior_from = prior_to - timedelta(days=days - 1)
        prior = self._eh_ratio_values(prior_from, prior_to, sources)

        categories = []
        for cat_key, cat_label, ratio_defs in self._eh_ratio_catalog():
            entries = []
            for key, label, fmt in ratio_defs:
                cur = current.get(key) or {'value': None, 'note': None}
                pri = prior.get(key) or {'value': None, 'note': None}
                value = cur['value']
                prior_value = pri['value']
                entries.append({
                    'key': key,
                    'label': label,
                    'format': fmt,
                    'value': None if value is None else round(value, 2),
                    'prior': (
                        None if prior_value is None
                        else round(prior_value, 2)
                    ),
                    'delta_pct': self._eh_ratio_delta_pct(
                        value, prior_value,
                    ),
                    'note': cur['note'],
                    'status': self._eh_ratio_status(key, value),
                })
            categories.append({
                'key': cat_key,
                'label': cat_label,
                'ratios': entries,
            })
        return {
            'available': True,
            'window': {
                'date_from': date_from.isoformat(),
                'date_to': date_to.isoformat(),
                'days': days,
                'prior_from': prior_from.isoformat(),
                'prior_to': prior_to.isoformat(),
            },
            'flags': {
                'inventory_detected': bool(sources['inventory_ids']),
                'interest_source': sources['interest_source'],
                'tax_source': sources['tax_source'],
            },
            'categories': categories,
        }

    # ------------------------------------------------------------------
    # core computation
    # ------------------------------------------------------------------

    def _eh_ratio_values(self, date_from, date_to, sources):
        """Compute every ratio (unrounded) for one window.

        Returns {key: {'value': float or None, 'note': str or None}}.
        A None value always carries a note naming the missing
        denominator; a float value may still carry a note (e.g. the
        DPO purchases proxy).
        """
        self.ensure_one()
        days = (date_to - date_from).days + 1
        opening_cutoff = date_from - timedelta(days=1)
        closing = self._eh_ratio_type_balances(date_to)
        opening = self._eh_ratio_type_balances(opening_cutoff)
        flows = self._eh_ratio_type_flows(date_from, date_to)

        def total(table, types):
            return sum(table.get(t, 0.0) for t in types)

        # ---- balance-sheet aggregates (asset side debit-positive;
        # liability / equity flipped to display-positive) ----
        current_assets = total(closing, RATIO_CURRENT_ASSET_TYPES)
        current_liab = -total(closing, RATIO_CURRENT_LIABILITY_TYPES)
        ncl_close = -total(closing, RATIO_NON_CURRENT_LIABILITY_TYPES)
        ncl_open = -total(opening, RATIO_NON_CURRENT_LIABILITY_TYPES)
        cash_close = total(closing, ('asset_cash',))
        prepayments_close = total(closing, ('asset_prepayments',))
        # Before a close journal transfers retained earnings, current-year
        # profit/loss still sits on income and expense accounts. Economic
        # equity must fold that unclosed P&L in at each measurement date;
        # otherwise ROE, ROCE and debt/equity overstate their denominators or
        # leverage whenever the dashboard runs mid-year.
        equity_close = -total(closing, RATIO_ECONOMIC_EQUITY_TYPES)
        equity_open = -total(opening, RATIO_ECONOMIC_EQUITY_TYPES)
        assets_close = total(closing, RATIO_ASSET_TYPES)
        assets_open = total(opening, RATIO_ASSET_TYPES)
        ar_avg = (
            total(opening, ('asset_receivable',))
            + total(closing, ('asset_receivable',))
        ) / 2.0
        ap_avg = (
            -total(opening, ('liability_payable',))
            - total(closing, ('liability_payable',))
        ) / 2.0

        inventory_ids = sources['inventory_ids']
        inv_close = self._eh_ratio_account_sum(
            inventory_ids, date_to=date_to,
        )
        inv_open = self._eh_ratio_account_sum(
            inventory_ids, date_to=opening_cutoff,
        )
        inv_avg = (inv_open + inv_close) / 2.0

        # ---- P&L flows (income credit-negative, flipped positive) ----
        revenue = -total(flows, RATIO_INCOME_TYPES)
        expenses = total(flows, RATIO_EXPENSE_TYPES)
        cogs = total(flows, RATIO_COGS_TYPES)
        interest = self._eh_ratio_account_sum(
            sources['interest_ids'], date_from=date_from, date_to=date_to,
        )
        tax = self._eh_ratio_account_sum(
            sources['tax_ids'], date_from=date_from, date_to=date_to,
        )
        net_income = revenue - expenses
        ebit = net_income + interest + tax

        assets_avg = (assets_open + assets_close) / 2.0
        equity_avg = (equity_open + equity_close) / 2.0
        capital_employed_avg = equity_avg + (ncl_open + ncl_close) / 2.0

        out = {}

        def put(key, numerator, denominator, zero_note, factor=1.0,
                note=None):
            if abs(denominator) < _ZERO:
                out[key] = {'value': None, 'note': zero_note}
            else:
                out[key] = {
                    'value': (numerator / denominator) * factor,
                    'note': note,
                }

        no_cl_note = _("No current liabilities on the ledger.")
        no_rev_note = _("No revenue recognised in the period.")

        # ---- liquidity (point-in-time at date_to) ----
        put('current_ratio', current_assets, current_liab, no_cl_note)
        put('quick_ratio',
            current_assets - inv_close - prepayments_close, current_liab,
            no_cl_note)
        put('cash_ratio', cash_close, current_liab, no_cl_note)

        # ---- efficiency (average balances over the window) ----
        if abs(revenue) < _ZERO:
            out['dso'] = {'value': None, 'note': no_rev_note}
        else:
            out['dso'] = {'value': ar_avg / (revenue / days), 'note': None}

        dpo_basis = cogs
        dpo_note = None
        if abs(dpo_basis) < _ZERO:
            dpo_basis = expenses
            dpo_note = _(
                "Purchases proxy: no direct-cost activity in the period; "
                "total expenses used as the basis.")
        if abs(dpo_basis) < _ZERO:
            out['dpo'] = {
                'value': None,
                'note': _("No cost activity in the period."),
            }
        else:
            out['dpo'] = {
                'value': ap_avg / (dpo_basis / days),
                'note': dpo_note,
            }

        if not inventory_ids:
            out['dio'] = {
                'value': None,
                'note': _(
                    "No inventory accounts detected "
                    "(name-based detection)."),
            }
        elif abs(inv_open) < _ZERO and abs(inv_close) < _ZERO:
            out['dio'] = {
                'value': None,
                'note': _("Inventory accounts carry no balance."),
            }
        elif abs(cogs) < _ZERO:
            out['dio'] = {
                'value': None,
                'note': _("No cost of sales in the period."),
            }
        else:
            out['dio'] = {'value': inv_avg / (cogs / days), 'note': None}

        if (out['dso']['value'] is not None
                and out['dio']['value'] is not None
                and out['dpo']['value'] is not None):
            out['ccc'] = {
                'value': (
                    out['dso']['value']
                    + out['dio']['value']
                    - out['dpo']['value']
                ),
                'note': None,
            }
        else:
            out['ccc'] = {
                'value': None,
                'note': _("Requires DSO, DIO and DPO."),
            }

        # ---- profitability ----
        put('gross_margin_pct', revenue - cogs, revenue, no_rev_note,
            factor=100.0)
        put('net_margin_pct', net_income, revenue, no_rev_note,
            factor=100.0)
        put('roa', net_income, assets_avg,
            _("Average total assets are zero."), factor=100.0)
        put('roe', net_income, equity_avg,
            _("Average equity is zero."), factor=100.0)
        put('roce', ebit, capital_employed_avg,
            _("Average capital employed is zero."), factor=100.0)

        # ---- leverage ----
        put('debt_to_equity', current_liab + ncl_close, equity_close,
            _("Equity balance is zero."))
        put('interest_cover', ebit, interest,
            _("No interest expense recognised in the period."))

        return out

    # ------------------------------------------------------------------
    # input resolution
    # ------------------------------------------------------------------

    def _eh_ratio_sources(self):
        """Resolve the account-id sets the type groups cannot express.

        Returns a dict with inventory_ids, interest_ids (+ its source
        'tag' / 'heuristic' / 'none'), and tax_ids (+ source), resolved
        once per payload so the current and prior windows use identical
        account sets.
        """
        self.ensure_one()
        inventory_ids = self._eh_ratio_accounts_matching(
            ('asset_current',), EH_INVENTORY_NAME_PATTERNS,
        )
        interest_ids, interest_source = self._eh_interest_expense_accounts()
        tax_ids = self._eh_ratio_accounts_matching(
            RATIO_EXPENSE_TYPES, EH_INCOME_TAX_PATTERNS,
        )
        # An account claimed by the interest set must not also feed the
        # tax add-back or EBIT would double-count it.
        interest_set = set(interest_ids)
        tax_ids = [i for i in tax_ids if i not in interest_set]
        return {
            'inventory_ids': inventory_ids,
            'interest_ids': interest_ids,
            'interest_source': interest_source,
            'tax_ids': tax_ids,
            'tax_source': 'heuristic' if tax_ids else 'none',
        }

    def _eh_interest_expense_accounts(self):
        """Expense accounts carrying interest expense.

        Preference order: accounts tagged with the IAS 7 interest-paid
        tag from eh_account_dynamic_reports (soft lookup), then the
        name heuristic. Returns (ids, source) with source in
        ('tag', 'heuristic', 'none').
        """
        self.ensure_one()
        tag = self.env.ref(_EH_INTEREST_TAG_XMLID, raise_if_not_found=False)
        if tag is not None:
            domain = self._eh_ratio_account_domain(RATIO_EXPENSE_TYPES)
            domain.append(('tag_ids', 'in', tag.ids))
            ids = self.env['account.account'].search(domain).ids
            if ids:
                return ids, 'tag'
        ids = self._eh_ratio_accounts_matching(
            RATIO_EXPENSE_TYPES, EH_INTEREST_NAME_PATTERNS,
        )
        return ids, ('heuristic' if ids else 'none')

    def _eh_ratio_account_domain(self, account_types):
        """Company-scoped account domain, version-aware: account.account
        carries company_ids (m2m) from Odoo 18 and company_id before."""
        Account = self.env['account.account']
        if 'company_ids' in Account._fields:
            domain = [('company_ids', 'in', [self.company_id.id])]
        else:
            domain = [('company_id', '=', self.company_id.id)]
        domain.append(('account_type', 'in', list(account_types)))
        return domain

    def _eh_ratio_accounts_matching(self, account_types, patterns):
        """Ids of company accounts of the given types whose name
        contains any of the lower-cased substring patterns."""
        self.ensure_one()
        accounts = self.env['account.account'].search(
            self._eh_ratio_account_domain(account_types),
        )
        hits = accounts.filtered(
            lambda a: any(p in (a.name or '').lower() for p in patterns),
        )
        return hits.ids

    # ------------------------------------------------------------------
    # SQL passes (one aggregate each, off the shared MoveLineQuery)
    # ------------------------------------------------------------------

    def _eh_ratio_type_balances(self, as_of):
        """Cumulative balance per account_type up to and including
        as_of. One grouped SQL pass."""
        self.ensure_one()
        query = MoveLineQuery(self.env, company_ids=[self.company_id.id])
        query.join_account()
        query.select_account_field('account_type', 'account_type')
        query.select(SQL("COALESCE(SUM(aml.balance), 0)"), 'balance')
        query.where_date_range(date_to=as_of)
        self._eh_apply_operational_move_scope(query)
        if self.posted_only:
            query.where_posted_only()
        query.group_by_account_field('account_type')
        return {
            row['account_type']: float(row['balance'] or 0.0)
            for row in query.execute()
        }

    def _eh_ratio_type_flows(self, date_from, date_to):
        """Balance movement per account_type inside the window. One
        grouped SQL pass."""
        self.ensure_one()
        query = MoveLineQuery(self.env, company_ids=[self.company_id.id])
        query.join_account()
        query.select_account_field('account_type', 'account_type')
        query.select(SQL("COALESCE(SUM(aml.balance), 0)"), 'balance')
        query.where_date_range(date_from=date_from, date_to=date_to)
        self._eh_apply_operational_move_scope(query)
        if self.posted_only:
            query.where_posted_only()
        query.group_by_account_field('account_type')
        return {
            row['account_type']: float(row['balance'] or 0.0)
            for row in query.execute()
        }

    def _eh_ratio_account_sum(self, account_ids, date_from=None,
                              date_to=None):
        """SUM(balance) over an explicit account-id set in a date
        scope. Returns 0.0 for an empty set without querying."""
        self.ensure_one()
        if not account_ids:
            return 0.0
        query = MoveLineQuery(self.env, company_ids=[self.company_id.id])
        query.where_accounts(account_ids)
        query.where_date_range(date_from=date_from, date_to=date_to)
        self._eh_apply_operational_move_scope(query)
        if self.posted_only:
            query.where_posted_only()
        query.select(SQL("COALESCE(SUM(aml.balance), 0)"), 'balance')
        rows = query.execute()
        return float(rows[0].get('balance') or 0.0) if rows else 0.0

    # ------------------------------------------------------------------
    # status + delta helpers
    # ------------------------------------------------------------------

    def _eh_ratio_status(self, key, value):
        """Map a ratio value onto a tile status via the company warn
        thresholds. 'na' without a value; 'info' without a rule."""
        if value is None:
            return 'na'
        rule = EH_RATIO_THRESHOLD_RULES.get(key)
        if not rule:
            return 'info'
        field_name, direction = rule
        threshold = self.company_id[field_name]
        if direction == 'below':
            return 'warn' if value < threshold else 'ok'
        return 'warn' if value > threshold else 'ok'

    @staticmethod
    def _eh_ratio_delta_pct(current, prior):
        """Percentage change vs prior, None when no comparable
        baseline (either side missing, or prior is zero)."""
        if current is None or prior is None:
            return None
        if abs(prior) < _ZERO:
            return None
        return round((current - prior) / abs(prior) * 100.0, 2)
