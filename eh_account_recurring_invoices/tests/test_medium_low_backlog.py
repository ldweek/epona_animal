# -*- encoding: utf-8 -*-
"""Focused regressions for the revalidated recurring-invoice backlog."""

from importlib.util import module_from_spec, spec_from_file_location
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_recurring_invoices', 'post_install', '-at_install')
class TestRecurringMediumLowBacklog(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref(
            'eh_account_base.group_eh_manager')
        cls.Template = cls.env['eh.recurring.invoice.template']
        cls.Wizard = cls.env['eh.recurring.invoice.proration.wizard']
        cls.Correction = cls.env[
            'eh.recurring.invoice.correction.wizard'
        ]
        cls.sale_journal = cls.journal_sale
        cls.plain_user = cls.env['res.users'].with_context(
            no_reset_password=True,
            mail_create_nosubscribe=True,
        ).create({
            'name': 'Recurring backlog operator',
            'login': 'recurring_backlog_operator',
            'email': 'recurring-backlog@example.com',
            'company_id': cls.company.id,
            'company_ids': [Command.set(cls.company.ids)],
            'group_ids': [Command.set([
                cls.env.ref('base.group_user').id,
                cls.env.ref('eh_account_base.group_eh_user').id,
            ])],
        })
        migration_path = (
            Path(__file__).resolve().parents[1]
            / 'migrations' / '19.0.1.1.2' / 'post-migration.py'
        )
        spec = spec_from_file_location(
            'eh_recurring_post_migration_112', migration_path,
        )
        cls.migration_112 = module_from_spec(spec)
        spec.loader.exec_module(cls.migration_112)

    def _template(self, code, **overrides):
        today = fields.Date.context_today(self.Template)
        vals = {
            'name': code.replace('_', ' ').title(),
            'code': code,
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'start_date': today,
            'next_run_date': today,
            'interval': 1,
            'interval_unit': 'month',
            'line_ids': [Command.create({
                'name': 'Recurring service',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        }
        vals.update(overrides)
        return self.Template.create(vals)

    def _post_source(self, code='backlog_proration_source', **overrides):
        template = self._template(code, auto_post=True, **overrides)
        template.action_activate()
        template.action_generate_now()
        return template, template.last_generated_move_id

    def test_foreign_currency_generation_and_company_currency_kpis(self):
        eur = self.env.ref('base.EUR')
        if not eur.active:
            eur.sudo().write({'active': True})
        run_date = fields.Date.context_today(self.Template)
        Rate = self.env['res.currency.rate']
        rate = Rate.search([
            ('currency_id', '=', eur.id),
            ('company_id', '=', self.company.id),
            ('name', '=', run_date),
        ], limit=1)
        rate_vals = {
            'currency_id': eur.id,
            'company_id': self.company.id,
            'name': run_date,
            'rate': 2.0,
        }
        if rate:
            rate.write({'rate': 2.0})
        else:
            Rate.create(rate_vals)

        template = self._template(
            'foreign_currency_kpi', currency_id=eur.id,
        )
        template.action_activate()
        expected = eur._convert(
            100.0, self.company.currency_id, self.company, run_date,
        )
        self.assertEqual(template.currency_id, eur)
        self.assertAlmostEqual(template.mrr_company, expected, places=2)
        self.assertAlmostEqual(template.arr_company, expected * 12, places=2)
        self.assertAlmostEqual(
            self.Template.eh_total_mrr(self.company), expected, places=2,
        )

        template.action_generate_now()
        move = template.last_generated_move_id
        self.assertEqual(move.currency_id, eur)
        self.assertTrue(move.eh_recurring_generation_key)
        with self.assertRaisesRegex(UserError, 'currency'):
            template.currency_id = self.company.currency_id

    def test_period_rewind_is_blocked_until_linked_sealed_correction(self):
        template, source = self._post_source('period_key_correction')
        period_start = source.eh_recurring_period_start
        generation_key = source.eh_recurring_generation_key
        template.next_run_date = period_start

        with self.assertRaisesRegex(UserError, 'already has active invoice'):
            template.action_generate_now()
        with self.assertRaises(AccessError):
            source.with_user(
                self.plain_user,
            ).action_eh_correct_recurring_invoice()

        correction = self.Correction.create({
            'move_id': source.id,
            'date': fields.Date.context_today(self.Correction),
            'reason': 'Incorrect customer billing amount entered',
        })
        result = correction.action_reverse()
        reversal = self.env['account.move'].browse(result['res_id'])
        source.invalidate_recordset()
        self.assertEqual(reversal.reversed_entry_id, source)
        self.assertEqual(reversal.state, 'posted')
        self.assertTrue(reversal.eh_sealed)
        self.assertEqual(source.eh_recurring_correction_move_id, reversal)
        self.assertEqual(source.eh_recurring_corrected_by_id, self.env.user)
        self.assertFalse(source.eh_recurring_generation_key)

        template.action_generate_now()
        reissued = template.last_generated_move_id
        self.assertNotEqual(reissued, source)
        self.assertEqual(reissued.eh_recurring_period_start, period_start)
        self.assertEqual(reissued.eh_recurring_generation_key, generation_key)

    def test_cancelled_draft_releases_period_key_for_retry(self):
        template = self._template('cancelled_period_retry')
        template.action_generate_now()
        draft = template.last_generated_move_id
        period_start = draft.eh_recurring_period_start
        self.assertTrue(draft.eh_recurring_generation_key)
        draft.button_cancel()
        self.assertFalse(draft.eh_recurring_generation_key)
        template.next_run_date = period_start
        template.action_generate_now()
        self.assertNotEqual(template.last_generated_move_id, draft)

    def test_cron_is_bounded_ordered_and_reports_remaining_progress(self):
        today = fields.Date.context_today(self.Template)
        due = self.Template
        for index, days_old in enumerate((3, 2, 1), start=1):
            template = self._template(
                'cron_batch_%s' % index,
                start_date=today - timedelta(days=days_old),
                next_run_date=today - timedelta(days=days_old),
            )
            template.action_activate()
            due |= template
        expected = due.sorted(
            lambda template: (
                template.company_id.id,
                template.next_run_date,
                template.id,
            )
        )[:2]
        progress = []

        def _progress(records, processed, remaining):
            progress.append((processed, remaining))
            return True

        with patch.object(
            type(self.Template), '_eh_commit_cron_progress', new=_progress,
        ):
            self.Template._cron_generate_due(limit=2)

        self.assertEqual(set(expected.mapped('count_generated')), {1})
        self.assertEqual((due - expected).mapped('count_generated'), [0])
        self.assertEqual(progress, [(1, 2), (1, 1)])

        # Odoo 16/17 scheduled actions expose only ``lastcall`` (no cron_id,
        # no progress API). That marker must enter the durable fallback,
        # while an ordinary direct caller must never commit its transaction.
        with patch.object(self.env.cr, 'commit') as commit:
            self.Template._eh_commit_cron_progress(1, 1)
            commit.assert_not_called()
            self.Template.with_context(
                lastcall=fields.Datetime.now(),
            )._eh_commit_cron_progress(1, 1)
            commit.assert_called_once_with()

    def test_operational_failure_without_moves_is_not_accounting_history(self):
        template = self._template(
            'failed_setup_is_editable', line_ids=[],
        )
        template._record_failure(UserError('missing setup account'))
        self.assertTrue(template.last_error)
        self.assertTrue(template.last_run_at)
        self.assertFalse(template._eh_has_accounting_history())
        template.unlink()
        self.assertFalse(template.exists())

    def test_views_action_and_demo_surface_controls_and_consistent_anchor(self):
        template_arch = self.env.ref(
            'eh_account_recurring_invoices.view_eh_recurring_template_form'
        ).arch_db
        move_arch = self.env.ref(
            'eh_account_recurring_invoices.view_move_form_eh_recurring'
        ).arch_db
        for field_name in (
            'recurring_subtotal', 'mrr', 'arr', 'mrr_company',
        ):
            self.assertIn('name="%s"' % field_name, template_arch)
        for field_name in (
            'eh_recurring_period_start', 'eh_recurring_period_end',
            'eh_recurring_correction_move_id',
        ):
            self.assertIn('name="%s"' % field_name, move_arch)
        action = self.env.ref(
            'eh_account_recurring_invoices.'
            'action_eh_recurring_proration_wizard'
        )
        self.assertIn(
            self.env.ref('eh_account_base.group_eh_manager'),
            action.group_ids,
        )

        demo_path = (
            Path(__file__).resolve().parents[1]
            / 'demo' / 'recurring_demo.xml'
        )
        root = etree.parse(str(demo_path))
        record = root.xpath(
            "//record[@id='demo_recurring_monthly']"
        )[0]
        values = {
            node.get('name'): node
            for node in record.xpath('./field')
        }
        self.assertEqual(
            values['start_date'].get('eval'),
            values['next_run_date'].get('eval'),
        )
        self.assertEqual(values['company_id'].get('ref'), 'base.main_company')
        self.assertIn('company_id', values['journal_id'].get('search'))
        self.assertIn('base.main_company', values['journal_id'].get('search'))

    def test_period_key_upgrade_backfills_unambiguous_current_history(self):
        template = self._template('migration_key_backfill')
        template.action_generate_now()
        move = template.last_generated_move_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET eh_recurring_generation_key = NULL "
            "WHERE id = %s",
            (move.id,),
        )
        self.migration_112.migrate(self.env.cr, '19.0.1.1.1')
        self.env.invalidate_all()
        self.assertEqual(
            move.eh_recurring_generation_key,
            '%s:%s' % (template.id, move.eh_recurring_period_start),
        )
        self.assertFalse(template.eh_legacy_integrity_unverified)
