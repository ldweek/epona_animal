# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
LLM extractor stub tests.

Verifies that the three shipped stubs (claude_llm, openai_llm,
local_llm) register, validate credentials, and refuse live calls
with a clear message pointing to the extension module.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_ap_automation.extractors import (
    registry, llm_stub,
)
from odoo.addons.eh_account_ap_automation.extractors.base import (
    ExtractorError, LineItemExtractor,
)


@tagged('post_install', '-at_install')
class ExtractorRegistrationTest(TransactionCase):

    def test_three_stubs_registered(self):
        keys = [k for k, _ in registry.extractor_choices()]
        for required in ('claude_llm', 'openai_llm', 'local_llm'):
            self.assertIn(required, keys)

    def test_get_returns_instance(self):
        # A provider adapter re-registers the same key with a live extractor,
        # so we
        # assert the registry resolves each key to a LineItemExtractor
        # bearing that key rather than to a specific stub class.
        for key in ('claude_llm', 'openai_llm', 'local_llm'):
            ext = registry.get_extractor(key)
            self.assertIsInstance(ext, LineItemExtractor)
            self.assertEqual(ext.EXTRACTOR_KEY, key)

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            registry.get_extractor('not_a_real_extractor')

    def test_has_extractor_returns_bool(self):
        self.assertTrue(registry.has_extractor('claude_llm'))
        self.assertFalse(registry.has_extractor('nope'))


@tagged('post_install', '-at_install')
class ClaudeStubTest(TransactionCase):

    def test_empty_credentials_rejected(self):
        ext = llm_stub.ClaudeLlmExtractor()
        with self.assertRaises(ExtractorError) as cm:
            list(ext.extract(b'', 'application/pdf', {}))
        self.assertIn('api_key', str(cm.exception))
        self.assertIn('model', str(cm.exception))

    def test_invalid_json_rejected(self):
        ext = llm_stub.ClaudeLlmExtractor()
        with self.assertRaises(ExtractorError) as cm:
            list(ext.extract(b'', 'application/pdf', {
                'credentials_json': '{not json',
            }))
        self.assertIn('not valid JSON', str(cm.exception))

    def test_missing_model_rejected(self):
        ext = llm_stub.ClaudeLlmExtractor()
        with self.assertRaises(ExtractorError) as cm:
            list(ext.extract(b'', 'application/pdf', {
                'credentials_json': '{"api_key": "a"}',
            }))
        self.assertIn('model', str(cm.exception))

    def test_happy_credentials_refuse_live_call(self):
        ext = llm_stub.ClaudeLlmExtractor()
        with self.assertRaises(ExtractorError) as cm:
            list(ext.extract(b'', 'application/pdf', {
                'credentials_json':
                    '{"api_key": "a", "model": "claude-opus-4-7"}',
            }))
        self.assertIn("extractor key 'claude_llm'", str(cm.exception))

    def test_requires_rasterisation(self):
        ext = llm_stub.ClaudeLlmExtractor()
        self.assertTrue(ext.REQUIRES_RASTERISATION)


@tagged('post_install', '-at_install')
class OpenAiStubTest(TransactionCase):

    def test_empty_credentials_rejected(self):
        ext = llm_stub.OpenAiLlmExtractor()
        with self.assertRaises(ExtractorError) as cm:
            list(ext.extract(b'', 'application/pdf', {}))
        self.assertIn('api_key', str(cm.exception))

    def test_happy_credentials_refuse_live_call(self):
        ext = llm_stub.OpenAiLlmExtractor()
        with self.assertRaises(ExtractorError) as cm:
            list(ext.extract(b'', 'application/pdf', {
                'credentials_json':
                    '{"api_key": "a", "model": "gpt-5"}',
            }))
        self.assertIn("extractor key 'openai_llm'", str(cm.exception))


@tagged('post_install', '-at_install')
class LocalLlmStubTest(TransactionCase):

    def test_empty_credentials_rejected(self):
        ext = llm_stub.LocalLlmExtractor()
        with self.assertRaises(ExtractorError) as cm:
            list(ext.extract(b'', 'application/pdf', {}))
        self.assertIn('endpoint', str(cm.exception))

    def test_endpoint_only_validates(self):
        ext = llm_stub.LocalLlmExtractor()
        with self.assertRaises(ExtractorError) as cm:
            list(ext.extract(b'', 'application/pdf', {
                'credentials_json':
                    '{"endpoint": "http://127.0.0.1:11434"}',
            }))
        # Validation passes; the only error left is the
        # install-the-extension message.
        self.assertIn("extractor key 'local_llm'", str(cm.exception))

    def test_model_only_validates(self):
        ext = llm_stub.LocalLlmExtractor()
        with self.assertRaises(ExtractorError) as cm:
            list(ext.extract(b'', 'application/pdf', {
                'credentials_json': '{"model": "qwen3-8b-instruct"}',
            }))
        self.assertIn("extractor key 'local_llm'", str(cm.exception))
