# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Vendor bill intake.

State machine:

  received -> parsed -> matched -> posted
                                \\-> exception -> matched (after override)
                                                \\-> rejected
                                \\-> rejected
                       -> rejected

received: file uploaded or captured from email; no parsing yet.
parsed: header and lines extracted (manually or by the regex parser).
matched: 3 way match attempted; outcome on each line.
posted: account.move created and posted; intake archived.
exception: at least one line is out of tolerance; user review required.
rejected: bill discarded with reason; no move posted.
"""

import hashlib
import itertools
import json
import logging
import math
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .regex_policy import MAX_SEARCH_CHARACTERS, compile_safe_regex

_logger = logging.getLogger(__name__)


_DEFAULT_INVOICE_REF_PATTERNS = (
    r'(?im)^Invoice No:[ \t]*([A-Z0-9\-]+)$',
    r'(?im)^Invoice Number:[ \t]*([A-Z0-9\-]+)$',
    r'(?im)^Invoice:[ \t]*([A-Z0-9\-]+)$',
    r'(?im)^Invoice#[ \t]*([A-Z0-9\-]+)$',
)
_DEFAULT_TOTAL_PATTERNS = (
    r'(?im)^Grand Total:[ \t]*([0-9][0-9,\.]*)$',
    r'(?im)^Total:[ \t]*([0-9][0-9,\.]*)$',
)
_LEGACY_INVOICE_REF_PATTERN = r'(?im)Invoice[:#\s]+([A-Z0-9\-]+)'
_LEGACY_TOTAL_PATTERN = r'(?im)Total[:\s]+([0-9][0-9,\.]*)'
_LINE_RE = re.compile(
    r'(?im)^\s*([A-Z0-9\-]+)\s+'
    r'(?P<qty>\d+(?:\.\d+)?)\s+'
    r'(?P<price>\d+(?:\.\d+)?)\s*$',
)

_MAX_RAW_TEXT_BYTES = 2 * 1024 * 1024
_MAX_SOURCE_DOCUMENT_BYTES = 20 * 1024 * 1024
_MAX_EXTRACTED_LINES = 1000
_MAX_REFERENCE_LENGTH = 512
_MAX_EXTRACTOR_KEY_LENGTH = 128
_MAX_REASON_LENGTH = 8000
_MAX_NOTES_LENGTH = 64 * 1024
_MAX_EXTRACTOR_METADATA_BYTES = 16 * 1024
_MAX_EMAIL_SUBJECT_BYTES = 256 * 1024
_TERMINAL_STATES = frozenset({'posted', 'rejected'})
_MATCHED_STATES = frozenset({'matched', 'exception'})
_MATCHED_LINE_STATUSES = ('ok', 'overridden')
_EXCEPTION_LINE_STATUSES = (
    'qty_exception', 'price_exception', 'no_match', 'over_received',
)
_CAPTURED_ATTACHMENT_FIELDS = frozenset({
    'name', 'res_model', 'res_field', 'res_id', 'company_id', 'type', 'url',
    'public', 'access_token', 'raw', 'datas', 'db_datas', 'store_fname',
    'file_size', 'checksum', 'mimetype',
})
_SOURCE_FIELDS = frozenset({
    'partner_id', 'purchase_order_id', 'raw_text', 'vendor_reference',
    'bill_date', 'extracted_total', 'currency_id', 'journal_id',
    'extractor_key', 'extractor_min_confidence',
})
_SERVER_FIELDS = frozenset({
    'name', 'received_at', 'parsed_at', 'parsed_by_id', 'matched_at', 'matched_by_id',
    'posted_at', 'posted_by_id', 'rejected_at', 'rejected_by_id', 'move_id',
    'duplicate_intake_id', 'duplicate_move_id', 'duplicate_override_log_id',
    'duplicate_override_reason', 'duplicate_overridden_at',
    'duplicate_overridden_by_id', 'duplicate_override_fingerprint',
    'extractor_last_run_summary', 'source_attachment_id',
    'source_fingerprint', 'source_size', 'source_mimetype',
    'source_captured_at', 'source_email_subject',
    'provenance_quarantined', 'provenance_quarantine_note',
})
_EH_MAIL_GATEWAY_INTERNAL = 'eh_ap_mail_gateway_internal'
_EH_MAIL_GATEWAY_CAPABILITY = object()


class EhApIntake(models.Model):
    _name = 'eh.ap.intake'
    _description = "Vendor Bill Intake"
    _inherit = [
        'mail.thread.main.attachment',
        'mail.activity.mixin',
        'eh.workflow.guard',
        'eh.list.statistics.mixin',
    ]
    _order = 'received_at desc, id desc'

    # The intake state machine (received -> parsed -> matched -> posted /
    # exception / rejected) is enforced by eh.workflow.guard: 'state' may only
    # change through the record's own actions (which run as su), never a direct
    # RPC write. A plain user cannot write({'state': 'posted'}) to skip
    # action_post, its manager check, duplicate block and journal entry.
    _eh_guarded_fields = ('state',)

    # The email-to-intake gateway is a SINGLE catchall inbox: the admin
    # points one mail.alias at this model (Settings > Technical > Email >
    # Aliases, alias_model = Vendor Bill Intake) and every message to that
    # address creates an intake via message_new below. The model is
    # deliberately NOT a mail.alias.mixin: that mixin _inherits mail.alias
    # with a required alias_id, which would spawn one dedicated alias per
    # vendor bill (an email inbox per record), growing the alias table
    # without bound. One standing alias, many intakes.

    provenance_quarantined = fields.Boolean(
        readonly=True, copy=False, index=True,
        help=(
            "Upgrade audit found that the legacy journal-entry link could not "
            "be proven from immutable same-company/vendor/source evidence. "
            "The record is retained but cannot continue through AP workflow."
        ),
    )
    provenance_quarantine_note = fields.Text(readonly=True, copy=False)

    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Create a vendor-bill intake from an inbound email.

        The mail subject becomes a placeholder vendor reference; the
        body (text/plain alternative when available) becomes raw_text;
        the partner is resolved by matching the sender's email to a
        contact whose email or alias matches. PDF attachments land on
        the intake's chatter so the regex parser sees the body and the
        line-extractor adapter (when configured) can pick up the PDF
        bytes via _line_extractor_document.

        Inputs from email are treated as untrusted: no auto-post, no
        auto-match. The intake stays in 'received' state until a user
        explicitly runs Parse and Match.
        """
        defaults = dict(custom_values or {})
        body = msg_dict.get('body') or ''
        # Plain-text first; if the message is HTML only, strip tags
        # using the standard Odoo helper so the regex parser has
        # something readable.
        if body and ('<' in body and '>' in body):
            from odoo.tools.mail import html2plaintext
            text = html2plaintext(body)
        else:
            text = body
        defaults.setdefault('raw_text', text)
        subject = str(msg_dict.get('subject') or '')
        if len(subject.encode('utf-8')) > _MAX_EMAIL_SUBJECT_BYTES:
            raise ValidationError(_(
                "Incoming email subject exceeds the 256 KiB safety limit."
            ))
        # Source provenance follows the actual gateway payload, never a value
        # supplied through alias custom defaults.
        defaults['source_email_subject'] = subject
        defaults.setdefault(
            'vendor_reference', self._eh_subject_reference(subject),
        )
        sender = (msg_dict.get('email_from') or '').strip()
        if sender and not defaults.get('partner_id'):
            partner = self._eh_resolve_email_sender(sender)
            if partner:
                defaults['partner_id'] = partner.id
        gateway = self.with_context(**{
            _EH_MAIL_GATEWAY_INTERNAL: _EH_MAIL_GATEWAY_CAPABILITY,
        })
        intake = super(EhApIntake, gateway).message_new(msg_dict, defaults)
        intake.message_post(body=_(
            "Intake created from incoming email; sender %s. "
            "No auto-post applied; review and run Parse to continue."
        ) % sender)
        return intake

    @api.model
    def _eh_subject_reference(self, subject):
        """Return a bounded, collision-resistant display reference."""
        subject = str(subject or '').strip()
        if len(subject) <= _MAX_REFERENCE_LENGTH:
            return subject
        suffix = '-' + hashlib.sha256(
            subject.encode('utf-8'),
        ).hexdigest()[:16]
        prefix = subject[:_MAX_REFERENCE_LENGTH - len(suffix)].rstrip()
        return prefix + suffix

    @api.model
    def _eh_resolve_email_sender(self, raw_from):
        """Best-effort partner resolution from a raw From header.

        Strict by design: when more than one candidate matches, the
        intake is left without partner_id rather than guessing. The
        user attaches the partner manually; the operational risk of
        a wrong partner attribution on a vendor bill is much higher
        than the convenience of a guess.
        """
        from odoo.tools import email_split
        emails = email_split(raw_from or '')
        if not emails:
            return self.env['res.partner']
        clean = emails[0].lower()
        candidates = self.env['res.partner'].search([
            ('email_normalized', '=', clean),
            ('parent_id', '=', False),
            ('company_id', 'in', [self.env.company.id, False]),
        ], limit=2)
        if len(candidates) == 1:
            return candidates
        return self.env['res.partner']

    name = fields.Char(
        required=True, copy=False, default='/', tracking=True,
    )
    state = fields.Selection([
        ('received', "Received"),
        ('parsed', "Parsed"),
        ('matched', "Matched"),
        ('posted', "Posted"),
        ('exception', "Exception"),
        ('rejected', "Rejected"),
    ], default='received', required=True, tracking=True)

    partner_id = fields.Many2one(
        'res.partner', string="Vendor", tracking=True,
        domain="[('parent_id', '=', False)]",
    )
    purchase_order_id = fields.Many2one(
        'purchase.order', string="Purchase Order",
        domain="[('partner_id', '=', partner_id), "
               "('state', 'in', ['purchase', 'done'])]",
        help="Optional: scope the 3 way match to lines on this PO.",
    )

    raw_text = fields.Text(
        help="Raw text content of the captured bill. Drives the parser.",
    )
    vendor_reference = fields.Char(
        string="Vendor Reference", tracking=True,
        help="Vendor invoice number / reference as extracted or entered.",
    )
    source_email_subject = fields.Text(
        string="Original Email Subject", readonly=True, copy=False,
        help=(
            "Full inbound email subject retained as source evidence. The "
            "vendor reference is a bounded display value when this is long."
        ),
    )
    bill_date = fields.Date(
        default=fields.Date.context_today,
    )
    extracted_total = fields.Monetary(
        help="Total extracted from the captured text (sanity check).",
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        default=lambda self: self.env.company.currency_id,
    )
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )

    line_ids = fields.One2many(
        'eh.ap.intake.line', 'intake_id', copy=False,
    )

    move_id = fields.Many2one(
        'account.move', string="Posted Bill", readonly=True, copy=False,
        ondelete='set null',
    )
    journal_id = fields.Many2one(
        'account.journal', string="Purchase Journal",
        domain="[('type', '=', 'purchase')]",
        help="Defaults to the company's first purchase journal at post.",
    )

    # ---- audit ----
    received_at = fields.Datetime(
        readonly=True, default=fields.Datetime.now, tracking=True,
    )
    parsed_at = fields.Datetime(readonly=True, tracking=True)
    parsed_by_id = fields.Many2one('res.users', readonly=True)
    matched_at = fields.Datetime(readonly=True, tracking=True)
    matched_by_id = fields.Many2one('res.users', readonly=True)
    posted_at = fields.Datetime(readonly=True, tracking=True)
    posted_by_id = fields.Many2one('res.users', readonly=True)
    rejected_at = fields.Datetime(readonly=True, tracking=True)
    rejected_by_id = fields.Many2one('res.users', readonly=True)
    rejection_reason = fields.Text(tracking=True)

    # ---- match summary ----
    line_count = fields.Integer(compute='_compute_match_summary', store=True)
    ok_count = fields.Integer(compute='_compute_match_summary', store=True)
    exception_count = fields.Integer(compute='_compute_match_summary', store=True)
    total_invoice_amount = fields.Monetary(
        compute='_compute_match_summary', store=True,
    )

    notes = fields.Text()

    # ---- duplicate detection ----
    duplicate_intake_id = fields.Many2one(
        'eh.ap.intake', string="Duplicate Of", readonly=True, copy=False,
        help=(
            "Set when the (vendor, vendor_reference, total, date) tuple "
            "matches an existing intake or posted bill. Posting is "
            "blocked until the user resolves the duplicate."
        ),
    )
    duplicate_move_id = fields.Many2one(
        'account.move', string="Duplicate Posted Bill",
        readonly=True, copy=False,
        help=(
            "Set when an account.move with the same partner and "
            "vendor reference is already posted in this company."
        ),
    )
    duplicate_override_request_reason = fields.Text(
        groups='eh_account_base.group_eh_manager',
        help=(
            "Manager-entered reason awaiting the Override Duplicate action. "
            "The action copies it into an append-only audit row."
        ),
    )
    duplicate_override_log_id = fields.Many2one(
        'eh.ap.duplicate.override.log', string="Current Duplicate Override",
        readonly=True, copy=False, ondelete='restrict',
    )
    duplicate_override_reason = fields.Text(
        readonly=True, copy=False,
        help=(
            "Reason an authorised user overrode the duplicate block. "
            "Captured in an append-only audit row."
        ),
    )
    duplicate_overridden_at = fields.Datetime(readonly=True, copy=False)
    duplicate_overridden_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    duplicate_override_fingerprint = fields.Char(
        readonly=True, copy=False,
    )
    duplicate_override_log_ids = fields.One2many(
        'eh.ap.duplicate.override.log', 'intake_id',
        string="Duplicate Override History", readonly=True,
    )

    # ---- immutable source evidence ----
    source_attachment_id = fields.Many2one(
        'ir.attachment', readonly=True, copy=False, ondelete='restrict',
        help="Binary document included in the captured source fingerprint.",
    )
    source_fingerprint = fields.Char(
        readonly=True, copy=False, index=True,
        help=(
            "SHA-256 over captured text plus the selected source attachment. "
            "Posting re-verifies this value."
        ),
    )
    source_size = fields.Integer(readonly=True, copy=False)
    source_mimetype = fields.Char(readonly=True, copy=False)
    source_captured_at = fields.Datetime(readonly=True, copy=False)

    # ---- pluggable line-item extractor ----
    extractor_key = fields.Selection(
        selection='_eh_extractor_choices',
        groups='eh_account_base.group_eh_manager',
        help=(
            "Optional key of a registered line-item extractor "
            "(typically an OCR/LLM adapter shipped by a partner package). "
            "Leave blank to use only the regex parser. The framework "
            "uses the extractor only for line-items; supplier and total "
            "still come from the regex header pass."
        ),
    )
    extractor_min_confidence = fields.Float(
        default=0.6, groups='eh_account_base.group_eh_manager',
        help=(
            "Lines extracted with a confidence below this value are "
            "discarded; the user reviews them manually. Range 0.0 to 1.0."
        ),
    )
    extractor_last_run_summary = fields.Text(readonly=True)

    @api.model
    def _eh_extractor_choices(self):
        from odoo.addons.eh_account_ap_automation.extractors.registry import (
            extractor_choices,
        )
        return extractor_choices()

    # ---- compute ----

    @api.depends('line_ids.match_status', 'line_ids.subtotal')
    def _compute_match_summary(self):
        for intake in self:
            intake.line_count = len(intake.line_ids)
            intake.ok_count = len(intake.line_ids.filtered(
                lambda l: l.match_status in _MATCHED_LINE_STATUSES,
            ))
            intake.exception_count = len(intake.line_ids.filtered(
                lambda l: l.match_status in _EXCEPTION_LINE_STATUSES,
            ))
            intake.total_invoice_amount = sum(
                intake.line_ids.mapped('subtotal'),
            )

    def _eh_list_statistics_hook(self):
        statistics = super()._eh_list_statistics_hook()
        for intake in self:
            if intake.line_count > 0:
                statistics[intake.id].append({
                    'iconClass': 'fa-list',
                    'value': intake.line_count,
                    'label': _("Lines"),
                    'tagClass': 'o_tag_color_1',
                    'actionMethod': 'action_view_lines',
                })
            if intake.ok_count > 0:
                statistics[intake.id].append({
                    'iconClass': 'fa-check',
                    'value': intake.ok_count,
                    'label': _("Matched"),
                    'tagClass': 'o_tag_color_10',
                    'actionMethod': 'action_view_matched_lines',
                })
            if intake.exception_count > 0:
                statistics[intake.id].append({
                    'iconClass': 'fa-exclamation-triangle',
                    'value': intake.exception_count,
                    'label': _("Exceptions"),
                    'tagClass': 'o_tag_color_2',
                    'actionMethod': 'action_view_exception_lines',
                })
        return statistics

    @api.constrains('extractor_min_confidence')
    def _check_extractor_min_confidence(self):
        for intake in self:
            value = intake.extractor_min_confidence
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValidationError(_(
                    "Extractor minimum confidence must be between 0 and 1."
                ))

    @api.constrains('extractor_key')
    def _check_extractor_key(self):
        from odoo.addons.eh_account_ap_automation.extractors.registry import (
            has_extractor,
        )
        for intake in self:
            if intake.extractor_key and not has_extractor(intake.extractor_key):
                raise ValidationError(_(
                    "Document extractor key %s is not registered.",
                    intake.extractor_key,
                ))

    # ---- create ----

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            server_fields = _SERVER_FIELDS
            if self.env.context.get(_EH_MAIL_GATEWAY_INTERNAL) \
                    is _EH_MAIL_GATEWAY_CAPABILITY:
                server_fields = server_fields - {'source_email_subject'}
            forged = sorted(set().union(*(
                set(vals or {}) & server_fields for vals in vals_list
            )))
            if forged:
                raise AccessError(_(
                    "AP intake audit and provenance fields are server-owned: "
                    "%s.", ', '.join(forged),
                ))
        for vals in vals_list:
            self._eh_validate_input_values(vals)
            if vals.get('name', '/') == '/':
                seq = self.env['ir.sequence'].next_by_code('eh.ap.intake') or '/'
                vals['name'] = seq
            vals.setdefault('received_at', fields.Datetime.now())
        records = super().create(vals_list)
        records._eh_validate_scope()
        return records

    def write(self, vals):
        vals = dict(vals or {})
        if not self.env.su:
            forged = sorted(set(vals) & _SERVER_FIELDS)
            if forged:
                raise AccessError(_(
                    "AP intake audit and provenance fields are server-owned: "
                    "%s.", ', '.join(forged),
                ))
            if 'company_id' in vals:
                raise AccessError(_(
                    "An AP intake company is immutable after creation."
                ))
            if {'extractor_key', 'extractor_min_confidence'} & set(vals) \
                    and not self.env.user.has_group(
                        'eh_account_base.group_eh_manager'):
                raise AccessError(_(
                    "Only ERP Heritage managers can configure document "
                    "extractors."
                ))
            if 'duplicate_override_request_reason' in vals \
                    and not self.env.user.has_group(
                        'eh_account_base.group_eh_manager'):
                raise AccessError(_(
                    "Only ERP Heritage managers can prepare a duplicate "
                    "override."
                ))
        if 'raw_text' in vals:
            self._eh_validate_raw_text(vals['raw_text'])
        self._eh_validate_input_values(vals)
        if {'rejection_reason', 'duplicate_override_request_reason'} & set(vals):
            immutable = self.filtered(
                lambda rec: rec.state in _TERMINAL_STATES,
            )
            if immutable:
                raise UserError(_(
                    "Terminal AP intake decisions cannot be edited: %s.",
                    ', '.join(immutable.mapped('display_name')),
                ))
        source_change = bool(set(vals) & _SOURCE_FIELDS)
        if source_change:
            if not self.env.su:
                self._eh_check_access('write')
            self._eh_lock_rows()
            frozen = self.filtered(lambda rec: rec.state in _TERMINAL_STATES)
            if frozen:
                raise UserError(_(
                    "Posted or rejected AP intake evidence cannot be edited: "
                    "%s.", ', '.join(frozen.mapped('display_name')),
                ))
        result = super().write(vals)
        self._eh_validate_scope()
        if source_change and not self.env.su:
            self.sudo()._eh_invalidate_review(_(
                "Source evidence changed; previous match and duplicate "
                "approval were invalidated."
            ), clear_source=True)
        return result

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        self._eh_lock_rows()
        retained = self.filtered(lambda rec: rec.state in _TERMINAL_STATES)
        if retained:
            raise UserError(_(
                "Posted or rejected AP intakes are retained audit evidence "
                "and cannot be deleted: %s.",
                ', '.join(retained.mapped('display_name')),
            ))
        with_history = self.filtered(
            lambda rec: rec.duplicate_override_log_ids
            or rec.line_ids.mapped('decision_log_ids'),
        )
        if with_history:
            raise UserError(_(
                "AP intakes with append-only decision history must be "
                "retained: %s. Archive or reject them instead.",
                ', '.join(with_history.mapped('display_name')),
            ))
        return super().unlink()

    @api.model
    def _eh_validate_raw_text(self, value):
        if value and len(value.encode('utf-8')) > _MAX_RAW_TEXT_BYTES:
            raise ValidationError(_(
                "Captured bill text exceeds the %(size)s MiB safety limit.",
                size=_MAX_RAW_TEXT_BYTES // (1024 * 1024),
            ))

    @api.model
    def _eh_validate_input_values(self, vals):
        self._eh_validate_raw_text(vals.get('raw_text'))
        limits = {
            'vendor_reference': _MAX_REFERENCE_LENGTH,
            'extractor_key': _MAX_EXTRACTOR_KEY_LENGTH,
            'rejection_reason': _MAX_REASON_LENGTH,
            'duplicate_override_request_reason': _MAX_REASON_LENGTH,
            'notes': _MAX_NOTES_LENGTH,
        }
        for field_name, limit in limits.items():
            value = vals.get(field_name)
            if value and len(value) > limit:
                raise ValidationError(_(
                    "%(field)s exceeds the %(limit)s-character safety limit.",
                    field=field_name, limit=limit,
                ))
        if 'extracted_total' in vals and vals['extracted_total'] not in (
                False, None):
            try:
                total = float(vals['extracted_total'])
            except (TypeError, ValueError) as exc:
                raise ValidationError(_(
                    "Captured document total must be a finite number."
                )) from exc
            if not math.isfinite(total):
                raise ValidationError(_(
                    "Captured document total must be a finite number."
                ))

    def _eh_validate_scope(self):
        for intake in self:
            company = intake.company_id
            if not self.env.su and company not in self.env.companies:
                raise AccessError(_(
                    "Company %s is outside the user's active companies.",
                    company.display_name,
                ))
            if not self.env.su:
                company._eh_check_access('read')
                intake.currency_id._eh_check_access('read')
                if intake.partner_id:
                    intake.partner_id._eh_check_access('read')
                if intake.purchase_order_id:
                    intake.purchase_order_id._eh_check_access('read')
                if intake.journal_id:
                    intake.journal_id._eh_check_access('read')
            partner_company = intake.partner_id.company_id
            if partner_company and partner_company != company:
                raise ValidationError(_(
                    "Vendor %(vendor)s belongs to %(vendor_company)s, not "
                    "intake company %(company)s.",
                    vendor=intake.partner_id.display_name,
                    vendor_company=partner_company.display_name,
                    company=company.display_name,
                ))
            po = intake.purchase_order_id
            if po and (
                    po.company_id != company
                    or po.partner_id.commercial_partner_id
                    != intake.partner_id.commercial_partner_id):
                raise ValidationError(_(
                    "Purchase order must belong to the intake company and "
                    "commercial vendor."
                ))
            journal = intake.journal_id
            if journal and (
                    journal.company_id != company or journal.type != 'purchase'):
                raise ValidationError(_(
                    "AP intake journal must be a purchase journal in the "
                    "intake company."
                ))

    def _eh_lock_rows(self):
        ids = sorted(set(self.ids))
        if not ids:
            return
        self.env.cr.execute(
            "SELECT id FROM eh_ap_intake WHERE id IN %s ORDER BY id FOR UPDATE",
            [tuple(ids)],
        )
        self.invalidate_recordset()

    def _eh_invalidate_review(self, message=None, clear_source=False):
        """Invalidate current approval pointers; append-only logs survive."""
        for intake in self:
            had_review_evidence = intake._eh_has_review_evidence()
            values = {
                'matched_at': False,
                'matched_by_id': False,
                'duplicate_intake_id': False,
                'duplicate_move_id': False,
                'duplicate_override_log_id': False,
                'duplicate_override_request_reason': False,
                'duplicate_override_reason': False,
                'duplicate_overridden_at': False,
                'duplicate_overridden_by_id': False,
                'duplicate_override_fingerprint': False,
            }
            if clear_source:
                values.update({
                    'source_attachment_id': False,
                    'source_fingerprint': False,
                    'source_size': 0,
                    'source_mimetype': False,
                    'source_captured_at': False,
                })
            if intake.state in _MATCHED_STATES:
                values['state'] = 'parsed'
            intake.write(values)
            intake.line_ids._eh_reset_review_evidence()
            if message and had_review_evidence:
                intake.message_post(body=message)

    def _eh_has_review_evidence(self):
        """Whether invalidation has a reviewed result worth announcing."""
        self.ensure_one()
        if self.state != 'received' or any((
                self.parsed_at, self.matched_at, self.source_fingerprint,
                self.duplicate_override_log_id, self.duplicate_intake_id,
                self.duplicate_move_id)):
            return True
        return any(
            line.match_status != 'pending'
            or line.match_evidence_fingerprint
            or line.decision_log_id
            for line in self.line_ids
        )

    # ---- transitions ----

    def _eh_prepare_transition(self):
        if not self.env.su:
            self._eh_check_access('write')
        self._eh_lock_rows()
        elevated = self.sudo()
        quarantined = elevated.filtered('provenance_quarantined')
        if quarantined:
            raise UserError(_(
                "AP intake provenance is quarantined and cannot continue: %s. "
                "Preserve the legacy record and recreate the intake from "
                "verified source evidence.",
                ', '.join(quarantined.mapped('display_name')),
            ))
        return elevated

    def action_parse(self):
        actor = self.env.user
        intakes = self._eh_prepare_transition()._eh_workflow_action()
        for intake in intakes:
            if intake.state not in ('received', 'parsed'):
                raise UserError(_(
                    "Parse is only available in received or parsed state.",
                ))
            intake._eh_capture_source_evidence()
            intake._parse()
            intake.write({
                'state': 'parsed',
                'parsed_at': fields.Datetime.now(),
                'parsed_by_id': actor.id,
            })

    def action_match(self):
        actor = self.env.user
        intakes = self._eh_prepare_transition()._eh_workflow_action()
        for intake in intakes:
            if intake.state not in ('parsed', 'exception'):
                raise UserError(_(
                    "Match is only available in parsed or exception state.",
                ))
            if not intake.line_ids:
                raise UserError(_(
                    "Cannot match: no intake lines to evaluate.",
                ))
            if len(intake.line_ids) > _MAX_EXTRACTED_LINES:
                raise UserError(_(
                    "Intake contains more than %s lines; split or review the "
                    "document before matching.", _MAX_EXTRACTED_LINES,
                ))
            if not intake.source_fingerprint:
                intake._eh_capture_source_evidence()
            else:
                intake._eh_verify_source_evidence()
            intake.line_ids._eh_lock_match_sources()
            intake._match()
            intake._eh_check_duplicate()
            new_state = (
                'matched' if intake.exception_count == 0 else 'exception'
            )
            intake.write({
                'state': new_state,
                'matched_at': fields.Datetime.now(),
                'matched_by_id': actor.id,
            })

    def action_override_duplicate(self):
        """Manager override of a flagged duplicate. Requires a reason."""
        self.ensure_one()
        if not self.env.su:
            self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only ERP Heritage managers can override duplicate flags.",
            ))
        actor = self.env.user
        intake = self._eh_prepare_transition()
        intake._eh_lock_duplicate_identity()
        if intake.state not in _MATCHED_STATES:
            raise UserError(_(
                "Duplicate overrides require a matched or exception intake."
            ))
        intake._eh_verify_source_evidence()
        intake._eh_check_duplicate()
        if not intake.duplicate_intake_id and not intake.duplicate_move_id:
            raise UserError(_(
                "No duplicate is currently flagged on this intake.",
            ))
        reason = (intake.duplicate_override_request_reason or '').strip()
        if not reason:
            raise UserError(_(
                "A reason is required when overriding a duplicate flag.",
            ))
        fingerprint = intake._eh_duplicate_evidence_fingerprint()
        log = self.env['eh.ap.duplicate.override.log'].sudo().create({
            'intake_id': intake.id,
            'duplicate_intake_id': intake.duplicate_intake_id.id,
            'duplicate_move_id': intake.duplicate_move_id.id,
            'reason': reason,
            'evidence_fingerprint': fingerprint,
            'overridden_by_id': actor.id,
            'overridden_at': fields.Datetime.now(),
        })
        intake.write({
            'duplicate_override_log_id': log.id,
            'duplicate_override_request_reason': False,
            'duplicate_override_reason': reason,
            'duplicate_overridden_at': log.overridden_at,
            'duplicate_overridden_by_id': actor.id,
            'duplicate_override_fingerprint': fingerprint,
        })
        intake.message_post(body=_(
            "Duplicate override applied by %(user)s. Reason: %(reason)s",
            user=actor.name,
            reason=reason,
        ))

    def _eh_check_duplicate(self):
        """Flag a duplicate when (vendor, vendor_reference, total, date)
        already exists on another intake or a posted vendor bill in the
        same company. Tolerance on total is 0.01 of the captured currency.

        Sets duplicate_intake_id / duplicate_move_id; does not raise.
        Posting blocks until the flag is cleared or overridden with a
        reason.
        """
        self.ensure_one()
        old_ids = (self.duplicate_intake_id.id, self.duplicate_move_id.id)
        if not self.partner_id or not self.vendor_reference:
            self.duplicate_intake_id = False
            self.duplicate_move_id = False
            return
        ref = self._eh_normalize_reference(self.vendor_reference)
        if not ref:
            self.duplicate_intake_id = False
            self.duplicate_move_id = False
            return
        total = self.extracted_total or 0.0
        commercial_partner = self.partner_id.commercial_partner_id
        self.env.cr.execute(
            """
                SELECT intake.id
                  FROM eh_ap_intake AS intake
                  JOIN res_partner AS partner ON partner.id = intake.partner_id
                 WHERE intake.id != %s
                   AND intake.company_id = %s
                   AND COALESCE(partner.commercial_partner_id, partner.id) = %s
                   AND intake.state != 'rejected'
                   AND intake.currency_id = %s
                   AND intake.bill_date IS NOT DISTINCT FROM %s
                   AND ABS(
                       COALESCE(intake.extracted_total, 0) - %s
                   ) <= %s
                   AND REGEXP_REPLACE(
                       UPPER(BTRIM(COALESCE(intake.vendor_reference, ''))),
                       '[[:space:]]+', ' ', 'g'
                   ) = %s
                 ORDER BY intake.id
                 LIMIT 1
            """,
            [
                self.id, self.company_id.id, commercial_partner.id,
                self.currency_id.id,
                self.bill_date or None, total,
                max(self.currency_id.rounding, 0.01), ref,
            ],
        )
        row = self.env.cr.fetchone()
        match = self.sudo().browse(row[0]).exists() if row else self.browse()
        self.duplicate_intake_id = match.id if match else False
        # Posted bills: normalized supplier reference is a stronger identity
        # than amount/date.  A legitimate reuse therefore requires a fresh,
        # explicit manager override against the exact found bill.
        params = [self.company_id.id, commercial_partner.id, ref]
        exclude_sql = ''
        if self.move_id:
            exclude_sql = 'AND move.id != %s'
            params.append(self.move_id.id)
        self.env.cr.execute(
            """
                SELECT move.id
                  FROM account_move AS move
                 WHERE move.company_id = %s
                   AND move.commercial_partner_id = %s
                   AND move.move_type IN ('in_invoice', 'in_refund')
                   AND move.state = 'posted'
                   AND REGEXP_REPLACE(
                       UPPER(BTRIM(COALESCE(move.ref, ''))),
                       '[[:space:]]+', ' ', 'g'
                   ) = %s
                   %s
                 ORDER BY move.id
                 LIMIT 1
            """ % ('%s', '%s', '%s', exclude_sql),
            params,
        )
        row = self.env.cr.fetchone()
        move_match = (
            self.env['account.move'].sudo().browse(row[0]).exists()
            if row else self.env['account.move']
        )
        self.duplicate_move_id = move_match.id if move_match else False
        new_ids = (self.duplicate_intake_id.id, self.duplicate_move_id.id)
        if new_ids != old_ids and any(new_ids):
            self.message_post(body=_(
                "Duplicate flag raised: vendor %(vendor)s, ref %(ref)s, "
                "total %(total).2f.",
                vendor=self.partner_id.display_name,
                ref=ref, total=total,
            ))

    @staticmethod
    def _eh_normalize_reference(reference):
        return ' '.join(str(reference or '').split()).upper()

    def _eh_duplicate_lock_key(self):
        self.ensure_one()
        commercial_partner = self.partner_id.commercial_partner_id
        reference = self._eh_normalize_reference(self.vendor_reference)
        if not (self.company_id and commercial_partner and reference):
            return False
        return 'eh-ap-duplicate|%s|%s|%s' % (
            self.company_id.id, commercial_partner.id, reference,
        )

    def _eh_lock_duplicate_identity(self):
        keys = sorted(filter(None, (
            intake._eh_duplicate_lock_key() for intake in self
        )))
        for key in keys:
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                [key],
            )

    def _eh_duplicate_evidence_fingerprint(self):
        self.ensure_one()
        payload = {
            'intake_id': self.id,
            'company_id': self.company_id.id,
            'commercial_partner_id': self.partner_id.commercial_partner_id.id,
            'reference': self._eh_normalize_reference(self.vendor_reference),
            'bill_date': str(self.bill_date or ''),
            'currency_id': self.currency_id.id,
            'total': format(self.extracted_total or 0.0, '.16g'),
            'source_fingerprint': self.source_fingerprint or '',
            'duplicate_intake': (
                self.duplicate_intake_id.id,
                str(self.duplicate_intake_id.write_date or ''),
                self.duplicate_intake_id.state or '',
            ),
            'duplicate_move': (
                self.duplicate_move_id.id,
                str(self.duplicate_move_id.write_date or ''),
                self.duplicate_move_id.state or '',
            ),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def action_post(self):
        if not self.env.su:
            self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only ERP Heritage managers can post AP intakes.",
            ))
        actor = self.env.user
        self = self._eh_prepare_transition()._eh_workflow_action()
        self._eh_lock_duplicate_identity()
        for intake in self:
            if intake.state != 'matched':
                raise UserError(_(
                    "Only matched intakes can be posted. Current state: %s.",
                    intake.state,
                ))
            if intake.exception_count > 0:
                raise UserError(_(
                    "Intake has %d unresolved exception line(s).",
                    intake.exception_count,
                ))
            if not intake.line_ids or len(intake.line_ids) > _MAX_EXTRACTED_LINES:
                raise UserError(_(
                    "Posting requires between 1 and %s reviewed lines.",
                    _MAX_EXTRACTED_LINES,
                ))
            if not intake.partner_id or not self._eh_normalize_reference(
                    intake.vendor_reference):
                raise UserError(_(
                    "A vendor and non-empty vendor reference are required "
                    "before posting."
                ))
            if not intake.extracted_total \
                    or not math.isfinite(intake.extracted_total) \
                    or intake.currency_id.compare_amounts(
                        intake.extracted_total, 0.0) <= 0:
                raise UserError(_(
                    "A positive finite captured document total is required "
                    "before "
                    "posting intake %s.", intake.display_name,
                ))
            if not intake.bill_date:
                raise UserError(_(
                    "A bill date is required before posting intake %s.",
                    intake.display_name,
                ))
            intake._eh_verify_source_evidence()
            intake.line_ids._eh_lock_match_sources()
            intake.line_ids._eh_validate_posting_evidence()
            intake._eh_check_duplicate()
            if intake.duplicate_intake_id or intake.duplicate_move_id:
                expected = intake._eh_duplicate_evidence_fingerprint()
                if not intake.duplicate_override_log_id or \
                        intake.duplicate_override_fingerprint != expected:
                    raise UserError(_(
                        "Duplicate evidence changed or has not received a "
                        "current manager override. Review and override intake "
                        "%s again.", intake.display_name,
                    ))
            move = intake._build_move(actor=actor)
            intake.write({
                'state': 'posted',
                'posted_at': fields.Datetime.now(),
                'posted_by_id': actor.id,
                'move_id': move.id,
            })

    def action_reject(self):
        if not self.env.su:
            self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only ERP Heritage managers can reject AP intakes.",
            ))
        actor = self.env.user
        self = self._eh_prepare_transition()._eh_workflow_action()
        for intake in self:
            if intake.state == 'posted':
                raise UserError(_(
                    "Posted intakes cannot be rejected.",
                ))
            if not (intake.rejection_reason or '').strip():
                raise UserError(_(
                    "A rejection reason is required for intake %s.",
                    intake.display_name,
                ))
            intake.write({
                'state': 'rejected',
                'rejected_at': fields.Datetime.now(),
                'rejected_by_id': actor.id,
            })

    def action_view_move(self):
        self.ensure_one()
        self._eh_check_access('read')
        if not self.move_id:
            raise UserError(_("No bill has been posted yet."))
        self.move_id._eh_check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
            'views': [(False, 'form')],
        }

    def _eh_line_list_action(self, name, statuses=None):
        self.ensure_one()
        self._eh_check_access('read')
        domain = [('intake_id', '=', self.id)]
        if statuses is not None:
            domain.append(('match_status', 'in', list(statuses)))
        return {
            'type': 'ir.actions.act_window',
            'name': name,
            'res_model': 'eh.ap.intake.line',
            'view_mode': 'list,form',
            'views': [(False, 'list'), (False, 'form')],
            'domain': domain,
            'context': {'default_intake_id': self.id},
        }

    def action_view_lines(self):
        self.ensure_one()
        return self._eh_line_list_action(
            _("Lines for %s") % self.display_name,
        )

    def action_view_matched_lines(self):
        self.ensure_one()
        return self._eh_line_list_action(
            _("Matched lines for %s") % self.display_name,
            _MATCHED_LINE_STATUSES,
        )

    def action_view_exception_lines(self):
        self.ensure_one()
        return self._eh_line_list_action(
            _("Exception lines for %s") % self.display_name,
            _EXCEPTION_LINE_STATUSES,
        )

    # ---- parser ----

    def _eh_source_attachment(self):
        self.ensure_one()
        if self.source_attachment_id:
            return self.source_attachment_id
        domain = [
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('type', '=', 'binary'),
        ]
        main = self.message_main_attachment_id
        if main and main.res_model == self._name and main.res_id == self.id \
                and (main.mimetype or '').startswith(
                    ('application/pdf', 'image/')):
            return main
        # Email signatures frequently arrive as image attachments after the
        # invoice. Prefer the newest PDF, then an image only when no PDF is
        # present, so an embedded logo cannot become the sealed bill source.
        pdf = self.env['ir.attachment'].search(
            domain + [('mimetype', '=like', 'application/pdf%')],
            order='id desc', limit=1,
        )
        if pdf:
            return pdf
        return self.env['ir.attachment'].search(
            domain + [('mimetype', '=like', 'image/%')],
            order='id desc', limit=1,
        )

    def _eh_source_payload(self):
        self.ensure_one()
        raw_text = (self.raw_text or '').encode('utf-8')
        self._eh_validate_raw_text(self.raw_text)
        attachment = self._eh_source_attachment()
        document = raw_text
        mimetype = 'text/plain'
        if attachment:
            attachment._eh_lock_ap_source_mutation()
            self.env.cr.execute(
                "SELECT id FROM ir_attachment WHERE id = %s FOR UPDATE",
                [attachment.id],
            )
            attachment.invalidate_recordset([
                'raw', 'mimetype', 'type', 'file_size', 'res_model', 'res_id',
                'company_id', 'public', 'name',
            ])
            if attachment.type != 'binary' \
                    or attachment.res_model != self._name \
                    or attachment.res_id != self.id:
                raise ValidationError(_(
                    "The captured source must remain a binary attachment "
                    "on this AP intake."
                ))
            if attachment.public:
                raise ValidationError(_(
                    "AP source documents cannot be public attachments."
                ))
            if attachment.company_id \
                    and attachment.company_id != self.company_id:
                raise ValidationError(_(
                    "AP source document company must match the intake "
                    "company."
                ))
            if attachment.file_size \
                    and attachment.file_size > _MAX_SOURCE_DOCUMENT_BYTES:
                raise ValidationError(_(
                    "Source document exceeds the %(size)s MiB safety limit.",
                    size=_MAX_SOURCE_DOCUMENT_BYTES // (1024 * 1024),
                ))
            document = attachment.sudo().raw or b''
            if isinstance(document, str):
                document = document.encode('utf-8')
            mimetype = attachment.mimetype or 'application/octet-stream'
        if len(document) > _MAX_SOURCE_DOCUMENT_BYTES:
            raise ValidationError(_(
                "Source document exceeds the %(size)s MiB safety limit.",
                size=_MAX_SOURCE_DOCUMENT_BYTES // (1024 * 1024),
            ))
        digest = hashlib.sha256()
        digest.update(b'eh-ap-source-v2\0')
        digest.update(str(attachment.id or 0).encode('ascii'))
        digest.update(b'\0attachment-name\0')
        digest.update(
            (attachment.name or '').encode('utf-8', 'replace')
            if attachment else b''
        )
        digest.update(b'\0attachment-company\0')
        digest.update(
            str(attachment.company_id.id or 0).encode('ascii')
            if attachment else b'0'
        )
        digest.update(b'\0raw-text\0')
        digest.update(raw_text)
        digest.update(b'\0email-subject\0')
        digest.update((self.source_email_subject or '').encode(
            'utf-8', 'replace',
        ))
        digest.update(b'\0document-mimetype\0')
        digest.update(mimetype.encode('utf-8', 'replace'))
        digest.update(b'\0document\0')
        digest.update(document)
        return attachment, document, mimetype, digest.hexdigest()

    def _eh_capture_source_evidence(self):
        self.ensure_one()
        attachment, document, mimetype, fingerprint = (
            self._eh_source_payload()
        )
        self.write({
            'source_attachment_id': attachment.id,
            'source_fingerprint': fingerprint,
            'source_size': len(document),
            'source_mimetype': mimetype,
            'source_captured_at': fields.Datetime.now(),
        })
        return fingerprint

    def _eh_verify_source_evidence(self):
        self.ensure_one()
        if not self.source_fingerprint:
            raise UserError(_(
                "Intake %s has no captured source fingerprint; parse or match "
                "it again before posting.", self.display_name,
            ))
        attachment, document, mimetype, fingerprint = self._eh_source_payload()
        if attachment != self.source_attachment_id \
                or len(document) != self.source_size \
                or mimetype != self.source_mimetype \
                or fingerprint != self.source_fingerprint:
            raise UserError(_(
                "Captured source evidence for intake %s changed after review. "
                "Posting is blocked; restore the source or restart review.",
                self.display_name,
            ))
        return True

    def _parse(self):
        """Default parser: regex-based extraction from raw_text.

        Sites can override this method (or _parse_lines / _parse_header)
        to plug in a real OCR provider.
        """
        self.ensure_one()
        if not self.raw_text and not self.source_attachment_id:
            raise UserError(_(
                "Intake %s has no captured text or source document to parse. "
                "Upload a document or provide bill text.",
                self.name,
            ))
        self._parse_header()
        self._parse_lines()

    def _parse_header(self):
        self.ensure_one()
        # Resolution order: partner override > company-wide setting > module fallback.
        company = self.company_id or self.env.company
        partner = self.partner_id.with_company(company)
        ref_pattern = (
            (partner and partner.eh_ap_invoice_ref_regex)
            or company.eh_ap_invoice_ref_regex
        )
        total_pattern = (
            (partner and partner.eh_ap_total_regex)
            or company.eh_ap_total_regex
        )
        # Base versions before this hardening release seeded permissive
        # values on every company. Treat those exact shipped bytes as the
        # obsolete default, not as an administrator-authored override.
        if ref_pattern == _LEGACY_INVOICE_REF_PATTERN:
            ref_pattern = False
        if total_pattern == _LEGACY_TOTAL_PATTERN:
            total_pattern = False
        m = self._eh_first_safe_regex_search(
            (ref_pattern,) if ref_pattern else _DEFAULT_INVOICE_REF_PATTERNS,
            self.raw_text or '', _("invoice reference"),
        )
        if m:
            self.vendor_reference = m.group(1)
        m = self._eh_first_safe_regex_search(
            (total_pattern,) if total_pattern else _DEFAULT_TOTAL_PATTERNS,
            self.raw_text or '', _("document total"),
        )
        if m:
            try:
                self.extracted_total = self._parse_decimal(m.group(1))
            except ValueError:
                pass

    @classmethod
    def _eh_first_safe_regex_search(cls, patterns, text, label):
        for pattern in patterns:
            match = cls._eh_safe_regex_search(pattern, text, label)
            if match:
                return match
        return None

    @staticmethod
    def _eh_safe_regex_search(pattern, text, label):
        if len(text) > MAX_SEARCH_CHARACTERS:
            raise UserError(_(
                "Captured text is too large for the bounded %s parser. Use "
                "a reviewed extractor or reduce it below %s characters.",
                label, MAX_SEARCH_CHARACTERS,
            ))
        try:
            compiled = compile_safe_regex(pattern)
        except ValueError as exc:
            raise UserError(_(
                "Configured %(label)s parser pattern is unsafe: %(error)s.",
                label=label, error=exc,
            )) from exc
        return compiled.search(text)

    def _parse_lines(self):
        self.ensure_one()
        Line = self.env['eh.ap.intake.line']
        Product = self.env['product.product']
        # Only seed lines if there are none yet to avoid clobbering
        # manually entered lines on re-parse.
        if self.line_ids:
            return
        # Pluggable extractor first: when configured, defer to the
        # registered adapter and use the regex parser only as a fallback
        # for empty extractor results.
        if self.extractor_key:
            try:
                if self._run_line_extractor():
                    return
            except Exception as exc:  # pragma: no cover -- adapter
                self.extractor_last_run_summary = (
                    "Extractor %r failed; see the server log."
                    % self.extractor_key
                )
                self.message_post(body=self.extractor_last_run_summary)
                _logger.exception(
                    "AP line extractor %r failed for intake %s: %s",
                    self.extractor_key, self.id, exc,
                )
        matches = list(itertools.islice(
            _LINE_RE.finditer(self.raw_text or ''),
            _MAX_EXTRACTED_LINES + 1,
        ))
        if len(matches) > _MAX_EXTRACTED_LINES:
            raise UserError(_(
                "Captured text contains more than %s candidate lines; split "
                "or review the document.", _MAX_EXTRACTED_LINES,
            ))
        parsed = []
        for match in matches:
            code = match.group(1)
            try:
                qty = float(match.group('qty'))
                price = float(match.group('price'))
            except ValueError:
                continue
            parsed.append((code, qty, price))
        codes = sorted({code for code, _qty, _price in parsed})
        products = Product.search([
            ('default_code', 'in', codes),
            ('product_tmpl_id.company_id', 'in', [
                self.company_id.id, False,
            ]),
        ], order='id') if codes else Product
        product_by_code = {}
        for product in products:
            current = product_by_code.get(product.default_code)
            if not current or (
                    not current.product_tmpl_id.company_id
                    and product.product_tmpl_id.company_id == self.company_id):
                product_by_code[product.default_code] = product
        line_vals_list = []
        for code, qty, price in parsed:
            product = product_by_code.get(code)
            line_vals_list.append({
                'intake_id': self.id,
                'product_code': code,
                'product_id': product.id if product else False,
                'invoice_qty': qty,
                'invoice_price': price,
                'subtotal': qty * price,
            })
        if line_vals_list:
            Line.create(line_vals_list)

    def _run_line_extractor(self):
        """Drive the configured extractor and seed line_ids from the
        result. Returns True when at least one line was created so the
        caller skips the regex fallback. Raises ExtractorError on any
        adapter-side failure; the caller logs and falls back.
        """
        from odoo.addons.eh_account_ap_automation.extractors.registry import (
            get_extractor, has_extractor,
        )
        if not has_extractor(self.extractor_key):
            self.extractor_last_run_summary = (
                "No extractor registered for key %r" % self.extractor_key
            )
            return False
        extractor = get_extractor(self.extractor_key)
        # The intake stores text only by default; for binary documents,
        # downstream packages can override _line_extractor_document() to
        # return (bytes, mime_type). The default returns the captured
        # raw_text encoded as utf-8, mime text/plain.
        document_bytes, mime_type = self._line_extractor_document()
        hints = {
            'partner_id': self.partner_id.id,
            'currency_id': self.currency_id.id,
            'parsed_total': self.extracted_total,
            'vendor_reference': self.vendor_reference,
            # eh_ap_ocr_config is restricted to base.group_system so a
            # plain internal user cannot read the extractor API key over
            # RPC; the intake reads it via sudo here so OCR still runs for
            # ordinary users without ever surfacing the secret to them.
            'credentials_json': (
                self.company_id.sudo().eh_ap_ocr_config or ''
            ),
        }
        results = list(itertools.islice(
            extractor.extract(document_bytes, mime_type, hints),
            _MAX_EXTRACTED_LINES + 1,
        ))
        if len(results) > _MAX_EXTRACTED_LINES:
            raise UserError(_(
                "Extractor returned more than %s lines; review the document "
                "or extractor configuration.", _MAX_EXTRACTED_LINES,
            ))
        threshold = self.extractor_min_confidence or 0.0
        kept = []
        for result in results:
            try:
                confidence = float(result.confidence or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                confidence = 0.0
            if confidence >= threshold:
                kept.append((result, confidence))
        dropped = len(results) - len(kept)
        Line = self.env['eh.ap.intake.line']
        line_vals_list = []
        for ext, confidence in kept:
            qty = float(ext.quantity or 0.0)
            price = float(ext.unit_price or 0.0)
            line_total = float(ext.line_total or qty * price)
            if not all(math.isfinite(value) for value in (
                    qty, price, line_total)) or qty <= 0:
                raise UserError(_(
                    "Extractor produced a non-finite amount or non-positive "
                    "quantity. No extracted lines were accepted."
                ))
            # Cross-check qty * unit_price against line_total. If it
            # diverges by more than 1 cent, prefer the line_total and
            # back-solve the unit_price; that minimises the sum drift.
            computed = round(qty * price, 2)
            if abs(computed - round(line_total, 2)) > 0.01 and qty:
                price = round(line_total / qty, 4)
            metadata_payload = {
                'extra': ext.extra or {},
                'tax_rate_pct': ext.tax_rate_pct,
            }
            try:
                metadata = json.dumps(
                    metadata_payload, sort_keys=True, separators=(',', ':'),
                )
            except (TypeError, ValueError) as exc:
                raise UserError(_(
                    "Extractor returned non-JSON line metadata."
                )) from exc
            if len(metadata.encode('utf-8')) > _MAX_EXTRACTOR_METADATA_BYTES:
                raise UserError(_(
                    "Extractor line metadata exceeds the 16 KiB safety limit."
                ))
            tax = self._eh_tax_for_extracted_rate(ext.tax_rate_pct)
            line_vals_list.append({
                'intake_id': self.id,
                'product_code': '',
                'description': str(ext.description or '')[:2000],
                'invoice_qty': qty,
                'invoice_price': price,
                'subtotal': round(line_total, 2),
                'extractor_confidence': confidence,
                'extractor_metadata_json': metadata,
                'tax_ids': [(6, 0, tax.ids)],
                'tax_provenance': 'extractor' if tax else 'none',
            })
        if line_vals_list:
            # Validate the complete adapter response before creating any
            # records. A malformed later item must not leave earlier OCR
            # lines behind and then append the regex fallback as duplicates.
            Line.create(line_vals_list)
        self.extractor_last_run_summary = (
            "Extractor %s: kept %s of %s line(s); %s below confidence %.2f."
            % (self.extractor_key, len(kept), len(results), dropped, threshold)
        )
        if kept:
            self.message_post(body=self.extractor_last_run_summary)
        return bool(kept)

    def _eh_tax_for_extracted_rate(self, rate):
        """Resolve an explicit extractor rate without making a tax guess."""
        self.ensure_one()
        if rate is None:
            return self.env['account.tax']
        try:
            rate = float(rate)
        except (TypeError, ValueError) as exc:
            raise UserError(_(
                "Extractor produced an invalid purchase-tax rate."
            )) from exc
        if not math.isfinite(rate) or not 0.0 <= rate <= 100.0:
            raise UserError(_(
                "Extractor purchase-tax rate must be between 0 and 100."
            ))
        candidates = self.env['account.tax'].search([
            ('company_id', '=', self.company_id.id),
            ('type_tax_use', '=', 'purchase'),
            ('amount_type', '=', 'percent'),
            ('price_include', '=', False),
            ('active', '=', True),
        ]).filtered(lambda tax: math.isclose(
            tax.amount, rate, rel_tol=0.0, abs_tol=1e-9,
        ))
        if len(candidates) != 1:
            raise UserError(_(
                "Extractor tax rate %(rate)s%% must match exactly one active, "
                "non-price-included purchase tax in %(company)s; found "
                "%(count)s.",
                rate=rate, company=self.company_id.display_name,
                count=len(candidates),
            ))
        return candidates

    def _line_extractor_document(self):
        """Return (bytes, mime_type) for the extractor.

        Prefers the original captured document: the chatter's main
        attachment, else the first PDF or image attachment on any
        message. Vision extractors need the real document, not the
        regex parser's text dump. Falls back to the utf-8 encoded
        raw_text (text/plain) when no binary document is present, which
        keeps text-only extractors and the no-attachment path working.
        """
        self.ensure_one()
        _attachment, document, mimetype, _fingerprint = (
            self._eh_source_payload()
        )
        return document, mimetype

    @staticmethod
    def _parse_decimal(text):
        """Parse a number that may use comma or dot as decimal separator."""
        text = text.strip().replace(' ', '')
        if ',' in text and '.' in text:
            # The right-most separator is the decimal marker; the other is
            # grouping. This handles both 1,234.56 and 1.234,56.
            if text.rfind(',') > text.rfind('.'):
                text = text.replace('.', '').replace(',', '.')
            else:
                text = text.replace(',', '')
        elif ',' in text:
            parts = text.split(',')
            if len(parts) == 2 and len(parts[1]) == 3 \
                    and 0 < len(parts[0]) <= 3:
                text = ''.join(parts)
            elif len(parts) > 2 and all(len(part) == 3 for part in parts[1:]):
                text = ''.join(parts)
            else:
                text = ''.join(parts[:-1]) + '.' + parts[-1]
        return float(text)

    # ---- matcher ----

    def _match(self):
        self.ensure_one()
        self._eh_validate_scope()
        if not self.partner_id:
            raise UserError(_(
                "Cannot match intake %s: partner is missing.", self.name,
            ))
        Profile = self.env['eh.ap.tolerance.profile']
        profile = Profile.resolve_for_partner(
            self.partner_id, company=self.company_id,
        )
        consumed_by_po_line = {}
        lines = self.line_ids.sorted(lambda line: (line.sequence, line.id))
        for line in lines:
            line._evaluate_match(
                profile, consumed_by_po_line=consumed_by_po_line,
            )
        # Stamp only after every sibling has been evaluated: each fingerprint
        # binds the complete same-PO sibling set, not a partially-built view
        # whose content depends on loop position.
        for line in lines:
            line._eh_stamp_match_fingerprint(profile)

    # ---- posting ----

    def _build_move(self, actor=None):
        self.ensure_one()
        self._eh_validate_scope()
        journal = self.journal_id or self.env['account.journal'].search([
            ('type', '=', 'purchase'),
            ('company_id', '=', self.company_id.id),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "No purchase journal configured for company %s.",
                self.company_id.display_name,
            ))
        if journal.company_id != self.company_id or journal.type != 'purchase':
            raise UserError(_(
                "Journal %s is not a purchase journal in the intake company.",
                journal.display_name,
            ))
        invoice_line_ids = []
        for line in self.line_ids:
            if line.match_status == 'rejected':
                raise UserError(_(
                    "Rejected line %s cannot be silently omitted from a "
                    "captured vendor bill. Reject the intake or correct and "
                    "re-match it.", line.display_name,
                ))
            invoice_line_ids.append((0, 0, line._build_invoice_line_vals()))
        if not invoice_line_ids:
            raise UserError(_(
                "Intake %s has no postable lines.", self.name,
            ))
        actor = actor or self.env.user
        Move = self.env['account.move'].with_user(actor).with_company(
            self.company_id,
        )
        move = Move._eh_create_sealed({
            'move_type': 'in_invoice',
            'company_id': self.company_id.id,
            'partner_id': self.partner_id.id,
            'invoice_date': self.bill_date,
            'ref': self.vendor_reference or self.name,
            'journal_id': journal.id,
            'currency_id': self.currency_id.id,
            'invoice_line_ids': invoice_line_ids,
        })
        move._eh_attach_ap_intake(self)
        move.invalidate_recordset(['amount_total'])
        if self.currency_id.compare_amounts(
                move.amount_total, self.extracted_total) != 0:
            raise UserError(_(
                "Generated bill total %(actual)s does not exactly match "
                "captured document total %(expected)s in %(currency)s. "
                "Review quantities, prices and taxes before posting.",
                actual=move.amount_total,
                expected=self.extracted_total,
                currency=self.currency_id.name,
            ))
        move.action_post()
        return move

    # ---- cron ----

    @api.model
    def _cron_advance(self, batch_size=200):
        """Drive received intakes through parse and then match.

        Per record try / except so one bad intake does not stop the
        batch. Posting still requires a manager and is not automatic.
        """
        try:
            batch_size = int(batch_size)
        except (TypeError, ValueError):
            batch_size = 200
        batch_size = min(max(batch_size, 1), 1000)
        received = self.search([
            ('state', '=', 'received'),
            ('raw_text', '!=', False),
        ], limit=batch_size)
        for intake in received:
            try:
                with self.env.cr.savepoint():
                    intake.action_parse()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Auto parse failed for intake %s: %s",
                    intake.display_name, exc,
                )
        parsed = self.search([
            ('state', '=', 'parsed'),
            ('partner_id', '!=', False),
        ], limit=batch_size)
        for intake in parsed:
            try:
                with self.env.cr.savepoint():
                    intake.action_match()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "Auto match failed for intake %s: %s",
                    intake.display_name, exc,
                )


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    def _eh_lock_ap_source_mutation(self):
        """Serialize capture with every mutation of a candidate source.

        A row lock alone is too late for ``write``/``unlink``: a writer can
        first observe that the attachment is not captured, wait behind the
        capture's row lock, and then mutate the newly-sealed source.  A
        transaction advisory lock is acquired before either side's evidence
        check, so the winner fully commits before the loser re-evaluates.
        """
        for attachment_id in sorted(set(self.ids)):
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ['eh-ap-source-attachment|%s' % attachment_id],
            )
        return self

    def _eh_captured_ap_sources(self):
        if not self:
            return self.env['eh.ap.intake']
        return self.env['eh.ap.intake'].sudo().search([
            ('source_attachment_id', 'in', self.ids),
        ], limit=1)

    def _eh_ap_source_candidates(self):
        """Attachments capable of being a captured AP source right now."""
        return self.filtered(
            lambda attachment: attachment.res_model == 'eh.ap.intake'
            and bool(attachment.res_id),
        )

    def write(self, vals):
        if _CAPTURED_ATTACHMENT_FIELDS.intersection(vals):
            if not self.env.su:
                self._eh_check_access('write')
            candidates = self._eh_ap_source_candidates()
            if candidates:
                candidates._eh_lock_ap_source_mutation()
                if candidates._eh_captured_ap_sources():
                    raise UserError(_(
                        "A captured AP source attachment is immutable. Change "
                        "the intake evidence first to restart review."
                    ))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            self._eh_check_access('unlink')
        candidates = self._eh_ap_source_candidates()
        if candidates:
            candidates._eh_lock_ap_source_mutation()
            if candidates._eh_captured_ap_sources():
                raise UserError(_(
                    "A captured AP source attachment is immutable and cannot "
                    "be deleted."
                ))
        return super().unlink()
