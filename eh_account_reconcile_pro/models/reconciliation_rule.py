# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
User-defined reconciliation rules.

The free heuristic engine in suggestion_engine.py scores candidates from
five signals (amount, date, partner, reference, history). Rules go a step
further: a manager defines a deterministic pattern that, when matched,
either auto-confirms a match or produces a high-confidence suggestion
the user can accept with one click.

Typical rule examples:

* "Bank fee under 10 dollars referenced as 'FEE' is a write off to the
  Bank Charges account."
* "Payment ref matching ^INV-\\d{6}$ between 100 and 50000 dollars
  matches receivable AML by amount and partner."
* "Anything credited from journal X partner Y always goes to expense
  account Z."

Rules are evaluated AFTER the heuristic engine. A rule that fires raises
the score by `score_boost` (default 0.5) and tags `rules_fired` with the
rule's code so the audit trail records which rule confirmed the match.

Sequence drives evaluation order. The first rule to fully match wins for
auto-confirm rules; suggestion-only rules can stack their boosts.
"""

import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError


_CODE_RE = re.compile(r'^[a-z][a-z0-9_]*$')
_RULE_ENGINE_FIELDS = frozenset({'fire_count', 'last_fired_at'})


class EhReconciliationRule(models.Model):
    _name = 'eh.reconciliation.rule'
    _description = "Bank reconciliation rule"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(
        required=True, copy=False,
        help="Stable identifier surfaced in the audit log when the rule fires.",
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )

    # ---- scope: when does this rule apply ----

    journal_ids = fields.Many2many(
        'account.journal',
        'eh_recon_rule_journal_rel',
        'rule_id', 'journal_id',
        domain="[('type', 'in', ['bank', 'cash'])]",
        check_company=True,
        help="Limit this rule to specific bank or cash journals. Empty means all bank/cash journals in the company.",
    )

    rule_type = fields.Selection(
        [
            ('match', "Suggest a match against a journal item"),
            ('write_off', "Auto write-off to a fixed account"),
        ],
        required=True, default='match',
    )

    # ---- pattern matching against the statement line ----

    payment_ref_regex = fields.Char(
        string="Payment ref regex",
        help="Optional. Statement line payment_ref must match this regex (Python re syntax). Anchored with ^ and $ if you want full match.",
    )
    narration_regex = fields.Char(
        string="Narration regex",
        help="Optional. Statement line narration must match this regex.",
    )
    amount_min = fields.Float(
        digits=(16, 2),
        help="Optional. Statement line absolute amount must be >= this. Leave 0 for no lower bound.",
    )
    amount_max = fields.Float(
        digits=(16, 2),
        help="Optional. Statement line absolute amount must be <= this. Leave 0 for no upper bound.",
    )
    direction = fields.Selection(
        [
            ('any', "Any"),
            ('credit', "Credit only (money in)"),
            ('debit', "Debit only (money out)"),
        ],
        default='any', required=True,
    )

    # ---- write-off target ----

    writeoff_account_id = fields.Many2one(
        'account.account',
        check_company=True,
        help="Required for write-off rules. The account the residual is written off to.",
    )
    writeoff_label = fields.Char(
        translate=True,
        help="Label written on the write-off journal item.",
    )
    writeoff_amount_type = fields.Selection(
        [
            ('residual', "Full residual"),
            ('fixed', "Fixed amount"),
            ('percentage', "Percentage of statement amount"),
        ],
        default='residual', required=True,
        help="How much of the statement line to write off: the whole "
             "remaining residual, a fixed amount (capped at the residual), "
             "or a percentage of the statement amount.",
    )
    writeoff_amount = fields.Float(
        digits=(16, 2),
        help="The fixed amount or the percentage value, interpreted "
             "according to writeoff_amount_type. Ignored for 'residual'.",
    )

    # ---- match boost ----

    partner_id = fields.Many2one(
        'res.partner',
        check_company=True,
        help="Optional. When set, the rule only suggests AMLs for this partner.",
    )
    score_boost = fields.Float(
        default=0.5, digits=(3, 2),
        help="Confidence boost added to the suggestion score when this rule fires (range 0.0 to 1.0).",
    )
    auto_confirm = fields.Boolean(
        default=False,
        help="When enabled, a suggestion produced by this rule is auto-confirmed instead of waiting for a user click. Use sparingly: requires high pattern specificity.",
    )

    # ---- audit ----

    fire_count = fields.Integer(
        default=0, readonly=True,
        help="Total number of times this rule has fired across all sessions.",
    )
    last_fired_at = fields.Datetime(readonly=True)

    notes = fields.Text()

    _unique_code_company = models.Constraint(
        'unique(code, company_id)',
        'Rule code must be unique per company.',
    )
    _check_score_boost = models.Constraint(
        'CHECK (score_boost >= 0 AND score_boost <= 1)',
        'Score boost must be between 0.0 and 1.0.',
    )
    _check_amounts = models.Constraint(
        'CHECK ((amount_min = 0 AND amount_max = 0) OR amount_min <= amount_max OR amount_max = 0)',
        'amount_max must be greater than or equal to amount_min when both are set.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(
                _RULE_ENGINE_FIELDS.intersection(vals) for vals in vals_list):
            raise AccessError(_(
                "Reconciliation rule usage counters are server-owned."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and _RULE_ENGINE_FIELDS.intersection(vals):
            raise AccessError(_(
                "Reconciliation rule usage counters are server-owned."
            ))
        return super().write(vals)

    @api.constrains('code')
    def _check_code_format(self):
        for rec in self:
            if not _CODE_RE.match(rec.code or ''):
                raise ValidationError(_(
                    "Rule code must match [a-z][a-z0-9_]* (got %r).",
                ) % rec.code)

    @api.constrains('rule_type', 'writeoff_account_id', 'active')
    def _check_writeoff_target(self):
        for rec in self:
            if rec.active and rec.rule_type == 'write_off' and not rec.writeoff_account_id:
                raise ValidationError(_(
                    "Active write-off rules require a writeoff_account_id.",
                ))

    @api.constrains(
        'company_id', 'journal_ids', 'writeoff_account_id', 'partner_id',
    )
    def _check_rule_company_scope(self):
        """Keep every rule dependency inside its declared company.

        ``check_company`` gives the normal UI/ORM protection.  This explicit
        constraint is the fail-closed backstop for elevated imports and for
        the account model's 16/17 singular versus 18/19 plural company field.
        """
        for rec in self:
            invalid_journals = rec.journal_ids.filtered(
                lambda journal: (
                    journal.company_id != rec.company_id
                    or journal.type not in ('bank', 'cash')
                )
            )
            if invalid_journals:
                raise ValidationError(_(
                    "Every rule journal must be a bank or cash journal in "
                    "the rule company."
                ))
            if rec.writeoff_account_id:
                account = rec.writeoff_account_id
                account_companies = (
                    account.company_ids
                    if 'company_ids' in account._fields
                    else account.company_id
                )
                if rec.company_id not in account_companies:
                    raise ValidationError(_(
                        "The write-off account must be available to the "
                        "rule company."
                    ))
            if (rec.partner_id.company_id
                    and rec.partner_id.company_id != rec.company_id):
                raise ValidationError(_(
                    "The rule partner must be global or belong to the rule "
                    "company."
                ))

    @api.constrains('writeoff_amount_type', 'writeoff_amount', 'active',
                    'rule_type')
    def _check_writeoff_amount(self):
        for rec in self:
            if not (rec.active and rec.rule_type == 'write_off'):
                continue
            if rec.writeoff_amount_type in ('fixed', 'percentage') and (
                    rec.writeoff_amount <= 0):
                raise ValidationError(_(
                    "A %(t)s write-off requires a positive writeoff_amount.",
                    t=rec.writeoff_amount_type,
                ))
            if rec.writeoff_amount_type == 'percentage' and (
                    rec.writeoff_amount > 100):
                raise ValidationError(_(
                    "A percentage write-off cannot exceed 100 (got %s).",
                ) % rec.writeoff_amount)

    @api.constrains('payment_ref_regex', 'narration_regex')
    def _check_regex(self):
        for rec in self:
            for value, label in (
                (rec.payment_ref_regex, 'payment_ref_regex'),
                (rec.narration_regex, 'narration_regex'),
            ):
                if not value:
                    continue
                try:
                    re.compile(value)
                except re.error as exc:
                    raise ValidationError(_(
                        "%(label)s is not a valid Python regex: %(err)s",
                        label=label, err=str(exc),
                    ))

    # ---- evaluation ----

    def matches_statement_line(self, statement_line):
        """Return True if this rule's filters match the given statement line.

        Pure read; does not mutate state. Caller decides what to do with
        the match (suggest, write off, auto confirm).
        """
        self.ensure_one()
        if not self.active:
            return False
        if statement_line.company_id != self.company_id:
            return False
        if self.journal_ids and statement_line.journal_id not in self.journal_ids:
            return False
        amount = abs(statement_line.amount or 0.0)
        if self.amount_min and amount < self.amount_min:
            return False
        if self.amount_max and amount > self.amount_max:
            return False
        if self.direction == 'credit' and (statement_line.amount or 0.0) < 0:
            return False
        if self.direction == 'debit' and (statement_line.amount or 0.0) > 0:
            return False
        if self.payment_ref_regex:
            if not statement_line.payment_ref:
                return False
            if not re.search(self.payment_ref_regex, statement_line.payment_ref):
                return False
        if self.narration_regex:
            narration = statement_line.narration or ''
            if not re.search(self.narration_regex, narration):
                return False
        return True

    def compute_writeoff_amount(self, statement_line):
        """Magnitude to write off for this rule against a statement line.

        * residual: the whole statement amount (the caller caps it at the
          actual open residual).
        * fixed: the configured amount, capped at the statement amount.
        * percentage: that percentage of the statement amount.

        Always returns a non-negative magnitude; the caller applies the
        sign that closes the suspense line.
        """
        self.ensure_one()
        base = abs(statement_line.amount or 0.0)
        if self.writeoff_amount_type == 'fixed':
            return min(self.writeoff_amount, base) if base else \
                self.writeoff_amount
        if self.writeoff_amount_type == 'percentage':
            return round(base * (self.writeoff_amount / 100.0), 2)
        return base

    @api.model
    def learn_from_history(self, min_support=3, min_purity=0.8,
                           history_limit=2000):
        """Create match rules from recurring reconciliation patterns.

        Clean-room frequency learning: scan past 'match' audit rows and,
        for each statement-text token, count how often it co-occurs with
        each partner. When a token maps to one partner at least
        `min_support` times and that partner accounts for at least
        `min_purity` of the token's matches, a match rule is created that
        boosts (case-insensitively) any future line carrying the token
        toward that partner. Idempotent: a token whose learned rule code
        already exists is skipped. Returns the rules created this run.
        """
        from collections import defaultdict
        engine = self.env['eh.reconciliation.suggestion.engine']
        company = self.env.company
        audits = self.env['eh.reconciliation.audit'].search(
            [('decision', '=', 'match'), ('aml_id', '!=', False),
             ('statement_line_id', '!=', False)],
            order='id desc', limit=history_limit)

        token_partner = defaultdict(lambda: defaultdict(int))
        token_total = defaultdict(int)
        for audit in audits:
            line = audit.statement_line_id
            aml = audit.aml_id
            if aml.company_id != company:
                continue
            partner = aml.partner_id or line.partner_id
            if not partner:
                continue
            tokens = engine._tokenize(engine._gather_text(
                getattr(line, 'payment_ref', None),
                getattr(line, 'ref', None),
                getattr(line, 'narration', None),
            ))
            for token in tokens:
                token_partner[token][partner.id] += 1
                token_total[token] += 1

        created = self.browse()
        for token, partners in token_partner.items():
            best_partner = max(partners, key=partners.get)
            support = partners[best_partner]
            total = token_total[token] or 1
            if support < min_support or (support / total) < min_purity:
                continue
            code = 'learned_%s' % token
            if self.search([('code', '=', code),
                            ('company_id', '=', company.id)], limit=1):
                continue
            created |= self.create({
                'name': "Learned: %s" % token,
                'code': code,
                'rule_type': 'match',
                'partner_id': best_partner,
                'payment_ref_regex': r'(?i)\b%s\b' % re.escape(token),
                'score_boost': 0.5,
                'auto_confirm': False,
                'notes': "Auto-learned from %d historical matches "
                         "(%.0f%% to this partner)." % (
                             support, 100.0 * support / total),
            })
        return created

    def action_learn_from_history(self):
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can learn new rules."
            ))
        created = self.learn_from_history()
        if not created:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("No new rules"),
                    'message': _(
                        "No sufficiently supported new pattern was found."
                    ),
                    'type': 'info',
                    'sticky': False,
                },
            }
        return {
            'type': 'ir.actions.act_window',
            'name': _("Learned Reconciliation Rules"),
            'res_model': 'eh.reconciliation.rule',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created.ids)],
        }

    @api.private
    def record_fire(self):
        """Bump the fire counter atomically.

        Atomic counter increments protect against the read-modify-write
        race that would otherwise lose increments under concurrent
        reconciliation activity. Mirrors the pattern in
        eh.reconciliation.session._increment_counters.
        """
        if not self:
            return
        if not self.env.su:
            raise AccessError(_(
                "Rule usage telemetry can only be updated by the private "
                "reconciliation engine."
            ))
        from odoo.tools import SQL
        self.flush_recordset(['fire_count'])
        self.env.cr.execute(SQL(
            "UPDATE eh_reconciliation_rule "
            "SET fire_count = fire_count + 1, "
            "    last_fired_at = NOW() AT TIME ZONE 'UTC' "
            "WHERE id IN %s",
            tuple(self.ids),
        ))
        self.invalidate_recordset(['fire_count', 'last_fired_at'])
