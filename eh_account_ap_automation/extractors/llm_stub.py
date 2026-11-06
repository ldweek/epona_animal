# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
LLM-backed extractor stubs.

These stubs validate the credentials shape but deliberately do NOT
make real API calls. Production deployments install a provider adapter
from their chosen vendor or implementation partner. That adapter
re-registers under the same EXTRACTOR_KEY and overrides
`extract` to call the actual hosted model.

The override pattern is the same as the bank-feed connectors and the
Peppol access-point adapters: register here under a stable key,
swap the implementation transparently from a downstream module
without changing any data or migrations.

Configuration shape (stored on the company or the intake profile via
a credentials_json field, JSON-encoded):

    {
        "api_key":  "...",          # provider API key
        "model":    "claude-opus-4-7",  # model id (provider-specific)
        "endpoint": "https://api...",   # optional override
        "timeout":  30                  # optional, seconds
    }

The pipeline only calls the extractor when an intake's
`extractor_key` field references a registered key. Sites that have
not configured any LLM keep the existing regex-only path.
"""

import json
from typing import Iterable, Optional

from .base import (
    LineItemExtractor, ExtractedLine, ExtractorError,
)
from .registry import register_extractor


_REQUIRED_KEYS = ('api_key', 'model')
_MAX_CREDENTIAL_BYTES = 64 * 1024


def _parse_credentials(provider, hints):
    """Parse + validate the credentials JSON from hints.

    Hints structure (passed by the intake):
      {'credentials_json': '{"api_key": "...", "model": "..."}', ...}

    Raises ExtractorError on missing or malformed credentials so the
    intake's chatter shows a precise failure.
    """
    raw = (hints or {}).get('credentials_json') or '{}'
    if isinstance(raw, str) \
            and len(raw.encode('utf-8')) > _MAX_CREDENTIAL_BYTES:
        raise ExtractorError(
            "%s: credentials_json exceeds the 64 KiB safety limit."
            % provider,
        )
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise ExtractorError(
            "%s: credentials_json is not valid JSON: %s"
            % (provider, exc),
        )
    if not isinstance(data, dict):
        raise ExtractorError(
            "%s: credentials_json must decode to a JSON object."
            % provider,
        )
    missing = [k for k in _REQUIRED_KEYS if not data.get(k)]
    if missing:
        raise ExtractorError(
            "%s: credentials_json missing required field(s): %s"
            % (provider, ', '.join(missing)),
        )
    return data


@register_extractor
class ClaudeLlmExtractor(LineItemExtractor):
    EXTRACTOR_KEY = 'claude_llm'
    EXTRACTOR_LABEL = "Claude (LLM stub)"
    REQUIRES_RASTERISATION = True

    def extract(
        self, document_bytes: bytes, mime_type: str,
        hints: Optional[dict] = None,
    ) -> Iterable[ExtractedLine]:
        # Validate before any network call so misconfiguration
        # surfaces at the first run rather than half-way through.
        _parse_credentials('Claude LLM', hints)
        raise ExtractorError(
            "Claude LLM stub adapter is registered but does not "
            "perform live API calls. Install a provider adapter that "
            "re-registers extractor key 'claude_llm' to enable real "
            "line-item extraction."
        )


@register_extractor
class OpenAiLlmExtractor(LineItemExtractor):
    EXTRACTOR_KEY = 'openai_llm'
    EXTRACTOR_LABEL = "OpenAI (LLM stub)"
    REQUIRES_RASTERISATION = True

    def extract(
        self, document_bytes: bytes, mime_type: str,
        hints: Optional[dict] = None,
    ) -> Iterable[ExtractedLine]:
        _parse_credentials('OpenAI LLM', hints)
        raise ExtractorError(
            "OpenAI LLM stub adapter is registered but does not "
            "perform live API calls. Install a provider adapter that "
            "re-registers extractor key 'openai_llm' to enable real "
            "line-item extraction."
        )


@register_extractor
class LocalLlmExtractor(LineItemExtractor):
    """On-prem LLM stub (Ollama, LM Studio, vLLM, llama.cpp server).

    Same validation pattern but the credentials shape allows a
    plain endpoint URL and no api_key (most local servers do not
    require one). When no api_key is supplied, the validator
    accepts an endpoint-only config.
    """

    EXTRACTOR_KEY = 'local_llm'
    EXTRACTOR_LABEL = "Local LLM (Ollama / vLLM stub)"
    REQUIRES_RASTERISATION = True

    def extract(
        self, document_bytes: bytes, mime_type: str,
        hints: Optional[dict] = None,
    ) -> Iterable[ExtractedLine]:
        raw = (hints or {}).get('credentials_json') or '{}'
        if isinstance(raw, str) \
                and len(raw.encode('utf-8')) > _MAX_CREDENTIAL_BYTES:
            raise ExtractorError(
                "Local LLM: credentials_json exceeds the 64 KiB safety "
                "limit."
            )
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError) as exc:
            raise ExtractorError(
                "Local LLM: credentials_json is not valid JSON: %s"
                % exc,
            )
        if not isinstance(data, dict):
            raise ExtractorError(
                "Local LLM: credentials_json must decode to a JSON "
                "object.",
            )
        if not data.get('endpoint') and not data.get('model'):
            raise ExtractorError(
                "Local LLM: credentials_json must include at least "
                "one of 'endpoint' (e.g. http://127.0.0.1:11434) "
                "or 'model' (the local model name).",
            )
        raise ExtractorError(
            "Local LLM stub adapter is registered but does not "
            "perform live HTTP calls. Install a provider adapter that "
            "re-registers extractor key 'local_llm' to enable real "
            "line-item extraction against your local endpoint."
        )
