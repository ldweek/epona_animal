# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.report.schedule: cron driven email delivery of dynamic reports.

A schedule binds a report record + an options dict + a recipient list +
a recurrence (daily, weekly, monthly). The cron _cron_run_due runs hourly
and dispatches every schedule whose next_run is past. Each delivery
generates an attachment (XLSX, PDF, or both) and emails it via mail.mail.

Failure handling:

* Delivery exceptions are caught per schedule. The error is logged on the
  schedule (last_error, last_run_status='error') and the next_run is still
  advanced so a single bad schedule does not freeze the queue.
* If a recipient list is empty at delivery time, the schedule errors with
  a clear message rather than silently sending to nobody.

Attachment lifecycle:

* Successful synchronous email removes its transient mail/attachment rows.
* Authenticated webhook download files are linked to their schedule and
  removed by autovacuum after 30 days.
* The execution audit row from the orchestrator captures the exact options
  used, so a recipient can verify the render is reproducible.
"""

import base64
import http.client
import ipaddress
import json
import logging
import socket
from datetime import timedelta
from urllib.parse import quote, urlsplit, urlunsplit

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.eh_account_base.tools.net_guard import (
    UnsafeUrlError, assert_safe_url,
)

from .period_options import (
    PERIOD_PRESET_SELECTION,
    apply_period_preset,
    parse_options_json,
    validate_period_fields,
)

_logger = logging.getLogger(__name__)

_BLOCKED_WEBHOOK_HOSTS = frozenset({
    'metadata',
    'metadata.google.internal',
})
_MAX_WEBHOOK_TIMEOUT_SECONDS = 60


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS with DNS fixed to one already-validated socket address.

    ``host`` remains the original hostname, so the Host header, TLS SNI, and
    certificate hostname verification all use the intended public service.
    Only the TCP destination is replaced with the validated numeric endpoint;
    no resolver runs between validation and connect.
    """

    def __init__(self, host, port, endpoint, timeout):
        self._eh_endpoint = endpoint
        super().__init__(host=host, port=port, timeout=timeout)

    def connect(self):
        if self._tunnel_host:
            raise OSError("Proxy tunnels are disabled for pinned webhooks.")
        family, socktype, proto, sockaddr = self._eh_endpoint
        raw_socket = socket.socket(family, socktype, proto)
        try:
            raw_socket.settimeout(self.timeout)
            if self.source_address:
                raw_socket.bind(self.source_address)
            raw_socket.connect(sockaddr)
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host.rstrip('.'),
            )
        except Exception:
            raw_socket.close()
            raise


