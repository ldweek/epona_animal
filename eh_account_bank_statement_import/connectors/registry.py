# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Connector registry.

Mirrors the parsers/registry.py pattern: connectors register themselves
under a unique CONNECTOR_KEY at import time, and the framework resolves
them by key when running the fetch cron. A localization or partner
package can `register_connector(MyAusBankConnector)` from its own
__init__.py without touching the base addon.

The registry intentionally does not import any concrete connectors:
this addon only ships the base contract and the Odoo-side plumbing.
Concrete connectors live in their own packages so a deployment opts
in to each one explicitly.
"""

from .base import LiveBankConnector


_CONNECTORS = {}


def register_connector(cls):
    """Register a LiveBankConnector subclass under its CONNECTOR_KEY.

    Returns the class unchanged so it can be used as a decorator.
    Re-registering an existing key replaces the prior entry; this lets
    a localization swap the default behaviour from a higher-priority
    addon. The replacement is logged at INFO level by the caller (the
    framework calls register_connector once per import).
    """
    if not isinstance(cls, type) or not issubclass(cls, LiveBankConnector):
        raise TypeError(
            "register_connector expects a LiveBankConnector subclass, got %r"
            % (cls,),
        )
    if not cls.CONNECTOR_KEY:
        raise ValueError(
            "%s.CONNECTOR_KEY must be set before registration"
            % cls.__name__,
        )
    _CONNECTORS[cls.CONNECTOR_KEY] = cls
    return cls


def get_connector(key):
    """Return an instance of the connector registered under `key`.

    Raises KeyError when no connector is registered; the caller
    surfaces this to the user with a message that lists the available
    keys so the configuration mistake is obvious.
    """
    cls = _CONNECTORS.get(key)
    if cls is None:
        raise KeyError(
            "No live connector registered for key %r. Available: %s"
            % (key, sorted(_CONNECTORS)),
        )
    return cls()


def connector_choices():
    """Return [(key, label), ...] for selection fields and UI menus."""
    return sorted(
        ((cls.CONNECTOR_KEY, cls.CONNECTOR_LABEL)
         for cls in _CONNECTORS.values()),
        key=lambda x: x[0],
    )
