# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
account.move live wiring for journal-entry anomaly detection.

Builds the Odoo-independent JournalEntrySummary records the detector
expects, runs the deterministic rules (optionally LLM-augmented when a
provider is configured), and persists each Finding as an
eh.account.anomaly.finding row. A scheduled scan sweeps recent posted
moves per company with savepoint isolation so one bad company never
freezes the queue.
"""

import hashlib
import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.eh_account_ai_agent.tools import anomaly_detector

from .anomaly_finding import (
    ANOMALY_ENGINE_CAPABILITY,
    ANOMALY_ENGINE_CONTEXT,
)

_logger = logging.getLogger(__name__)

MAX_MANUAL_SCAN_MOVES = 200
MAX_CRON_SCAN_MOVES_PER_COMPANY = 500
MAX_PROVIDER_MOVES_PER_CALL = 50
_ANOMALY_SCAN_SCHEMA = 2
_POSTER_FIELD = 'eh_anomaly_posted_by_id'
_POSTED_AT_FIELD = 'eh_anomaly_posted_at'
_POST_EVIDENCE_FIELDS = frozenset({_POSTER_FIELD, _POSTED_AT_FIELD})
_AUTO_RECONCILED_RULES = frozenset({
    'round_outlier_v2', 'weekend_post_v2', 'just_under_threshold',
})


class AccountMove(models.Model):
    _inherit = 'account.move'

    eh_anomaly_finding_ids = fields.One2many(
        'eh.account.anomaly.finding', 'move_id',
        string="Anomaly findings", readonly=True,
    )
    eh_anomaly_count = fields.Integer(
        compute='_compute_eh_anomaly_count',
    )
    eh_anomaly_open_count = fields.Integer(
        compute='_compute_eh_anomaly_count',
    )
    eh_anomaly_posted_by_id = fields.Many2one(
        'res.users', string="Actual posting user", readonly=True,
        copy=False, index=True,
        help=(
            "Server-stamped user who actually posted this entry. Anomaly "
            "review uses this evidence to enforce maker/checker separation."
        ),
    )
    eh_anomaly_posted_at = fields.Datetime(
        string="Actual posting time", readonly=True, copy=False, index=True,
        help=(
            "Server-stamped UTC instant when this entry was posted. Weekend "
            "detection converts it to the posting user's timezone; the "
            "accounting date is not treated as a posting timestamp."
        ),
    )

    @api.ondelete(at_uninstall=False)
    def _unlink_blocks_anomaly_evidence(self):
        """Keep immutable findings and their review trail reachable.

        A cascade on finding.move_id bypassed the finding unlink guard. A
        reviewed finding then failed later with a raw review-event FK error.
        Resolve the policy at the parent boundary and keep a DB restrict as
        defence in depth.
        """
        if self.env['eh.account.anomaly.finding'].sudo().search([
            ('move_id', 'in', self.ids),
        ], limit=1):
            raise UserError(_(
                "This journal entry has immutable anomaly evidence and "
                "cannot be deleted. Reverse it through accounting workflow "
                "instead."
            ))

    @api.model_create_multi
    def create(self, vals_list):
        if any(_POST_EVIDENCE_FIELDS.intersection(vals) for vals in vals_list) \
                and (
            self.env.context.get(ANOMALY_ENGINE_CONTEXT)
            is not ANOMALY_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "The anomaly posting evidence is server-owned and cannot be "
                "supplied by a client."
            ))
        return super().create(vals_list)

    def write(self, vals):
        if _POST_EVIDENCE_FIELDS.intersection(vals) and (
            self.env.context.get(ANOMALY_ENGINE_CONTEXT)
            is not ANOMALY_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "The anomaly posting evidence is server-owned and immutable."
            ))
        return super().write(vals)

    def _post(self, soft=True):
        """Stamp the real poster only on records core actually posted."""
        posted_by = self.env.user
        posted = super()._post(soft=soft)
        if posted:
            posted.sudo().with_context(**{
                ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
            }).write({
                _POSTER_FIELD: posted_by.id,
                _POSTED_AT_FIELD: fields.Datetime.now(),
            })
        return posted

    @api.depends('eh_anomaly_finding_ids.state')
    def _compute_eh_anomaly_count(self):
        for move in self:
            findings = move.eh_anomaly_finding_ids
            move.eh_anomaly_count = len(findings)
            move.eh_anomaly_open_count = len(
                findings.filtered(lambda f: f.state == 'open')
            )

    # ---- summary builder ----

    def _eh_build_anomaly_summary(self, baseline_median=0.0):
        """Build one JournalEntrySummary for this move.

        Magnitude is the sum of debit legs, which is meaningful for
        every move type (invoices, bills, and pure journal entries
        alike), unlike amount_total which is zero for misc entries.
        """
        self.ensure_one()
        magnitude = sum(self.line_ids.mapped('debit'))
        balances = {}
        for line in self.line_ids:
            balances[line.account_id.id] = (
                balances.get(line.account_id.id, 0.0)
                + line.debit - line.credit
            )
        posted_by = self.eh_anomaly_posted_by_id or self.create_uid
        posted_at = None
        if self.eh_anomaly_posted_at:
            posted_at = fields.Datetime.context_timestamp(
                self.with_user(posted_by).with_context(
                    tz=posted_by.tz or 'UTC',
                ),
                self.eh_anomaly_posted_at,
            )
        return anomaly_detector.JournalEntrySummary(
            id=self.id,
            date=self.date,
            amount=magnitude,
            posted_at=posted_at,
            posted_by=(
                self.eh_anomaly_posted_by_id.login
                or self.eh_anomaly_posted_by_id.name
                or self.create_uid.login
                or self.create_uid.name
                or ''
            ),
            journal_id=self.journal_id.id,
            account_codes=tuple(
                code for code in self.line_ids.account_id.with_company(
                    self.company_id
                ).mapped('code')
                if code
            ),
            account_balances=tuple(
                (account_id, balance)
                for account_id, balance in balances.items()
                if not self.company_currency_id.is_zero(balance)
            ),
            currency_decimals=self.company_currency_id.decimal_places,
            reversed_entry_id=(
                self.reversed_entry_id.id
                if self.reversed_entry_id else 0
            ),
            baseline_median=baseline_median,
            reference=self.ref or self.name or '',
        )

    def _eh_anomaly_population_medians(self):
        """Stable posted population median by company/journal/calendar month."""
        posted = self.filtered(lambda move: move.state == 'posted' and move.date)
        if not posted:
            return {}
        journal_ids = sorted(set(posted.journal_id.ids))
        first_date = min(posted.mapped('date')).replace(day=1)
        last_date = max(posted.mapped('date'))
        if last_date.month == 12:
            end_date = last_date.replace(
                year=last_date.year + 1, month=1, day=1,
            )
        else:
            end_date = last_date.replace(month=last_date.month + 1, day=1)
        self.env.cr.execute("""
            SELECT journal_id, period,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY amount)
              FROM (
                    SELECT move.id, move.journal_id,
                           date_trunc('month', move.date)::date AS period,
                           SUM(line.debit) AS amount
                      FROM account_move AS move
                      JOIN account_move_line AS line
                        ON line.move_id = move.id
                     WHERE move.company_id = %s
                       AND move.state = 'posted'
                       AND move.journal_id = ANY(%s)
                       AND move.date >= %s
                       AND move.date < %s
                     GROUP BY move.id, move.journal_id,
                              date_trunc('month', move.date)::date
                    HAVING SUM(line.debit) > 0
                   ) AS population
             GROUP BY journal_id, period
        """, (
            posted.company_id.id, journal_ids, first_date, end_date,
        ))
        return {
            (journal_id, period): float(median or 0.0)
            for journal_id, period, median in self.env.cr.fetchall()
        }

    # ---- scan ----

    def _eh_assert_anomaly_scan_authorized(self):
        """Authorize before config sudo, SQL locks, cache reads or network."""
        self._eh_check_access('write')
        internal = self.env.context.get(ANOMALY_ENGINE_CONTEXT) is (
            ANOMALY_ENGINE_CAPABILITY
        )
        if internal:
            return
        if not (
            self.env.user.has_group('eh_account_base.group_eh_manager')
            or self.env.user.has_group(
                'eh_account_ai_agent.group_eh_ai_scanner'
            )
        ):
            raise AccessError(_(
                "Only an EH Accounting Manager or designated AI anomaly "
                "scanner can run a scan."
            ))
        if len(self) > MAX_MANUAL_SCAN_MOVES:
            raise UserError(_(
                "A manual anomaly scan is limited to %(limit)d journal "
                "entries. Narrow the selection and retry.",
                limit=MAX_MANUAL_SCAN_MOVES,
            ))

    @staticmethod
    def _eh_anomaly_scan_fingerprint(
        company, summaries, deterministic_findings, provider_key,
        provider_config, approval_threshold, median_multiplier,
    ):
        config_text = provider_config or ''
        if not isinstance(config_text, str):
            config_text = json.dumps(
                config_text, sort_keys=True, separators=(',', ':'),
            )
        payload = {
            'schema': _ANOMALY_SCAN_SCHEMA,
            'company_id': company.id,
            'provider_key': provider_key,
            # Credentials influence provider output, but secrets must never be
            # persisted in cache evidence.
            'provider_config_sha256': hashlib.sha256(
                config_text.encode('utf-8')
            ).hexdigest(),
            'approval_threshold': approval_threshold,
            'median_multiplier': median_multiplier,
            'entries': [
                {
                    'id': entry.id,
                    'date': entry.date.isoformat() if entry.date else '',
                    'amount': entry.amount,
                    'posted_by': entry.posted_by,
                    'account_codes': sorted(entry.account_codes),
                    'reference': entry.reference,
                }
                for entry in summaries
            ],
            # These are the exact prompt facts; selection-wide median/reversal
            # rules can change them even when one provider chunk is unchanged.
            'deterministic_findings': [
                {
                    'move_id': finding.move_id,
                    'rule': finding.rule,
                    'severity': finding.severity,
                    'description': finding.description,
                    'amount': finding.amount,
                    'date': (
                        finding.date.isoformat() if finding.date else ''
                    ),
                }
                for finding in deterministic_findings
            ],
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(',', ':'),
            ensure_ascii=False,
        ).encode('utf-8')
        return hashlib.sha256(canonical).hexdigest()

    def _eh_provider_cache_hit(self, company, fingerprint):
        # Transaction advisory lock makes the cache check and paid call one
        # serialized critical section across workers.
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ('eh_ai_anomaly:%s:%s' % (company.id, fingerprint),),
        )
        return bool(self.env['eh.account.anomaly.scan.cache'].sudo().search([
            ('company_id', '=', company.id),
            ('fingerprint', '=', fingerprint),
        ], limit=1))

    def _eh_record_provider_cache(
        self, company, fingerprint, provider_key, move_count,
    ):
        return self.env['eh.account.anomaly.scan.cache'].sudo().with_context(
            **{ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY}
        ).create({
            'company_id': company.id,
            'fingerprint': fingerprint,
            'provider_key': provider_key,
            'move_count': move_count,
        })

    def eh_scan_anomalies(self):
        """Run the detector over this recordset and persist new findings.

        Idempotent: a finding already stored for a (move, rule) pair is
        left untouched, so re-scanning never duplicates. Returns the
        findings created on this pass.
        """
        self._eh_assert_anomaly_scan_authorized()
        Finding = self.env['eh.account.anomaly.finding']
        created = Finding
        by_company = {}
        for move in self.filtered(lambda m: m.state == 'posted'):
            by_company.setdefault(move.company_id, self.browse())
            by_company[move.company_id] |= move
        for company, moves in by_company.items():
            medians = moves._eh_anomaly_population_medians()
            summaries = [
                move._eh_build_anomaly_summary(
                    medians.get(
                        (move.journal_id.id, move.date.replace(day=1)), 0.0,
                    )
                )
                for move in moves
            ]
            approval_threshold = company.eh_ai_anomaly_approval_threshold
            median_multiplier = (
                company.eh_ai_anomaly_median_multiplier or 3.0
            )
            # Access to the exact moves/company was established above. Read the
            # secret provider config only now, never before authorization.
            company_config = company.sudo()
            provider_key = company_config.eh_ai_provider_key or 'manual'
            provider_config = (
                company_config.eh_ai_provider_config or None
            )
            findings = anomaly_detector.detect(
                summaries,
                approval_threshold=approval_threshold,
                median_multiplier=median_multiplier,
                provider_key='manual',
                weekend_days=company._eh_get_anomaly_weekend_days(),
            )
            if provider_key != 'manual':
                uncached = []
                for summary in summaries:
                    summary_findings = [
                        finding for finding in findings
                        if finding.move_id == summary.id
                    ]
                    fingerprint = self._eh_anomaly_scan_fingerprint(
                        company, [summary], summary_findings, provider_key,
                        provider_config, approval_threshold,
                        median_multiplier,
                    )
                    if not self._eh_provider_cache_hit(company, fingerprint):
                        uncached.append((summary, fingerprint))
                for start in range(0, len(uncached),
                                   MAX_PROVIDER_MOVES_PER_CALL):
                    pending = uncached[
                        start:start + MAX_PROVIDER_MOVES_PER_CALL
                    ]
                    chunk = [summary for summary, _fingerprint in pending]
                    chunk_ids = {entry.id for entry in chunk}
                    chunk_findings = [
                        finding for finding in findings
                        if finding.move_id in chunk_ids
                    ]
                    before = len(chunk_findings)
                    try:
                        invoked = anomaly_detector.augment_with_provider(
                            chunk, chunk_findings,
                            provider_key=provider_key,
                            provider_config=provider_config,
                        )
                    except anomaly_detector.ProviderError:
                        _logger.warning(
                            "Anomaly provider call failed for company %s; "
                            "deterministic findings retained.",
                            company.id,
                        )
                        continue
                    if invoked:
                        findings.extend(chunk_findings[before:])
                        for _summary, fingerprint in pending:
                            self._eh_record_provider_cache(
                                company, fingerprint, provider_key, 1,
                            )
            # Remote provider work is complete. Serialize persistence only
            # now, in deterministic move order, so concurrent manual scans
            # re-read committed findings instead of racing into the unique
            # (move, rule) constraint and no row lock spans HTTP calls.
            self.env.cr.execute(
                "SELECT id FROM account_move WHERE id = ANY(%s) "
                "ORDER BY id FOR UPDATE",
                [moves.ids],
            )
            existing = Finding.search([
                ('move_id', 'in', moves.ids),
            ])
            present = {(finding.move_id, finding.rule) for finding in findings}
            stale = existing.filtered(lambda finding: (
                finding.source == 'deterministic'
                and finding.state == 'open'
                and finding.rule in _AUTO_RECONCILED_RULES
                and (finding.move_id.id, finding.rule) not in present
            ))
            if stale:
                stale.with_context(**{
                    ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
                })._eh_engine_set_withdrawn()
            returned = existing.filtered(lambda finding: (
                finding.state == 'withdrawn'
                and (finding.move_id.id, finding.rule) in present
            ))
            if returned:
                returned.with_context(**{
                    ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
                })._eh_engine_reactivate()
            seen = {(f.move_id.id, f.rule) for f in existing}
            vals = [
                {
                    'move_id': f.move_id,
                    'rule': f.rule,
                    'severity': f.severity,
                    'description': f.description,
                    'amount': f.amount,
                    'date': f.date,
                    'source': f.source,
                    'provider_key': f.provider_key,
                    'provider_evidence_hash': f.provider_evidence_hash,
                }
                for f in findings
                if (f.move_id, f.rule) not in seen
            ]
            if vals:
                created |= Finding.sudo().with_context(**{
                    ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
                }).create(vals)
        return created

    def action_eh_scan_anomalies(self):
        """Manual scan button: scan the selection, then show findings."""
        self.eh_scan_anomalies()
        action = self.env['ir.actions.actions']._for_xml_id(
            'eh_account_ai_agent.action_eh_account_anomaly_finding'
        )
        action['domain'] = [('move_id', 'in', self.ids)]
        action['context'] = {'search_default_open': 1}
        return action

    def action_eh_view_anomalies(self):
        """Smart-button: open this move's findings."""
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id(
            'eh_account_ai_agent.action_eh_account_anomaly_finding'
        )
        action['domain'] = [('move_id', '=', self.id)]
        action['context'] = {'default_move_id': self.id}
        return action

    # ---- cron ----

    @api.model
    def _cron_scan_anomalies(self):
        """Scheduled sweep: scan each company's recent posted moves.

        Each company runs in its own savepoint so a failure on one
        company is logged and skipped without aborting the rest.
        """
        if not (
            self.env.su
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(_(
                "Only the scheduled-job administrator can run the global "
                "anomaly sweep."
            ))
        today = fields.Date.context_today(self)
        companies = self.env['res.company'].search([])
        for company in companies:
            try:
                with self.env.cr.savepoint():
                    lookback = company.eh_ai_anomaly_lookback_days or 30
                    since = today - timedelta(days=lookback)
                    moves = self.with_company(company).search([
                        ('company_id', '=', company.id),
                        ('state', '=', 'posted'),
                        ('date', '>=', since),
                    ], order='date desc, id desc',
                        limit=MAX_CRON_SCAN_MOVES_PER_COMPANY)
                    moves.with_context(**{
                        ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
                    }).eh_scan_anomalies()
            except Exception as exc:  # noqa: BLE001 - isolate per company
                detail = ' '.join(str(exc).split())[:500] or _("No details")
                _logger.warning(
                    "Anomaly scan failed for company %s (%s): %s",
                    company.id, type(exc).__name__, detail,
                )
        return True
