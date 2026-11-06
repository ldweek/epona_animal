# -*- encoding: utf-8 -*-
"""Access-point HTTP transport boundary tests."""

import io
from unittest import mock

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_einvoice_peppol.tools import peppol_http
from odoo.addons.eh_account_einvoice_peppol.tools.access_point_registry import (
    AccessPointError,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]


@tagged('post_install', '-at_install')
class TestPeppolHttp(TransactionCase):

    def test_private_access_point_is_rejected(self):
        with self.assertRaises(AccessPointError):
            peppol_http.request('GET', 'http://10.0.0.5/messages')

    def test_oversized_access_point_response_is_rejected(self):
        with mock.patch.object(
                peppol_http, 'MAX_ACCESS_POINT_RESPONSE_BYTES', 16), \
                mock.patch.object(
                    peppol_http,
                    '_open_no_redirect',
                    return_value=_Response(b'x' * 17),
                ):
            with self.assertRaisesRegex(AccessPointError, 'exceeds'):
                peppol_http.request(
                    'GET',
                    'https://8.8.8.8/messages',
                    expect_json=False,
                )

    def test_http_error_body_redacts_secrets(self):
        error = peppol_http.urllib.error.HTTPError(
            'https://8.8.8.8/messages', 401, 'Unauthorized', {},
            io.BytesIO(b'authorization: Bearer-secret api_key=topsecret'),
        )
        with mock.patch.object(
                peppol_http, '_open_no_redirect', side_effect=error):
            with self.assertRaises(AccessPointError) as caught:
                peppol_http.request(
                    'GET', 'https://8.8.8.8/messages', expect_json=False,
                )
        message = str(caught.exception)
        self.assertNotIn('Bearer-secret', message)
        self.assertNotIn('topsecret', message)

    def test_redirect_handler_never_builds_a_second_request(self):
        handler = peppol_http._NoRedirectHandler()
        original = peppol_http.urllib.request.Request(
            'https://8.8.8.8/messages', method='GET',
        )

        redirected = handler.redirect_request(
            original, None, 302, 'Found',
            {'Location': 'http://127.0.0.1/latest/meta-data'},
            'http://127.0.0.1/latest/meta-data',
        )

        self.assertIsNone(redirected)

    def test_request_uses_no_redirect_opener(self):
        opener = mock.Mock()
        opener.open.return_value = _Response(b'{}')
        with mock.patch.object(
                peppol_http.urllib.request, 'build_opener',
                return_value=opener) as build_opener:
            result = peppol_http.request(
                'GET', 'https://8.8.8.8/messages', expect_json=True,
            )

        self.assertEqual(result, {})
        handler = build_opener.call_args.args[0]
        self.assertIsInstance(handler, peppol_http._NoRedirectHandler)
        opener.open.assert_called_once()
