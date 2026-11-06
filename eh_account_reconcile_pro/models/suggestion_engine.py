# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Heuristic suggestion engine for bank statement reconciliation.

Given a bank statement line, scores candidate journal items by combining
five signals: amount match, date proximity, partner match, reference text
similarity, and historical pattern lookup. Returns top N candidates above
a configurable threshold.

Each signal is a function of (statement_line, aml) returning a value in
[0.0, 1.0]. Signals are then weighted and summed; default weights are
tuned for typical SMB bank flows. Localizations or larger deployments
override _DEFAULT_WEIGHTS via inheritance.

Why an Odoo AbstractModel rather than a plain Python class:

* Localizations can _inherit and override individual heuristics (for
  example, replace _score_reference with a regex tuned to local bank
  memo conventions).
* The engine has env access for direct SQL on historical patterns.
* Tests can dispatch through env[model] cleanly, mock individual rules.
"""

import re
from datetime import timedelta

from odoo import api, fields, models
from odoo.osv import expression


_TOKEN_RE = re.compile(r'\w+', re.UNICODE)
_MIN_TOKEN_LEN = 3


class EhSuggestionEngine(models.AbstractModel):
    _name = 'eh.reconciliation.suggestion.engine'
    _description = "Bank reconciliation suggestion engine"

    _DEFAULT_WEIGHTS = {
        'amount': 0.40,
        'date': 0.20,
        'partner': 0.25,
        'reference': 0.10,
        'history': 0.05,
    }

    DEFAULT_THRESHOLD = 0.30
    DEFAULT_LIMIT = 10
    CANDIDATE_FETCH_LIMIT = 200

    # Date proximity falls off linearly over this window in days.
    DATE_WINDOW_DAYS = 60

    # Amount match tolerances.
    AMOUNT_TOL_EXACT = 0.0
    AMOUNT_TOL_NEAR = 0.01
    AMOUNT_TOL_LOOSE = 0.05

    # ---- public api ----

    @api.model
    def find_suggestions(self, statement_line, limit=None, threshold=None,
                         candidate_amls=None):
        """Return scored suggestions for a statement line.

        :param statement_line: account.bank.statement.line record.
        :param limit: max number of suggestions returned (default 10).
        :param threshold: minimum total score (default 0.30).
        :param candidate_amls: optional pre-filtered AML recordset. If None,
            the engine queries unreconciled receivable/payable and configured
            outstanding-payment lines in the same company.
        :return: list of dicts with keys aml_id, score, breakdown,
            rules_fired. Sorted by score descending.
        """
        limit = limit if limit is not None else self.DEFAULT_LIMIT
        threshold = (
            threshold if threshold is not None else self.DEFAULT_THRESHOLD
        )
        if candidate_amls is None:
            candidate_amls = self._fetch_candidates(statement_line)

        # Resolve the statement-wide inputs once.  The former implementation
        # searched the rule table, evaluated every regex, and counted audit
        # history again for every candidate.  That made one workspace click
        # grow linearly in database queries as the candidate pool grew.
        statement_tokens = self._tokenize(self._gather_text(
            getattr(statement_line, 'payment_ref', None),
            getattr(statement_line, 'ref', None),
            getattr(statement_line, 'narration', None),
        ))
        Rule = self.env['eh.reconciliation.rule']
        matching_rules = Rule.search([
            ('active', '=', True),
            ('rule_type', '=', 'match'),
            ('company_id', '=', statement_line.company_id.id),
        ], order='sequence, id').filtered(
            lambda rule: rule.matches_statement_line(statement_line)
        )
        partner_ids = candidate_amls.partner_id.ids
        history_counts = {partner_id: 0 for partner_id in partner_ids}
        if partner_ids:
            ninety_days_ago = (
                fields.Date.context_today(self) - timedelta(days=90)
            )
            audits = self.env['eh.reconciliation.audit'].search([
                ('aml_id.partner_id', 'in', partner_ids),
                ('decision', '=', 'match'),
                ('decided_at', '>=', ninety_days_ago),
                ('aml_id.company_id', '=', statement_line.company_id.id),
            ])
            for audit in audits:
                partner_id = audit.aml_id.partner_id.id
                if partner_id in history_counts:
                    history_counts[partner_id] += 1

        scored = []
        for aml in candidate_amls:
            score = self.score_match(
                statement_line, aml,
                statement_tokens=statement_tokens,
                matching_rules=matching_rules,
                recent_history_count=history_counts.get(
                    aml.partner_id.id, 0,
                ),
            )
            if score['total'] >= threshold:
                scored.append({
                    'aml_id': aml.id,
                    'score': round(score['total'], 4),
                    'breakdown': {
                        k: round(v, 4) for k, v in score['breakdown'].items()
                    },
                    'rules_fired': score['rules_fired'],
                })
        scored.sort(key=lambda x: x['score'], reverse=True)
        return scored[:limit]

    @api.model
    def score_match(self, statement_line, aml, weights=None,
                    statement_tokens=None, matching_rules=None,
                    recent_history_count=None):
        """Compute a combined match score for one (statement_line, aml) pair.

        Heuristic signals run first; user-defined rules then add their
        boost on top. A rule with a partner_id filter only contributes
        when the candidate AML matches that partner. Rules that fire
        also stamp the rules_fired list with their codes so the audit
        log can show which rule confirmed the match.

        :return: dict with keys total, breakdown (per-signal scores),
            rules_fired (signal and rule codes that contributed).
        """
        weights = weights or self._DEFAULT_WEIGHTS
        breakdown = {
            'amount': self._score_amount(statement_line, aml),
            'date': self._score_date(statement_line, aml),
            'partner': self._score_partner(statement_line, aml),
            'reference': self._score_reference(
                statement_line, aml, statement_tokens=statement_tokens,
            ),
            'history': self._score_history(
                statement_line, aml, recent_count=recent_history_count,
            ),
        }
        total = sum(breakdown[k] * weights.get(k, 0.0) for k in breakdown)
        rules_fired = sorted(k for k, v in breakdown.items() if v > 0)

        rule_boost, rule_codes = self._apply_rules(
            statement_line, aml, matching_rules=matching_rules,
        )
        if rule_boost:
            total = min(1.0, total + rule_boost)
            rules_fired = rules_fired + ['rule:%s' % c for c in rule_codes]

        return {
            'total': total,
            'breakdown': breakdown,
            'rules_fired': rules_fired,
        }

    @api.model
    def _apply_rules(self, statement_line, aml, matching_rules=None):
        """Return (boost, list_of_rule_codes) for the rules that match.

        A rule with `partner_id` set only contributes when the AML's
        partner matches; this prevents a partner-specific rule from
        boosting unrelated candidates. Other rule shapes (write-off
        rules, journal-only rules) do not boost AML candidates and are
        evaluated separately by the workspace before suggestions are
        even fetched.
        """
        rules = matching_rules
        if rules is None:
            Rule = self.env['eh.reconciliation.rule']
            rules = Rule.search([
                ('active', '=', True),
                ('rule_type', '=', 'match'),
                ('company_id', '=', statement_line.company_id.id),
            ], order='sequence, id')
        boost = 0.0
        codes = []
        for rule in rules:
            if (matching_rules is None
                    and not rule.matches_statement_line(statement_line)):
                continue
            if rule.partner_id and aml.partner_id != rule.partner_id:
                continue
            boost += rule.score_boost
            codes.append(rule.code)
        return boost, codes

    # ---- candidate pool ----

    @api.model
    def _fetch_candidates(self, statement_line, search_term=None):
        """Return open control items a bank line can actually clear.

        Invoices live on receivable/payable. A payment registered before its
        bank statement arrives lives on the payment method line's configured
        outstanding account instead. Both are first-class reconciliation
        candidates; arbitrary current-asset/liability accounts are not.

        ``search_term`` is an optional server-side item search over the move,
        journal-item, partner and account identifiers. Record rules remain in
        force because neither journals nor move lines are elevated.
        """
        company_id = statement_line.company_id.id
        journals = self.env['account.journal'].search([
            ('company_id', '=', company_id),
            ('type', 'in', ('bank', 'cash')),
        ])
        payment_method_lines = (
            journals.mapped('inbound_payment_method_line_ids')
            | journals.mapped('outbound_payment_method_line_ids')
        )
        outstanding_accounts = payment_method_lines.mapped(
            'payment_account_id'
        )
        domain = [
            ('company_id', '=', company_id),
            ('amount_residual', '!=', 0),
            ('parent_state', '=', 'posted'),
            ('reconciled', '=', False),
            ('move_id', '!=', statement_line.move_id.id),
        ]
        account_domain = [[
            ('account_id.account_type', 'in', (
                'asset_receivable', 'liability_payable',
            )),
        ]]
        if outstanding_accounts:
            account_domain.append([
                ('account_id', 'in', outstanding_accounts.ids),
            ])
        domain = expression.AND([domain, expression.OR(account_domain)])
        search_term = (search_term or '').strip()
        if search_term:
            search_domain = expression.OR([
                [(field_name, 'ilike', search_term)]
                for field_name in (
                    'move_id.name', 'move_id.ref', 'ref', 'name',
                    'partner_id.name', 'account_id.code', 'account_id.name',
                )
            ])
            domain = expression.AND([domain, search_domain])
        return self.env['account.move.line'].search(
            domain, limit=self.CANDIDATE_FETCH_LIMIT,
        )

    # ---- individual heuristics ----

    @api.model
    def _score_amount(self, statement_line, aml):
        """1.0 exact, 0.9 within 1 percent, 0.5 within 5 percent, else 0."""
        sl_amount = abs(float(
            self._statement_amount_company_currency(statement_line)
        ))
        aml_amount = abs(float(aml.amount_residual or 0.0))
        if sl_amount == 0.0 or aml_amount == 0.0:
            return 0.0
        if sl_amount == aml_amount:
            return 1.0
        diff_ratio = abs(sl_amount - aml_amount) / max(sl_amount, aml_amount)
        if diff_ratio <= self.AMOUNT_TOL_NEAR:
            return 0.9
        if diff_ratio <= self.AMOUNT_TOL_LOOSE:
            return 0.5
        return 0.0

    @api.model
    def _score_date(self, statement_line, aml):
        """Linear fall off over DATE_WINDOW_DAYS; same date scores 1.0."""
        sl_date = statement_line.date
        aml_date = aml.date
        if not sl_date or not aml_date:
            return 0.0
        days = abs((sl_date - aml_date).days)
        if days == 0:
            return 1.0
        if days >= self.DATE_WINDOW_DAYS:
            return 0.0
        return 1.0 - (days / self.DATE_WINDOW_DAYS)

    @api.model
    def _score_partner(self, statement_line, aml):
        """Partner match. 1.0 exact, 0.85 commercial parent, 0.5 if the
        statement has no partner at all (uncertain), 0 otherwise.
        """
        aml_partner = aml.partner_id
        if not aml_partner:
            return 0.0
        sl_partner = statement_line.partner_id
        if not sl_partner:
            return 0.5
        if sl_partner.id == aml_partner.id:
            return 1.0
        sl_commercial = sl_partner.commercial_partner_id
        aml_commercial = aml_partner.commercial_partner_id
        if sl_commercial and aml_commercial and sl_commercial.id == aml_commercial.id:
            return 0.85
        return 0.0

    @api.model
    def _score_reference(self, statement_line, aml, statement_tokens=None):
        """Token overlap between the statement memo / ref and the AML
        move name / ref / label. Returns 1.0 for full token-set equality,
        0.7 for >= 50 percent overlap, 0.3 for any overlap, 0 otherwise.
        Tokens shorter than _MIN_TOKEN_LEN are ignored as too generic.
        """
        aml_text = self._gather_text(
            aml.move_id.name if aml.move_id else None,
            aml.ref,
            aml.name,
        )
        if statement_tokens is None:
            sl_text = self._gather_text(
                getattr(statement_line, 'payment_ref', None),
                getattr(statement_line, 'ref', None),
                getattr(statement_line, 'narration', None),
            )
            sl_tokens = self._tokenize(sl_text)
        else:
            sl_tokens = statement_tokens
        aml_tokens = self._tokenize(aml_text)
        if not sl_tokens or not aml_tokens:
            return 0.0
        if sl_tokens == aml_tokens:
            return 1.0
        common = sl_tokens & aml_tokens
        if not common:
            return 0.0
        ratio = len(common) / max(len(sl_tokens), len(aml_tokens))
        if ratio >= 0.5:
            return 0.7
        return 0.3

    @api.model
    def _score_history(self, statement_line, aml, recent_count=None):
        """Boost based on how many times this partner has been matched
        recently in the audit log. Encourages convergence toward a
        recognised partner pattern.

        The audit lookup runs without sudo so the suggestion respects
        record rules: a user only sees patterns from audit rows their
        own access permits, which keeps cross-company reconciliation
        history from leaking into the heuristic. The query is also
        scoped to the current company so a multi-company user does not
        get scored on activity from a sibling company they happen to
        have read on, but did not perform the reconciliation in.
        """
        partner = aml.partner_id
        if not partner:
            return 0.0
        if recent_count is None:
            ninety_days_ago = (
                fields.Date.context_today(self) - timedelta(days=90)
            )
            company_id = statement_line.company_id.id
            recent_count = self.env[
                'eh.reconciliation.audit'
            ].search_count([
                ('aml_id.partner_id', '=', partner.id),
                ('decision', '=', 'match'),
                ('decided_at', '>=', ninety_days_ago),
                ('aml_id.company_id', '=', company_id),
            ])
        if recent_count >= 5:
            return 1.0
        if recent_count >= 2:
            return 0.5
        if recent_count >= 1:
            return 0.2
        return 0.0

    # ---- counterpart prediction ----

    @api.model
    def predict_counterpart(self, statement_line, history_limit=500,
                            min_overlap=0.0):
        """Predict the most likely counterpart account and partner for a
        statement line from reconciliation history.

        Clean-room frequency model: each past 'match' audit row is weighted
        by the Jaccard token overlap between its statement line text and
        this line's text; the highest-weighted account and partner win.
        Useful when no open item matches outright and the operator needs a
        starting point (a recurring fee that always posts to the same
        expense account, a known counterparty). Returns {} when nothing in
        history overlaps. The score is the winner's share of total weight.
        """
        from collections import defaultdict
        tokens = self._tokenize(self._gather_text(
            getattr(statement_line, 'payment_ref', None),
            getattr(statement_line, 'ref', None),
            getattr(statement_line, 'narration', None),
        ))
        if not tokens:
            return {}
        company = statement_line.company_id
        audits = self.env['eh.reconciliation.audit'].search(
            [('decision', '=', 'match'), ('aml_id', '!=', False),
             ('statement_line_id', '!=', False)],
            order='id desc', limit=history_limit)

        account_weight = defaultdict(float)
        account_support = defaultdict(int)
        partner_weight = defaultdict(float)
        partner_support = defaultdict(int)
        account_total = 0.0
        partner_total = 0.0
        for audit in audits:
            past = audit.statement_line_id
            aml = audit.aml_id
            if not past or not aml or aml.company_id != company:
                continue
            past_tokens = self._tokenize(self._gather_text(
                getattr(past, 'payment_ref', None),
                getattr(past, 'ref', None),
                getattr(past, 'narration', None),
            ))
            if not past_tokens:
                continue
            union = tokens | past_tokens
            overlap = len(tokens & past_tokens) / len(union) if union else 0.0
            if overlap <= min_overlap:
                continue
            if aml.account_id:
                account_weight[aml.account_id.id] += overlap
                account_support[aml.account_id.id] += 1
                account_total += overlap
            partner = aml.partner_id or past.partner_id
            if partner:
                partner_weight[partner.id] += overlap
                partner_support[partner.id] += 1
                partner_total += overlap

        result = {}
        if account_weight:
            best = max(account_weight, key=account_weight.get)
            result['account_id'] = best
            result['account_score'] = round(
                account_weight[best] / account_total, 3) if account_total \
                else 0.0
            result['account_support'] = account_support[best]
        if partner_weight:
            best = max(partner_weight, key=partner_weight.get)
            result['partner_id'] = best
            result['partner_score'] = round(
                partner_weight[best] / partner_total, 3) if partner_total \
                else 0.0
            result['partner_support'] = partner_support[best]
        return result

    # ---- helpers ----

    @api.model
    def _statement_amount_company_currency(self, statement_line):
        """Return the bank-line amount in company currency.

        ``account.bank.statement.line.amount`` is expressed in the journal
        currency, while ``account.move.line.amount_residual`` is always in
        company currency.  Comparing the raw numbers produces false exact
        matches whenever the bank journal is foreign-currency denominated.
        """
        company = statement_line.company_id
        company_currency = company.currency_id
        statement_currency = (
            statement_line.currency_id or company_currency
        )
        amount = statement_line.amount or 0.0
        if not statement_currency or statement_currency == company_currency:
            return company_currency.round(amount) if company_currency else amount
        conversion_date = (
            statement_line.date or fields.Date.context_today(self)
        )
        return statement_currency._convert(
            amount, company_currency, company, conversion_date,
        )

    @staticmethod
    def _gather_text(*parts):
        joined = ' '.join(str(p) for p in parts if p)
        return joined.lower()

    @staticmethod
    def _tokenize(text):
        if not text:
            return set()
        return {
            t for t in _TOKEN_RE.findall(text)
            if len(t) >= _MIN_TOKEN_LEN
        }
