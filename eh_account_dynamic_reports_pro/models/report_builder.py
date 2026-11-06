# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.report.builder: user-defined custom reports.

A builder record describes a report as an ordered list of lines:

* section_header: a non-numeric label that opens a section.
* account_aggregate: a numeric line that sums account.move.line balances
  filtered by account codes or account types, with a sign flip.
* formula: a numeric line whose value is computed from previously named
  lines using a small whitelisted arithmetic DSL. Used for subtotals,
  computed totals like Net Profit, and ratios.

Publishing a builder registers a corresponding eh.account.dynamic.report
record pointing at the generic builder handler. The published report
shows up in the OWL viewer like any other dynamic report and supports
XLSX, PDF, drill down (where applicable), and the orchestrator's cache.

v1 layout: every builder produces a single value column (Description plus
Amount). Multi-period and comparative columns are a v1.1 expansion.

Builder and line edits bump only their owning companies' report-input
versions, so cached payloads cannot outlive a published definition change.
"""

import ast
import math
import re
import sys

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


_CODE_RE = re.compile(r'^[a-z][a-z0-9_]*$')
_EH_BUILDER_PARENT_UNLINK_CONTEXT = 'eh_builder_parent_unlink_capability'
_EH_BUILDER_PARENT_UNLINK_CAPABILITY = object()

# ``ast.Num`` (and its siblings) were deprecated in Python 3.8 and start
# raising DeprecationWarning in 3.12 ahead of outright removal. On any
# Python >= 3.8 the parser only ever emits ``ast.Constant`` for numeric
# literals, so ``ast.Num`` nodes are never produced by ``ast.parse``
# here. We still keep a *guarded* reference so hand-built or pre-3.8
# parse trees evaluate, while never touching the deprecated alias on
# 3.12+ (where doing so would warn now and fail once it is removed).
# ``isinstance(x, ())`` is always False, so the empty-tuple fallback is a
# no-op both in the whitelist and in the evaluator dispatch below.
_LEGACY_NUMERIC_NODES = ast.Num if sys.version_info < (3, 12) else ()

# AST nodes allowed inside a builder formula. Anything else (function
# calls, attribute access, comprehensions, assignments) is rejected
# before evaluation, so a malicious formula cannot escape the
# evaluator into Python at large.
_ALLOWED_FORMULA_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Name,
    ast.Load,
    ast.Constant,
    _LEGACY_NUMERIC_NODES,
    ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.USub, ast.UAdd,
)

_FORMULA_FIGURE_TYPES = frozenset({
    'monetary', 'percentage', 'float', 'integer',
})


class FormulaError(UserError):
    """Raised when a builder formula is rejected or fails to evaluate."""


def _finite_number(value):
    """Return one finite float or fail before JSON/export serialization."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FormulaError(_(
            "Formula values must be finite numbers."
        )) from exc
    if not math.isfinite(number):
        raise FormulaError(_(
            "Formula values must be finite numbers."
        ))
    return number


def _validated_formula_tree(formula):
    """Parse and validate formula structure without evaluating balances."""
    if not formula or not str(formula).strip():
        return None
    try:
        tree = ast.parse(str(formula), mode='eval')
    except SyntaxError as exc:
        raise FormulaError(_(
            "Formula does not parse: %s",
        ) % exc)
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_FORMULA_NODES):
            raise FormulaError(_(
                "Formula uses a disallowed construct: %s",
            ) % type(node).__name__)
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise FormulaError(_("Only numeric constants are allowed."))
            _finite_number(value)
    return tree


