# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Unit tests for the shared eh_edi_core credential vault.

The master key is sourced from a dedicated test environment variable so
the tests never touch a real key file or ir.config_parameter. The most
important test is test_for_namespace_reproduces_legacy_identifiers, which
guards the invariant that the namespace mapping reproduces the exact env
var, key file, and parameter key the standalone module vaults used. If
that mapping ever drifts, production ciphertext written by the previous
vaults would become unrecoverable.
"""

import base64
import os
import stat
import tempfile
import json
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from odoo.tests import BaseCase, tagged

from odoo.addons.eh_edi_core.tools.crypto import credential_vault
from odoo.addons.eh_edi_core.tools.crypto.credential_vault import (
    Vault,
    VaultError,
    for_namespace,
)


_TEST_ENV_VAR = 'EH_EDI_CORE_TEST_MASTER_KEY'
_TEST_KEY = b'\x01' * 32
_TEST_KEY_B64 = base64.b64encode(_TEST_KEY).decode('ascii')


class _FakeParam:
    """Minimal stand-in for ir.config_parameter so the fallback branch is
    testable without the ORM."""

    def __init__(self, value):
        self._value = value

    def sudo(self):
        return self

    def get_param(self, key, default=None):
        return self._value


class _FakeEnv:
    def __init__(self, value):
        self._param = _FakeParam(value)

    def __getitem__(self, model):
        if model != 'ir.config_parameter':
            raise KeyError(model)
        return self._param


@tagged('eh_edi_core', 'post_install', '-at_install')
class TestCredentialVault(BaseCase):

    def setUp(self):
        super().setUp()
        self._env_backup = os.environ.get(_TEST_ENV_VAR)
        os.environ[_TEST_ENV_VAR] = _TEST_KEY_B64
        # A vault whose key resolves from the test env var. The key file
        # and param key point at locations that do not exist so the env
        # branch is the only one that fires.
        self.vault = Vault(
            env_var=_TEST_ENV_VAR,
            key_file='/nonexistent/eh_edi_core_test.master.key',
            param_key='eh_edi_core_test.master_key_b64',
        )

    def tearDown(self):
        if self._env_backup is None:
            os.environ.pop(_TEST_ENV_VAR, None)
        else:
            os.environ[_TEST_ENV_VAR] = self._env_backup
        super().tearDown()

    # ---- the critical safety net ----

    def test_for_namespace_reproduces_legacy_identifiers(self):
        """The namespace mapping must reproduce, byte for byte, the env
        var, key file, and parameter key the standalone mu and fr vaults
        used; otherwise existing ciphertext becomes unrecoverable."""
        mu = for_namespace('mu_einv', 'eh_l10n_mu_einvoicing')
        self.assertEqual(mu._env_var, 'EH_MU_EINV_MASTER_KEY')
        self.assertEqual(mu._key_file, '/etc/odoo/eh_mu_einv.master.key')
        self.assertEqual(mu._param_key, 'eh_l10n_mu_einvoicing.master_key_b64')
        self.assertEqual(mu._key_id_env_var, 'EH_MU_EINV_MASTER_KEY_ID')
        self.assertEqual(mu._keyring_env_var, 'EH_MU_EINV_MASTER_KEYRING')
        self.assertEqual(
            mu._keyring_param_key,
            'eh_l10n_mu_einvoicing.master_keyring_json',
        )

        fr = for_namespace('fr_einv', 'eh_l10n_fr_einvoicing')
        self.assertEqual(fr._env_var, 'EH_FR_EINV_MASTER_KEY')
        self.assertEqual(fr._key_file, '/etc/odoo/eh_fr_einv.master.key')
        self.assertEqual(fr._param_key, 'eh_l10n_fr_einvoicing.master_key_b64')

    # ---- crypto behaviour ----

    def test_roundtrip(self):
        ct = self.vault.encrypt('hello', associated_data=b'aad:1')
        self.assertNotEqual(ct, 'hello')
        self.assertTrue(base64.b64decode(ct).startswith(b'EHCV\x01'))
        pt = self.vault.decrypt(ct, associated_data=b'aad:1')
        self.assertEqual(pt, 'hello')

    def test_versioned_key_rotation_and_reencrypt(self):
        env_var = 'EH_EDI_CORE_ROTATION_MASTER_KEY'
        id_var = env_var + '_ID'
        ring_var = env_var + '_KEYRING'
        old_key = b'\x03' * 32
        new_key = b'\x04' * 32
        backup = {name: os.environ.get(name) for name in (
            env_var, id_var, ring_var,
        )}
        try:
            os.environ[env_var] = base64.b64encode(old_key).decode('ascii')
            os.environ[id_var] = '2026-q1'
            old_vault = Vault(
                env_var=env_var,
                key_file='/nonexistent/rotation.key',
                param_key='eh_edi_core_test.rotation',
            )
            old_ciphertext = old_vault.encrypt(
                'rotate-me', associated_data='company:9',
            )

            os.environ[env_var] = base64.b64encode(new_key).decode('ascii')
            os.environ[id_var] = '2026-q2'
            os.environ[ring_var] = json.dumps({
                '2026-q1': base64.b64encode(old_key).decode('ascii'),
            })
            new_vault = Vault(
                env_var=env_var,
                key_file='/nonexistent/rotation.key',
                param_key='eh_edi_core_test.rotation',
            )
            self.assertEqual(
                new_vault.decrypt(
                    old_ciphertext, associated_data='company:9',
                ),
                'rotate-me',
            )
            rotated = new_vault.rotate_ciphertext(
                old_ciphertext, associated_data='company:9',
            )
            raw = base64.b64decode(rotated)
            self.assertIn(b'2026-q2', raw[:32])
            self.assertEqual(
                new_vault.decrypt(rotated, associated_data='company:9'),
                'rotate-me',
            )
        finally:
            for name, value in backup.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_unknown_versioned_key_id_fails_closed(self):
        ct = self.vault.encrypt('hello')
        raw = bytearray(base64.b64decode(ct))
        key_start = len(b'EHCV') + 2
        raw[key_start:key_start + len('primary')] = b'unknown'

        with self.assertRaisesRegex(VaultError, 'unknown key id'):
            self.vault.decrypt(base64.b64encode(raw).decode('ascii'))

    def test_unknown_ciphertext_version_fails_closed(self):
        raw = bytearray(base64.b64decode(self.vault.encrypt('hello')))
        raw[len(b'EHCV')] = 99

        with self.assertRaisesRegex(VaultError, 'Unsupported.*version 99'):
            self.vault.decrypt(base64.b64encode(raw).decode('ascii'))

    def test_associated_data_binding(self):
        ct = self.vault.encrypt('hello', associated_data=b'aad:1')
        with self.assertRaises(VaultError):
            self.vault.decrypt(ct, associated_data=b'aad:2')

    def test_associated_data_accepts_str(self):
        ct = self.vault.encrypt('hello', associated_data='company:7')
        self.assertEqual(self.vault.decrypt(ct, associated_data='company:7'),
                         'hello')

    def test_empty_passthrough(self):
        self.assertEqual(self.vault.encrypt(''), '')
        self.assertEqual(self.vault.encrypt(None), '')
        self.assertEqual(self.vault.decrypt(''), '')
        self.assertEqual(self.vault.decrypt(False), '')

    def test_tampered_ciphertext_raises(self):
        ct = self.vault.encrypt('secret', associated_data=b'x')
        raw = base64.b64decode(ct)
        tampered = base64.b64encode(
            raw[:-1] + bytes([raw[-1] ^ 1])
        ).decode('ascii')
        with self.assertRaises(VaultError):
            self.vault.decrypt(tampered, associated_data=b'x')

    def test_short_ciphertext_raises(self):
        with self.assertRaises(VaultError):
            self.vault.decrypt(base64.b64encode(b'tooshort').decode('ascii'))

    def test_not_base64_raises(self):
        with self.assertRaises(VaultError):
            self.vault.decrypt('not valid base64 @@@')

    def test_on_disk_format_compatibility(self):
        """A blob built independently with raw AES-256-GCM in the
        documented layout (12-byte nonce || ciphertext-with-tag, base64)
        must decrypt through the vault. This proves ciphertext written by
        the pre-refactor standalone vaults is still readable."""
        aes = AESGCM(_TEST_KEY)
        nonce = b'\x09' * 12
        body = aes.encrypt(nonce, b'legacy-secret', b'company:42')
        blob_b64 = base64.b64encode(nonce + body).decode('ascii')
        self.assertEqual(
            self.vault.decrypt(blob_b64, associated_data=b'company:42'),
            'legacy-secret',
        )

    # ---- master key resolution ----

    def test_wrong_key_length_raises(self):
        os.environ[_TEST_ENV_VAR] = base64.b64encode(b'\x02' * 16).decode()
        with self.assertRaises(VaultError):
            self.vault.encrypt('x')

    def test_not_base64_key_raises(self):
        os.environ[_TEST_ENV_VAR] = 'this is not base64 @@@'
        with self.assertRaises(VaultError):
            self.vault.encrypt('x')

    def test_is_configured_true_with_env(self):
        self.assertTrue(self.vault.is_configured())

    def test_not_configured_raises_and_reports_false(self):
        bare = Vault(
            env_var='EH_EDI_CORE_DEFINITELY_UNSET_VAR',
            key_file='/nonexistent/none.key',
            param_key='eh_edi_core_test.absent_param',
        )
        self.assertFalse(bare.is_configured())
        self.assertFalse(bare.is_configured(_FakeEnv(None)))
        with self.assertRaises(VaultError):
            bare.encrypt('x')

    def test_config_parameter_fallback(self):
        bare = Vault(
            env_var='EH_EDI_CORE_DEFINITELY_UNSET_VAR',
            key_file='/nonexistent/none.key',
            param_key='eh_edi_core_test.master_key_b64',
        )
        env = _FakeEnv(_TEST_KEY_B64)
        self.assertTrue(bare.is_configured(env))
        ct = bare.encrypt('viafallback', env=env, associated_data=b'a')
        self.assertEqual(bare.decrypt(ct, env=env, associated_data=b'a'),
                         'viafallback')

    def test_config_parameter_warning_is_emitted_once_per_vault(self):
        bare = Vault(
            env_var='EH_EDI_CORE_DEFINITELY_UNSET_VAR',
            key_file='/nonexistent/none.key',
            param_key='eh_edi_core_test.master_key_b64',
        )
        env = _FakeEnv(_TEST_KEY_B64)

        with patch.object(credential_vault._logger, 'warning') as warning:
            ciphertext = bare.encrypt('one', env=env)
            bare.decrypt(ciphertext, env=env)
            bare.is_configured(env)

        warning.assert_called_once()

    def test_key_file_permission_enforced(self):
        unset_var = 'EH_EDI_CORE_DEFINITELY_UNSET_VAR'
        os.environ.pop(unset_var, None)
        fd, path = tempfile.mkstemp(prefix='eh_edi_core_test_', suffix='.key')
        try:
            with os.fdopen(fd, 'w') as fh:
                fh.write(_TEST_KEY_B64)
            bare = Vault(
                env_var=unset_var,
                key_file=path,
                param_key='eh_edi_core_test.unused',
            )
            # Loose perms must be refused.
            os.chmod(path, 0o644)
            with self.assertRaises(VaultError):
                bare.encrypt('x')
            # 0600 must be accepted and round-trip.
            os.chmod(path, 0o600)
            ct = bare.encrypt('fromfile', associated_data=b'a')
            self.assertEqual(bare.decrypt(ct, associated_data=b'a'),
                             'fromfile')
        finally:
            os.remove(path)

    def test_module_exposes_public_api(self):
        """The module exposes the names the per-module shims re-export."""
        self.assertTrue(hasattr(credential_vault, 'for_namespace'))
        self.assertTrue(hasattr(credential_vault, 'Vault'))
        self.assertTrue(hasattr(credential_vault, 'VaultError'))
