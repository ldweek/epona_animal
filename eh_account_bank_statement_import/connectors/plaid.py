# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Plaid connector stub.

Targets US, CA, UK, IE, FR, ES, NL banks via Plaid's aggregator API
(/transactions/sync endpoint family). Ships the credentials shape and
the registration entry so a deployment can configure profiles against
this key from day one. The actual HTTP integration is intentionally
NOT shipped here: a production-ready Plaid integration needs a paid
client_id / secret pair and a deployment-specific callback host for
the Plaid Link flow, both of which differ per customer.

Sites that need the live integration install
`eh_account_bank_statement_import_plaid` (separate paid module from
ERP Heritage or a partner) which imports this module and overrides
`fetch_transactions` to make the actual /transactions/sync call.

The override pattern matters: by registering here under the same
CONNECTOR_KEY, the partner module replaces the stub in the registry
without changing any profile data or migration. Operators flip
nothing on upgrade.

Configuration shape (stored in profile.credentials_json as JSON):

    {
        "client_id":     "...",          # Plaid client id
        "secret":        "...",          # Plaid secret (sandbox/dev/prod)
        "access_token":  "...",          # per-item access token from Link
        "account_id":    "...",          # Plaid account id (one per profile)
        "environment":   "sandbox" | "development" | "production"
    }

The stub validates the shape and raises ConnectorError when fields
are missing so misconfiguration surfaces at the first cron pass
rather than mid-fetch.
"""

import datetime
import json
from typing import Iterable

from .base import LiveBankConnector, BankTransaction, ConnectorError
from .registry import register_connector


_REQUIRED_KEYS = (
    'client_id', 'secret', 'access_token', 'account_id',
)


@register_connector
class PlaidConnector(LiveBankConnector):
    CONNECTOR_KEY = 'plaid'
    CONNECTOR_LABEL = "Plaid (US/CA/UK/EU)"
    CREDENTIALS_HELP = (
        "Configure profile.credentials_json with the JSON shape: "
        "{\"client_id\": \"...\", \"secret\": \"...\", "
        "\"access_token\": \"...\", \"account_id\": \"...\", "
        "\"environment\": \"sandbox\" | \"development\" | "
        "\"production\"}. The stub validates the shape but does NOT "
        "make API calls; install the paid plaid extension module to "
        "enable live transaction sync."
    )

    def authenticate(self, profile) -> dict:
        creds = self._parse_credentials(profile)
        # Real adapter exchanges the access_token for a session here.
        # The stub returns the parsed credentials so the framework's
        # session caching path stays consistent.
        return creds

    def fetch_transactions(
        self, profile,
        since_date: datetime.date,
        until_date: datetime.date,
        session: dict,
    ) -> Iterable[BankTransaction]:
        # Stub: validate session and refuse loudly.
        if not session.get('access_token'):
            raise ConnectorError(
                "Plaid stub: access_token missing on profile %s. "
                "Configure credentials_json with client_id, secret, "
                "access_token, and account_id, then install the "
                "Plaid extension module to enable live sync." %
                getattr(profile, 'name', '<unknown>'),
            )
        raise ConnectorError(
            "Plaid stub adapter is registered but does not perform "
            "live HTTP calls. Install the eh_account_bank_statement_"
            "import_plaid extension module to enable real "
            "transaction sync against Plaid's /transactions/sync "
            "endpoint."
        )

    @staticmethod
    def _parse_credentials(profile):
        # Runtime connector contract passes a plain ``credentials`` mapping;
        # direct unit callers and legacy adapters may still expose JSON text.
        raw = getattr(profile, 'credentials', None)
        if raw is None:
            raw = getattr(profile, 'credentials_json', None) or '{}'
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError) as exc:
            raise ConnectorError(
                "Plaid: credentials_json is not valid JSON: %s" % exc,
            )
        if not isinstance(data, dict):
            raise ConnectorError(
                "Plaid: credentials_json must decode to a JSON object.",
            )
        missing = [k for k in _REQUIRED_KEYS if not data.get(k)]
        if missing:
            raise ConnectorError(
                "Plaid: credentials_json missing required field(s): %s"
                % ', '.join(missing),
            )
        env = data.get('environment') or 'sandbox'
        if env not in ('sandbox', 'development', 'production'):
            raise ConnectorError(
                "Plaid: environment must be sandbox, development, or "
                "production (got %r)" % env,
            )
        return dict(data, environment=env)
