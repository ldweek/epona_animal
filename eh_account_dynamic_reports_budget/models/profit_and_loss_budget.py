# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Budget-vs-actual columns on the Profit and Loss report.

Inherits the P&L handler and, when options carry a budget_id, appends a
Budget column and a Budget Variance column to the computed payload:
per account (summed from the budget lines), per section total, and on
Net Profit (derived by account type so the figures reconcile).
"""

from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EhProfitAndLossBudgetHandler(models.AbstractModel):
    _inherit = 'eh.account.dynamic.report.handler.profit_and_loss'

    @api.model
    @api.private
    def normalize_options(self, options):
        """Bind selected-budget cache identity to its locked input snapshot."""
        normalized = super().normalize_options(options)
        # Picker lifecycle filtering only controls which budget records the
        # browser offers.  It never changes report arithmetic, so retaining
        # it would create ghost cache/audit identities for identical output.
        normalized.pop('budget_state_filter', None)
        # Internal tokens are server-owned.  Never trust or retain an RPC
        # caller's value when no valid selected budget exists.
        normalized.pop('_budget_cache_version', None)
        raw_budget_id = normalized.get('budget_id')
        if not raw_budget_id:
            normalized.pop('budget_id', None)
            normalized.pop('budget_apportionment', None)
            return normalized
        try:
            budget_id = int(raw_budget_id)
        except (TypeError, ValueError, OverflowError):
            normalized.pop('budget_id', None)
            normalized.pop('budget_apportionment', None)
            return normalized
        if budget_id <= 0:
            normalized.pop('budget_id', None)
            normalized.pop('budget_apportionment', None)
            return normalized

        budget = self.env['eh.budget.budget'].browse(budget_id).exists()
        if not budget:
            normalized.pop('budget_id', None)
            normalized.pop('budget_apportionment', None)
            return normalized
        budget._eh_check_access('read')
        cache_version = budget._eh_report_cache_snapshot()
        if cache_version is None:
            normalized.pop('budget_id', None)
            normalized.pop('budget_apportionment', None)
            return normalized
        normalized['budget_id'] = budget.id
        normalized['_budget_cache_version'] = cache_version
        apportionment = normalized.get('budget_apportionment') or 'daily'
        if apportionment not in ('daily', 'period_exact'):
            apportionment = 'daily'
        normalized['budget_apportionment'] = apportionment
        return normalized

    @api.model
    def compute(self, options):
        budget_id = options.get('budget_id')
        if not budget_id:
            return super().compute(options)
        try:
            budget_id = int(budget_id)
        except (TypeError, ValueError, OverflowError):
            fallback_options = dict(options)
            fallback_options.pop('budget_id', None)
            fallback_options.pop('budget_apportionment', None)
            return super().compute(fallback_options)
        budget = self.env['eh.budget.budget'].browse(budget_id).exists()
        if not budget:
            fallback_options = dict(options)
            fallback_options.pop('budget_id', None)
            fallback_options.pop('budget_apportionment', None)
            return super().compute(fallback_options)
        budget._eh_check_access('read')
        budget._eh_assert_single_analytic_plan()
        budget._eh_assert_non_overlapping_actual_scopes()

        unsupported_axis_keys = [
            key for key in (
                'analytic_column_account_ids',
                'analytic_column_plan_ids',
            ) if options.get(key)
        ]
        comparison = options.get('comparison') or 'none'
        comparison_number = self._eh_normalize_comparison_number(
            options.get('comparison_number', 1),
        )
        if comparison == 'custom':
            unsupported_axis_keys.append('comparison_custom_dates')
        if comparison_number > 1:
            unsupported_axis_keys.append('comparison_number')
        if comparison != 'none' and options.get(
                'comparison_order') == 'ascending':
            unsupported_axis_keys.append('comparison_order')
        if unsupported_axis_keys:
            raise UserError(_(
                "Budget Profit and Loss does not yet support column-axis "
                "options: %(options)s.",
                options=', '.join(sorted(set(unsupported_axis_keys))),
            ))

        analytic_lines = budget.line_ids.filtered('analytic_account_id')
        analytic_account_ids = sorted(set(
            analytic_lines.mapped('analytic_account_id').ids,
        ))
        if analytic_lines and len(analytic_lines) != len(budget.line_ids):
            raise UserError(_(
                "Budget Profit and Loss comparison cannot mix company-wide "
                "and analytic-scoped budget lines. Use one coherent scope "
                "per budget version."
            ))

        company_ids = [
            int(company_id) for company_id in (
                options.get('company_ids') or [self.env.company.id]
            )
        ]
        if len(company_ids) != 1 or company_ids[0] != budget.company_id.id:
            raise UserError(_(
                "Budget comparison requires one report company, and that "
                "company must own the selected budget."
            ))
        if (options.get('pnl_presentation') or 'by_nature') != 'by_nature':
            raise UserError(_(
                "Budget comparison currently supports the by-nature Profit "
                "and Loss presentation only."
            ))
        if options.get('horizontal_group_by'):
            raise UserError(_(
                "Budget comparison does not support horizontal company "
                "groups."
            ))
        unsupported_filters = [
            key for key in (
                'journal_ids', 'partner_ids',
            ) if options.get(key)
        ]
        if unsupported_filters:
            raise UserError(_(
                "Budget comparison cannot apply these dimensions to the "
                "budget baseline: %(dimensions)s.",
                dimensions=', '.join(sorted(unsupported_filters)),
            ))

        actual_options = dict(options)
        if analytic_account_ids:
            requested_analytics = sorted(set(
                int(account_id)
                for account_id in options.get('analytic_account_ids') or ()
            ))
            if (requested_analytics
                    and requested_analytics != analytic_account_ids):
                raise UserError(_(
                    "The report analytic filter must match the selected "
                    "budget's analytic scope exactly."
                ))
            requested_plans = sorted(set(
                int(plan_id)
                for plan_id in options.get('analytic_plan_ids') or ()
            ))
            budget_plan_ids = sorted(set(
                analytic_lines.mapped('analytic_account_id.plan_id').ids,
            ))
            if requested_plans and requested_plans != budget_plan_ids:
                raise UserError(_(
                    "The report analytic-plan filter must match the selected "
                    "budget's analytic plan exactly."
                ))
            # MoveLineQuery applies one allocation weight per matching JSON
            # distribution key.  This makes every actual row, subtotal, net
            # profit, comparison, PDF and XLSX value use the same analytic
            # allocation contract as Budget Pro itself.
            actual_options['analytic_account_ids'] = analytic_account_ids
            actual_options.pop('analytic_plan_ids', None)
        elif (options.get('analytic_account_ids')
              or options.get('analytic_plan_ids')):
            raise UserError(_(
                "A company-wide budget cannot be combined with an analytic "
                "report filter. Select an analytic-scoped budget instead."
            ))

        payload = super().compute(actual_options)
        source_currency = budget.company_id.currency_id
        target_currency = source_currency
        target_id = options.get('presentation_currency_id')
        if target_id:
            try:
                target_id = int(target_id)
            except (TypeError, ValueError, OverflowError) as exc:
                raise UserError(_(
                    "The presentation currency is invalid."
                )) from exc
            target_currency = self.env['res.currency'].browse(
                target_id,
            ).exists()
            if not target_currency:
                raise UserError(_(
                    "The presentation currency no longer exists."
                ))

        # Scope the budget to the report's date window the same way the
        # actuals returned by super() are scoped to options['date'].
        # Without this a Q1 report would compare Q1 actuals against the
        # full-year budget. A budget line whose period overlaps the window
        # is time-apportioned to the fraction of its period that falls
        # inside the window.  Policy is explicit in options and payload meta:
        # ``daily`` preserves proportional inclusive-day apportionment;
        # ``period_exact`` rejects partial periods instead of inventing an
        # allocation the budget owner did not enter.
        date_opt = options.get('date') or {}
        rep_from = fields.Date.to_date(date_opt.get('date_from'))
        rep_to = fields.Date.to_date(date_opt.get('date_to'))
        if not (rep_from and rep_to):
            raise UserError(_(
                "Budget comparison requires a complete report period."
            ))
        apportionment = options.get('budget_apportionment') or 'daily'
        if apportionment not in ('daily', 'period_exact'):
            raise UserError(_(
                "Budget apportionment must be daily or period_exact."
            ))
        conversion_rate = 1.0
        flow_currency_table = None
        if target_id:
            flow_currency_table = self._resolve_flow_currency_table(
                options, [budget.company_id.id], rep_from, rep_to,
            )
        if target_currency != source_currency:
            # Budget and actual are both period flows. Use the exact same
            # day-weighted average table as the parent P&L so a mid-period FX
            # change cannot manufacture a variance between equal source
            # amounts.
            if flow_currency_table is None:  # pragma: no cover - defensive
                raise UserError(_(
                    "Budget presentation-currency translation is missing."
                ))
            conversion_rate = flow_currency_table.rate_map[
                budget.company_id.id
            ]

        raw_amount_by_account = defaultdict(float)
        type_by_account = {}
        account_by_id = {}
        selected_account_ids = {
            int(account_id) for account_id in options.get('account_ids') or []
        }
        selected_account_types = set(options.get('account_type_ids') or [])
        for bline in budget.line_ids:
            account = bline.account_id
            if selected_account_ids and account.id not in selected_account_ids:
                continue
            if (selected_account_types
                    and account.account_type not in selected_account_types):
                continue
            amount = bline.budgeted_amount or 0.0
            if (rep_from and rep_to
                    and bline.period_from and bline.period_to):
                # Overlap of [period_from, period_to] with [rep_from, rep_to],
                # both inclusive, measured in days.
                ov_from = max(bline.period_from, rep_from)
                ov_to = min(bline.period_to, rep_to)
                if ov_to < ov_from:
                    # No overlap; the line falls entirely outside the window.
                    continue
                partial_overlap = (
                    ov_from != bline.period_from
                    or ov_to != bline.period_to
                )
                if apportionment == 'period_exact' and partial_overlap:
                    raise UserError(_(
                        "Budget line %(line)s only partly overlaps the report "
                        "period. Choose Daily overlap apportionment or split "
                        "the budget into exact reporting periods.",
                        line=bline.display_name,
                    ))
                period_days = (bline.period_to - bline.period_from).days + 1
                overlap_days = (ov_to - ov_from).days + 1
                if apportionment == 'daily' and period_days > 0:
                    amount = amount * overlap_days / period_days
            raw_amount_by_account[account.id] += amount
            type_by_account[account.id] = account.account_type
            account_by_id[account.id] = account

        # One rounded target-currency contract drives every account row,
        # section subtotal, net budget, variance, PDF and XLSX consumer.
        amount_by_account = {
            account_id: target_currency.round(amount * conversion_rate)
            for account_id, amount in raw_amount_by_account.items()
        }

        income_budget = target_currency.round(sum(
            amt for acc, amt in amount_by_account.items()
            if type_by_account.get(acc) in self.INCOME_TYPES))
        expense_budget = target_currency.round(sum(
            amt for acc, amt in amount_by_account.items()
            if type_by_account.get(acc) in self.EXPENSE_TYPES))
        net_budget = target_currency.round(income_budget - expense_budget)
        section_budget = {
            'section-income-total': income_budget,
            'section-expenses-total': expense_budget,
        }

        hierarchical = bool(options.get('hierarchical_groups', True))
        group_budget = defaultdict(float)
        if hierarchical:
            for account_id, budget_value in amount_by_account.items():
                account_type = type_by_account.get(account_id)
                if account_type in self.INCOME_TYPES:
                    section_id = 'income'
                elif account_type in self.EXPENSE_TYPES:
                    section_id = 'expenses'
                else:
                    continue
                path = []
                group = account_by_id[account_id].sudo().group_id
                while group:
                    path.append(group.id)
                    group = group.parent_id
                path.reverse()
                for depth in range(1, len(path) + 1):
                    line_id = "section-%s-group-%s" % (
                        section_id,
                        "_".join(str(group_id) for group_id in path[:depth]),
                    )
                    group_budget[line_id] += budget_value

        # Comparison helpers append prior-only rows after the current report.
        # Extract every movable account/group row first, then rebuild it under
        # its authoritative account-type section. This also keeps prior-only
        # rows and groups inside the hierarchy instead of after Net Profit.
        account_lines_by_section = defaultdict(dict)
        group_lines_by_section = defaultdict(dict)
        movable_line_ids = set()
        for line in payload.get('lines', []):
            line_id = line.get('id') or ''
            if line_id.startswith('account-'):
                raw_account_id = line_id.split('-', 1)[1]
                if not raw_account_id.isdigit():
                    continue
                account = self.env['account.account'].sudo().browse(
                    int(raw_account_id),
                ).exists()
                if not account:
                    continue
                if account.account_type in self.INCOME_TYPES:
                    section_id = 'income'
                elif account.account_type in self.EXPENSE_TYPES:
                    section_id = 'expenses'
                else:
                    continue
                account_lines_by_section[section_id][line_id] = line
                movable_line_ids.add(line_id)
                continue
            for section_id in ('income', 'expenses'):
                if line_id.startswith(
                        'section-%s-group-' % section_id):
                    group_lines_by_section[section_id][line_id] = line
                    movable_line_ids.add(line_id)
                    break
        if movable_line_ids:
            payload['lines'] = [
                line for line in payload['lines']
                if line.get('id') not in movable_line_ids
            ]

        # A budget-only account must remain visible even with show_zero=False;
        # otherwise account rows do not foot to their displayed section total.
        visible_account_ids = {
            int(line_id.split('-', 1)[1])
            for lines_by_id in account_lines_by_section.values()
            for line_id in lines_by_id
        }
        missing_by_section = defaultdict(list)
        for account_id, budget_value in amount_by_account.items():
            if account_id in visible_account_ids or target_currency.is_zero(
                    budget_value):
                continue
            account = account_by_id[account_id]
            account_type = type_by_account.get(account_id)
            if account_type in self.INCOME_TYPES:
                section_id = 'income'
                higher_is_better = True
            elif account_type in self.EXPENSE_TYPES:
                section_id = 'expenses'
                higher_is_better = False
            else:
                continue
            missing_by_section[section_id].append({
                'account': account,
                'higher_is_better': higher_is_better,
            })

        def _empty_columns():
            columns = []
            for column in payload.get('columns', [])[1:]:
                figure_type = column.get('figure_type', 'string')
                columns.append({
                    'expression_label': column.get('expression_label'),
                    'value': 0.0 if figure_type in (
                        'monetary', 'float', 'integer', 'percentage',
                    ) else '',
                })
            return columns

        if hierarchical:
            # Rebuild only each section's account/group skeleton from the
            # union of visible actual accounts and non-zero budget accounts.
            # Existing lines retain every comparison column; newly required
            # groups/leaves start at zero actual and receive budget below.
            for section_id in ('income', 'expenses'):
                header_id = 'section-%s-header' % section_id
                total_id = 'section-%s-total' % section_id
                header_at = next(
                    index for index, line in enumerate(payload['lines'])
                    if line.get('id') == header_id
                )
                total_at = next(
                    index for index, line in enumerate(payload['lines'])
                    if line.get('id') == total_id
                )
                existing_by_id = dict(
                    group_lines_by_section.get(section_id, {}),
                )
                existing_by_id.update(
                    account_lines_by_section.get(section_id, {}),
                )
                account_ids = {
                    int(line_id.split('-', 1)[1])
                    for line_id in existing_by_id
                    if line_id.startswith('account-')
                    and line_id.split('-', 1)[1].isdigit()
                }
                account_ids.update(
                    item['account'].id
                    for item in missing_by_section.get(section_id, [])
                )
                accounts = self.env['account.account'].sudo().browse(
                    sorted(account_ids),
                )
                rows = []
                for account in accounts:
                    existing = existing_by_id.get(
                        'account-%s' % account.id,
                    )
                    actual = 0.0
                    if existing:
                        actual = next((
                            column.get('value') or 0.0
                            for column in existing.get('columns', [])
                            if column.get('expression_label') == 'amount'
                        ), 0.0)
                    rows.append({
                        'account_id': account.id,
                        'account_code': account.code or '',
                        'account_name': account.name or '',
                        'amount': actual,
                    })

                skeleton = self._render_account_lines_grouped(
                    rows,
                    section_id=section_id,
                    show_zero=True,
                    unfolded_ids=set(options.get('unfolded_lines') or []),
                    options=options,
                    currency=target_currency,
                )
                rebuilt = []
                for skeleton_line in skeleton:
                    line_id = skeleton_line.get('id')
                    if line_id in existing_by_id:
                        rebuilt.append(existing_by_id[line_id])
                        continue
                    skeleton_line['columns'] = _empty_columns()
                    meta = skeleton_line.setdefault('meta', {})
                    meta['higher_is_better'] = section_id == 'income'
                    if line_id.startswith('account-'):
                        meta['budget_only'] = True
                    rebuilt.append(skeleton_line)
                payload['lines'][header_at + 1:total_at] = rebuilt
        else:
            for section_id in ('income', 'expenses'):
                new_lines = list(
                    account_lines_by_section.get(section_id, {}).values(),
                )
                for item in missing_by_section.get(section_id, []):
                    account = item['account']
                    new_lines.append({
                        'id': 'account-%s' % account.id,
                        'name': '%s %s' % (
                            account.code or '', account.name or '',
                        ),
                        'level': 1,
                        'columns': _empty_columns(),
                        'unfoldable': False,
                        'meta': {
                            'account_id': account.id,
                            'account_code': account.code or '',
                            'higher_is_better': item['higher_is_better'],
                            'budget_only': True,
                        },
                    })
                new_lines.sort(key=lambda line: (
                    line['meta']['account_code'], line['meta']['account_id'],
                ))
                if not new_lines:
                    continue
                total_id = 'section-%s-total' % section_id
                insert_at = next(
                    (index for index, line in enumerate(payload['lines'])
                     if line.get('id') == total_id),
                    len(payload['lines']),
                )
                payload['lines'][insert_at:insert_at] = new_lines

        payload['columns'].append({
            'expression_label': 'budget', 'name': _("Budget"),
            'figure_type': 'monetary'})
        payload['columns'].append({
            'expression_label': 'budget_variance', 'name': _("Budget Var"),
            'figure_type': 'monetary'})
        payload['columns'].append({
            'expression_label': 'budget_variance_pct',
            'name': _("Budget Var %"),
            'figure_type': 'percentage'})

        for line in payload.get('lines', []):
            line_id = line.get('id') or ''
            budget_value = 0.0
            if line_id.startswith('account-'):
                try:
                    budget_value = amount_by_account.get(
                        int(line_id.split('-', 1)[1]), 0.0)
                except ValueError:
                    budget_value = 0.0
            elif line_id in section_budget:
                budget_value = section_budget[line_id]
            elif line_id in group_budget:
                budget_value = group_budget[line_id]
            elif line_id == 'net_profit':
                budget_value = net_budget
            budget_value = target_currency.round(budget_value)
            actual = next(
                (c['value'] for c in line.get('columns', [])
                 if c['expression_label'] == 'amount'), 0.0) or 0.0
            variance = target_currency.round(actual - budget_value)
            variance_pct = (
                variance / abs(budget_value)
                if not target_currency.is_zero(budget_value)
                else ''
            )
            structural = (
                line.get('meta', {}).get('kind') == 'section_header'
            )
            line['columns'].append({
                'expression_label': 'budget',
                'value': '' if structural else budget_value})
            line['columns'].append({
                'expression_label': 'budget_variance',
                'value': '' if structural else variance})
            line['columns'].append({
                'expression_label': 'budget_variance_pct',
                'value': '' if structural else variance_pct})
            line_meta = line.setdefault('meta', {})
            line_meta['budget_variance_convention'] = 'actual_minus_budget'

        totals = payload.setdefault('totals', {})
        totals['income_budget'] = income_budget
        totals['expense_budget'] = expense_budget
        totals['net_budget'] = net_budget
        meta = payload.setdefault('meta', {})
        meta['budget_id'] = budget.id
        meta['budget_company_id'] = budget.company_id.id
        meta['budget_currency_id'] = target_currency.id
        meta['budget_currency_translation_policy'] = (
            'period_average' if flow_currency_table is not None
            else 'identity'
        )
        meta['budget_apportionment'] = apportionment
        meta['budget_actual_scope'] = (
            'analytic_allocation' if analytic_account_ids else 'company'
        )
        meta['budget_analytic_account_ids'] = analytic_account_ids
        meta['budget_variance_convention'] = 'actual_minus_budget'
        meta['unsupported_option_keys'] = [
            'analytic_column_account_ids',
            'analytic_column_plan_ids',
            'comparison_custom_date_from',
            'comparison_custom_date_to',
            'comparison_number',
            'comparison_order',
        ]
        total_figure_types = meta.setdefault('total_figure_types', {})
        total_figure_types.update({
            'income_budget': 'monetary',
            'expense_budget': 'monetary',
            'net_budget': 'monetary',
        })
        return payload
