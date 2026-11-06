# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Minimal HTTP helper for LLM provider extension modules.

Uses only the Python standard library (urllib) so provider modules add
no third-party dependency. Provider adapters call http_post_json to
POST a JSON body and parse a JSON response, translating every transport
or decode failure into a ProviderError the capability layer already
knows how to swallow.

Tests monkeypatch http_post_json so the real network is never touched
in CI.
"""

import json
import re
import urllib.error
import urllib.request

from odoo.addons.eh_account_base.tools.net_guard import (
    UnsafeUrlError, assert_safe_url,
)
from .provider_registry import ProviderError


MAX_LLM_RESPONSE_BYTES = 4 * 1024 * 1024
_SECRET_RE = re.compile(
    r'(?i)(authorization|bearer|access[_-]?token|api[_-]?key|secret|password)'
    r'(["\']?\s*[:=]\s*["\']?)([^"\',\s}&]+)'
)


def _redact_secrets(message):
    return _SECRET_RE.sub(r'\1\2***', str(message or ''))


def _validate_url(url, allow_private=False):
    """SSRF guard for admin-configured LLM endpoints.

    Local providers using loopback remain supported. Private LANs require an
    explicit caller opt-in; metadata, link-local, reserved destinations, and
    hostnames resolving to them stay blocked.
    """
    try:
        return assert_safe_url(
            url, allow_loopback=True, allow_private=allow_private,
        )
    except UnsafeUrlError as exc:
        raise ProviderError("Unsafe LLM endpoint: %s" % exc) from exc


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never replay credentials or bodies to a redirect destination.

    urllib's default handler automatically follows 30x responses. Validating
    only the configured URL is therefore insufficient: a public endpoint can
    redirect the Authorization header and POST body to a private or metadata
    address. Provider endpoints are configured as final URLs, so fail closed
    on every redirect instead of attempting to revalidate/replay it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request, timeout):
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(request, timeout=timeout)


def http_post_json(
    url, payload, headers=None, timeout=30, allow_private=False,
):
    """POST `payload` as JSON to `url`; return the parsed JSON response.

    :param url: absolute endpoint URL.
    :param payload: dict serialised to a JSON request body.
    :param headers: optional dict of extra request headers.
    :param timeout: socket timeout in seconds.
    :raises ProviderError: on any connection, HTTP, or decode error.
    """
    _validate_url(url, allow_private=allow_private)
    body = json.dumps(payload).encode('utf-8')
    req_headers = {'Content-Type': 'application/json'}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(
        url, data=body, headers=req_headers, method='POST',
    )
    try:
        with _open_no_redirect(request, timeout=timeout) as resp:
            raw_bytes = resp.read(MAX_LLM_RESPONSE_BYTES + 1)
            if len(raw_bytes) > MAX_LLM_RESPONSE_BYTES:
                raise ProviderError(
                    "LLM endpoint response exceeds %d bytes."
                    % MAX_LLM_RESPONSE_BYTES,
                )
            raw = raw_bytes.decode('utf-8')
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = _redact_secrets(
                exc.read(501).decode('utf-8', errors='replace')[:500]
            )
        except Exception:  # noqa: BLE001 - best-effort detail only
            detail = ''
        raise ProviderError(
            "LLM endpoint returned HTTP %s: %s" % (exc.code, detail),
        )
    except urllib.error.URLError as exc:
        raise ProviderError(
            "Could not reach LLM endpoint: %s" % _redact_secrets(exc.reason),
        )
    except ProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw error
        raise ProviderError(
            "LLM request failed (%s)." % type(exc).__name__)
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProviderError(
            "LLM endpoint returned non-JSON response: %s" % exc,
        )
