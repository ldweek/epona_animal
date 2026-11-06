# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Live bank connector base class.

A connector is a plain Python class that knows how to authenticate
against a single bank and pull recent transactions for one account.
The framework treats every connector identically; concrete
implementations live in their own packages so legal, jurisdictional,
or partnership constraints stay contained.

The contract (override these on a concrete subclass):

* `CONNECTOR_KEY` -- short, lowercase, unique. Stored on the profile
  record so the framework can resolve which connector to invoke.
* `CONNECTOR_LABEL` -- human-readable name for selection menus.
* `authenticate(profile)` -> dict of session credentials. Called at the
  top of every fetch; the framework caches the returned dict for the
  duration of one fetch run only, so connectors do not have to
  persist tokens themselves.
* `fetch_transactions(profile, since_date, until_date, session)`
  -> iterable of `BankTransaction` records.

Connector implementations MUST NOT touch the Odoo ORM directly. They
operate on plain Python dicts and the profile (which is a plain
namedtuple-style passthrough). The framework converts the returned
records into `account.bank.statement.line` entries inside a
per-statement savepoint so a single bad transaction does not freeze
the run.

Idempotency is the framework's responsibility: every transaction
carries a `provider_reference` and the framework refuses to import a
transaction whose reference has been seen before for the same
account. Connectors MUST set provider_reference; if a bank does not
expose one, derive it deterministically from the transaction's date,
amount, and counterparty (the upstream import_log already handles
de-duplication on the same key).
"""

import dataclasses
import datetime
import math
from typing import Iterable, Optional


@dataclasses.dataclass
class BankTransaction:
    """Plain transport record for a single bank movement.

    Field semantics:

    * `provider_reference` -- bank-side unique id for this transaction.
      Required. If the bank does not expose one, derive a deterministic
      hash from date, amount, counterparty inside the connector.
    * `posted_date` -- value date as recorded by the bank.
    * `amount` -- positive for credit (money in), negative for debit
      (money out). The framework writes this directly as the statement
      line `amount` after the journal's currency rounding.
    * `currency` -- ISO 4217 code; the framework will reject the line
      if this differs from the journal's currency unless the journal
      itself is multi-currency enabled.
    * `counterparty_name`, `counterparty_account` -- best-effort
      attribution; safe to leave blank.
    * `description` -- free-form narrative the bank attached.
    * `extra` -- catch-all dict for connector-specific metadata; the
      framework persists it on the import_log.line row but does not
      use it for matching or import semantics.
    """

    provider_reference: str
    posted_date: datetime.date
    amount: float
    currency: str
    counterparty_name: str = ''
    counterparty_account: str = ''
    description: str = ''
    extra: Optional[dict] = None

    def validate(self) -> None:
        """Raise ConnectorError on any malformed field."""
        if not self.provider_reference:
            raise ConnectorError("provider_reference is required")
        if (
            isinstance(self.amount, bool)
            or not isinstance(self.amount, (int, float))
            or not math.isfinite(float(self.amount))
        ):
            raise ConnectorError(
                "amount must be a finite number (got %r)" % (self.amount,)
            )
        if not self.currency:
            raise ConnectorError("currency is required (ISO 4217)")
        if len(self.currency) != 3:
            raise ConnectorError(
                "currency must be a 3-letter ISO code (got %r)"
                % (self.currency,),
            )
        if not isinstance(self.posted_date, datetime.date):
            raise ConnectorError(
                "posted_date must be a date instance (got %r)"
                % (self.posted_date,),
            )


class ConnectorError(RuntimeError):
    """Raised by a connector when authentication or fetch fails.

    The framework catches this per profile and posts the message to the
    profile's chatter while continuing with the next profile, so a
    single broken bank does not freeze the entire fetch run.
    """


class LiveBankConnector:
    """Base class for live bank connectors.

    Subclass and set `CONNECTOR_KEY` and `CONNECTOR_LABEL`, then
    implement `authenticate` and `fetch_transactions`. Register the
    subclass via `register_connector(...)` at import time so the
    framework picks it up.
    """

    CONNECTOR_KEY = ''
    CONNECTOR_LABEL = ''

    # Optional: description shown in the profile form to help the user
    # configure the right credentials. Plain text; HTML is escaped.
    CREDENTIALS_HELP = ''

    def authenticate(self, profile) -> dict:
        """Return a session dict for the duration of one fetch run.

        The framework discards the returned dict at the end of the
        run; connectors do not persist tokens between runs except via
        their own off-platform storage. The default implementation
        returns an empty dict; subclasses override.
        """
        return {}

    def fetch_transactions(
        self, profile,
        since_date: datetime.date,
        until_date: datetime.date,
        session: dict,
    ) -> Iterable[BankTransaction]:
        """Yield `BankTransaction` records covering the closed interval.

        Concrete implementations make the HTTP / OAuth / WebSocket
        calls here. The framework consumes the iterable lazily, so
        connectors can stream large windows without buffering.
        """
        raise NotImplementedError(
            "%s must implement fetch_transactions"
            % type(self).__name__,
        )

    def disconnect(self, profile, session: dict) -> None:
        """Tear down the session. Default is a no-op; override when the
        provider requires an explicit logout (rare).
        """
        return None