def safe_eval_formula(formula, names):
    """Evaluate a whitelisted arithmetic formula against a names dict.

    Allowed: numeric constants, identifiers (looked up in `names`),
    binary +, -, *, /, unary +, -. Anything else raises FormulaError.
    Unknown identifiers and division by zero fail closed: either condition
    means the authored financial formula is not trustworthy.
    """
    tree = _validated_formula_tree(formula)
    if tree is None:
        return 0.0

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise FormulaError(_("Only numeric constants are allowed."))
            return _finite_number(v)
        if _LEGACY_NUMERIC_NODES and isinstance(node, _LEGACY_NUMERIC_NODES):
            return _finite_number(node.n)  # pre-3.8 parse trees only
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise FormulaError(_(
                    "Unknown formula identifier: %s",
                ) % node.id)
            return _finite_number(names[node.id] or 0.0)
        if isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Add):
                return _finite_number(left + right)
            if isinstance(node.op, ast.Sub):
                return _finite_number(left - right)
            if isinstance(node.op, ast.Mult):
                return _finite_number(left * right)
            if isinstance(node.op, ast.Div):
                if not right:
                    raise FormulaError(_("Formula divides by zero."))
                return _finite_number(left / right)
            raise FormulaError(_("Operator not allowed."))
        if isinstance(node, ast.UnaryOp):
            v = _eval(node.operand)
            if isinstance(node.op, ast.USub):
                return _finite_number(-v)
            if isinstance(node.op, ast.UAdd):
                return _finite_number(+v)
            raise FormulaError(_("Unary operator not allowed."))
        raise FormulaError(_("Node type not allowed."))

    return _eval(tree)


class EhAccountDynamicReport(models.Model):
    _inherit = 'eh.account.dynamic.report'

    _EH_CACHE_DEFINITION_FIELDS = frozenset({
        'code', 'name', 'handler_model', 'description', 'active',
        'company_id',
    })

    company_id = fields.Many2one(
        'res.company',
        index=True,
        copy=False,
        ondelete='cascade',
        help=(
            "Company owning a custom report definition. Standard report "
            "definitions remain global when this field is empty."
        ),
    )