class EhReportSchedule(models.Model):
    _name = 'eh.report.schedule'
    _description = "Scheduled report email delivery"
    _order = 'next_run asc, id asc'
    _inherit = ['mail.thread']

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True)

    user_id = fields.Many2one(
        'res.users',
        required=True,
        default=lambda self: self.env.user,
        index=True,
        readonly=True,
        help=(
            "Display owner. It is fixed to the immutable create_uid for "
            "non-superuser-created schedules."
        ),
    )
    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    report_id = fields.Many2one(
        'eh.account.dynamic.report',
        required=True,
        ondelete='cascade',
        index=True,
    )

    options_json = fields.Text(
        required=True,
        default='{}',
        string="Advanced Options JSON",
        help=(
            "Validated JSON options applied at delivery time. Use Period "
            "Preset for normal date configuration; edit JSON only for "
            "advanced report filters."
        ),
    )
    period_preset = fields.Selection(
        PERIOD_PRESET_SELECTION,
        required=True,
        default='options',
        help=(
            "Structured date scope evaluated at delivery time. Use Saved "
            "Options preserves the date block in Advanced Options JSON."
        ),
    )
    period_date_from = fields.Date(string="Date From")
    period_date_to = fields.Date(string="Date To")

    interval = fields.Integer(default=1, required=True)
    interval_unit = fields.Selection(
        [
            ('day', "Day(s)"),
            ('week', "Week(s)"),
            ('month', "Month(s)"),
        ],
        default='month',
        required=True,
    )
    next_run = fields.Datetime(
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    last_run = fields.Datetime(readonly=True, tracking=True)
    last_run_status = fields.Selection(
        [
            ('success', "Success"),
            ('error', "Error"),
        ],
        readonly=True,
        tracking=True,
    )
    last_error = fields.Text(readonly=True, tracking=True)
    last_attachment_count = fields.Integer(
        default=0, readonly=True, tracking=True,
    )

    delivery_format = fields.Selection(
        [
            ('xlsx', "XLSX"),
            ('pdf', "PDF"),
            ('both', "XLSX + PDF"),
        ],
        default='xlsx',
        required=True,
    )

    recipient_user_ids = fields.Many2many(
        'res.users',
        'eh_report_schedule_user_rel',
        'schedule_id',
        'user_id',
        string="User Recipients",
    )
    recipient_partner_ids = fields.Many2many(
        'res.partner',
        'eh_report_schedule_partner_rel',
        'schedule_id',
        'partner_id',
        string="Partner Recipients",
    )
    recipient_emails = fields.Char(
        help="Additional comma separated email addresses.",
    )

    subject = fields.Char(
        required=True,
        default="Scheduled Report",
        translate=True,
    )
    body = fields.Html(translate=True)

    # ---- delivery channel ----
    #
    # The original delivery path posts the rendered attachments via
    # mail.mail. Modern shops also want the report to land directly in
    # a Slack/Teams channel. The webhook channel POSTs a JSON payload
    # to a configurable URL with a download URL for each attachment;
    # Slack and Teams both consume the same shape (text + optional
    # attachment array). Generic HTTP webhooks accept the same payload.
    delivery_channel = fields.Selection(
        [
            ('email',   "Email"),
            ('webhook', "Webhook (Slack / Teams / HTTP)"),
            ('both',    "Email + Webhook"),
        ],
        default='email', required=True,
        help=(
            "Email sends the report as an attachment via mail.mail. "
            "Webhook POSTs a JSON payload to webhook_url with the "
            "report subject, body, and a download link per attachment. "
            "Both fires email and webhook in sequence; an email failure "
            "does not block the webhook and vice versa."
        ),
    )
    webhook_url = fields.Char(
        help=(
            "HTTPS endpoint that accepts a JSON POST. Slack incoming "
            "webhooks (https://hooks.slack.com/...) and MS Teams "
            "incoming connectors both work without further config; "
            "generic webhooks receive the canonical payload shape."
        ),
    )
    webhook_format = fields.Selection(
        [
            ('slack',   "Slack"),
            ('teams',   "Microsoft Teams"),
            ('generic', "Generic JSON"),
        ],
        default='slack',
        help=(
            "Selects the JSON shape the payload is wrapped in. Slack "
            "uses {text, attachments[]}; Teams uses MessageCard; "
            "Generic uses the raw {subject, body, attachments} dict."
        ),
    )
    webhook_timeout = fields.Integer(
        default=15,
        required=True,
        help="Network timeout in seconds for the webhook POST.",
    )

    _positive_interval = models.Constraint(
        'check(interval > 0)',
        'Schedule interval must be a positive integer.',
    )
    _bounded_webhook_timeout = models.Constraint(
        'check(webhook_timeout is not null '
        'and webhook_timeout between 1 and 60)',
        'Webhook timeout must be between one and 60 seconds.',
    )

    @api.model
    def _eh_validate_webhook_timeout_value(self, value):
        try:
            timeout = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(_(
                "Webhook timeout must be a whole number."
            )) from exc
        if not 1 <= timeout <= _MAX_WEBHOOK_TIMEOUT_SECONDS:
            raise ValidationError(_(
                "Webhook timeout must be between one and "
                "%(maximum)s seconds.",
                maximum=_MAX_WEBHOOK_TIMEOUT_SECONDS,
            ))
        return timeout

    @api.constrains('webhook_timeout')
    def _check_webhook_timeout(self):
        for schedule in self:
            schedule._eh_validate_webhook_timeout_value(
                schedule.webhook_timeout,
            )

    def _eh_validate_webhook_timeout(self):
        self.ensure_one()
        timeout = self.webhook_timeout or 0
        if not 1 <= timeout <= _MAX_WEBHOOK_TIMEOUT_SECONDS:
            raise UserError(_(
                "Schedule %(schedule)s has an unsafe webhook timeout. Set "
                "it to between one and %(maximum)s seconds before running.",
                schedule=self.display_name,
                maximum=_MAX_WEBHOOK_TIMEOUT_SECONDS,
            ))
        return True

    @api.model
    def _eh_validate_options_json_value(self, value, label="Schedule"):
        return parse_options_json(value, label, ValidationError)

    @api.constrains('options_json')
    def _check_options_json(self):
        for schedule in self:
            schedule._eh_validate_options_json_value(
                schedule.options_json,
                "Schedule '%s'" % schedule.display_name,
            )

    @api.constrains(
        'period_preset', 'period_date_from', 'period_date_to',
    )
    def _check_period_fields(self):
        for schedule in self:
            validate_period_fields(
                schedule.period_preset,
                schedule.period_date_from,
                schedule.period_date_to,
                "Schedule '%s'" % schedule.display_name,
                ValidationError,
            )

    # ---- owner integrity ----

    def _eh_guard_owner(self, vals):
        """Keep displayed owner and immutable creator on one trust boundary.

        Delegating ``user_id`` while execution remains bound to ``create_uid``
        creates two competing owners and makes authorization regressions easy.
        Non-superuser schedules therefore always belong to their creator.
        Superuser/module data remains exempt for migration purposes.
        """
        if self.env.su or 'user_id' not in vals:
            return
        if vals.get('user_id') != self.env.uid:
            raise UserError(_(
                "A scheduled report must be owned by its creator. Create the "
                "schedule while signed in as the intended owner."
            ))

    def _eh_check_owner_access(self, operation='write'):
        """Allow owner work and company-scoped manager administration."""
        if self.env.su:
            return True
        # Public methods are callable on browse(guessed_id) recordsets; make
        # the record-rule/ACL decision explicit before reading creator fields
        # or doing cron-like work.
        self._eh_check_access(operation)
        is_manager = self.env.user.has_group(
            'eh_account_base.group_eh_manager',
        )
        allowed_company_ids = set(
            self.env.context.get('allowed_company_ids')
            or self.env.user.company_ids.ids
        )
        for schedule in self:
            if (
                schedule.create_uid != self.env.user
                and not (
                    is_manager
                    and schedule.company_id.id in allowed_company_ids
                )
            ):
                raise AccessError(_(
                    "Only the creator or an Accounting Manager for the "
                    "schedule company may administer scheduled report "
                    "%(schedule)s.",
                    schedule=schedule.display_name,
                ))
        return True

    def _eh_authorized_execution_report(self):
        """Return the report in the only identity allowed to render it.

        The scheduler cron runs as the technical superuser.  Switching the
        report recordset with ``with_user`` is therefore not enough on its
        own: a missing creator used to fall back to that cron user, an
        inactive creator remains usable through the ORM, and a direct Python
        method call does not automatically re-check the report ACL.

        ``create_uid`` is the immutable authority for new schedules.  Legacy
        rows whose displayed owner differs from it are deliberately rejected
        instead of guessing which of the two users was intended.  Technical
        superuser/module-created rows are rejected too because user 1 has
        implicit bypass rights, not an explicit grant to this report.
        """
        self.ensure_one()
        owner = self.create_uid
        if not owner or not owner.exists():
            raise AccessError(_(
                "Scheduled report %(schedule)s has no creator. Recreate it "
                "while signed in as the intended owner.",
                schedule=self.display_name,
            ))
        if owner._is_superuser():
            raise AccessError(_(
                "Scheduled report %(schedule)s was created by the technical "
                "superuser and has no explicit human authorization. "
                "Recreate it as an authorized user.",
                schedule=self.display_name,
            ))
        if not owner.active:
            raise AccessError(_(
                "Schedule owner %(owner)s is inactive; scheduled report "
                "%(schedule)s cannot run.",
                owner=owner.display_name,
                schedule=self.display_name,
            ))
        if not self.user_id or self.user_id != owner:
            raise AccessError(_(
                "Scheduled report %(schedule)s has conflicting legacy "
                "owners and cannot run. Recreate it as the intended owner.",
                schedule=self.display_name,
            ))

        company = self.company_id
        if company not in owner.company_ids:
            raise AccessError(_(
                "Schedule owner %(owner)s no longer has access to company "
                "%(company)s.",
                owner=owner.display_name,
                company=company.display_name,
            ))

        report = (
            self.report_id.with_user(owner)
            .with_context(allowed_company_ids=[company.id])
            .with_company(company)
        )
        # This is intentionally an explicit access check.  Calling
        # render_xlsx/render_pdf from Python does not provide the RPC layer's
        # model ACL check, and report handlers include raw SQL/sudo paths.
        report._eh_check_access('read')
        return report

    @api.model
    @api.private
    def _eh_quarantine_unsafe_schedules(self):
        """Deactivate active legacy rows which cannot prove their authority.

        Used by the upgrade migration.  Keeping this on the model makes the
        migration use exactly the same decision as runtime execution and
        keeps custom report record rules/ACLs in force.
        """
        unsafe = self.browse()
        reasons = {}
        schedules = self.with_context(active_test=False).search([
            ('active', '=', True),
        ])
        for schedule in schedules:
            try:
                schedule._eh_authorized_execution_report()
            except AccessError as exc:
                unsafe |= schedule
                reasons[schedule.id] = str(exc)
        for schedule in unsafe:
            schedule.with_context(
                tracking_disable=True,
                mail_notrack=True,
            ).write({
                'active': False,
                'last_run_status': 'error',
                'last_error': _(
                    "Disabled during upgrade: %(reason)s",
                    reason=reasons[schedule.id],
                )[:8000],
            })
        return unsafe.ids

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'options_json' in vals:
                self._eh_validate_options_json_value(vals['options_json'])
            if 'webhook_timeout' in vals:
                self._eh_validate_webhook_timeout_value(
                    vals['webhook_timeout'],
                )
            self._eh_guard_owner(vals)
            if not self.env.su:
                vals['user_id'] = self.env.uid
        schedules = super().create(vals_list)
        if not self.env.su:
            for schedule in schedules:
                schedule._eh_authorized_execution_report()
        return schedules

    def write(self, vals):
        self._eh_check_owner_access()
        if 'options_json' in vals:
            self._eh_validate_options_json_value(vals['options_json'])
        if 'webhook_timeout' in vals:
            self._eh_validate_webhook_timeout_value(vals['webhook_timeout'])
        self._eh_guard_owner(vals)
        result = super().write(vals)
        if (
            not self.env.su
            and {'company_id', 'report_id', 'user_id'}.intersection(vals)
        ):
            for schedule in self:
                schedule._eh_authorized_execution_report()
        return result

    def unlink(self):
        self._eh_check_owner_access('unlink')
        return super().unlink()

    # ---- public actions ----

    def action_run_now(self):
        """Run the schedule immediately. Useful for testing the cadence
        without waiting for the cron tick."""
        self.ensure_one()
        self._eh_check_owner_access()
        if not self._eh_try_lock_delivery():
            raise UserError(_(
                "This schedule is already being delivered by another worker."))
        self.invalidate_recordset([
            'active', 'next_run', 'last_run', 'last_run_status',
        ])
        try:
            with self.env.cr.savepoint():
                self._send_now()
                self._advance_next_run()
        except Exception as exc:
            # Do not re-raise: an RPC exception rolls back its transaction and
            # would erase truthful attempt telemetry. The notification lets
            # the user's transaction commit the error and, when this manual
            # run was already due, its planned-cadence advancement.
            with self.env.cr.savepoint():
                self.invalidate_recordset([
                    'next_run', 'last_run', 'last_run_status', 'last_error',
                ])
                self._record_failure(exc)
                self._advance_next_run()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _("Scheduled report failed"),
                    'message': str(exc)[:8000],
                    'type': 'danger',
                    'sticky': True,
                },
            }
        return True

    def action_pause(self):
        self._eh_check_owner_access()
        for schedule in self:
            schedule.active = False
        return True

    def action_resume(self):
        self._eh_check_owner_access()
        for schedule in self:
            schedule._eh_authorized_execution_report()
            schedule.active = True
        return True

    @api.model
    def _cron_run_due(self, auto_commit=True):
        """Runner entry point for ir.cron. Dispatches every active
        schedule whose next_run is in the past.

        Each candidate runs inside its own savepoint.  Successful delivery
        state and failed-attempt advancement are committed before moving to
        the next candidate: a later PostgreSQL/render failure therefore
        cannot roll back an email/webhook already sent and make it due again.

        ``auto_commit=False`` exists for callers which already own transaction
        boundaries (notably tests).  The installed cron uses the default.
        """
        now = fields.Datetime.now()
        due = self.search([
            ('active', '=', True),
            ('next_run', '<=', now),
        ])
        for schedule in due:
            try:
                with self.env.cr.savepoint():
                    if not schedule._eh_try_lock_delivery():
                        continue
                    schedule.invalidate_recordset([
                        'active', 'next_run', 'last_run', 'last_run_status',
                    ])
                    # Another worker may have completed between due-search
                    # and lock acquisition.
                    if (
                        not schedule.active
                        or not schedule.next_run
                        or schedule.next_run > now
                    ):
                        continue
                    schedule._send_now()
                    schedule._advance_next_run(reference=now)
            except Exception as exc:
                _logger.warning(
                    "eh.report.schedule %s failed: %s",
                    schedule.id, exc,
                )
                try:
                    # The failed savepoint restored a usable cursor. Record
                    # and advance in a fresh savepoint so a delivery which
                    # reached an external service is never retried merely
                    # because its final local write raised a database error.
                    with self.env.cr.savepoint():
                        schedule.invalidate_recordset([
                            'active', 'next_run', 'last_run',
                            'last_run_status', 'last_error',
                        ])
                        schedule._record_failure(exc)
                        schedule._advance_next_run(reference=now)
                except Exception as state_exc:
                    _logger.error(
                        "Could not persist failure state for "
                        "eh.report.schedule %s: %s",
                        schedule.id, state_exc,
                    )
            finally:
                if auto_commit:
                    # ir.cron is a transaction boundary.  Per-candidate
                    # commits preserve completed external delivery state and
                    # release the row lock before processing the next item.
                    self.env.cr.commit()
        return True

    # ---- internals ----

    def _eh_try_lock_delivery(self):
        """Claim one schedule row without waiting on another dispatcher."""
        self.ensure_one()
        self.env.cr.execute(
            "SELECT id FROM eh_report_schedule "
            "WHERE id = %s FOR UPDATE SKIP LOCKED",
            [self.id],
        )
        return bool(self.env.cr.fetchone())

    def _send_now(self):
        """Dispatch a single delivery via the configured channel(s).

        Delivery channels run independently: an email failure does not
        block the webhook and vice versa. The schedule is recorded as
        successful when at least one channel succeeds; a total failure
        bubbles up to the cron handler which sets last_run_status=error.
        """
        self.ensure_one()
        self._eh_validate_webhook_timeout()
        options = self._parse_options()
        attachments = self._build_attachments(options)
        if not attachments:
            raise UserError(_("Could not build any attachment for delivery."))

        body_html = self.body or self._default_body_html(options)
        successes = 0
        errors = []

        if self.delivery_channel in ('email', 'both'):
            try:
                self._dispatch_email(attachments, body_html)
                successes += 1
            except Exception as exc:  # noqa: BLE001
                errors.append("email: %s" % exc)

        if self.delivery_channel in ('webhook', 'both'):
            try:
                self._dispatch_webhook(attachments, body_html)
                successes += 1
            except Exception as exc:  # noqa: BLE001
                errors.append("webhook: %s" % exc)

        if not successes:
            raise UserError(_(
                "All delivery channels failed for schedule '%(name)s': "
                "%(errors)s",
                name=self.name,
                errors='; '.join(errors),
            ))

        self.write({
            'last_run': fields.Datetime.now(),
            'last_run_status': 'success',
            'last_error': '; '.join(errors)[:8000] if errors else False,
            'last_attachment_count': len(attachments),
        })
        return True

    def _dispatch_email(self, attachments, body_html):
        """Original email path. Refused when no recipients are set."""
        self.ensure_one()
        emails = self._resolve_recipient_emails()
        if not emails:
            raise UserError(_(
                "Schedule '%s' has no recipient emails configured.",
            ) % self.name)
        mail_vals = {
            'subject': self.subject,
            'body_html': body_html,
            'email_to': ','.join(emails),
            'email_from': (
                self.company_id.email
                or self.user_id.email
                or self.env.company.email
                or self.env.user.email
                or False
            ),
            # Delivery attachments live inside the outgoing MIME message.
            # Keeping the transient mail/attachment rows after a successful
            # synchronous send only creates unbounded filestore growth.
            'auto_delete': True,
            'attachment_ids': [(0, 0, att) for att in attachments],
        }
        # Roll back transient mail + binary attachment together on failure.
        # Otherwise a caught SMTP exception in a successful webhook+email run
        # leaves an outgoing mail which global mail cron may send much later.
        with self.env.cr.savepoint():
            mail = self.env['mail.mail'].sudo().create(mail_vals)
            # Odoo 16-19 mail.mail.send() returns None on success. Delivery
            # failure is an exception when raise_exception=True.
            mail.send(raise_exception=True)

    def _eh_get_public_base_url(self):
        """Return the configured public HTTPS origin for bearer links.

        Webhook attachment URLs contain access tokens.  Refuse to create the
        attachment or token unless ``web.base.url`` is an absolute HTTPS URL;
        otherwise a relative URL is unusable and plain HTTP exposes the token
        in transit.
        """
        self.ensure_one()
        base_url = (
            self.env['ir.config_parameter'].sudo().get_param(
                'web.base.url', default='',
            )
            or ''
        ).strip()
        try:
            parsed = urlsplit(base_url)
            host = parsed.hostname
            username = parsed.username
            password = parsed.password
            port = parsed.port
        except (TypeError, UnicodeError, ValueError) as exc:
            raise UserError(_(
                "Scheduled webhook downloads require web.base.url to be "
                "an absolute HTTPS URL.",
            )) from exc
        if (
            parsed.scheme.lower() != 'https'
            or not parsed.netloc
            or not host
            or username is not None
            or password is not None
            or parsed.query
            or parsed.fragment
            or (port is not None and port < 1)
        ):
            raise UserError(_(
                "Scheduled webhook downloads require web.base.url to be "
                "an absolute HTTPS URL without credentials, query, or "
                "fragment.",
            ))
        return base_url.rstrip('/')

    def _dispatch_webhook(self, attachments, body_html):
        """POST a JSON payload to the configured webhook URL.

        Attachments are linked-by-URL rather than body-encoded so the
        webhook payload stays under the platform-specific size limits
        (Slack rejects payloads above 30 kB; Teams adaptive cards
        cap at 28 kB). Each attachment record is stored as ir.attachment
        with a random bearer token, so the external receiver can download it
        without an Odoo session until 30-day autovacuum removes file+token.
        These URLs are confidential and are sent only to validated HTTPS
        destinations.

        The destination is resolved exactly once, every answer is checked as
        a numeric public address, and the chosen socket address is pinned for
        the TLS connection.  Redirects and proxy tunnels are deliberately not
        supported: either would move DNS/target selection outside this trust
        boundary and re-open SSRF after validation.
        """
        self.ensure_one()
        self._eh_validate_webhook_timeout()
        if not self.webhook_url:
            raise UserError(_(
                "Schedule '%s' has webhook delivery enabled but no "
                "webhook_url configured.",
            ) % self.name)

        # These links carry bearer credentials.  Validate the public URL
        # before creating a delivery attachment or generating any token.
        base_url = self._eh_get_public_base_url()

        # Resolve and validate before creating attachments.  In particular,
        # do not validate a hostname and then let the HTTP client resolve it a
        # second time: that check/use gap permits DNS rebinding to an internal
        # or metadata address.
        try:
            parsed, endpoint = self._eh_resolve_webhook_endpoint()
        except UnsafeUrlError as exc:
            raise UserError(_(
                "Webhook URL for schedule '%(name)s' is not allowed: "
                "%(reason)s",
                name=self.name, reason=str(exc),
            )) from exc

        # The external POST and its local tokenized blobs are one delivery
        # unit.  If the POST fails, rolling back this savepoint removes both
        # attachment and token while leaving another successful channel (for
        # example email) intact for truthful partial-success telemetry.
        with self.env.cr.savepoint():
            delivery_attachments = [
                dict(
                    values,
                    res_model='eh.report.schedule',
                    res_id=self.id,
                    eh_report_schedule_delivery=True,
                )
                for values in attachments
            ]
            attachment_ids = self.env['ir.attachment'].sudo().create(
                delivery_attachments,
            )
            attachment_tokens = attachment_ids.generate_access_token()
            attachment_links = [
                {
                    'name': att.name,
                    # Incoming-webhook recipients have no Odoo browser
                    # session. Use standard bearer attachment tokens;
                    # autovacuum deletes both file and token after the
                    # documented 30-day window.
                    'url': (
                        "%s/web/content/%d?download=true&access_token=%s"
                        % (base_url, att.id, quote(token, safe=''))
                    ),
                    'mimetype': att.mimetype,
                    'size_bytes': att.file_size or 0,
                }
                for att, token in zip(attachment_ids, attachment_tokens)
            ]

            payload = self._build_webhook_payload(
                subject=self.subject,
                body_text=self._strip_html(body_html),
                attachment_links=attachment_links,
            )
            encoded = json.dumps(payload).encode('utf-8')
            timeout = self.webhook_timeout
            self._eh_post_pinned_webhook(parsed, endpoint, encoded, timeout)

    def _eh_resolve_webhook_endpoint(self):
        """Return ``(parsed_url, endpoint)`` after one fail-closed lookup.

        Every address in the DNS answer is checked, not only the address we
        select.  A hostname with a mixed public/private answer is therefore
        rejected outright instead of relying on resolver ordering.  The
        returned endpoint is the exact ``getaddrinfo`` tuple later handed to
        :class:`_PinnedHTTPSConnection`; no second resolver call occurs.
        """
        self.ensure_one()
        url = (self.webhook_url or '').strip()
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            username = parsed.username
            password = parsed.password
            parsed_port = parsed.port
        except (TypeError, UnicodeError, ValueError) as exc:
            raise UnsafeUrlError(
                "Webhook URL is malformed: %s" % exc
            ) from exc
        if parsed.scheme.lower() != 'https':
            raise UnsafeUrlError(
                "Only HTTPS webhook URLs are allowed."
            )
        if username is not None or password is not None:
            raise UnsafeUrlError(
                "Webhook URLs must not contain embedded credentials."
            )
        if not host:
            raise UnsafeUrlError("Webhook URL has no host.")
        normalized_host = host.lower().rstrip('.')
        if normalized_host in _BLOCKED_WEBHOOK_HOSTS:
            raise UnsafeUrlError(
                "Webhook host %r targets a cloud metadata service." % host
            )
        port = 443 if parsed_port is None else parsed_port
        if port < 1:
            raise UnsafeUrlError("Webhook URL has an invalid port.")

        try:
            infos = socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except (socket.gaierror, UnicodeError) as exc:
            raise UnsafeUrlError(
                "Webhook host %r could not be resolved: %s" % (host, exc)
            ) from exc

        endpoints = []
        seen = set()
        for family, socktype, proto, _canonname, sockaddr in infos:
            if family not in (socket.AF_INET, socket.AF_INET6):
                raise UnsafeUrlError(
                    "Webhook host %r resolved to an unsupported address "
                    "family." % host
                )
            if not sockaddr:
                raise UnsafeUrlError(
                    "Webhook host %r resolved without a socket address."
                    % host
                )
            address = sockaddr[0]
            try:
                # A zone identifier is meaningful only relative to this host
                # and must not be accepted as an Internet webhook endpoint.
                if '%' in address:
                    raise ValueError("scoped IPv6 address")
                ip = ipaddress.ip_address(address)
            except (TypeError, ValueError) as exc:
                raise UnsafeUrlError(
                    "Webhook host %r resolved to an invalid address %r."
                    % (host, address)
                ) from exc

            # Reuse the shared policy on a numeric literal.  This cannot cause
            # another DNS lookup and covers loopback, RFC1918/ULA, link-local,
            # multicast, reserved, unspecified and IPv4-mapped IPv6 values.
            literal = (
                "https://[%s]/" % ip.compressed
                if ip.version == 6
                else "https://%s/" % ip.compressed
            )
            assert_safe_url(literal)

            endpoint = (family, socktype, proto, sockaddr)
            endpoint_key = (family, socktype, proto, tuple(sockaddr))
            if endpoint_key not in seen:
                seen.add(endpoint_key)
                endpoints.append(endpoint)

        if not endpoints:
            raise UnsafeUrlError(
                "Webhook host %r resolved to no usable addresses." % host
            )

        # Prefer IPv4 when both are present to avoid a predictable deployment
        # failure on hosts whose resolver advertises IPv6 without v6 egress.
        # We intentionally do not retry another address after a POST: retries
        # could duplicate a side effect at the remote webhook.
        endpoint = next(
            (item for item in endpoints if item[0] == socket.AF_INET),
            endpoints[0],
        )
        return parsed, endpoint

    def _eh_post_pinned_webhook(self, parsed, endpoint, encoded, timeout):
        """POST once to a validated address without redirect or DNS retry."""
        self.ensure_one()
        host = parsed.hostname
        parsed_port = parsed.port
        port = 443 if parsed_port is None else parsed_port
        target = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
        connection = _PinnedHTTPSConnection(
            host=host,
            port=port,
            endpoint=endpoint,
            timeout=timeout,
        )
        try:
            connection.request(
                'POST',
                target,
                body=encoded,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'eh-account-dynamic-reports/1.0',
                },
            )
            response = connection.getresponse()
            status = int(response.status or 0)
            body = response.read(2048).decode('utf-8', errors='replace')
            if 300 <= status < 400:
                location = response.getheader('Location') or ''
                raise UserError(_(
                    "Webhook returned HTTP %(status)s redirect to "
                    "%(location)s. Redirects are disabled.",
                    status=status,
                    location=location or "an unspecified destination",
                ))
            if status < 200 or status >= 300:
                raise UserError(_(
                    "Webhook returned HTTP %(status)s: %(body)s",
                    status=status,
                    body=body or response.reason or "empty response",
                ))
        except UserError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise UserError(_(
                "Webhook POST failed: %s",
            ) % exc) from exc
        finally:
            connection.close()

    def _build_webhook_payload(self, subject, body_text, attachment_links):
        """Return the JSON dict for the chosen webhook_format.

        Slack expects {text, attachments[]} where each attachment has
        title/title_link. Teams expects an Adaptive-Card-style
        MessageCard with sections. Generic just emits the canonical
        triple {subject, body, attachments}.
        """
        self.ensure_one()
        if self.webhook_format == 'slack':
            return {
                'text': subject,
                'attachments': [
                    {
                        'color': '#1A2C3D',
                        'title': link['name'],
                        'title_link': link['url'],
                        'text': body_text or '',
                        'footer': 'ERP Heritage Accounting Suite',
                    }
                    for link in attachment_links
                ] or [{
                    'color': '#1A2C3D',
                    'text': body_text or '',
                    'footer': 'ERP Heritage Accounting Suite',
                }],
            }
        if self.webhook_format == 'teams':
            return {
                '@type': 'MessageCard',
                '@context': 'https://schema.org/extensions',
                'summary': subject,
                'themeColor': '1A2C3D',
                'title': subject,
                'sections': [
                    {
                        'text': body_text or '',
                    },
                    {
                        'title': 'Attachments',
                        'facts': [
                            {'name': link['name'], 'value': link['url']}
                            for link in attachment_links
                        ],
                    },
                ] if attachment_links else [
                    {'text': body_text or ''},
                ],
            }
        # Generic: caller controls the consumer; emit the canonical
        # {subject, body, attachments} triple.
        return {
            'subject': subject,
            'body': body_text or '',
            'attachments': attachment_links,
            'company': self.company_id.display_name,
            'schedule': self.name,
        }

    @staticmethod
    def _strip_html(html):
        """Crude HTML-to-text stripper for webhook payloads.

        Webhook gateways render text, not HTML. We strip tags and
        collapse whitespace; preserving every nuance of the email
        body is not the goal; webhook delivery is the high-level
        notification, the attachment is the source of truth.
        """
        if not html:
            return ''
        import re
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:1500]

    def _record_failure(self, exc):
        self.ensure_one()
        self.write({
            'last_run': fields.Datetime.now(),
            'last_run_status': 'error',
            'last_error': str(exc)[:8000],
            'last_attachment_count': 0,
        })

    def _advance_next_run(self, reference=None):
        """Move an overdue cadence to its first future planned occurrence.

        Base calculation on ``next_run``, never delivery completion time. This
        keeps scheduled wall-clock time stable when hourly cron runs late and
        keeps manual early runs from resetting the planned cadence. A monthly
        cadence which starts on month-end remains on month-end (Jan 31 ->
        Feb 28/29 -> Mar 31) instead of drifting to the 28th.
        """
        self.ensure_one()
        reference = fields.Datetime.to_datetime(
            reference or fields.Datetime.now()
        )
        planned = self.next_run or reference
        if planned > reference:
            return planned

        delta = self._compute_delta()
        preserve_month_end = (
            self.interval_unit == 'month'
            and (planned + timedelta(days=1)).month != planned.month
        )
        while planned <= reference:
            planned += delta
            if preserve_month_end:
                planned += relativedelta(day=31)
        self.next_run = planned
        return planned

    def _compute_delta(self):
        if self.interval_unit == 'day':
            return relativedelta(days=self.interval)
        if self.interval_unit == 'week':
            return relativedelta(weeks=self.interval)
        return relativedelta(months=self.interval)

    def _parse_options(self):
        self.ensure_one()
        options = parse_options_json(
            self.options_json,
            "Schedule '%s'" % self.display_name,
            UserError,
        )
        validate_period_fields(
            self.period_preset,
            self.period_date_from,
            self.period_date_to,
            "Schedule '%s'" % self.display_name,
            UserError,
        )
        options = apply_period_preset(
            options,
            self.period_preset,
            self.period_date_from,
            self.period_date_to,
        )
        date_options = options.get('date')
        if isinstance(date_options, dict):
            owner = self.create_uid
            schedule_as_owner = self.with_user(owner).with_context(
                tz=owner.tz or 'UTC',
            )
            today = fields.Date.context_today(schedule_as_owner)
            previous_month_end = today.replace(day=1) - relativedelta(days=1)
            fiscal_year = self.company_id.compute_fiscalyear_dates(today)
            token_dates = {
                'today': today,
                'auto_month_start': today.replace(day=1),
                'auto_month_end': (
                    today.replace(day=1)
                    + relativedelta(months=1, days=-1)
                ),
                'auto_qtd': today.replace(
                    month=((today.month - 1) // 3) * 3 + 1,
                    day=1,
                ),
                'auto_ytd': fiscal_year['date_from'],
                'auto_prev_month_start': previous_month_end.replace(day=1),
                'auto_prev_month_end': previous_month_end,
            }
            resolved_date = dict(date_options)
            for key in ('date_from', 'date_to'):
                value = resolved_date.get(key)
                if value in token_dates:
                    resolved_date[key] = fields.Date.to_string(
                        token_dates[value],
                    )
                elif isinstance(value, str) and value.startswith('auto_'):
                    raise UserError(_(
                        "Schedule '%(name)s' uses unsupported date token "
                        "'%(token)s'.",
                        name=self.display_name,
                        token=value,
                    ))
            options['date'] = resolved_date
        return options

    def _resolve_recipient_emails(self):
        """Combine user, partner, and free text email lists. Returns a
        deduplicated, comma free list of email addresses."""
        emails = set()
        for user in self.recipient_user_ids:
            if user.email:
                emails.add(user.email.strip())
        for partner in self.recipient_partner_ids:
            if partner.email:
                emails.add(partner.email.strip())
        if self.recipient_emails:
            for raw in self.recipient_emails.split(','):
                stripped = raw.strip()
                if stripped:
                    emails.add(stripped)
        return sorted(e for e in emails if e and '@' in e)

    def _build_attachments(self, options):
        self.ensure_one()
        attachments = []
        # SECURITY: the cron runs as root, which bypasses record rules. Render
        # every attachment as the schedule's IMMUTABLE creator (create_uid),
        # never the display-only user_id, so the report engine's
        # company-scope clamp (_eh_clamp_company_ids) applies to the person who
        # actually owns this schedule. Binding with_user() to a mutable field
        # would let any writer re-point the owner at a higher-privilege user
        # (base.group_system) or a better-scoped colleague and have the root
        # cron render another company/user's financials under those rights and
        # email them out; create_uid is stamped once by the ORM and cannot be
        # reassigned over RPC or the form.
        report = self._eh_authorized_execution_report()
        company = self.company_id
        # ``with_company`` alone does not narrow a multi-company user's
        # allowed companies. Force both the report environment and the
        # serialized options to this schedule's single immutable company.
        # Report handlers include raw SQL/sudo paths and therefore must never
        # receive caller-controlled company_ids here.
        scoped_options = dict(options or {})
        scoped_options['company_ids'] = [company.id]
        today_str = fields.Date.context_today(self).isoformat()

        if self.delivery_format in ('xlsx', 'both'):
            xlsx_bytes = report.render_xlsx(scoped_options)
            attachments.append({
                'name': "%s_%s.xlsx" % (report.code, today_str),
                'datas': base64.b64encode(xlsx_bytes),
                'mimetype': (
                    'application/vnd.openxmlformats-officedocument'
                    '.spreadsheetml.sheet'
                ),
            })
        if self.delivery_format in ('pdf', 'both'):
            pdf_bytes = report.render_pdf(scoped_options)
            attachments.append({
                'name': "%s_%s.pdf" % (report.code, today_str),
                'datas': base64.b64encode(pdf_bytes),
                'mimetype': 'application/pdf',
            })
        return attachments

    def _default_body_html(self, options):
        report = self.report_id
        date_block = options.get('date') or {}
        period = ''
        if date_block.get('date_from') and date_block.get('date_to'):
            period = " for the period %s to %s" % (
                date_block['date_from'], date_block['date_to'],
            )
        return (
            "<p>Hello,</p>"
            "<p>Please find attached the latest <strong>%(name)s</strong>%(period)s.</p>"
            "<p>This is an automated delivery from your scheduled report.</p>"
            "<p>Generated by ERP Heritage Accounting.</p>"
        ) % {
            'name': report.name or '',
            'period': period,
        }


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    eh_report_schedule_delivery = fields.Boolean(
        default=False,
        index=True,
        readonly=True,
        copy=False,
        help=(
            "Technical marker for report files retained temporarily so a "
            "webhook recipient can download them through authenticated "
            "Odoo access."
        ),
    )

    @api.autovacuum
    def _gc_eh_report_schedule_deliveries(self):
        """Delete expired webhook report blobs; preserve schedule audit."""
        cutoff = fields.Datetime.now() - timedelta(days=30)
        expired = self.sudo().search([
            ('eh_report_schedule_delivery', '=', True),
            ('create_date', '<', cutoff),
        ], order='id', limit=5000)
        if expired:
            expired.unlink()
        return True
