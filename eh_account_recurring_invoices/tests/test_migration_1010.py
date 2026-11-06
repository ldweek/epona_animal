# -*- encoding: utf-8 -*-
"""Fail-closed upgrade regressions for recurring invoice evidence."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_recurring_invoices', 'post_install', '-at_install')
class TestRecurringMigration1010(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.Template = cls.env['eh.recurring.invoice.template']
        cls.sale_journal = cls.env['account.journal'].search([
            ('company_id', '=', cls.company.id),
            ('type', '=', 'sale'),
        ], limit=1)
        if not cls.sale_journal:
            cls.sale_journal = cls.env['account.journal'].create({
                'name': 'Recurring migration sales',
                'code': 'RMIG',
                'type': 'sale',
                'company_id': cls.company.id,
            })
        path = (
            Path(__file__).parents[1]
            / 'migrations' / '19.0.1.0.10' / 'post-migration.py'
        )
        spec = spec_from_file_location(
            'eh_recurring_post_migration_1010', path,
        )
        cls.migration = module_from_spec(spec)
        spec.loader.exec_module(cls.migration)

    def _template(self, code, **extra):
        vals = {
            'name': code,
            'code': code,
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'start_date': fields.Date.from_string('2026-04-01'),
            'next_run_date': fields.Date.from_string('2026-04-01'),
            'line_ids': [(0, 0, {
                'name': 'Legacy service',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        }
        vals.update(extra)
        return self.Template.create(vals)

    def _migrate(self):
        self.env.flush_all()
        self.migration.migrate(self.env.cr, '19.0.1.0.9')
        self.env.invalidate_all()

    def test_valid_looking_linked_history_is_preserved_but_quarantined(self):
        template = self._template('legacy_valid_shape', auto_post=True)
        template.action_activate()
        template.action_generate_now()
        move = template.last_generated_move_id
        original_count = template.count_generated
        original_period = (
            move.eh_recurring_period_start,
            move.eh_recurring_period_end,
        )

        self._migrate()

        self.assertTrue(template.eh_legacy_integrity_unverified)
        self.assertEqual(template.count_generated, original_count)
        self.assertEqual(template.last_generated_move_id, move)
        self.assertEqual(move.eh_recurring_template_id, template)
        self.assertEqual(
            (move.eh_recurring_period_start, move.eh_recurring_period_end),
            original_period,
        )
        self.assertFalse(move.eh_sealed)
        self.assertTrue(move.eh_legacy_unverified_seal)
        self.assertTrue(move.eh_recurring_legacy_unverified)
        self.assertEqual(move.state, 'posted')
        with self.assertRaisesRegex(UserError, 'quarantined'):
            template.action_generate_now()
        with self.assertRaisesRegex(UserError, 'quarantined'):
            template.write({'name': 'rewritten legacy template'})
        with self.assertRaises(UserError):
            move.invoice_line_ids[0].write({'price_unit': 1.0})
        with self.assertRaises(UserError):
            move.with_context(force_delete=True).unlink()

        template.write({
            'eh_legacy_review_note': (
                'Reviewed source contract, schedule, invoice and period '
                'anchors; future billing requires manual resume.'
            ),
        })
        template.action_review_legacy_and_resume_setup()
        self.assertFalse(template.eh_legacy_integrity_unverified)
        self.assertEqual(template.state, 'paused')
        self.assertEqual(template.eh_legacy_reviewed_by_id, self.env.user)
        self.assertTrue(template.eh_legacy_reviewed_at)
        self.assertTrue(move.eh_recurring_legacy_unverified)
        self.assertTrue(move.eh_legacy_unverified_seal)
        self.assertFalse(move.eh_sealed)
        template.write({'name': 'Reviewed future template'})
        template.action_resume()
        template.action_generate_now()
        new_move = template.last_generated_move_id
        self.assertNotEqual(new_move, move)
        self.assertFalse(new_move.eh_recurring_legacy_unverified)
        self.assertFalse(new_move.eh_legacy_unverified_seal)

    def test_legacy_workflow_anchor_without_move_is_quarantined(self):
        template = self._template('legacy_active_no_move')
        template.action_activate()
        self.assertFalse(template.generated_move_ids)

        self._migrate()

        self.assertTrue(template.eh_legacy_integrity_unverified)
        self.assertFalse(template.generated_move_ids)
        with self.assertRaisesRegex(UserError, 'quarantined'):
            template.action_pause()
        with self.assertRaisesRegex(UserError, 'quarantined'):
            template.unlink()

    def test_unused_draft_configuration_is_not_fabricated_as_history(self):
        template = self._template('legacy_unused_draft')

        self._migrate()

        self.assertFalse(template.eh_legacy_integrity_unverified)
        template.write({'name': 'Reviewed unused configuration'})
        self.assertEqual(template.name, 'Reviewed unused configuration')
