# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Basiq connector stub.

Targets AU and NZ banks via Basiq's Open Banking aggregator API
(/transactions endpoint family). Same stub pattern as the Plaid
connector: ships the credentials shape and the registry entry; the
real HTTP integration lives in a deployment-specific extension
module.

Configuration shape (stored in profile.credentials_json):

    {
        "api_key":     "...",       # Basiq server-side key
        "user_id":     "...",       # Basiq user (per end-customer)
        "account_id":  "...",       # Basiq account id (one per profile)
        "environment": "sandbox" | "production"
    }

The AU side is the high-value half: every AU SMB has a CBA / NAB / ANZ /
WBC account and the manual file-import path is the most-cited pain point
in the brutal-review feedback.
"""

import datetime
import json
from typing import Iterable

from .base import LiveBankConnector, BankTransaction, ConnectorError
from .registry import register_connector


_REQUIRED_KEYS = ('api_key', 'user_id', 'account_id')


@register_connector
class BasiqConnector(LiveBankConnector):
    CONNECTOR_KEY = 'basiq'
    CONNECTOR_LABEL = "Basiq (AU/NZ Open Banking)"
    CREDENTIALS_HELP = (
        "Configure profile.credentials_json with the JSON shape: "
        "{\"api_key\": \"...\", \"user_id\": \"...\", "
        "\"account_id\": \"...\", \"environment\": \"sandbox\" | "
        "\"production\"}. The stub validates the shape but does NOT "
        "make API calls; install the paid Basiq extension module to "
        "enable live transaction sync against AU / NZ banks (CBA, "
        "NAB, ANZ, WBC, ASB, BNZ, etc.)."
    )

    def authenticate(self, profile) -> dict:
        return self._parse_credentials(profile)

    def fetch_transactions(
        self, profile,
        since_date: datetime.date,
        until_date: datetime.date,
        session: dict,
    ) -> Iterable[BankTransaction]:
        if not session.get('api_key'):
            raise ConnectorError(
                "Basiq stub: api_key missing on profile %s. Configure "
                "credentials_json before enabling the profile, then "
                "install the Basiq extension module to enable live "
                "sync." % getattr(profile, 'name', '<unknown>'),
            )
        raise ConnectorError(
            "Basiq stub adapter is registered but does not perform "
            "live HTTP calls. Install the eh_account_bank_statement_"
            "import_basiq extension module to enable real "
            "transaction sync against Basiq's Open Banking gateway."
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
                "Basiq: credentials_json is not valid JSON: %s" % exc,
            )
        if not isinstance(data, dict):
            raise ConnectorError(
                "Basiq: credentials_json must decode to a JSON object.",
            )
        missing = [k for k in _REQUIRED_KEYS if not data.get(k)]
        if missing:
            raise ConnectorError(
                "Basiq: credentials_json missing required field(s): %s"
                % ', '.join(missing),
            )
        env = data.get('environment') or 'sandbox'
        if env not in ('sandbox', 'production'):
            raise ConnectorError(
                "Basiq: environment must be sandbox or production "
                "(got %r)" % env,
            )
        return dict(data, environment=env)
