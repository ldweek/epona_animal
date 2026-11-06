# -*- encoding: utf-8 -*-
from datetime import date

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.eh_account_sepa_dd.models.sepa_creditor import (
    normalize_creditor_identifier,
)


@tagged('eh_account_sepa_dd', 'post_install', '-at_install')
class TestSepaDdBacklogRegressions(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.journal = cls.env['account.journal'].create({
            'name': 'DD Backlog Bank',
            'code': 'DDBL',
            'type': 'bank',
            'company_id': cls.company.id,
        })
        cls.creditor = cls.env['eh.sepa.creditor'].create({
            'name': 'DD Backlog Creditor',
            'company_id': cls.company.id,
            'journal_id': cls.journal.id,
            'creditor_identifier': ' DE98 ZZZ0 9999999999 ',
            'creditor_name': 'DD Backlog Creditor',
            'iban': 'DE89370400440532013000',
            'pre_notification_days': 0,
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'DD Backlog Customer',
        })

    def _mandate(self, **overrides):
        values = {
            'creditor_id': self.creditor.id,
            'partner_id': self.partner.id,
            'debtor_name': 'Original Debtor',
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 15),
            'local_instrument': 'CORE',
            'state': 'draft',
        }
        values.update(overrides)
        return self.env['eh.sepa.mandate'].create(values)

    def test_creditor_identifier_is_normalized_and_mod97_checked(self):
        self.assertEqual(
            self.creditor.creditor_identifier,
            'DE98ZZZ09999999999',
        )
        self.assertEqual(
            normalize_creditor_identifier('de98 zzz0 9999999999'),
            'DE98ZZZ09999999999',
        )
        with self.assertRaisesRegex(ValidationError, 'mod-97-10'):
            normalize_creditor_identifier('DE97ZZZ09999999999')

    def test_creditor_name_keeps_translation_storage_contract(self):
        """Upgrades must keep ORM writes compatible with JSONB storage."""
        self.assertTrue(self.creditor._fields['name'].translate)
        self.env['res.lang']._activate_lang('fr_FR')
        self.creditor.with_context(lang='fr_FR').name = 'Créancier DD'
        self.assertEqual(
            self.creditor.with_context(lang='fr_FR').name,
            'Créancier DD',
        )
        self.assertEqual(
            self.creditor.with_context(lang='en_US').name,
            'DD Backlog Creditor',
        )

    def test_sequence_is_used_and_default_instrument_is_company_scoped(self):
        self.company.eh_sepa_dd_default_instrument = 'B2B'
        business = self.env['res.partner'].create({
            'name': 'Sequence B2B GmbH', 'is_company': True,
        })
        values = {
            'creditor_id': self.creditor.id,
            'partner_id': business.id,
            'debtor_iban': 'FR1420041010050500013M02606',
            'signature_date': date(2026, 1, 16),
            'state': 'draft',
        }
        defaulted = self.env['eh.sepa.mandate'].create(values)
        self.assertTrue(defaulted.mandate_id.startswith('MNDT/'))
        self.assertEqual(defaulted.local_instrument, 'B2B')

    def test_check_company_rejects_foreign_journal_and_partner(self):
        foreign = self.env['res.company'].create({'name': 'DD Foreign Co'})
        foreign_journal = self.env['account.journal'].with_company(
            foreign,
        ).create({
            'name': 'DD Foreign Bank',
            'code': 'DDFR',
            'type': 'bank',
            'company_id': foreign.id,
        })
        with self.assertRaises(UserError):
            self.env['eh.sepa.creditor'].create({
                'name': 'Invalid mixed company creditor',
                'company_id': self.company.id,
                'journal_id': foreign_journal.id,
                'creditor_identifier': 'DE98ZZZ09999999999',
                'creditor_name': 'Invalid',
                'iban': 'DE89370400440532013000',
            })
        foreign_partner = self.env['res.partner'].create({
            'name': 'Foreign debtor', 'company_id': foreign.id,
        })
        with self.assertRaises(UserError):
            self._mandate(partner_id=foreign_partner.id)

    def test_amendment_tie_uses_highest_id_and_original_debtor_name(self):
        mandate = self._mandate(
            mandate_id='MNDT-AMEND-TIE', state='active',
        )
        mandate.write({'debtor_name': 'Second Debtor'})
        mandate.write({'debtor_name': 'Third Debtor'})
        amendments = mandate.amendment_ids.sorted('id')
        self.assertEqual(len(amendments), 2)
        self.env.cr.execute(
            "UPDATE eh_sepa_mandate_amendment SET amended_at = %s "
            "WHERE id IN %s",
            (amendments[0].amended_at, tuple(amendments.ids)),
        )
        mandate.invalidate_recordset(['amendment_ids'])
        latest = mandate._eh_pending_amendment_for_rendering()
        self.assertEqual(latest.id, max(amendments.ids))
        self.assertEqual(
            mandate._eh_latest_amendment_for_rendering(latest),
            {'original_debtor_name': 'Second Debtor'},
        )

    def test_partner_identity_change_is_not_a_dead_amendment_path(self):
        mandate = self._mandate(
            mandate_id='MNDT-PARTNER-IDENTITY', state='active',
        )
        replacement = self.env['res.partner'].create({
            'name': 'Replacement debtor requires new mandate',
        })
        self.assertFalse(mandate._eh_prepare_amendments({
            'partner_id': replacement.id,
        }))

    def test_non_sepa_text_name_fails_closed(self):
        with self.assertRaisesRegex(UserError, 'supported by SEPA'):
            self.env['eh.batch.payment']._eh_required_sepa_text(
                '\u4e2d\u6587', 'Debtor name', 70,
            )

    def test_retired_cor1_and_ui_controls_are_not_exposed(self):
        fields_to_check = [
            self.env['res.company']._fields['eh_sepa_dd_default_instrument'],
            self.env['eh.sepa.creditor']._fields['default_local_instrument'],
            self.env['eh.sepa.mandate']._fields['local_instrument'],
            self.env['account.payment']._fields['eh_sepa_dd_local_instrument'],
            self.env['eh.sepa.dd.export']._fields['local_instrument'],
        ]
        for field in fields_to_check:
            values = {key for key, _label in field._description_selection(self.env)}
            self.assertNotIn('COR1', values)
        menu = self.env.ref('eh_account_sepa_dd.menu_eh_sepa_dd_export')
        self.assertNotIn(
            self.env.ref('eh_account_base.group_eh_user'),
            menu.group_ids,
        )
        self.assertIn(
            self.env.ref('eh_account_base.group_eh_manager'),
            menu.group_ids,
        )
        self.assertIn(
            self.env.ref('eh_account_base.group_eh_auditor'),
            menu.group_ids,
        )
        batch_arch = self.env.ref(
            'eh_account_sepa_dd.view_eh_batch_payment_form_sepa_dd',
        ).arch_db
        self.assertIn('action_view_sepa_dd_exports', batch_arch)
        self.assertIn('sepa_dd_export_count', batch_arch)
        self.assertIn('eh_sepa_dd_final_collection', batch_arch)
        export_arch = self.env.ref(
            'eh_account_sepa_dd.view_eh_sepa_dd_export_form',
        ).arch_db
        self.assertEqual(export_arch.count('name="state"'), 1)
