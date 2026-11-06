# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
AI provider registry + stub tests.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_ai_agent.tools import (
    provider_registry, provider_stubs,
)
from odoo.addons.eh_account_ai_agent.tools.provider_registry import (
    ProviderError,
)


@tagged('post_install', '-at_install')
class ProviderRegistrationTest(TransactionCase):

    def test_four_stubs_registered(self):
        keys = provider_registry.list_providers()
        for required in ('manual', 'claude', 'openai', 'local'):
            self.assertIn(required, keys)

    def test_unknown_key_raises(self):
        with self.assertRaises(ProviderError):
            provider_registry.get_provider('not_a_real_provider', {})

    def test_has_provider_returns_bool(self):
        self.assertTrue(provider_registry.has_provider('manual'))
        self.assertFalse(provider_registry.has_provider('nope'))


@tagged('post_install', '-at_install')
class ManualProviderTest(TransactionCase):

    def test_is_manual_flag_set(self):
        p = provider_registry.get_provider('manual', {})
        self.assertTrue(p.is_manual)

    def test_chat_raises(self):
        p = provider_registry.get_provider('manual', {})
        with self.assertRaises(ProviderError):
            p.chat([{'role': 'user', 'content': 'hi'}])


# The stub-behaviour tests below construct the stub provider classes
# directly rather than resolving through the registry. A paid adapter
# module (eh_account_ai_agent_claude / _openai / _local) re-registers
# the same key with a live implementation, so going through
# provider_registry.get_provider would return the live provider when an
# adapter is co-installed. Building the stub class explicitly keeps
# these tests focused on the stub contract and green regardless of
# which adapter modules are present in the database.
@tagged('post_install', '-at_install')
class ClaudeStubTest(TransactionCase):

    def test_empty_config_rejected(self):
        with self.assertRaises(ProviderError) as cm:
            provider_stubs.ClaudeProvider({})
        self.assertIn('api_key', str(cm.exception))

    def test_missing_model_rejected(self):
        with self.assertRaises(ProviderError):
            provider_stubs.ClaudeProvider({'api_key': 'x'})

    def test_happy_config_chat_refuses(self):
        p = provider_stubs.ClaudeProvider({
            'api_key': 'x', 'model': 'claude-opus-4-7',
        })
        self.assertFalse(p.is_manual)
        with self.assertRaises(ProviderError) as cm:
            p.chat([])
        self.assertIn('extension module', str(cm.exception))

    def test_json_string_config_accepted(self):
        p = provider_stubs.ClaudeProvider(
            '{"api_key": "x", "model": "claude-opus-4-7"}',
        )
        self.assertEqual(p.config['model'], 'claude-opus-4-7')


@tagged('post_install', '-at_install')
class OpenAiStubTest(TransactionCase):

    def test_happy_config_chat_refuses(self):
        p = provider_stubs.OpenAiProvider({
            'api_key': 'x', 'model': 'gpt-5',
        })
        with self.assertRaises(ProviderError) as cm:
            p.chat([])
        self.assertIn('extension module', str(cm.exception))


@tagged('post_install', '-at_install')
class LocalStubTest(TransactionCase):

    def test_endpoint_only_validates(self):
        p = provider_stubs.LocalProvider(
            {'endpoint': 'http://127.0.0.1:11434'},
        )
        self.assertEqual(p.config['endpoint'], 'http://127.0.0.1:11434')

    def test_model_only_validates(self):
        p = provider_stubs.LocalProvider({'model': 'qwen3-8b'})
        self.assertEqual(p.config['model'], 'qwen3-8b')

    def test_empty_rejected(self):
        with self.assertRaises(ProviderError) as cm:
            provider_stubs.LocalProvider({})
        self.assertIn('endpoint', str(cm.exception))
