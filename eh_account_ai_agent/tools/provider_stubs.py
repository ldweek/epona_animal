# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
AI provider stubs.

* ManualProvider: deliberate no-op. The capability layer detects this
  and skips the LLM path, returning the deterministic default. Used
  by sites that want the deterministic capabilities without enabling
  any LLM dependency.

* ClaudeProvider, OpenAiProvider, LocalProvider: validate
  credentials, refuse live calls with an install-the-extension
  message. Real adapter modules override these under the same key.
"""

import json

from .provider_registry import register_provider, ProviderError


def _parse_creds(provider, config, required=('api_key', 'model')):
    """Validate the credentials dict + required keys.

    config can be a dict already or a JSON string. Raises ProviderError
    naming the offending field.
    """
    if isinstance(config, str):
        try:
            data = json.loads(config) if config else {}
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                "%s: config is not valid JSON: %s" % (provider, exc),
            )
    elif isinstance(config, dict):
        data = config
    else:
        raise ProviderError(
            "%s: config must be a dict or JSON string." % provider,
        )
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ProviderError(
            "%s: config missing required field(s): %s"
            % (provider, ', '.join(missing)),
        )
    return data


class ManualProvider:
    """Deliberate no-op provider.

    Capabilities check `getattr(provider, 'is_manual', False)` and
    fall back to their deterministic implementation. This makes the
    deterministic-only path explicit in configuration: a site that
    has not chosen an LLM provider gets the deterministic capability
    without the framework treating it as a missing-provider error.
    """

    is_manual = True

    def __init__(self, config):
        self.config = config or {}

    def chat(self, messages, **kwargs):
        raise ProviderError(
            "Manual provider is a deliberate no-op. The capability "
            "layer should detect is_manual=True and use the "
            "deterministic default instead of calling chat()."
        )


class ClaudeProvider:
    is_manual = False

    def __init__(self, config):
        self.config = _parse_creds('Claude provider', config)

    def chat(self, messages, **kwargs):
        raise ProviderError(
            "Claude provider stub is registered but does not perform "
            "live API calls. Install the eh_account_ai_agent_claude "
            "extension module to enable real chat completion against "
            "Anthropic's API."
        )


class OpenAiProvider:
    is_manual = False

    def __init__(self, config):
        self.config = _parse_creds('OpenAI provider', config)

    def chat(self, messages, **kwargs):
        raise ProviderError(
            "OpenAI provider stub is registered but does not perform "
            "live API calls. Install the eh_account_ai_agent_openai "
            "extension module to enable real chat completion against "
            "OpenAI's API."
        )


class LocalProvider:
    """On-prem OpenAI-compatible endpoint stub.

    Accepts either an endpoint URL or a model name (or both) since
    most local servers (Ollama, vLLM, llama.cpp) do not require an
    api_key.
    """

    is_manual = False

    def __init__(self, config):
        if isinstance(config, str):
            try:
                data = json.loads(config) if config else {}
            except (TypeError, ValueError) as exc:
                raise ProviderError(
                    "Local provider: config is not valid JSON: %s"
                    % exc,
                )
        elif isinstance(config, dict):
            data = config
        else:
            raise ProviderError(
                "Local provider: config must be a dict or JSON "
                "string.",
            )
        if not data.get('endpoint') and not data.get('model'):
            raise ProviderError(
                "Local provider: config must include at least one "
                "of 'endpoint' (e.g. http://127.0.0.1:11434) or "
                "'model' (the local model name).",
            )
        self.config = data

    def chat(self, messages, **kwargs):
        raise ProviderError(
            "Local provider stub is registered but does not perform "
            "live HTTP calls. Install the "
            "eh_account_ai_agent_local extension module to enable "
            "real chat completion against your local OpenAI-"
            "compatible LLM endpoint."
        )


# Register all four stubs at import time. Real adapter modules
# replace these via register_provider under the same key.
register_provider('manual', ManualProvider)
register_provider('claude', ClaudeProvider)
register_provider('openai', OpenAiProvider)
register_provider('local', LocalProvider)
