# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Live connector cron isolation.

cron_fetch_due_profiles runs every active profile through the shared
eh.cron.batch.mixin so one bank's failure rolls back only that profile
and the run continues. Before the mixin the bare savepoint still let an
unexpected error propagate and abort the whole cron.
"""

from datetime import date
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_bank_statement_import.connectors.base import (
    BankTransaction,
    ConnectorError,
)
from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_bank_statement_import', 'integration', 'post_install',
        '-at_install')
class TestLiveConnectorCronIsolation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Profile = cls.env['eh.bank.live.connector.profile']

    def _make_profile(self, name, code):
        # One profile per journal (unique_journal constraint), so each
        # profile gets its own fresh bank journal.
        journal = self.env['account.journal'].create({
            'name': name + ' Bank',
            'code': code,
            'type': 'bank',
            'company_id': self.env.company.id,
        })
        if ('suspense_account_id' in journal._fields
                and not journal.suspense_account_id):
            suspense = EhAccountIntegrationTestCase._ensure_account(
                self.env, '1099', 'Bank Suspense', 'asset_current',
            )
            if not suspense.reconcile:
                suspense.reconcile = True
            journal.suspense_account_id = suspense
        return self.Profile.create({
            'name': name,
            'journal_id': journal.id,
            'company_id': self.env.company.id,
            'connector_key': 'manual',
            'active': True,
        })

    def test_credential_fields_build_json(self):
        import json
        p = self._make_profile('Creds Plaid', 'LVCJ')
        p.connector_key = 'plaid'
        p.write({
            'cred_client_id': 'cid-1', 'cred_secret': 'sec-1',
            'cred_access_token': 'tok-1', 'cred_account_id': 'acc-1',
            'cred_environment': 'sandbox',
        })
        data = json.loads(p.credentials_json)
        self.assertEqual(data['client_id'], 'cid-1')
        self.assertEqual(data['secret'], 'sec-1')
        self.assertEqual(data['access_token'], 'tok-1')
        self.assertEqual(data['account_id'], 'acc-1')
        self.assertEqual(data['environment'], 'sandbox')

    def test_advanced_credentials_editor_uses_masked_char_widget(self):
        field = self.Profile._fields['advanced_credentials_json']
        self.assertEqual(field.type, 'char')
        view = self.env.ref(
            'eh_account_bank_statement_import.'
            'view_eh_bank_live_connector_profile_form',
        )
        arch = etree.fromstring(view.arch_db.encode())
        editors = arch.xpath(
            ".//field[@name='advanced_credentials_json'][@password='True']"
        )
        self.assertEqual(len(editors), 1)
        self.assertFalse(editors[0].get('widget'))
        self.assertFalse(arch.xpath(".//field[@name='credentials_json']"))

    def test_live_materialise_validates_currency_and_rounds_amount(self):
        profile = self._make_profile('Currency proof', 'LVCR')
        currency = (
            profile.journal_id.currency_id or profile.company_id.currency_id
        )
        valid = BankTransaction(
            provider_reference='LIVE-ROUND-1',
            posted_date=date(2026, 8, 5),
            amount=1.005,
            currency=currency.name,
            description='Rounded live transaction',
        )
        count, latest = profile._materialise([valid])
        self.assertEqual(count, 1)
        self.assertEqual(latest, valid.posted_date)
        line = self.env['account.bank.statement.line'].search([
            ('eh_provider_reference', '=', valid.provider_reference),
        ])
        self.assertEqual(line.amount, currency.round(valid.amount))

        other_code = 'EUR' if currency.name != 'EUR' else 'USD'
        invalid = BankTransaction(
            provider_reference='LIVE-WRONG-CURRENCY',
            posted_date=date(2026, 8, 6),
            amount=20.0,
            currency=other_code,
        )
        with self.assertRaises(ConnectorError):
            profile._materialise([invalid])
        self.assertFalse(self.env['account.bank.statement.line'].search([
            ('eh_provider_reference', '=', invalid.provider_reference),
        ]))

    def test_live_materialise_rejects_nonfinite_amount(self):
        profile = self._make_profile('Finite proof', 'LVFN')
        currency = (
            profile.journal_id.currency_id or profile.company_id.currency_id
        )
        invalid = BankTransaction(
            provider_reference='LIVE-INFINITY',
            posted_date=date(2026, 8, 6),
            amount=float('inf'),
            currency=currency.name,
        )
        with self.assertRaises(ConnectorError):
            profile._materialise([invalid])

    def test_live_and_file_import_share_exact_bank_reference(self):
        profile = self._make_profile('Cross channel proof', 'LVXC')
        currency = (
            profile.journal_id.currency_id or profile.company_id.currency_id
        )
        Line = self.env['account.bank.statement.line']
        source_ref = 'CROSS-CHANNEL-001'
        canonical = Line._eh_canonical_import_ref(
            profile.journal_id.id, source_ref,
        )
        existing = Line.create({
            'journal_id': profile.journal_id.id,
            'date': date(2026, 8, 7),
            'amount': currency.round(42.0),
            'payment_ref': 'Imported from MT940 first',
            'unique_import_ref': canonical,
        })
        transaction = BankTransaction(
            provider_reference=source_ref,
            posted_date=date(2026, 8, 7),
            amount=42.0,
            currency=currency.name,
            description='Fetched from API later',
        )

        count, latest = profile._materialise([transaction])

        self.assertEqual((count, latest), (0, transaction.posted_date))
        self.assertEqual(Line.search_count([
            ('journal_id', '=', profile.journal_id.id),
            '|', ('id', '=', existing.id),
            ('eh_provider_reference', '=', source_ref),
        ]), 1)

        new_transaction = BankTransaction(
            provider_reference='CROSS-CHANNEL-002',
            posted_date=date(2026, 8, 8),
            amount=50.0,
            currency=currency.name,
        )
        count, latest = profile._materialise([new_transaction])
        self.assertEqual((count, latest), (1, new_transaction.posted_date))
        live_line = Line.search([
            ('eh_provider_reference', '=', 'CROSS-CHANNEL-002'),
        ])
        self.assertEqual(
            live_line.unique_import_ref,
            Line._eh_canonical_import_ref(
                profile.journal_id.id, 'CROSS-CHANNEL-002',
            ),
        )

    def test_credential_fields_parse_from_json(self):
        p = self._make_profile('Creds Basiq', 'LVCB')
        p.connector_key = 'basiq'
        p.credentials_json = '{"api_key": "k-9", "user_id": "u-9", "account_id": "a-9"}'
        p.invalidate_recordset()
        self.assertEqual(p.cred_api_key, 'k-9')
        self.assertEqual(p.cred_user_id, 'u-9')
        self.assertEqual(p.cred_account_id, 'a-9')

    def test_clearing_credential_field_drops_key(self):
        import json
        p = self._make_profile('Creds Clear', 'LVCC')
        p.connector_key = 'plaid'
        p.write({'cred_client_id': 'x', 'cred_secret': 'y'})
        p.write({'cred_secret': False})
        data = json.loads(p.credentials_json)
        self.assertIn('client_id', data)
        self.assertNotIn('secret', data)

    def test_cron_fetch_isolates_one_bad_profile(self):
        p1 = self._make_profile('Profile A', 'LVB1')
        p2 = self._make_profile('Profile B', 'LVB2')
        Klass = type(self.Profile)
        calls = []

        def flaky(profile):
            calls.append(profile.id)
            if profile.id == p1.id:
                raise ValueError("forced failure")
            # p2: succeed without reaching a real connector.

        with patch.object(Klass, '_run_fetch', flaky):
            self.Profile.cron_fetch_due_profiles()

        self.assertIn(p1.id, calls)
        self.assertIn(
            p2.id, calls,
            "second profile must be reached despite the first failing",
        )

    def test_core_user_error_is_reported_and_stops_cron_retries(self):
        profile = self._make_profile('Core validation', 'LVCV')
        currency = (
            profile.journal_id.currency_id or profile.company_id.currency_id
        )
        transaction = BankTransaction(
            provider_reference='CORE-ERROR-1',
            posted_date=date(2026, 8, 7),
            amount=10.0,
            currency=currency.name,
        )
        connector = patch(
            'odoo.addons.eh_account_bank_statement_import.models.'
            'live_connector_profile.get_connector'
        ).start()
        self.addCleanup(patch.stopall)
        connector.return_value.authenticate.return_value = object()
        connector.return_value.fetch_transactions.return_value = [transaction]
        Klass = type(profile)
        with patch.object(
            Klass, '_materialise', autospec=True,
            side_effect=UserError('Journal configuration needs review.'),
        ):
            profile._run_fetch()
        profile.invalidate_recordset()
        self.assertFalse(profile.active)
        self.assertEqual(profile.last_run_state, 'error')
        self.assertIn('Journal configuration needs review',
                      profile.last_run_message)

    def test_profile_company_must_match_bank_journal(self):
        other = self.env['res.company'].create({'name': 'Live Feed Other Co'})
        journal = self.env['account.journal'].create({
            'name': 'Other Bank', 'code': 'LVOB', 'type': 'bank',
            'company_id': other.id,
        })
        with self.assertRaises(ValidationError):
            self.Profile.create({
                'name': 'Cross-company feed',
                'company_id': self.env.company.id,
                'journal_id': journal.id,
                'connector_key': 'manual',
            })

    def test_archived_profile_does_not_block_active_replacement(self):
        archived = self._make_profile('Archived feed', 'LVAR')
        archived.write({'active': False})
        archived.flush_recordset(['active'])
        self.assertFalse(self.Profile.with_context(active_test=False).search([
            ('journal_id', '=', archived.journal_id.id),
            ('active', '=', True),
        ]))
        replacement = self.Profile.create({
            'name': 'Replacement feed',
            'company_id': archived.company_id.id,
            'journal_id': archived.journal_id.id,
            'connector_key': 'manual',
            'active': True,
        })
        self.assertTrue(replacement.active)
        with self.assertRaisesRegex(
            ValidationError, 'one active live connector profile'
        ):
            self.Profile.create({
                'name': 'Second active feed',
                'company_id': archived.company_id.id,
                'journal_id': archived.journal_id.id,
                'connector_key': 'manual',
                'active': True,
            })

    def test_legacy_bad_scope_is_quarantined_before_connector_lookup(self):
        profile = self._make_profile('Legacy malformed', 'LVLM')
        other = self.env['res.company'].create({'name': 'Legacy Feed Other'})
        journal = self.env['account.journal'].create({
            'name': 'Legacy Other Bank', 'code': 'LVLO', 'type': 'bank',
            'company_id': other.id,
        })
        # Simulate a pre-constraint legacy row without asking ORM validation to
        # bless it. Cron must disable it before connector lookup/network I/O.
        self.env.cr.execute(
            "UPDATE eh_bank_live_connector_profile SET journal_id = %s "
            "WHERE id = %s", [journal.id, profile.id])
        profile.invalidate_recordset()
        with patch(
                'odoo.addons.eh_account_bank_statement_import.models.'
                'live_connector_profile.get_connector') as connector:
            self.Profile.cron_fetch_due_profiles()
        connector.assert_not_called()
        profile.invalidate_recordset()
        self.assertFalse(profile.active)
        self.assertEqual(profile.last_run_state, 'error')
        self.assertNotIn('credential', profile.last_run_message.lower())

    def test_upgrade_quarantines_without_rewriting_legacy_credentials(self):
        profile = self._make_profile('Legacy credential payload', 'LVCP')
        # A pre-hardening row could contain valid JSON of the wrong shape.
        # Keep the exact payload as evidence; upgrade may disable the profile
        # but must not invent replacement credentials.
        self.env.cr.execute(
            "UPDATE eh_bank_live_connector_profile "
            "SET credentials_json = '[]' WHERE id = %s",
            [profile.id],
        )
        migration_path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.3.3' / 'post-migration.py'
        )
        spec = spec_from_file_location(
            'eh_bank_live_profile_post_migration', migration_path)
        migration = module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.migrate(self.env.cr, '19.0.1.3.2')
        profile.invalidate_recordset()
        self.assertFalse(profile.active)
        self.assertEqual(profile.credentials_json, '[]')
        self.assertEqual(profile.last_run_state, 'error')

    def test_unauthorised_caller_is_denied_before_connector_lookup(self):
        other = self.env['res.company'].create({'name': 'Private Feed Co'})
        journal = self.env['account.journal'].create({
            'name': 'Private Feed Bank', 'code': 'LVPF', 'type': 'bank',
            'company_id': other.id,
        })
        profile = self.Profile.create({
            'name': 'Private feed', 'company_id': other.id,
            'journal_id': journal.id, 'connector_key': 'manual',
        })
        user = self.env['res.users'].create({
            'name': 'Scoped Bank Manager', 'login': 'scoped_bank_manager',
            'company_id': self.env.company.id,
            'company_ids': [(6, 0, self.env.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_manager').id,
            ])],
        })
        with patch(
                'odoo.addons.eh_account_bank_statement_import.models.'
                'live_connector_profile.get_connector') as connector:
            with self.assertRaises(AccessError):
                profile.with_user(user).action_fetch_now()
        connector.assert_not_called()
