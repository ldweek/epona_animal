# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Minimal HTTP helper for Peppol access-point adapter modules.

Standard library only (urllib), so transport adapters add no
third-party dependency. Every failure becomes an AccessPointError,
which the outbound send action and the inbound poll cron already catch.

Adapter test suites monkeypatch request so CI never touches a real
access point, SMP, or AS4 gateway.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from odoo.addons.eh_account_base.tools.net_guard import (
    UnsafeUrlError, assert_safe_url,
)
from .access_point_registry import AccessPointError


MAX_ACCESS_POINT_RESPONSE_BYTES = 20 * 1024 * 1024
_SECRET_RE = re.compile(
    r'(?i)(authorization|bearer|access[_-]?token|api[_-]?key|secret|password)'
    r'(["\']?\s*[:=]\s*["\']?)([^"\',\s}&]+)'
)


def _redact_secrets(message):
    return _SECRET_RE.sub(r'\1\2***', str(message or ''))


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return redirect responses to the caller without following them.

    The destination of a 3xx response is controlled by the remote server.
    Following it inside urllib would bypass the safety decision made for the
    original URL and permit a public endpoint to bounce the worker into a
    loopback, metadata, or private-LAN address.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_no_redirect(request_obj, timeout):
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(request_obj, timeout=timeout)


def request(method, url, headers=None, body=None, form=None,
            expect_json=True, timeout=30):
    """Perform an HTTP request.

    :param expect_json: when True, decode and return parsed JSON; when
        False, return the raw response bytes (for SMP XML / receipts).
    :raises AccessPointError: on any connection, HTTP, or decode error.
    """
    try:
        assert_safe_url(url)
    except UnsafeUrlError as exc:
        raise AccessPointError(
            "Unsafe access-point URL: %s" % exc,
        ) from exc
    req_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
        req_headers.setdefault('Content-Type', 'application/json')
    elif form is not None:
        data = urllib.parse.urlencode(form).encode('utf-8')
        req_headers.setdefault(
            'Content-Type', 'application/x-www-form-urlencoded')
    request_obj = urllib.request.Request(
        url, data=data, headers=req_headers, method=method)
    try:
        with _open_no_redirect(request_obj, timeout=timeout) as resp:
            raw = resp.read(MAX_ACCESS_POINT_RESPONSE_BYTES + 1)
            if len(raw) > MAX_ACCESS_POINT_RESPONSE_BYTES:
                raise AccessPointError(
                    "Access-point response exceeds %d bytes."
                    % MAX_ACCESS_POINT_RESPONSE_BYTES,
                )
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            detail = _redact_secrets(
                exc.read(501).decode('utf-8', errors='replace')[:500]
            )
        except Exception:  # noqa: BLE001 - best-effort detail only
            detail = ''
        raise AccessPointError(
            "Access point returned HTTP %s: %s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        raise AccessPointError(
            "Could not reach access point: %s" % _redact_secrets(exc.reason))
    except AccessPointError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak a raw error
        raise AccessPointError(
            "Access-point request failed (%s)." % type(exc).__name__)
    if not expect_json:
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw.decode('utf-8'))
    except (TypeError, ValueError) as exc:
        raise AccessPointError(
            "Access point returned non-JSON response: %s" % exc)