class EhReportBuilder(models.Model):
    _name = 'eh.report.builder'
    _description = "Custom dynamic report builder"
    _order = 'sequence, name'

    name = fields.Char(required=True)
    code = fields.Char(
        required=True,
        copy=False,
        help=(
            "Stable identifier used to publish the report. Must be "
            "lowercase, start with a letter, and contain only letters, "
            "digits, or underscores. Example: my_custom_pl"
        ),
    )
    description = fields.Text()
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )

    line_ids = fields.One2many(
        'eh.report.builder.line',
        'builder_id',
        copy=True,
    )

    is_published = fields.Boolean(default=False, readonly=True)
    published_report_id = fields.Many2one(
        'eh.account.dynamic.report',
        readonly=True,
        copy=False,
        ondelete='set null',
    )

    label_column_name = fields.Char(
        default="Description",
        help="Header label for the leftmost column.",
    )
    amount_column_name = fields.Char(
        default="Amount",
        help="Header label for the value column.",
    )

    _unique_code = models.Constraint(
        'unique(code)',
        'Builder code must be unique.',
    )

    _SERVER_LIFECYCLE_FIELDS = frozenset({
        'is_published', 'published_report_id',
    })

    def _eh_invalidate_builder_cache(self):
        companies = self.mapped('company_id') or self.env.company
        companies._eh_bump_move_version(companies.ids)

    @api.model_create_multi
    def create(self, vals_list):
        protected = []
        for incoming in vals_list:
            vals = dict(incoming or {})
            vals['is_published'] = False
            vals['published_report_id'] = False
            protected.append(vals)
        builders = super().create(protected)
        builders._eh_invalidate_builder_cache()
        return builders

    def write(self, vals):
        if self._SERVER_LIFECYCLE_FIELDS.intersection(vals):
            raise UserError(_(
                "Publish state is controlled by Publish and Unpublish."
            ))
        if 'code' in vals:
            changed_published = self.filtered(
                lambda rec: rec.published_report_id
                and vals.get('code') != rec.code
            )
            if changed_published:
                raise UserError(_(
                    "A published builder code is immutable. Duplicate the "
                    "builder to publish it under a new code."
                ))
        old_companies = self.mapped('company_id')
        result = super().write(vals)
        if {
            'name', 'description', 'sequence', 'active', 'company_id',
        }.intersection(vals):
            for builder in self.filtered('published_report_id'):
                builder.published_report_id.write({
                    'name': builder.name,
                    'description': builder.description or '',
                    'sequence': builder.sequence,
                    'active': bool(builder.active and builder.is_published),
                    'company_id': builder.company_id.id,
                })
        if vals:
            companies = old_companies | self.mapped('company_id')
            if companies:
                companies._eh_bump_move_version(companies.ids)
        return result

    def unlink(self):
        companies = self.mapped('company_id')
        published_reports = self.mapped('published_report_id')
        capable = self.with_context({
            _EH_BUILDER_PARENT_UNLINK_CONTEXT:
                _EH_BUILDER_PARENT_UNLINK_CAPABILITY,
        })
        result = super(EhReportBuilder, capable).unlink()
        if published_reports:
            published_reports.unlink()
        if companies:
            companies._eh_bump_move_version(companies.ids)
        return result

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not _CODE_RE.match(rec.code or ''):
                raise ValidationError(_(
                    "Builder code must match [a-z][a-z0-9_]* (got %r).",
                ) % rec.code)

    def _eh_validate_formula_dependencies(self):
        """Validate line selectors, types, and ordered formula dependencies."""
        for builder in self:
            known = set()
            valid_account_types = {
                value
                for value, _label in self.env['account.account']._fields[
                    'account_type'
                ]._description_selection(self.env)
            }
            for line in builder.line_ids.sorted(
                    lambda item: (item.sequence, item.id)):
                if line.line_type == 'account_aggregate':
                    if line.sign not in ('+', '-'):
                        raise ValidationError(_(
                            "Aggregate line %(line)s requires an explicit "
                            "positive or negative sign.",
                            line=line.display_name,
                        ))
                    if line.account_scope == 'codes':
                        selectors = [
                            value.strip()
                            for value in (line.account_codes or '').split(',')
                            if value.strip()
                        ]
                        if not selectors:
                            raise ValidationError(_(
                                "Aggregate line %(line)s requires at least "
                                "one account-code prefix.",
                                line=line.display_name,
                            ))
                    elif line.account_scope == 'types':
                        selectors = {
                            value.strip()
                            for value in (line.account_types or '').split(',')
                            if value.strip()
                        }
                        if not selectors:
                            raise ValidationError(_(
                                "Aggregate line %(line)s requires at least "
                                "one account type.",
                                line=line.display_name,
                            ))
                        unknown_types = sorted(
                            selectors - valid_account_types,
                        )
                        if unknown_types:
                            raise ValidationError(_(
                                "Aggregate line %(line)s has unknown account "
                                "types: %(types)s.",
                                line=line.display_name,
                                types=', '.join(unknown_types),
                            ))
                    else:
                        raise ValidationError(_(
                            "Aggregate line %(line)s requires an account "
                            "selector scope.",
                            line=line.display_name,
                        ))
                if line.line_type == 'formula' and line.formula:
                    try:
                        tree = ast.parse(str(line.formula), mode='eval')
                    except SyntaxError as exc:
                        raise ValidationError(_(
                            "Formula on line %(line)s does not parse: "
                            "%(reason)s",
                            line=line.display_name,
                            reason=str(exc),
                        )) from exc
                    names = {
                        node.id for node in ast.walk(tree)
                        if isinstance(node, ast.Name)
                    }
                    missing = sorted(names - known)
                    if missing:
                        raise ValidationError(_(
                            "Formula line %(line)s references unknown or "
                            "later line codes: %(codes)s.",
                            line=line.display_name,
                            codes=', '.join(missing),
                        ))
                    try:
                        _validated_formula_tree(line.formula)
                    except FormulaError as exc:
                        raise ValidationError(_(
                            "Formula on line %(line)s is not allowed: "
                            "%(reason)s",
                            line=line.display_name,
                            reason=str(exc),
                        )) from exc
                elif line.line_type == 'formula':
                    raise ValidationError(_(
                        "Formula line %(line)s requires a formula.",
                        line=line.display_name,
                    ))
                if (
                    line.line_type == 'formula'
                    and line.figure_type not in _FORMULA_FIGURE_TYPES
                ):
                    raise ValidationError(_(
                        "Formula line %(line)s requires a supported display "
                        "type.",
                        line=line.display_name,
                    ))
                if line.line_type != 'section_header' and line.code:
                    known.add(line.code)
        return True

    # ---- publish lifecycle ----

    @api.model
    def _eh_lock_definition_table(self, table_name):
        """Serialize rare definition-code allocation against all writers."""
        if table_name not in {
            'eh_account_dynamic_report', 'eh_report_builder',
        }:
            raise ValueError("Unsupported definition table lock")
        self.env.cr.execute(
            "LOCK TABLE %s IN SHARE ROW EXCLUSIVE MODE" % table_name,
        )

    def action_publish(self):
        DynamicReport = self.env['eh.account.dynamic.report']
        for rec in self:
            rec._eh_validate_formula_dependencies()
            rec._eh_lock_definition_table('eh_account_dynamic_report')
            conflict = DynamicReport.with_context(active_test=False).search([
                ('code', '=', rec.code),
                ('id', '!=', rec.published_report_id.id or 0),
            ], limit=1)
            if conflict:
                raise UserError(_(
                    "Report code '%(code)s' is already used by %(report)s. "
                    "Choose another builder code before publishing.",
                    code=rec.code,
                    report=conflict.display_name,
                ))
            if rec.published_report_id:
                if rec.published_report_id.code != rec.code:
                    raise UserError(_(
                        "Published report code does not match this builder. "
                        "Unpublish and review the definition before retrying."
                    ))
                rec.published_report_id.write({
                    'name': rec.name,
                    'description': rec.description or '',
                    'sequence': rec.sequence,
                    'active': rec.active,
                    'company_id': rec.company_id.id,
                })
            else:
                report = DynamicReport.create({
                    'code': rec.code,
                    'name': rec.name,
                    'handler_model': 'eh.account.dynamic.report.handler.builder',
                    'sequence': rec.sequence,
                    'description': rec.description or '',
                    'active': rec.active,
                    'company_id': rec.company_id.id,
                })
                super(EhReportBuilder, rec).write({
                    'published_report_id': report.id,
                })
            super(EhReportBuilder, rec).write({'is_published': True})
        return True

    def action_unpublish(self):
        for rec in self:
            if rec.published_report_id:
                rec.published_report_id.active = False
            super(EhReportBuilder, rec).write({'is_published': False})
        return True

    def action_open_viewer(self):
        self.ensure_one()
        if not self.is_published or not self.published_report_id:
            raise UserError(_(
                "Builder must be published before it can be viewed.",
            ))
        return {
            'type': 'ir.actions.client',
            'tag': 'eh_account_dynamic_report',
            'name': self.name,
            'context': {'report_code': self.code},
        }

    def action_duplicate(self):
        """Clone builders under deterministic, collision-free codes."""
        new_records = self.env[self._name]
        for rec in self:
            rec._eh_lock_definition_table('eh_report_builder')
            base_code = rec.code + '_copy'
            code = base_code
            suffix = 2
            while self.with_context(active_test=False).search_count([
                ('code', '=', code),
            ]):
                code = '%s_%s' % (base_code, suffix)
                suffix += 1
            copy = rec.copy({
                'name': rec.name + " (copy)",
                'code': code,
                'is_published': False,
                'published_report_id': False,
            })
            new_records |= copy
        if len(new_records) == 1:
            return {
                'type': 'ir.actions.act_window',
                'res_model': self._name,
                'res_id': new_records.id,
                'view_mode': 'form',
                'views': [(False, 'form')],
                'target': 'current',
            }
        return True


