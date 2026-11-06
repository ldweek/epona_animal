# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.bank.live.connector.profile -- Odoo-side persistence for one live
bank connector configuration.

A profile binds:

* a connector key (which connector implementation to invoke)
* a journal (where the imported lines land)
* a credentials_json blob (whatever the connector needs to authenticate)
* a since_date pointer (the framework only fetches forward from here,
  preventing duplicate imports across cron runs)

The framework does not expose credentials_json on any UI surface that
escapes back to the server; only the dedicated form view shows it, and
even there it is rendered behind a password widget. For real-world
deployments deal with secrets through the Odoo passwords manager or a
dedicated vault; this field exists so a small deployment can run
without that infrastructure.
"""

import logging
from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.eh_account_bank_statement_import.connectors.registry import (
    connector_choices, get_connector,
)
from odoo.addons.eh_account_bank_statement_import.connectors.base import (
    ConnectorError,
)
from odoo.addons.eh_account_bank_statement_import.connectors.http_util import (
    redact_secrets,
)

_logger = logging.getLogger(__name__)

_BANK_IMPORT_LOCK_NAMESPACE = 1162363465


class EhBankLiveConnectorProfile(models.Model):
    _name = 'eh.bank.live.connector.profile'
    _description = "Live bank connector profile"
    _order = 'name'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.cron.batch.mixin']

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    journal_id = fields.Many2one(
        'account.journal',
        required=True,
        check_company=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        ondelete='restrict',
        tracking=True,
    )
    connector_key = fields.Selection(
        selection='_selection_connector_keys',
        required=True,
        tracking=True,
    )
    credentials_json = fields.Text(
        groups='eh_account_base.group_eh_manager',
        help=(
            "Connector-specific credentials as JSON. The framework hands "
            "this verbatim to the connector's authenticate() method. "
            "Treated as a secret on every UI surface."
        ),
    )
    advanced_credentials_json = fields.Char(
        string="Advanced credentials JSON",
        compute='_compute_advanced_credentials_json',
        inverse='_inverse_advanced_credentials_json',
        store=False,
        groups='eh_account_base.group_eh_manager',
        help=(
            "Masked developer-facing editor for the raw connector JSON. "
            "The stored credentials_json field remains the source of truth."
        ),
    )

    @api.depends('credentials_json')
    def _compute_advanced_credentials_json(self):
        for profile in self:
            profile.advanced_credentials_json = (
                profile.credentials_json or False
            )

    def _inverse_advanced_credentials_json(self):
        for profile in self:
            profile.credentials_json = (
                profile.advanced_credentials_json or False
            )

    # ---- friendly credential entry ----
    # These labelled fields are the human-friendly face of credentials_json:
    # the operator fills in the named boxes for their connector and the
    # framework assembles the JSON. credentials_json stays the single source
    # of truth (and remains available to power users under Advanced).
    cred_client_id = fields.Char(
        "Client ID", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False,
        groups='eh_account_base.group_eh_manager')
    cred_secret = fields.Char(
        "Secret", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False,
        groups='eh_account_base.group_eh_manager')
    cred_access_token = fields.Char(
        "Access Token", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False,
        groups='eh_account_base.group_eh_manager')
    cred_account_id = fields.Char(
        "Account ID", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False,
        groups='eh_account_base.group_eh_manager')
    cred_environment = fields.Selection(
        [('sandbox', "Sandbox"), ('development', "Development"),
         ('production', "Production")],
        string="Environment", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False,
        groups='eh_account_base.group_eh_manager')
    cred_api_key = fields.Char(
        "API Key", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False,
        groups='eh_account_base.group_eh_manager')
    cred_user_id = fields.Char(
        "User ID", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False,
        groups='eh_account_base.group_eh_manager')
    cred_secret_id = fields.Char(
        "Secret ID", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False,
        groups='eh_account_base.group_eh_manager')
    cred_secret_key = fields.Char(
        "Secret Key", compute='_compute_credential_fields',
        inverse='_inverse_credential_fields', store=False,
        groups='eh_account_base.group_eh_manager')

    _CRED_FIELD_MAP = {
        'cred_client_id': 'client_id',
        'cred_secret': 'secret',
        'cred_access_token': 'access_token',
        'cred_account_id': 'account_id',
        'cred_environment': 'environment',
        'cred_api_key': 'api_key',
        'cred_user_id': 'user_id',
        'cred_secret_id': 'secret_id',
        'cred_secret_key': 'secret_key',
    }

    @api.depends('credentials_json')
    def _compute_credential_fields(self):
        import json
        for rec in self:
            data = {}
            if rec.credentials_json:
                try:
                    data = json.loads(rec.credentials_json)
                except ValueError:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            for field, key in rec._CRED_FIELD_MAP.items():
                value = data.get(key) or False
                if field == 'cred_environment' and value not in (
                    False, 'sandbox', 'development', 'production',
                ):
                    value = False
                rec[field] = value

    def _inverse_credential_fields(self):
        import json
        for rec in self:
            data = {}
            # Keep any keys already in the JSON that we don't surface as
            # fields, so a hand-edited blob is never silently truncated.
            if rec.credentials_json:
                try:
                    data = json.loads(rec.credentials_json)
                except ValueError:
                    data = {}
            if not isinstance(data, dict):
                data = {}
            for field, key in rec._CRED_FIELD_MAP.items():
                value = rec[field]
                if value:
                    data[key] = value
                elif key in data:
                    data.pop(key)
            rec.credentials_json = json.dumps(data) if data else False
    since_date = fields.Date(
        help=(
            "Earliest date the framework will request from the bank on "
            "the next run. Auto-advances to the latest fetched "
            "posted_date after each successful run."
        ),
    )
    last_run_at = fields.Datetime(readonly=True, tracking=True)
    last_run_state = fields.Selection(
        [('idle', "Idle"), ('ok', "Success"), ('error', "Error")],
        default='idle', readonly=True, tracking=True,
    )
    last_run_message = fields.Text(readonly=True)
    transactions_imported_count = fields.Integer(
        readonly=True,
        help="Cumulative count across all runs of this profile.",
    )

    def init(self):
        """Race-safe uniqueness for active bindings only."""
        self.env.cr.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "eh_bank_live_connector_profile_active_journal_uniq "
            "ON eh_bank_live_connector_profile (journal_id) WHERE active"
        )

    @api.constrains('active', 'journal_id')
    def _check_active_journal_unique(self):
        for rec in self.filtered('active'):
            if self.search_count([
                ('id', '!=', rec.id),
                ('active', '=', True),
                ('journal_id', '=', rec.journal_id.id),
            ], limit=1):
                raise ValidationError(_(
                    "A journal can only be bound to one active live "
                    "connector profile. Archive the current profile first."
                ))

    @staticmethod
    def _is_active_journal_conflict(exc):
        return getattr(getattr(exc, 'diag', None), 'constraint_name', '') == (
            'eh_bank_live_connector_profile_active_journal_uniq'
        )

    @api.model_create_multi
    def create(self, vals_list):
        try:
            with self.env.cr.savepoint():
                return super().create(vals_list)
        except IntegrityError as exc:
            if not self._is_active_journal_conflict(exc):
                raise
            raise ValidationError(_(
                "A journal can only be bound to one active live connector "
                "profile. Archive the current profile first."
            )) from exc

    def write(self, vals):
        try:
            with self.env.cr.savepoint():
                return super().write(vals)
        except IntegrityError as exc:
            if not self._is_active_journal_conflict(exc):
                raise
            raise ValidationError(_(
                "A journal can only be bound to one active live connector "
                "profile. Archive the current profile first."
            )) from exc

    @api.model
    def _selection_connector_keys(self):
        choices = connector_choices()
        if choices:
            return choices
        # No connectors registered: still allow the field to exist so
        # the form view does not crash. The user gets an explicit error
        # at run time naming the missing addon.
        return [('__none__', "(no connectors registered)")]

    @api.constrains('credentials_json')
    def _check_credentials_json(self):
        import json
        for rec in self:
            if not rec.credentials_json:
                continue
            try:
                data = json.loads(rec.credentials_json)
            except ValueError as exc:
                raise ValidationError(_(
                    "credentials_json must be valid JSON: %s",
                ) % exc)
            if not isinstance(data, dict):
                raise ValidationError(_(
                    "credentials_json must contain a JSON object."))

    @api.constrains('company_id', 'journal_id')
    def _check_profile_journal_scope(self):
        for rec in self:
            if (rec.journal_id.company_id != rec.company_id
                    or rec.journal_id.type not in ('bank', 'cash')):
                raise ValidationError(_(
                    "The connector profile and its bank/cash journal must "
                    "belong to the same company."))

    def _runtime_scope_error(self):
        """Return a generic structural error without touching a connector."""
        self.ensure_one()
        if (not self.company_id or not self.journal_id
                or self.journal_id.company_id != self.company_id
                or self.journal_id.type not in ('bank', 'cash')):
            return _(
                "This legacy connector profile has an invalid company or "
                "journal binding and was disabled for review.")
        import json
        try:
            credentials = (json.loads(self.credentials_json)
                           if self.credentials_json else {})
        except (TypeError, ValueError):
            credentials = None
        if not isinstance(credentials, dict):
            return _(
                "This legacy connector profile has malformed credentials "
                "and was disabled for review.")
        return False

    def _check_caller_scope(self):
        """Authorize the profile's targets before authentication/network I/O."""
        self.ensure_one()
        if self.env.su:
            return
        if self.company_id not in self.env.companies:
            raise AccessError(_(
                "You cannot run a connector profile outside your active "
                "companies."))
        self._eh_check_access('write')
        self.company_id._eh_check_access('read')
        self.journal_id._eh_check_access('read')

    def _quarantine_legacy_profile(self, message):
        """Disable malformed legacy rows without exposing credentials."""
        self.ensure_one()
        self.sudo().write({
            'active': False,
            'last_run_at': fields.Datetime.now(),
            'last_run_state': 'error',
            'last_run_message': redact_secrets(message)[:2000],
        })

    def action_fetch_now(self):
        """Run the connector once, immediately, with chatter feedback."""
        for rec in self:
            rec._check_caller_scope()
            rec._run_fetch()
        return True

    @api.model
    @api.private
    def cron_fetch_due_profiles(self):
        """Run every active profile in turn, with one savepoint per
        profile so a single bank failure does not freeze the queue.
        """
        profiles = self.search([
            ('active', '=', True),
            ('connector_key', '!=', '__none__'),
        ])
        # Per-profile savepoint via the shared batch mixin so one bad
        # bank does not freeze the queue: its failure rolls back only
        # that profile and the run continues. Previously the bare
        # savepoint still let an unexpected error propagate and abort
        # the whole cron.
        self._eh_for_each_savepoint(
            profiles,
            lambda profile: profile._run_fetch(),
            log_label="Live connector fetch",
        )
        return True

    def _run_fetch(self):
        self.ensure_one()
        structural_error = self._runtime_scope_error()
        if structural_error:
            self._quarantine_legacy_profile(structural_error)
            return
        self._check_caller_scope()
        self.invalidate_recordset([
            'since_date', 'transactions_imported_count', 'credentials_json',
        ])
        import json
        try:
            connector = get_connector(self.connector_key)
        except KeyError as exc:
            self._record_error(str(exc))
            return
        creds = {}
        if self.credentials_json:
            try:
                creds = json.loads(self.credentials_json)
            except ValueError as exc:
                self._record_error("credentials_json invalid: %s" % exc)
                return

        # Build a passthrough struct so connectors do not import the ORM.
        profile_view = type('LiveProfileView', (), {
            'connector_key': self.connector_key,
            'company_id': self.company_id.id,
            'journal_id': self.journal_id.id,
            'currency_code': self.journal_id.currency_id.name
                or self.company_id.currency_id.name,
            'credentials': creds,
        })()

        today = fields.Date.context_today(self)
        since = self.since_date or (today - timedelta(days=30))
        try:
            session = connector.authenticate(profile_view)
            transactions = list(connector.fetch_transactions(
                profile_view, since, today, session,
            ))
            connector.disconnect(profile_view, session)
            # Serialize only database materialisation. Network I/O can take
            # seconds and must not block manual imports for this journal.
            self.env.cr.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [_BANK_IMPORT_LOCK_NAMESPACE, self.journal_id.id],
            )
            count, latest_date = self._materialise(transactions)
        except UserError as exc:
            # Core accounting validation is actionable configuration/data
            # evidence, not an opaque connector crash. Stop cron retries until
            # a manager reviews and reactivates the profile.
            self._record_error(str(exc))
            self.write({'active': False})
            return
        except ConnectorError as exc:
            self._record_error(str(exc))
            return
        except Exception as exc:  # pragma: no cover -- unexpected
            # A third-party exception can embed request/session credentials.
            # Record only its class; expected ConnectorError messages pass
            # through the dedicated secret redactor above.
            _logger.error(
                "Live connector %s failed with %s",
                self.id, type(exc).__name__,
            )
            self._record_error(
                "Unexpected connector error (%s)." % type(exc).__name__
            )
            return

        self.write({
            'last_run_at': fields.Datetime.now(),
            'last_run_state': 'ok',
            'last_run_message': _(
                "Imported %(count)s transactions; latest %(latest)s.",
                count=count, latest=latest_date or 'n/a',
            ),
            'transactions_imported_count': (
                self.transactions_imported_count + count
            ),
            'since_date': latest_date or since,
        })
        self.message_post(body=self.last_run_message)

    def _materialise(self, transactions):
        """Convert connector BankTransaction records into bank statement
        lines on the bound journal. De-duplicates by provider_reference;
        a transaction whose reference has been seen before is skipped.

        Returns (imported_count, latest_posted_date).
        """
        if not transactions:
            return 0, False

        # Validate everything up front so we do not partially insert
        # then bail. Each invalid line surfaces as a ConnectorError so
        # the caller records the problem and stops the run.
        for tx in transactions:
            tx.validate()

        journal_currency = (
            self.journal_id.currency_id or self.company_id.currency_id
        )
        expected_code = (journal_currency.name or '').upper()
        for tx in transactions:
            if tx.currency.upper() != expected_code:
                raise ConnectorError(
                    "transaction %s is in %s but journal %s is in %s"
                    % (
                        tx.provider_reference,
                        tx.currency.upper(),
                        self.journal_id.display_name,
                        expected_code,
                    )
                )

        StatementLine = self.env['account.bank.statement.line']
        # search_read returns a list of dicts; build the set from the
        # values, never wrap the dict list in set() (TypeError:
        # unhashable type: 'dict').
        provider_refs = sorted({
            str(tx.provider_reference or '').strip()
            for tx in transactions if tx.provider_reference
        })
        canonical_by_provider = {
            provider_ref: StatementLine._eh_canonical_import_ref(
                self.journal_id.id, provider_ref,
            )
            for provider_ref in provider_refs
        }
        existing_rows = StatementLine.search_read([
            ('journal_id', '=', self.journal_id.id),
            '|',
            ('eh_provider_reference', 'in', provider_refs),
            ('unique_import_ref', 'in', list(
                canonical_by_provider.values(),
            )),
        ], ['eh_provider_reference', 'unique_import_ref'])
        seen = {
            row['eh_provider_reference']
            for row in existing_rows if row['eh_provider_reference']
        }
        seen.update(
            provider_ref
            for provider_ref, canonical_ref in canonical_by_provider.items()
            if any(
                row['unique_import_ref'] == canonical_ref
                for row in existing_rows
            )
        )

        latest = max(tx.posted_date for tx in transactions)
        new_vals = []
        for tx in transactions:
            provider_reference = str(tx.provider_reference or '').strip()
            if provider_reference in seen:
                continue
            seen.add(provider_reference)
            new_vals.append({
                'journal_id': self.journal_id.id,
                'date': tx.posted_date,
                'amount': journal_currency.round(tx.amount),
                'payment_ref': (tx.description or tx.counterparty_name
                                or provider_reference),
                'partner_name': tx.counterparty_name or False,
                'eh_provider_reference': provider_reference,
                'unique_import_ref': canonical_by_provider[
                    provider_reference
                ],
            })
        if new_vals:
            StatementLine.create(new_vals)
        return len(new_vals), latest

    def _record_error(self, message):
        message = redact_secrets(message)[:2000]
        self.write({
            'last_run_at': fields.Datetime.now(),
            'last_run_state': 'error',
            'last_run_message': message,
        })
        self.message_post(body=_("Live fetch failed: %s") % message)
