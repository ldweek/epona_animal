# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.account.anomaly.finding: persisted output of the journal-entry
anomaly scan.

The detection logic itself lives in tools/anomaly_detector.py as a
pure, Odoo-independent function. This model is the live wiring: each
deterministic Finding the detector returns is stored as one record
against the offending account.move so accountants triage it from the
UI, the cron can run unattended, and the result is queryable for KPIs.

The detected evidence (rule, description, amount, date) is immutable
after creation; only the triage fields (state, reviewer, review note)
can change. Re-scanning a move never duplicates a finding: the unique
(move, rule) constraint plus a create-if-absent upsert make the scan
idempotent.
"""

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

_SERVER_OWNED_FIELDS = frozenset(
    {
        'move_id', 'company_id', 'rule', 'severity', 'description', 'amount',
        'date', 'source', 'provider_key', 'provider_evidence_hash',
    }
    | {
        'detected_at', 'detected_by_id', 'state', 'reviewed_by_id',
        'reviewed_at',
    }
)
ANOMALY_ENGINE_CONTEXT = 'eh_anomaly_engine_capability'
ANOMALY_ENGINE_CAPABILITY = object()


class EhAccountAnomalyFinding(models.Model):
    _name = 'eh.account.anomaly.finding'
    _description = "Journal entry anomaly finding"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'detected_at desc, id desc'
    _rec_name = 'description'

    move_id = fields.Many2one(
        'account.move', required=True, ondelete='restrict', index=True,
    )
    company_id = fields.Many2one(
        related='move_id.company_id', store=True, readonly=True, index=True,
    )
    currency_id = fields.Many2one(
        related='move_id.company_id.currency_id', readonly=True,
    )
    rule = fields.Char(required=True, readonly=True)
    severity = fields.Selection(
        [('low', "Low"), ('medium', "Medium"), ('high', "High")],
        required=True, readonly=True, index=True,
    )
    description = fields.Text(required=True, readonly=True)
    amount = fields.Monetary(
        currency_field='currency_id', readonly=True,
    )
    date = fields.Date(readonly=True)
    source = fields.Selection(
        [('deterministic', "Deterministic rule"), ('provider', "AI provider")],
        required=True, readonly=True, default='deterministic', index=True,
    )
    provider_key = fields.Char(readonly=True, index=True)
    provider_evidence_hash = fields.Char(
        readonly=True, size=64,
        help=(
            "SHA-256 of the provider's complete raw description before the "
            "bounded display excerpt was stored."
        ),
    )
    detected_at = fields.Datetime(
        readonly=True, default=fields.Datetime.now,
    )
    detected_by_id = fields.Many2one(
        'res.users', readonly=True, index=True,
        help="Real user whose authorized scan produced this finding.",
    )

    state = fields.Selection(
        [
            ('open', "Open"),
            ('reviewed', "Reviewed"),
            ('dismissed', "Dismissed"),
            ('withdrawn', "No longer detected"),
        ],
        default='open', required=True, index=True, tracking=True,
    )
    reviewed_by_id = fields.Many2one('res.users', readonly=True)
    reviewed_at = fields.Datetime(readonly=True)
    review_note = fields.Text()
    review_event_ids = fields.One2many(
        'eh.account.anomaly.review.event', 'finding_id', readonly=True,
        string="Review history",
    )

    _unique_move_rule = models.Constraint(
        'unique(move_id, rule)',
        'A move can carry at most one finding per rule.',
    )

    # ---- triage actions ----

    @api.model_create_multi
    def create(self, vals_list):
        # Findings are detector output, not manager-authored records. A plain
        # string/bool context flag is client-controlled; require the exact
        # in-process capability object even under sudo.
        if self.env.context.get(ANOMALY_ENGINE_CONTEXT) is not (
            ANOMALY_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Anomaly findings can only be created by the scan engine."
            ))
        vals_list = [
            dict(
                vals,
                detected_by_id=self.env.user.id,
                detected_at=fields.Datetime.now(),
                state='open',
                reviewed_by_id=False,
                reviewed_at=False,
            )
            for vals in vals_list
        ]
        return super().create(vals_list)

    def _eh_lock_review_transitions(self):
        """Authorize, lock and re-read before any triage state decision."""
        self._eh_check_access('write')
        if not (
            self.env.user.has_group('eh_account_base.group_eh_manager')
            or self.env.user.has_group(
                'eh_account_ai_agent.group_eh_ai_reviewer'
            )
        ):
            raise AccessError(_(
                "Only an EH Accounting Manager or designated anomaly "
                "reviewer can triage findings."
            ))
        ids = sorted(self.ids)
        if ids:
            self.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_account_anomaly_finding WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(ids),),
            )
            self.invalidate_recordset()
        for finding in self:
            posting_user = (
                finding.move_id.eh_anomaly_posted_by_id
                or finding.move_id.create_uid
            )
            if posting_user == self.env.user:
                raise UserError(_(
                    "Segregation of duties: you cannot review an anomaly on "
                    "a journal entry you posted. A different reviewer must "
                    "triage this finding."
                ))
        return self

    def _eh_record_review_event(self, action, note=None):
        return self.env['eh.account.anomaly.review.event'].sudo().with_context(
            **{ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY}
        ).create([
            {
                'finding_id': finding.id,
                'action': action,
                'note': finding.review_note or '' if note is None else note,
            }
            for finding in self
        ])

    def action_mark_reviewed(self):
        self._eh_lock_review_transitions()
        if self.filtered(lambda finding: finding.state != 'open'):
            raise UserError(_("Only open anomaly findings can be reviewed."))
        self.sudo().with_context(**{
            ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
        }).write({
            'state': 'reviewed',
            'reviewed_by_id': self.env.user.id,
            'reviewed_at': fields.Datetime.now(),
        })
        self._eh_record_review_event('reviewed')
        return True

    def action_dismiss(self):
        self._eh_lock_review_transitions()
        if self.filtered(lambda finding: finding.state == 'dismissed'):
            raise UserError(_("This anomaly finding is already dismissed."))
        if self.filtered(lambda finding: not (finding.review_note or '').strip()):
            raise UserError(_(
                "Enter a review note explaining why the finding is being "
                "dismissed."
            ))
        self.sudo().with_context(**{
            ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
        }).write({
            'state': 'dismissed',
            'reviewed_by_id': self.env.user.id,
            'reviewed_at': fields.Datetime.now(),
        })
        self._eh_record_review_event('dismissed')
        return True

    def action_reopen(self):
        self._eh_lock_review_transitions()
        if self.filtered(lambda finding: finding.state == 'withdrawn'):
            raise UserError(_(
                "A finding withdrawn by the scan engine cannot be manually "
                "reopened. Rescan the entry; the engine will reactivate it "
                "if the evidence recurs."
            ))
        if self.filtered(lambda finding: finding.state == 'open'):
            raise UserError(_("This anomaly finding is already open."))
        self.sudo().with_context(**{
            ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
        }).write({
            'state': 'open',
            'reviewed_by_id': False,
            'reviewed_at': False,
        })
        self._eh_record_review_event('reopened')
        return True

    def _eh_engine_set_withdrawn(self):
        if self.env.context.get(ANOMALY_ENGINE_CONTEXT) is not (
            ANOMALY_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Only the anomaly engine can withdraw stale findings."
            ))
        records = self.filtered(lambda finding: finding.state == 'open')
        if records:
            records.sudo().with_context(**{
                ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
            }).write({
                'state': 'withdrawn',
                'reviewed_by_id': False,
                'reviewed_at': False,
            })
            records._eh_record_review_event(
                'withdrawn',
                note="Deterministic evidence no longer present on rescan.",
            )
        return records

    def _eh_engine_reactivate(self):
        if self.env.context.get(ANOMALY_ENGINE_CONTEXT) is not (
            ANOMALY_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Only the anomaly engine can reactivate a finding."
            ))
        records = self.filtered(lambda finding: finding.state == 'withdrawn')
        if records:
            records.sudo().with_context(**{
                ANOMALY_ENGINE_CONTEXT: ANOMALY_ENGINE_CAPABILITY,
            }).write({
                'state': 'open',
                'reviewed_by_id': False,
                'reviewed_at': False,
            })
            records._eh_record_review_event(
                'reactivated',
                note="Deterministic evidence recurred on rescan.",
            )
        return records

    def action_open_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    # ---- immutability guard ----

    def write(self, vals):
        touched = _SERVER_OWNED_FIELDS.intersection(vals)
        if touched and self.env.context.get(ANOMALY_ENGINE_CONTEXT) is not (
            ANOMALY_ENGINE_CAPABILITY
        ):
            self._eh_check_access('write')
            raise AccessError(_(
                "Anomaly evidence and review provenance are server-owned; "
                "cannot change %s. Use the review actions instead.",
                ', '.join(sorted(touched)),
            ))
        if 'review_note' in vals and self.env.context.get(
                ANOMALY_ENGINE_CONTEXT) is not ANOMALY_ENGINE_CAPABILITY:
            self._eh_lock_review_transitions()
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_immutable_evidence(self):
        raise UserError(_(
            "Anomaly findings are audit evidence and cannot be deleted."
        ))


class EhAccountAnomalyReviewEvent(models.Model):
    _name = 'eh.account.anomaly.review.event'
    _description = "Immutable anomaly review event"
    _order = 'occurred_at desc, id desc'

    finding_id = fields.Many2one(
        'eh.account.anomaly.finding', required=True, ondelete='restrict',
        index=True,
    )
    company_id = fields.Many2one(
        related='finding_id.company_id', store=True, readonly=True, index=True,
    )
    action = fields.Selection(
        [
            ('reviewed', "Reviewed"),
            ('dismissed', "Dismissed"),
            ('reopened', "Reopened"),
            ('withdrawn', "Withdrawn by scan"),
            ('reactivated', "Reactivated by scan"),
        ],
        required=True, readonly=True, index=True,
    )
    actor_id = fields.Many2one(
        'res.users', required=True, readonly=True, index=True,
    )
    occurred_at = fields.Datetime(required=True, readonly=True, index=True)
    note = fields.Text(readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(ANOMALY_ENGINE_CONTEXT) is not (
            ANOMALY_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Anomaly review events can only be recorded by a review "
                "transition."
            ))
        now = fields.Datetime.now()
        vals_list = [
            dict(vals, actor_id=self.env.user.id, occurred_at=now)
            for vals in vals_list
        ]
        return super().create(vals_list)

    def write(self, vals):
        self._eh_check_access('write')
        raise AccessError(_("Anomaly review history is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_immutable_review_history(self):
        raise UserError(_("Anomaly review history cannot be deleted."))


class EhAccountAnomalyScanCache(models.Model):
    _name = 'eh.account.anomaly.scan.cache'
    _description = "Completed anomaly provider-call cache"
    _order = 'scanned_at desc, id desc'

    company_id = fields.Many2one(
        'res.company', required=True, readonly=True, index=True,
        ondelete='cascade',
    )
    fingerprint = fields.Char(
        required=True, readonly=True, size=64, index=True,
    )
    provider_key = fields.Char(required=True, readonly=True, index=True)
    move_count = fields.Integer(required=True, readonly=True)
    scanned_by_id = fields.Many2one(
        'res.users', required=True, readonly=True, index=True,
    )
    scanned_at = fields.Datetime(required=True, readonly=True, index=True)

    _unique_company_fingerprint = models.Constraint(
        'unique(company_id, fingerprint)',
        'This exact company/provider anomaly scan is already cached.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(ANOMALY_ENGINE_CONTEXT) is not (
            ANOMALY_ENGINE_CAPABILITY
        ):
            raise AccessError(_(
                "Anomaly provider cache evidence can only be created by the "
                "scan engine."
            ))
        now = fields.Datetime.now()
        vals_list = [
            dict(vals, scanned_by_id=self.env.user.id, scanned_at=now)
            for vals in vals_list
        ]
        return super().create(vals_list)

    def write(self, vals):
        self._eh_check_access('write')
        raise AccessError(_("Anomaly provider cache evidence is immutable."))

    @api.ondelete(at_uninstall=False)
    def _unlink_immutable_cache(self):
        raise UserError(_("Anomaly provider cache evidence cannot be deleted."))
