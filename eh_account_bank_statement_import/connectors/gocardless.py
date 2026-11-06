# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
GoCardless connector stub.

Targets EU and UK banks via GoCardless Bank Account Data (the former
Nordigen Open Banking API). Same stub pattern as the Plaid and Basiq
connectors: it registers the 'gocardless' key and validates the
credential shape so a deployment can configure profiles against it from
day one, but it does not make HTTP calls. The live integration ships in
the separate `eh_account_bank_statement_import_gocardless` module, which
imports this key and overrides fetch_transactions to call the real
bankaccountdata.gocardless.com API.

Registering the stub here keeps the live-connector form view honest: the
GoCardless credentials group is backed by a selectable key whether or not
the live module is installed, and the operator gets a clear "install the
extension" error instead of a silent dead option.

Configuration shape (stored in profile.credentials_json):

    {
        "secret_id":   "...",       # GoCardless secret id
        "secret_key":  "...",       # GoCardless secret key
        "account_id":  "...",       # GoCardless account id (one per profile)
    }
"""

import datetime
import json
from typing import Iterable

from .base import LiveBankConnector, BankTransaction, ConnectorError
from .registry import register_connector


_REQUIRED_KEYS = ('secret_id', 'secret_key', 'account_id')


@register_connector
class GoCardlessConnector(LiveBankConnector):
    CONNECTOR_KEY = 'gocardless'
    CONNECTOR_LABEL = "GoCardless Bank Account Data (EU/UK)"
    CREDENTIALS_HELP = (
        "Configure profile.credentials_json with the JSON shape: "
        "{\"secret_id\": \"...\", \"secret_key\": \"...\", "
        "\"account_id\": \"...\"}. The stub validates the shape but does "
        "NOT make API calls; install the eh_account_bank_statement_import_"
        "gocardless extension module to enable live transaction sync "
        "against EU / UK banks."
    )

    def authenticate(self, profile) -> dict:
        return self._parse_credentials(profile)

    def fetch_transactions(
        self, profile,
        since_date: datetime.date,
        until_date: datetime.date,
        session: dict,
    ) -> Iterable[BankTransaction]:
        if not session.get('secret_id'):
            raise ConnectorError(
                "GoCardless stub: secret_id missing on profile %s. "
                "Configure credentials_json before enabling the profile, "
                "then install the GoCardless extension module to enable "
                "live sync." % getattr(profile, 'name', '<unknown>'),
            )
        raise ConnectorError(
            "GoCardless stub adapter is registered but does not perform "
            "live HTTP calls. Install the eh_account_bank_statement_"
            "import_gocardless extension module to enable real "
            "transaction sync against GoCardless Bank Account Data."
        )

    @staticmethod
    def _parse_credentials(profile):
        # Runtime connector contract passes a plain ``credentials`` mapping;
        # keep credentials_json only as a backwards-compatible seam for
        # direct stub use outside the framework.
        raw = getattr(profile, 'credentials', None)
        if raw is None:
            raw = getattr(profile, 'credentials_json', None) or '{}'
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError) as exc:
            raise ConnectorError(
                "GoCardless: credentials_json is not valid JSON: %s" % exc,
            )
        if not isinstance(data, dict):
            raise ConnectorError(
                "GoCardless: credentials_json must decode to a JSON object.",
            )
        missing = [k for k in _REQUIRED_KEYS if not data.get(k)]
        if missing:
            raise ConnectorError(
                "GoCardless: credentials_json missing required field(s): %s"
                % ', '.join(missing),
            )
        return dict(data)
