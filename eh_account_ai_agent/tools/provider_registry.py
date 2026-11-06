# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
AI provider registry.

Same pattern as the bank-feed connector registry and the Peppol
access-point registry: providers register themselves under a unique
key at import time, and the framework resolves them by key when a
capability needs an LLM call.

The contract is a single method on the provider:

    chat(messages, **kwargs) -> str

`messages` is a list of {'role': 'system' | 'user' | 'assistant',
'content': str} dicts (the de facto LLM API shape that every major
provider speaks). Returns the assistant's completion as a plain
string. Real provider modules implement this against their HTTP
API; stub providers raise ProviderError with an install-the-
extension-module message.

The registry intentionally does NOT make any HTTP calls itself.
All provider integrations live in separate paid extension modules
(eh_account_ai_agent_claude, eh_account_ai_agent_openai, etc.)
that import this module and call register_provider under the same
key as the stub. The override is transparent: company config and
capability call sites do not change on upgrade.
"""

from contextlib import contextmanager


_REGISTRY = {}
_MISSING = object()


class ProviderError(RuntimeError):
    """Raised by a provider when authentication / call fails.

    The capability layer catches this and falls back to its
    deterministic default; the failure message is logged on the
    underlying record for the user to see.
    """


def register_provider(key, factory):
    """Register a provider under `key`.

    :param key: stable string identifier (e.g. 'claude', 'openai',
        'local', 'manual'). Surfaces in the company config picker.
    :param factory: callable taking the company config dict and
        returning a provider instance with a `chat(messages,
        **kwargs)` method.
    """
    if not key or not callable(factory):
        raise ValueError(
            "register_provider requires a non-empty key and a "
            "callable factory.",
        )
    _REGISTRY[key] = factory


@contextmanager
def temporary_provider(key, factory):
    """Install a provider for one bounded scope, then restore exact state.

    Tests and diagnostics must not leak process-global provider factories into
    later registries or databases. Production extensions should continue to
    use ``register_provider`` at import time.
    """
    previous = _REGISTRY.get(key, _MISSING)
    register_provider(key, factory)
    try:
        yield
    finally:
        if previous is _MISSING:
            _REGISTRY.pop(key, None)
        else:
            _REGISTRY[key] = previous


def get_provider(key, config):
    """Return a provider instance for the registered key.

    :param key: identifier passed to register_provider.
    :param config: deployment-specific dict (URL, API token, model
        id, etc.). Provider factories interpret it.
    :raises ProviderError: when no provider is registered for key.
    """
    factory = _REGISTRY.get(key)
    if not factory:
        raise ProviderError(
            "No AI provider registered for key %r. Install the "
            "corresponding ERP Heritage AI provider extension "
            "module." % key,
        )
    return factory(config)


def has_provider(key):
    return key in _REGISTRY


def list_providers():
    """Return registered provider keys, sorted."""
    return sorted(_REGISTRY.keys())
