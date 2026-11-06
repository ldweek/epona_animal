# -*- encoding: utf-8 -*-
"""Transport-boundary tests for LLM HTTP helper."""

import io
from unittest import mock

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_ai_agent.tools import llm_http
from odoo.addons.eh_account_ai_agent.tools.provider_registry import ProviderError


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
class TestLlmHttp(TransactionCase):

    def test_loopback_opt_in_does_not_open_private_lan(self):
        with self.assertRaises(ProviderError):
            llm_http._validate_url('http://10.0.0.5/v1/chat')
        parsed = llm_http._validate_url('http://127.0.0.1:11434/v1/chat')
        self.assertEqual(parsed.hostname, '127.0.0.1')

    def test_private_lan_requires_explicit_opt_in(self):
        parsed = llm_http._validate_url(
            'http://10.0.0.5/v1/chat', allow_private=True,
        )
        self.assertEqual(parsed.hostname, '10.0.0.5')

    def test_private_opt_in_does_not_open_link_local_metadata(self):
        with self.assertRaises(ProviderError):
            llm_http._validate_url(
                'http://169.254.169.254/latest/meta-data/',
                allow_private=True,
            )

    def test_oversized_response_is_rejected(self):
        with mock.patch.object(llm_http, 'MAX_LLM_RESPONSE_BYTES', 16), \
                mock.patch.object(
                    llm_http, '_open_no_redirect',
                    return_value=_Response(b'x' * 17),
                ):
            with self.assertRaisesRegex(ProviderError, 'exceeds'):
                llm_http.http_post_json(
                    'https://8.8.8.8/v1/chat',
                    {'prompt': 'test'},
                )

    def test_http_error_body_redacts_secrets(self):
        error = llm_http.urllib.error.HTTPError(
            'https://8.8.8.8/v1/chat', 401, 'Unauthorized', {},
            io.BytesIO(b'authorization: Bearer-secret api_key=topsecret'),
        )
        with mock.patch.object(
                llm_http, '_open_no_redirect', side_effect=error):
            with self.assertRaises(ProviderError) as caught:
                llm_http.http_post_json(
                    'https://8.8.8.8/v1/chat', {'prompt': 'test'},
                )
        message = str(caught.exception)
        self.assertNotIn('Bearer-secret', message)
        self.assertNotIn('topsecret', message)

    def test_redirect_is_not_followed_or_replayed_with_authorization(self):
        error = llm_http.urllib.error.HTTPError(
            'https://8.8.8.8/v1/chat', 302, 'Found',
            {'Location': 'http://169.254.169.254/latest/meta-data/'},
            io.BytesIO(b''),
        )
        with mock.patch.object(
                llm_http, '_open_no_redirect', side_effect=error) as opened:
            with self.assertRaises(ProviderError):
                llm_http.http_post_json(
                    'https://8.8.8.8/v1/chat', {'prompt': 'test'},
                    headers={'Authorization': 'Bearer must-not-be-forwarded'},
                )

        opened.assert_called_once()
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, 'https://8.8.8.8/v1/chat')
        self.assertEqual(
            request.get_header('Authorization'),
            'Bearer must-not-be-forwarded',
        )
        handler = llm_http._NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(
            request, None, 302, 'Found', error.headers,
            error.headers['Location'],
        ))
