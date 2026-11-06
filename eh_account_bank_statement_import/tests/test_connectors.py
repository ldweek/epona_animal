# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Connector registration + credentials validation tests.

The framework already covers profile / fetch / dedup. These tests
confirm the three shipped stubs (manual, plaid, basiq) register, that
Plaid and Basiq enforce their credentials shape, and that the manual
stub is a deliberate no-op so the cron exits cleanly.
"""

import datetime
import unittest
import urllib.error
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_bank_statement_import.connectors import (
    registry, manual, plaid, basiq, gocardless, http_util,
)
from odoo.addons.eh_account_bank_statement_import.connectors.base import (
    ConnectorError, LiveBankConnector,
)


class _FakeProfile:
    """Plain-Python passthrough mimicking the profile contract."""

    def __init__(self, name='test', credentials_json='{}', credentials=None):
        self.name = name
        self.credentials_json = credentials_json
        if credentials is not None:
            self.credentials = credentials


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit=-1):
        return self.payload if limit < 0 else self.payload[:limit]


@tagged('post_install', '-at_install')
class HttpUtilTest(TransactionCase):

    def test_response_limit_remains_a_clear_connector_error(self):
        with patch.object(http_util, '_MAX_RESPONSE_BYTES', 4), patch.object(
                http_util.urllib.request, 'urlopen',
                return_value=_FakeResponse(b'12345')):
            with self.assertRaises(ConnectorError) as raised:
                http_util.request_json('GET', 'https://bank.invalid/feed')
        self.assertIn('exceeded', str(raised.exception))

    def test_transport_error_redacts_url_and_error_secrets(self):
        url = 'https://bank.invalid/feed?access_token=url-secret'
        reason = 'api_key=reason-secret'
        with patch.object(
                http_util.urllib.request, 'urlopen',
                side_effect=urllib.error.URLError(reason)):
            with self.assertRaises(ConnectorError) as raised:
                http_util.request_json('GET', url)
        message = str(raised.exception)
        self.assertNotIn('url-secret', message)
        self.assertNotIn('reason-secret', message)


@tagged('post_install', '-at_install')
class ConnectorRegistrationTest(TransactionCase):

    def test_three_stubs_registered(self):
        keys = [k for k, _ in registry.connector_choices()]
        for required in ('manual', 'plaid', 'basiq'):
            self.assertIn(required, keys)

    def test_get_connector_returns_instance(self):
        # The manual stub is never overridden, so it always resolves to
        # the shipped class. A paid adapter (eh_account_bank_statement_
        # import_plaid / _basiq) re-registers the 'plaid' / 'basiq' keys
        # with a live connector, so assert those resolve to a
        # LiveBankConnector bearing the key rather than the stub class.
        m = registry.get_connector('manual')
        self.assertIsInstance(m, manual.ManualConnector)
        for key in ('plaid', 'basiq'):
            conn = registry.get_connector(key)
            self.assertIsInstance(conn, LiveBankConnector)
            self.assertEqual(conn.CONNECTOR_KEY, key)

    def test_get_connector_raises_for_unknown_key(self):
        with self.assertRaises(KeyError):
            registry.get_connector('nope_does_not_exist')


@tagged('post_install', '-at_install')
class ManualConnectorTest(TransactionCase):

    def test_authenticate_returns_empty(self):
        m = manual.ManualConnector()
        self.assertEqual(m.authenticate(_FakeProfile()), {})

    def test_fetch_yields_nothing(self):
        m = manual.ManualConnector()
        result = list(m.fetch_transactions(
            _FakeProfile(),
            datetime.date(2026, 1, 1),
            datetime.date(2026, 1, 31),
            {},
        ))
        self.assertEqual(result, [])


@tagged('post_install', '-at_install')
class PlaidConnectorTest(TransactionCase):

    def test_empty_credentials_rejected(self):
        p = plaid.PlaidConnector()
        with self.assertRaises(ConnectorError) as cm:
            p.authenticate(_FakeProfile())
        msg = str(cm.exception)
        self.assertIn('client_id', msg)
        self.assertIn('secret', msg)

    def test_invalid_json_rejected(self):
        p = plaid.PlaidConnector()
        with self.assertRaises(ConnectorError) as cm:
            p.authenticate(_FakeProfile(credentials_json='{not json'))
        self.assertIn('not valid JSON', str(cm.exception))

    def test_happy_credentials_default_sandbox(self):
        p = plaid.PlaidConnector()
        creds = p.authenticate(_FakeProfile(credentials_json=(
            '{"client_id":"a","secret":"b","access_token":"c","account_id":"d"}'
        )))
        self.assertEqual(creds['environment'], 'sandbox')
        self.assertEqual(creds['client_id'], 'a')

    def test_runtime_credentials_mapping_is_honoured(self):
        creds = plaid.PlaidConnector().authenticate(_FakeProfile(credentials={
            'client_id': 'a', 'secret': 'b', 'access_token': 'c',
            'account_id': 'd',
        }))
        self.assertEqual(creds['client_id'], 'a')
        self.assertEqual(creds['environment'], 'sandbox')

    def test_explicit_environment_honoured(self):
        p = plaid.PlaidConnector()
        creds = p.authenticate(_FakeProfile(credentials_json=(
            '{"client_id":"a","secret":"b","access_token":"c","account_id":"d","environment":"production"}'
        )))
        self.assertEqual(creds['environment'], 'production')

    def test_invalid_environment_rejected(self):
        p = plaid.PlaidConnector()
        with self.assertRaises(ConnectorError) as cm:
            p.authenticate(_FakeProfile(credentials_json=(
                '{"client_id":"a","secret":"b","access_token":"c","account_id":"d","environment":"live"}'
            )))
        self.assertIn('sandbox', str(cm.exception))

    def test_fetch_refuses_loudly(self):
        p = plaid.PlaidConnector()
        creds = p.authenticate(_FakeProfile(credentials_json=(
            '{"client_id":"a","secret":"b","access_token":"c","account_id":"d"}'
        )))
        with self.assertRaises(ConnectorError) as cm:
            list(p.fetch_transactions(
                _FakeProfile(), datetime.date(2026, 1, 1),
                datetime.date(2026, 1, 31), creds,
            ))
        self.assertIn('extension module', str(cm.exception))


@tagged('post_install', '-at_install')
class BasiqConnectorTest(TransactionCase):

    def test_empty_credentials_rejected(self):
        b = basiq.BasiqConnector()
        with self.assertRaises(ConnectorError) as cm:
            b.authenticate(_FakeProfile())
        self.assertIn('api_key', str(cm.exception))

    def test_happy_credentials_default_sandbox(self):
        b = basiq.BasiqConnector()
        creds = b.authenticate(_FakeProfile(credentials_json=(
            '{"api_key":"k","user_id":"u","account_id":"a"}'
        )))
        self.assertEqual(creds['environment'], 'sandbox')

    def test_runtime_credentials_mapping_is_honoured(self):
        creds = basiq.BasiqConnector().authenticate(_FakeProfile(credentials={
            'api_key': 'k', 'user_id': 'u', 'account_id': 'a',
        }))
        self.assertEqual(creds['api_key'], 'k')
        self.assertEqual(creds['environment'], 'sandbox')

    def test_invalid_environment_rejected(self):
        b = basiq.BasiqConnector()
        with self.assertRaises(ConnectorError) as cm:
            b.authenticate(_FakeProfile(credentials_json=(
                '{"api_key":"k","user_id":"u","account_id":"a","environment":"dev"}'
            )))
        self.assertIn('sandbox', str(cm.exception))

    def test_fetch_refuses_loudly(self):
        b = basiq.BasiqConnector()
        creds = b.authenticate(_FakeProfile(credentials_json=(
            '{"api_key":"k","user_id":"u","account_id":"a"}'
        )))
        with self.assertRaises(ConnectorError) as cm:
            list(b.fetch_transactions(
                _FakeProfile(), datetime.date(2026, 1, 1),
                datetime.date(2026, 1, 31), creds,
            ))
        self.assertIn('extension module', str(cm.exception))


@tagged('post_install', '-at_install')
class GoCardlessConnectorTest(TransactionCase):

    def test_runtime_credentials_mapping_is_honoured(self):
        creds = gocardless.GoCardlessConnector().authenticate(
            _FakeProfile(
                credentials_json='{}',
                credentials={
                    'secret_id': 'id', 'secret_key': 'key',
                    'account_id': 'account',
                },
            )
        )
        self.assertEqual(creds, {
            'secret_id': 'id', 'secret_key': 'key',
            'account_id': 'account',
        })
