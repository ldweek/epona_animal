# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Access Point adapter registry.

PEPPOL transport (AS4 message exchange against the OpenPeppol network)
is normally provided by a third-party access point (Storecove, Pagero,
Tickstar, Comarch, EDICOM, etc.). This module is intentionally
BYO-access-point: it ships the contract and a manual stub, and
expects deployment-specific add-on modules to register a real
adapter for whichever provider the deployment uses.

The contract is a simple Python class with two methods:

    submit(xml_bytes, recipient_endpoint_id, recipient_endpoint_scheme)
        -> dict {transmission_id, status, raw_response}
    poll(since_iso) -> list of dicts {message_id, sender, xml_bytes,
                                       received_at}

Adapters register via register_adapter(key, factory). Modules that
ship an adapter call register at import time so configuration on the
server picks up every installed adapter.

Why a registry, not a base class hierarchy?

* Adapters live in separate modules with their own dependencies
  (HTTP libraries, signing libraries, vendor SDKs). A registry keeps
  the import surface in this module empty.
* The contract is small enough that a duck-typed callable is more
  honest than a deep class hierarchy.
* Sites can override the registry from a setup hook to inject a mock
  adapter for testing.
"""


_REGISTRY = {}


class AccessPointError(RuntimeError):
    """Raised when an adapter call fails or no adapter is registered.

    Carries the adapter key and the underlying cause so the caller
    can surface a precise message to the user.
    """


def register_adapter(key, factory):
    """Register an access-point adapter.

    :param key: stable string identifier (e.g. 'storecove', 'pagero').
        Surfaces in the dashboard config and in the inbound-bill
        audit trail.
    :param factory: callable taking the company config dict and
        returning an adapter instance with submit / poll methods.
    """
    if not key or not callable(factory):
        raise ValueError(
            "register_adapter requires a non-empty key and a callable "
            "factory.",
        )
    _REGISTRY[key] = factory


def get_adapter(key, config):
    """Return an adapter instance for the registered key.

    :param key: identifier passed to register_adapter.
    :param config: deployment-specific dict (URL, API token, signing
        cert path, etc.). Adapter factories interpret it.
    :raises AccessPointError: when no adapter is registered for key.
    """
    factory = _REGISTRY.get(key)
    if not factory:
        raise AccessPointError(
            "No access-point adapter registered for key %r. Install "
            "the corresponding ERP Heritage access-point module." % key,
        )
    return factory(config)


def has_adapter(key):
    return key in _REGISTRY


def list_adapters():
    """Return the registered adapter keys, sorted.

    Used by the dashboard config dropdown so the operator only sees
    adapters that are actually installed.
    """
    return sorted(_REGISTRY.keys())


# ---- ManualAccessPoint -----------------------------------------------------
#
# Default no-op adapter. submit() drops the XML to the local outbox
# attachment directory; poll() reads any XML files dropped into the
# inbox attachment directory by an external process (cron, mail
# pickup script, manual upload). Useful for sites that route through
# a 3PL via SFTP / email rather than direct AS4.
#
# Real adapters (Storecove, Pagero, Tickstar) ship in their own
# modules and override this stub by registering under the same key
# the deployment chooses in the company config.


class ManualAccessPoint:
    """Reference adapter for sites that integrate via file drop.

    submit():
        Always returns ``{'transmission_id': None, 'status': 'queued',
        'raw_response': 'manual'}``. The caller is expected to take
        the XML attachment off the inbound record and hand it to
        whatever transport channel the deployment uses (SFTP, email
        attachment, manual upload to an access-point portal).

    poll():
        Returns an empty list. Sites that want inbound polling must
        register a real adapter; the manual flow expects an external
        cron / mail handler to write attachments to the inbound model
        directly.
    """

    def __init__(self, config):
        self.config = config or {}

    def submit(self, xml_bytes, recipient_endpoint_id,
               recipient_endpoint_scheme):
        return {
            'transmission_id': None,
            'status': 'queued',
            'raw_response': 'manual',
        }

    def poll(self, since_iso):
        return []


register_adapter('manual', ManualAccessPoint)