class EhReportBuilderLine(models.Model):
    _name = 'eh.report.builder.line'
    _description = "Custom report builder line"
    _order = 'sequence, id'

    builder_id = fields.Many2one(
        'eh.report.builder',
        required=True,
        ondelete='cascade',
        index=True,
    )
    sequence = fields.Integer(default=10)

    name = fields.Char(required=True)
    code = fields.Char(
        help=(
            "Optional stable name referenced by formula lines. Must match "
            "[a-z][a-z0-9_]* if set."
        ),
    )

    line_type = fields.Selection(
        [
            ('section_header', "Section Header"),
            ('account_aggregate', "Account Aggregate"),
            ('formula', "Formula"),
        ],
        required=True,
        default='account_aggregate',
    )
    level = fields.Integer(
        default=1,
        help=(
            "0 for section headers and computed totals (rendered bold), "
            "1 for data rows."
        ),
    )

    # account_aggregate fields
    account_scope = fields.Selection(
        [
            ('codes', "Account Codes"),
            ('types', "Account Types"),
        ],
        default='codes',
        required=True,
    )
    account_codes = fields.Char(
        help=(
            "Comma separated code prefixes (matches account.code LIKE "
            "'<prefix>%'). Example: 1000,1100,2000"
        ),
    )
    account_types = fields.Char(
        help=(
            "Comma separated account_type values. Example: "
            "income,income_other or expense,expense_direct_cost"
        ),
    )
    sign = fields.Selection(
        [
            ('+', "Positive (+)"),
            ('-', "Negative (-)"),
        ],
        default='+',
        required=True,
    )
    aggregate_basis = fields.Selection(
        [
            ('period', "Period Movement"),
            ('closing', "Closing Balance"),
        ],
        default='period',
        required=True,
        help=(
            "Period Movement sums journal items from Date From through Date "
            "To. Closing Balance sums all journal items through Date To, for "
            "balance-sheet accounts."
        ),
    )

    # formula fields
    formula = fields.Char(
        help=(
            "Whitelisted arithmetic referencing other line codes. Example: "
            "income - expenses, or revenue * 0.1 for a 10 percent flag. "
            "Allowed: + - * / and parentheses. Identifiers must map to "
            "other lines' computed values; unknown names fail closed."
        ),
    )
    figure_type = fields.Selection(
        [
            ('monetary', "Monetary"),
            ('percentage', "Percentage"),
            ('float', "Number"),
            ('integer', "Integer"),
        ],
        default='monetary',
        required=True,
        help=(
            "Display and export type for formula results. Percentages use "
            "fractions: 0.25 is rendered as 25%."
        ),
    )
    is_section_total = fields.Boolean(
        default=False,
        help=(
            "Style this formula line as a section total (top border, "
            "bold). Otherwise it renders as a regular computed line."
        ),
    )

    def _eh_invalidate_builder_cache(self):
        companies = self.mapped('builder_id.company_id') or self.env.company
        companies._eh_bump_move_version(companies.ids)

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._eh_invalidate_builder_cache()
        published = lines.mapped('builder_id').filtered('is_published')
        published._eh_validate_formula_dependencies()
        return lines

    def write(self, vals):
        builders = self.mapped('builder_id')
        old_companies = self.mapped('builder_id.company_id')
        result = super().write(vals)
        builders |= self.mapped('builder_id')
        builders.filtered('is_published')._eh_validate_formula_dependencies()
        if vals:
            companies = old_companies | self.mapped('builder_id.company_id')
            companies._eh_bump_move_version(companies.ids)
        return result

    def unlink(self):
        builders = self.mapped('builder_id')
        companies = self.mapped('builder_id.company_id')
        result = super().unlink()
        parent_unlink = (
            self.env.context.get(_EH_BUILDER_PARENT_UNLINK_CONTEXT)
            is _EH_BUILDER_PARENT_UNLINK_CAPABILITY
        )
        if not parent_unlink:
            builders.filtered(
                'is_published')._eh_validate_formula_dependencies()
        if companies:
            companies._eh_bump_move_version(companies.ids)
        return result

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if rec.code and not _CODE_RE.match(rec.code):
                raise ValidationError(_(
                    "Line code must match [a-z][a-z0-9_]* (got %r).",
                ) % rec.code)

    @api.constrains('builder_id', 'code')
    def _check_unique_code_per_builder(self):
        for rec in self:
            if not rec.code:
                continue
            others = rec.builder_id.line_ids.filtered(
                lambda l: l.code == rec.code and l.id != rec.id,
            )
            if others:
                raise ValidationError(_(
                    "Line code %r is duplicated within builder %s.",
                ) % (rec.code, rec.builder_id.name))
