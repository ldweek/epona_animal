# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
End to end import wizard tests.

Drives the wizard through a full upload, verifies the statement and its
lines are materialised, and exercises the idempotent reimport guard
(uploading the same file twice produces one statement, not two).
"""

import base64
import hashlib
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_bank_statement_import', 'integration', 'post_install', '-at_install')
class TestImportWizard(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['eh.account.bank.statement.import.wizard']
        cls.Profile = cls.env['eh.account.bank.statement.import.profile']
        cls.Statement = cls.env['account.bank.statement']
        cls.Log = cls.env['eh.account.bank.statement.import.log']

        cls.bank_journal = cls.env['account.journal'].search(
            [('type', '=', 'bank'),
             ('company_id', '=', cls.env.company.id)],
            limit=1,
        )
        if not cls.bank_journal:
            cls.bank_journal = cls.env['account.journal'].create({
                'name': 'Test Bank',
                'code': 'TBK',
                'type': 'bank',
                'company_id': cls.env.company.id,
            })

        # Keep the profile currency aligned with the journal's effective
        # currency (its own currency, or the company currency when blank)
        # so the wizard's currency guard does not reject the happy-path
        # fixtures. The mismatch case is covered by its own test below.
        cls.journal_currency = (
            cls.bank_journal.currency_id
            or cls.bank_journal.company_id.currency_id
        )

        cls.profile = cls.Profile.create({
            'name': 'Test profile',
            'journal_id': cls.bank_journal.id,
            'currency_code': cls.journal_currency.name,
            'csv_delimiter': ',',
            'csv_quotechar': '"',
            'csv_encoding': 'utf-8',
            'csv_header_rows': 1,
            'decimal_separator': '.',
            'date_format': '%Y-%m-%d',
            'col_date': 0,
            'col_amount': 1,
            'col_ref': 2,
            'col_narration': 3,
        })

    def _csv_bytes(self, body):
        return body.encode('utf-8')

    def _make_wizard(self, content):
        return self.Wizard.create({
            'journal_id': self.bank_journal.id,
            'profile_id': self.profile.id,
            'format_key': 'csv',
            'file_data': base64.b64encode(content),
            'filename': 'test.csv',
        })

    # ---- happy path ----

    def test_import_creates_statement_and_lines(self):
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-04-15,150.00,INV-100,Sale to customer\n"
            "2026-04-16,-25.50,FEE,Bank service charge\n"
            "2026-04-17,300.00,INV-101,Sale to other\n",
        )
        wizard = self._make_wizard(content)
        action = wizard.action_import()
        self.assertEqual(action['res_model'], 'account.bank.statement')
        statement = self.Statement.browse(action['res_id'])
        self.assertEqual(len(statement.line_ids), 3)
        amounts = sorted(statement.line_ids.mapped('amount'))
        self.assertEqual(amounts, [-25.50, 150.00, 300.00])

    def test_unmatched_counterparty_name_is_retained(self):
        parsed = {
            'statement_date': '2026-04-18',
            'currency_code': self.journal_currency.name,
            'lines': [{
                'date': '2026-04-18', 'amount': 19.0,
                'payment_ref': 'UNKNOWN-COUNTERPARTY',
                'narration': 'Counterparty supplied by bank',
                'partner_name': 'No Such Partner 9f3d2',
                'unique_import_ref': 'UNMATCHED-PARTNER-1',
            }],
        }
        statement, count, skipped = self._make_wizard(
            self._csv_bytes('x')
        )._materialise(parsed)
        self.assertEqual((count, skipped), (1, 0))
        self.assertFalse(statement.line_ids.partner_id)
        self.assertEqual(
            statement.line_ids.partner_name, 'No Such Partner 9f3d2')

    def test_profile_defaults_to_its_journal_and_rejects_cross_company(self):
        wizard = self.Wizard.new({'profile_id': self.profile.id})
        wizard._onchange_profile_id()
        self.assertEqual(wizard.journal_id, self.bank_journal)

        other = self.env['res.company'].create({'name': 'Import Profile Other'})
        other_journal = self.env['account.journal'].create({
            'name': 'Other Profile Bank', 'code': 'OPBK', 'type': 'bank',
            'company_id': other.id,
        })
        with self.assertRaisesRegex(ValidationError, 'profile company'):
            self.Profile.create({
                'name': 'Invalid cross-company profile',
                'company_id': self.company.id,
                'journal_id': other_journal.id,
            })

    def test_import_log_records_done(self):
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-04-20,42.00,X,Y\n",
        )
        wizard = self._make_wizard(content)
        wizard.action_import()
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id)],
            order='id desc', limit=1,
        )
        self.assertEqual(log.state, 'done')
        self.assertEqual(log.line_count, 1)
        self.assertTrue(log.statement_id)
        self.assertEqual(log.statement_ids, log.statement_id)
        self.assertTrue(log.file_hash)
        self.assertEqual(log.imported_by_id, self.env.user)
        self.assertEqual(len(log.source_attachment_ids), 1)
        source = log.source_attachment_ids
        self.assertEqual(source.res_model, 'account.bank.statement')
        self.assertEqual(source.res_id, log.statement_id.id)
        self.assertEqual(base64.b64decode(source.datas), content)
        with self.assertRaises(UserError):
            source.sudo().write({'name': 'replaced.csv'})
        with self.assertRaises(UserError):
            source.sudo().unlink()
        ordinary_user = self.env['res.users'].create({
            'name': 'Bank Import Audit Forger',
            'login': 'bank_import_audit_forger_test',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        with self.assertRaises(AccessError):
            self.Log.with_user(ordinary_user).create({
                'journal_id': self.bank_journal.id,
                'company_id': self.company.id,
                'format_key': 'csv',
                'state': 'done',
                'filename': 'forged.csv',
            })
        with self.assertRaises(UserError):
            log.write({'state': 'error'})
        with self.assertRaises(UserError):
            log.sudo().unlink()

    def test_multi_statement_payload_is_atomic_and_keeps_exact_source(self):
        content = self._csv_bytes('multi-statement-exact-source')
        wizard = self._make_wizard(content)
        parsed = {
            'statements': [
                {
                    'source_statement_id': 'CAMT-A',
                    'statement_date': '2026-04-20',
                    'currency_code': self.journal_currency.name,
                    'lines': [{
                        'date': '2026-04-20', 'amount': 10.0,
                        'payment_ref': 'A', 'unique_import_ref': 'MULTI-A',
                    }],
                },
                {
                    'source_statement_id': 'CAMT-B',
                    'statement_date': '2026-04-21',
                    'currency_code': self.journal_currency.name,
                    'lines': [{
                        'date': '2026-04-21', 'amount': 20.0,
                        'payment_ref': 'B', 'unique_import_ref': 'MULTI-B',
                    }],
                },
            ],
        }
        parser = patch(
            'odoo.addons.eh_account_bank_statement_import.wizards.'
            'import_wizard.registry.get_parser',
        ).start()
        self.addCleanup(patch.stopall)
        parser.return_value.parse.return_value = parsed

        action = wizard.action_import()

        self.assertIn(action['view_mode'], ('list,form', 'tree,form'))
        self.assertNotIn('res_id', action)
        log = self.Log.search([
            ('file_hash', '=', hashlib.sha256(content).hexdigest()),
        ], order='id desc', limit=1)
        self.assertEqual(len(log.statement_ids), 2)
        self.assertEqual(set(log.statement_ids.mapped('name')), {
            'CAMT-A', 'CAMT-B',
        })
        self.assertEqual(len(log.source_attachment_ids), 2)
        self.assertEqual(
            set(log.source_attachment_ids.mapped('res_id')),
            set(log.statement_ids.ids),
        )
        for attachment in log.source_attachment_ids:
            self.assertEqual(base64.b64decode(attachment.datas), content)

    def test_multi_statement_validation_rolls_back_whole_file(self):
        content = self._csv_bytes('multi-statement-invalid-second')
        wizard = self._make_wizard(content)
        other_code = 'EUR' if self.journal_currency.name != 'EUR' else 'USD'
        parsed = {
            'statements': [
                {
                    'source_statement_id': 'VALID-FIRST',
                    'currency_code': self.journal_currency.name,
                    'lines': [{
                        'date': '2026-04-22', 'amount': 1.0,
                        'payment_ref': 'first', 'unique_import_ref': 'ATOMIC-A',
                    }],
                },
                {
                    'source_statement_id': 'INVALID-SECOND',
                    'currency_code': other_code,
                    'lines': [{
                        'date': '2026-04-22', 'amount': 2.0,
                        'payment_ref': 'second', 'unique_import_ref': 'ATOMIC-B',
                    }],
                },
            ],
        }
        parser = patch(
            'odoo.addons.eh_account_bank_statement_import.wizards.'
            'import_wizard.registry.get_parser',
        ).start()
        self.addCleanup(patch.stopall)
        parser.return_value.parse.return_value = parsed
        before = self.Statement.search_count([
            ('journal_id', '=', self.bank_journal.id),
        ])

        with self.assertRaises(UserError):
            wizard.action_import()

        self.assertEqual(self.Statement.search_count([
            ('journal_id', '=', self.bank_journal.id),
        ]), before)
        self.assertFalse(self.env['ir.attachment'].search([
            ('name', '=', wizard.filename),
            ('res_model', '=', 'account.bank.statement'),
            ('res_id', '>', 0),
            ('description', 'ilike', hashlib.sha256(content).hexdigest()),
        ]))

    # ---- idempotency ----

    def test_reimport_same_file_returns_duplicate(self):
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-04-21,500.00,INV-200,Big sale\n",
        )
        first = self._make_wizard(content)
        first.action_import()

        second = self._make_wizard(content)
        action = second.action_import()
        # Second import navigates to the log, not a fresh statement.
        self.assertEqual(
            action['res_model'],
            'eh.account.bank.statement.import.log',
        )
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id)],
            order='id desc', limit=1,
        )
        self.assertEqual(log.state, 'duplicate')
        # Total statements created so far is exactly one.
        statements = self.Statement.search(
            [('journal_id', '=', self.bank_journal.id)],
        )
        self.assertEqual(len(statements), 1)

    def test_deleted_statement_reimport_requires_manager_recovery(self):
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-04-22,25.00,RECOVER-1,Deleted statement recovery\n",
        )
        first = self._make_wizard(content)
        first_action = first.action_import()
        original = self.Statement.browse(first_action['res_id'])
        original_log = self.Log.search([
            ('statement_id', '=', original.id),
        ], order='id desc', limit=1)
        source_attachment = original_log.source_attachment_ids
        original.unlink()
        self.assertTrue(source_attachment.exists())
        self.assertEqual(
            source_attachment.res_model, original_log._name,
        )
        self.assertEqual(source_attachment.res_id, original_log.id)

        blocked = self._make_wizard(content).action_import()
        self.assertEqual(blocked['tag'], 'display_notification')
        self.assertEqual(blocked['params']['type'], 'warning')

        ordinary_user = self.env['res.users'].create({
            'name': 'Bank Import Recovery User',
            'login': 'bank_import_recovery_user_test',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('account.group_account_user').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        forged = self._make_wizard(content).with_user(ordinary_user)
        forged.reimport_deleted_statement = True
        with self.assertRaises(UserError):
            forged.action_import()

        manager_group = self.env.ref('eh_account_base.group_eh_manager')
        self.env.user.group_ids |= manager_group
        recovery = self._make_wizard(content)
        recovery.reimport_deleted_statement = True
        recovered_action = recovery.action_import()
        recovered = self.Statement.browse(recovered_action['res_id'])
        self.assertTrue(recovered.exists())
        self.assertEqual(len(recovered.line_ids), 1)

    def test_plain_accounting_user_can_run_import_wizard(self):
        ordinary_user = self.env['res.users'].create({
            'name': 'Bank Import Operator',
            'login': 'bank_import_operator_test',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('account.group_account_user').id,
                self.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-04-23,35.00,USER-IMPORT-1,Ordinary user import\n",
        )
        wizard = self.Wizard.with_user(ordinary_user).create({
            'journal_id': self.bank_journal.id,
            'profile_id': self.profile.id,
            'format_key': 'csv',
            'file_data': base64.b64encode(content),
            'filename': 'ordinary-user.csv',
        })
        action = wizard.action_import()
        statement = self.Statement.browse(action['res_id'])
        self.assertTrue(statement.exists())
        log = self.Log.search([
            ('statement_id', '=', statement.id),
        ], limit=1)
        self.assertEqual(log.imported_by_id, ordinary_user)

    def test_repeated_bank_source_refs_are_stable_and_do_not_abort(self):
        """Bad upstream IDs must not violate unique_import_ref."""
        wizard = self._make_wizard(self._csv_bytes("x"))

        def _parsed():
            return {
                'statement_date': False,
                'opening_balance': 0.0,
                'closing_balance': 15.0,
                'currency_code': self.journal_currency.name,
                'lines': [
                    {
                        'date': '2026-08-01', 'amount': 5.0,
                        'payment_ref': 'Coffee', 'narration': 'Card',
                        'unique_import_ref': 'NOTPROVIDED',
                    },
                    {
                        'date': '2026-08-01', 'amount': 5.0,
                        'payment_ref': 'Coffee', 'narration': 'Card',
                        'unique_import_ref': 'NOTPROVIDED',
                    },
                    {
                        'date': '2026-08-02', 'amount': 5.0,
                        'payment_ref': 'Parking', 'narration': 'Meter',
                        'unique_import_ref': 'NOTPROVIDED',
                    },
                ],
            }

        statement, count, skipped = wizard._materialise(_parsed())
        self.assertEqual((count, skipped), (3, 0))
        refs = statement.line_ids.mapped('unique_import_ref')
        self.assertEqual(len(refs), len(set(refs)))
        self.assertTrue(all('|ehdup:' in ref for ref in refs))

        duplicate, count, skipped = wizard._materialise(_parsed())
        self.assertFalse(duplicate)
        self.assertEqual((count, skipped), (0, 3))

        # A later partial export containing only one formerly duplicated
        # line still matches its deterministic occurrence-zero candidate.
        partial = _parsed()
        partial['lines'] = partial['lines'][2:]
        duplicate, count, skipped = wizard._materialise(partial)
        self.assertFalse(duplicate)
        self.assertEqual((count, skipped), (0, 1))

    def test_missing_source_refs_get_content_idempotency_keys(self):
        wizard = self._make_wizard(self._csv_bytes("x"))

        def parsed():
            return {
                'currency_code': self.journal_currency.name,
                'lines': [
                    {
                        'date': '2026-08-03', 'amount': 12.34,
                        'payment_ref': 'MT940 NONREF',
                        'narration': 'No bank reference supplied',
                        'unique_import_ref': False,
                    },
                ],
            }

        statement, count, skipped = wizard._materialise(parsed())
        self.assertEqual((count, skipped), (1, 0))
        self.assertIn('ehcontent:', statement.line_ids.unique_import_ref)
        duplicate, count, skipped = wizard._materialise(parsed())
        self.assertFalse(duplicate)
        self.assertEqual((count, skipped), (0, 1))

    def test_file_import_skips_legacy_live_provider_reference(self):
        """Feed-first/file-later imports share exact bank identity."""
        self.env['account.bank.statement.line'].create({
            'journal_id': self.bank_journal.id,
            'date': '2026-08-04',
            'amount': self.journal_currency.round(73.25),
            'payment_ref': 'Legacy live connector row',
            'eh_provider_reference': 'BANK-XCHANNEL-1',
            # Pre-1.3.8 live rows did not populate unique_import_ref.
            'unique_import_ref': False,
        })
        wizard = self._make_wizard(self._csv_bytes("x"))
        parsed = {
            'currency_code': self.journal_currency.name,
            'lines': [{
                'date': '2026-08-04',
                'amount': 73.25,
                'payment_ref': 'Same transaction from statement file',
                'narration': '',
                'unique_import_ref': 'BANK-XCHANNEL-1',
            }],
        }

        statement, count, skipped = wizard._materialise(parsed)

        self.assertFalse(statement)
        self.assertEqual((count, skipped), (0, 1))

    # ---- error path ----

    def test_csv_format_requires_profile(self):
        content = self._csv_bytes("Date,Amount\n2026-01-01,100\n")
        wizard = self.Wizard.create({
            'journal_id': self.bank_journal.id,
            'profile_id': False,
            'format_key': 'csv',
            'file_data': base64.b64encode(content),
            'filename': 'no_profile.csv',
        })
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_oversized_upload_is_rejected_before_hash_or_parser(self):
        from odoo.addons.eh_account_bank_statement_import.wizards import (
            import_wizard as import_wizard_module,
        )
        wizard = self._make_wizard(b'123456789')
        with patch.object(
                import_wizard_module, '_MAX_IMPORT_BASE64_BYTES', 8), \
                patch.object(
                    import_wizard_module.hashlib, 'sha256',
                    side_effect=AssertionError("oversized file was hashed")):
            with self.assertRaises(UserError) as raised:
                wizard.action_import()
        self.assertIn('too large', str(raised.exception).lower())

    def test_malformed_csv_logs_error(self):
        # A row that fails parsing produces a UserError AND a log row.
        # Using try/except instead of assertRaises so the test does not
        # open an implicit savepoint that would roll back the audit-log
        # row created inside action_import.
        content = self._csv_bytes(
            "Date,Amount,Ref,Memo\n"
            "not-a-date,not-a-number,X,Y\n",
        )
        wizard = self._make_wizard(content)
        raised = False
        try:
            wizard.action_import()
        except UserError:
            raised = True
        self.assertTrue(raised, "action_import must raise UserError on bad CSV")
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id),
             ('state', '=', 'error')],
            order='id desc', limit=1,
        )
        self.assertTrue(log)
        self.assertIn("row 1", log.error_message or "")

    # ---- currency guard ----

    def test_currency_mismatch_blocks_and_logs(self):
        # A profile whose currency differs from the journal's effective
        # currency must stop the import with a UserError and leave an
        # audited error row, never book the lines at face value in the
        # wrong currency.
        other_code = 'AUD' if self.journal_currency.name != 'AUD' else 'USD'
        other_currency = self.env['res.currency'].with_context(
            active_test=False).search([('name', '=', other_code)], limit=1)
        other_currency.active = True
        mismatch_profile = self.Profile.create({
            'name': 'Mismatch profile',
            'journal_id': self.bank_journal.id,
            'currency_code': other_code,
            'csv_delimiter': ',',
            'csv_quotechar': '"',
            'csv_encoding': 'utf-8',
            'csv_header_rows': 1,
            'decimal_separator': '.',
            'date_format': '%Y-%m-%d',
            'col_date': 0,
            'col_amount': 1,
            'col_ref': 2,
            'col_narration': 3,
        })
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-06-01,75.00,INV-300,Sale\n",
        )
        wizard = self.Wizard.create({
            'journal_id': self.bank_journal.id,
            'profile_id': mismatch_profile.id,
            'format_key': 'csv',
            'file_data': base64.b64encode(content),
            'filename': 'mismatch.csv',
        })
        raised = False
        try:
            wizard.action_import()
        except UserError as exc:
            raised = True
            self.assertIn(other_code, str(exc))
        self.assertTrue(raised, "currency mismatch must raise UserError")
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id),
             ('state', '=', 'error')],
            order='id desc', limit=1)
        self.assertTrue(log)
        self.assertIn(other_code, log.error_message or '')

    def test_source_account_must_match_selected_journal(self):
        bank_account = self.env['res.partner.bank'].create({
            'partner_id': self.env.company.partner_id.id,
            'acc_number': 'AU12 3456 7890',
        })
        self.bank_journal.bank_account_id = bank_account
        wizard = self._make_wizard(self._csv_bytes("x"))
        base = {
            'currency_code': self.journal_currency.name,
            'lines': [{
                'date': '2026-08-04', 'amount': 10.0,
                'payment_ref': 'Account proof',
                'unique_import_ref': 'ACCOUNT-PROOF-1',
            }],
        }
        matching = dict(base, account_id='AU1234567890')
        statement, count, skipped = wizard._materialise(matching)
        self.assertEqual((count, skipped), (1, 0))
        self.assertTrue(statement)

        mismatching = dict(
            base,
            account_id='AU9999999999',
            lines=[dict(base['lines'][0], unique_import_ref='ACCOUNT-BAD-1')],
        )
        with self.assertRaises(UserError):
            wizard._materialise(mismatching)

    # ---- partial-overlap reimport: end balance stays consistent ----

    def test_partial_overlap_reimport_keeps_consistent_end_balance(self):
        # A file with an opening/closing balance is imported, then re-imported
        # as a partial overlap: two lines repeat (skipped as duplicates) and
        # one line is new. The parsed closing_balance describes the WHOLE
        # file, so stamping it onto the second statement (which retained only
        # the one new line) would produce an inconsistent end balance. The
        # wizard must instead keep balance_end_real consistent with the lines
        # actually imported.
        wizard = self._make_wizard(self._csv_bytes("x"))  # journal/profile only
        currency = self.journal_currency

        first_parsed = {
            'statement_date': False,
            'opening_balance': 100.00,
            'closing_balance': 160.00,  # 100 + 10 + 20 + 30
            'currency_code': currency.name,
            'lines': [
                {'date': '2026-07-01', 'amount': 10.00,
                 'payment_ref': 'P1', 'narration': 'one',
                 'unique_import_ref': 'OV-1'},
                {'date': '2026-07-02', 'amount': 20.00,
                 'payment_ref': 'P2', 'narration': 'two',
                 'unique_import_ref': 'OV-2'},
                {'date': '2026-07-03', 'amount': 30.00,
                 'payment_ref': 'P3', 'narration': 'three',
                 'unique_import_ref': 'OV-3'},
            ],
        }
        stmt1, count1, skipped1 = wizard._materialise(first_parsed)
        self.assertEqual(count1, 3)
        self.assertEqual(skipped1, 0)
        # No lines dropped: the file's closing balance is authoritative.
        self.assertAlmostEqual(stmt1.balance_end_real, 160.00, places=2)

        # Re-import: two existing refs (OV-2, OV-3) overlap, one new (OV-4).
        # The file still declares its own whole-file closing balance, which
        # is now wrong for a statement that keeps only the single new line.
        second_parsed = {
            'statement_date': False,
            'opening_balance': 100.00,
            'closing_balance': 205.00,  # whole-file figure, not for the retained line
            'currency_code': currency.name,
            'lines': [
                {'date': '2026-07-02', 'amount': 20.00,
                 'payment_ref': 'P2', 'narration': 'two',
                 'unique_import_ref': 'OV-2'},
                {'date': '2026-07-03', 'amount': 30.00,
                 'payment_ref': 'P3', 'narration': 'three',
                 'unique_import_ref': 'OV-3'},
                {'date': '2026-07-04', 'amount': 45.00,
                 'payment_ref': 'P4', 'narration': 'four',
                 'unique_import_ref': 'OV-4'},
            ],
        }
        stmt2, count2, skipped2 = wizard._materialise(second_parsed)
        self.assertEqual(count2, 1)
        self.assertEqual(skipped2, 2)
        self.assertEqual(len(stmt2.line_ids), 1)

        # Bank closing stays authoritative. Incremental statement backs into
        # opening 160 so opening + retained 45 = reported closing 205.
        retained_total = sum(stmt2.line_ids.mapped('amount'))
        self.assertAlmostEqual(retained_total, 45.00, places=2)
        self.assertAlmostEqual(stmt2.balance_start, 160.00, places=2)
        self.assertAlmostEqual(stmt2.balance_end_real, 205.00, places=2)
        # Statement is internally consistent: start + retained == end.
        self.assertAlmostEqual(
            stmt2.balance_start + retained_total,
            stmt2.balance_end_real, places=2)

    # ---- all-duplicate reimport: no empty (illegal) statement ----

    def test_reimport_all_duplicate_lines_creates_no_empty_statement(self):
        content = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-05-01,10.00,A1,first\n"
            "2026-05-02,20.00,A2,second\n"
            "2026-05-03,30.00,A3,third\n",
        )
        self._make_wizard(content).action_import()
        base_count = self.Statement.search_count(
            [('journal_id', '=', self.bank_journal.id)])

        # Same three rows, reordered: different file bytes (new hash) but
        # each line's unique import reference is unchanged, so all three are
        # skipped as duplicates and nothing should be created.
        reordered = self._csv_bytes(
            "Date,Amount,Reference,Memo\n"
            "2026-05-03,30.00,A3,third\n"
            "2026-05-01,10.00,A1,first\n"
            "2026-05-02,20.00,A2,second\n",
        )
        action = self._make_wizard(reordered).action_import()

        # No crash, no act_window: a friendly notification instead.
        self.assertEqual(action.get('tag'), 'display_notification')
        # No new (and no empty/illegal) statement was created.
        self.assertEqual(
            self.Statement.search_count(
                [('journal_id', '=', self.bank_journal.id)]),
            base_count)
        # The outcome is logged as a duplicate with zero lines.
        log = self.Log.search(
            [('journal_id', '=', self.bank_journal.id),
             ('state', '=', 'duplicate')], order='id desc', limit=1)
        self.assertTrue(log)
        self.assertEqual(log.line_count, 0)
        self.assertEqual(log.skipped_count, 3)
