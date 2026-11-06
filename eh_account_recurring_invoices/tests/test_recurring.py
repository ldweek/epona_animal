# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Recurring invoices module tests.

Covers recurrence math (day / week / month / quarter / year), generation
correctness (move_type, partner, lines, journal), counter increments,
end_date and count_total termination conditions, pause and resume
lifecycle, cron isolation, and the unique code constraint.
"""

from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_recurring_invoices', 'integration', 'post_install', '-at_install')
class TestRecurringTemplate(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['eh.recurring.invoice.template']
        # Need a sale journal.
        cls.sale_journal = cls.env['account.journal'].search(
            [('company_id', '=', cls.company.id), ('type', '=', 'sale')],
            limit=1,
        )
        if not cls.sale_journal:
            cls.sale_journal = cls.env['account.journal'].create({
                'name': 'Sales',
                'code': 'SALE',
                'type': 'sale',
                'company_id': cls.company.id,
            })

    def _make_template(self, **overrides):
        vals = {
            'name': 'Monthly support',
            'code': 'monthly_support',
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'interval': 1,
            'interval_unit': 'month',
            'start_date': fields.Date.context_today(self.env['res.users']),
            'next_run_date': fields.Date.context_today(self.env['res.users']),
            'line_ids': [
                (0, 0, {
                    'name': 'Support',
                    'account_id': self.account_revenue.id,
                    'quantity': 1.0,
                    'price_unit': 100.0,
                }),
            ],
        }
        vals.update(overrides)
        return self.Template.create(vals)

    # ---- constraints ----

    def test_code_format_constraint(self):
        with self.assertRaises(UserError):
            self._make_template(code='Bad-Code')
        with self.assertRaises(UserError):
            self._make_template(code='123starts_with_digit')
        # valid
        self._make_template(code='valid_one', name='one')

    def test_unique_code_per_company(self):
        self._make_template(code='dup_one')
        with self.assertRaises(Exception):
            self._make_template(code='dup_one', name='dup')

    def test_positive_interval_required(self):
        with self.assertRaises(Exception):
            self._make_template(code='zero_interval', interval=0)

    # ---- lifecycle ----

    def test_activate_requires_lines(self):
        tpl = self.Template.create({
            'name': 'No Lines',
            'code': 'no_lines',
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'start_date': fields.Date.context_today(self.env['res.users']),
            'next_run_date': fields.Date.context_today(self.env['res.users']),
        })
        with self.assertRaises(UserError):
            tpl.action_activate()

    def test_activate_pause_resume_finish(self):
        tpl = self._make_template(code='lifecycle_test')
        tpl.action_activate()
        self.assertEqual(tpl.state, 'active')
        tpl.action_pause()
        self.assertEqual(tpl.state, 'paused')
        tpl.action_resume()
        self.assertEqual(tpl.state, 'active')
        tpl.action_finish()
        self.assertEqual(tpl.state, 'finished')

    def test_finished_cannot_be_reactivated(self):
        tpl = self._make_template(code='cannot_revive')
        tpl.action_finish()
        with self.assertRaises(UserError):
            tpl.action_activate()

    def test_finished_template_can_be_duplicated_as_fresh_draft(self):
        tpl = self._make_template(code='duplicate_finished')
        tpl.action_activate()
        tpl.action_generate_now()
        tpl.action_finish()

        duplicate = tpl.copy()

        self.assertEqual(duplicate.state, 'draft')
        self.assertEqual(duplicate.code, 'duplicate_finished_copy')
        self.assertEqual(duplicate.count_generated, 0)
        self.assertFalse(duplicate.last_generated_move_id)
        self.assertFalse(duplicate.last_error)
        self.assertTrue(duplicate.line_ids)
        duplicate.action_activate()
        self.assertEqual(duplicate.state, 'active')

        second = tpl.copy()
        self.assertEqual(second.code, 'duplicate_finished_copy_2')

    # ---- recurrence math ----

    def test_advance_by_one_day(self):
        tpl = self._make_template(
            code='daily', interval=1, interval_unit='day',
            next_run_date=fields.Date.from_string('2026-06-15'),
        )
        tpl._advance_next_run()
        self.assertEqual(
            tpl.next_run_date, fields.Date.from_string('2026-06-16'),
        )

    def test_advance_by_two_weeks(self):
        tpl = self._make_template(
            code='biweekly', interval=2, interval_unit='week',
            next_run_date=fields.Date.from_string('2026-06-15'),
        )
        tpl._advance_next_run()
        self.assertEqual(
            tpl.next_run_date, fields.Date.from_string('2026-06-29'),
        )

    def test_advance_by_one_month(self):
        tpl = self._make_template(
            code='monthly_adv', interval=1, interval_unit='month',
            start_date=fields.Date.from_string('2026-06-15'),
            next_run_date=fields.Date.from_string('2026-06-15'),
        )
        tpl._advance_next_run()
        self.assertEqual(
            tpl.next_run_date, fields.Date.from_string('2026-07-15'),
        )

    def test_advance_by_one_quarter(self):
        tpl = self._make_template(
            code='quarterly_adv', interval=1, interval_unit='quarter',
            start_date=fields.Date.from_string('2026-01-15'),
            next_run_date=fields.Date.from_string('2026-01-15'),
        )
        tpl._advance_next_run()
        self.assertEqual(
            tpl.next_run_date, fields.Date.from_string('2026-04-15'),
        )

    def test_advance_by_one_year(self):
        tpl = self._make_template(
            code='yearly_adv', interval=1, interval_unit='year',
            start_date=fields.Date.from_string('2026-06-15'),
            next_run_date=fields.Date.from_string('2026-06-15'),
        )
        tpl._advance_next_run()
        self.assertEqual(
            tpl.next_run_date, fields.Date.from_string('2027-06-15'),
        )

    def test_month_end_day_does_not_drift(self):
        # Regression: a 31st-of-month schedule must keep landing on the
        # 31st whenever the month allows, instead of decaying to 28 after
        # the first short month. start_date is the billing-day anchor.
        tpl = self._make_template(
            code='month_end', interval=1, interval_unit='month',
            start_date=fields.Date.from_string('2026-01-31'),
            next_run_date=fields.Date.from_string('2026-01-31'),
        )
        expected = [
            '2026-02-28',  # clamped (no 31 Feb)
            '2026-03-31',  # recovers to 31, not stuck at 28
            '2026-04-30',  # clamped
            '2026-05-31',  # recovers
            '2026-06-30',  # clamped
            '2026-07-31',  # recovers
        ]
        for want in expected:
            tpl._advance_next_run()
            self.assertEqual(
                tpl.next_run_date, fields.Date.from_string(want),
                "day-of-month drifted: got %s want %s" % (
                    tpl.next_run_date, want),
            )

    def test_calendar_anchor_comes_from_selected_first_billing_date(self):
        tpl = self._make_template(
            code='billing_date_anchor',
            interval=1,
            interval_unit='month',
            start_date=fields.Date.from_string('2026-01-01'),
            next_run_date=fields.Date.from_string('2026-01-31'),
        )
        self.assertEqual(tpl.billing_day, 31)
        tpl._advance_next_run()
        self.assertEqual(
            tpl.next_run_date, fields.Date.from_string('2026-02-28'),
        )
        tpl._advance_next_run()
        self.assertEqual(
            tpl.next_run_date, fields.Date.from_string('2026-03-31'),
        )

    def test_leap_year_feb_29_anchor(self):
        # 2028 is a leap year: a 29th anchor lands on 29 Feb 2028 but
        # clamps to 28 Feb 2026/2027.
        tpl = self._make_template(
            code='feb29', interval=1, interval_unit='year',
            start_date=fields.Date.from_string('2028-02-29'),
            next_run_date=fields.Date.from_string('2028-02-29'),
        )
        tpl._advance_next_run()
        self.assertEqual(
            tpl.next_run_date, fields.Date.from_string('2029-02-28'),
        )

    # ---- generation ----

    def test_generate_now_creates_draft_invoice(self):
        tpl = self._make_template(code='gen_now_test')
        tpl.action_activate()
        tpl.action_generate_now()
        self.assertEqual(tpl.count_generated, 1)
        self.assertTrue(tpl.last_generated_move_id)
        move = tpl.last_generated_move_id
        self.assertEqual(move.move_type, 'out_invoice')
        self.assertEqual(move.partner_id, self.partner_a)
        self.assertEqual(move.state, 'draft')
        self.assertFalse(move.eh_sealed)
        self.assertFalse(move.eh_legacy_unverified_seal)
        self.assertFalse(move.eh_recurring_legacy_unverified)
        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertEqual(move.eh_recurring_template_id, tpl)
        expected_start, expected_end = tpl._eh_period_bounds(
            move.invoice_date
        )
        self.assertEqual(move.eh_recurring_period_start, expected_start)
        self.assertEqual(move.eh_recurring_period_end, expected_end)
        move.invoice_line_ids[0].write({'price_unit': 125.0})
        self.assertEqual(move.invoice_line_ids[0].price_unit, 125.0)
        move.button_cancel()
        self.assertEqual(move.state, 'cancel')

    def test_explicit_line_tax_and_account_use_fiscal_position_mapping(self):
        source_tax_vals = {
            'name': 'Recurring source tax 10%',
            'amount': 10.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
        }
        tax_country = (
            getattr(self.company, 'account_fiscal_country_id', False)
            or self.company.country_id
            or self.env.ref('base.us')
        )
        if (
            'account_fiscal_country_id' in self.company._fields
            and self.company.account_fiscal_country_id != tax_country
        ):
            self.company.account_fiscal_country_id = tax_country
        if 'country_id' in self.env['account.tax']._fields and tax_country:
            source_tax_vals['country_id'] = tax_country.id
        source_tax = self.env['account.tax'].create(source_tax_vals)
        destination_tax_vals = {
            'name': 'Recurring mapped tax 0%',
            'amount': 0.0,
            'amount_type': 'percent',
            'type_tax_use': 'sale',
            'company_id': self.company.id,
        }
        if 'country_id' in self.env['account.tax']._fields and tax_country:
            destination_tax_vals['country_id'] = tax_country.id
        modern_tax_map = (
            'original_tax_ids' in self.env['account.tax']._fields
        )
        if modern_tax_map:
            destination_tax_vals['original_tax_ids'] = [
                Command.set(source_tax.ids),
            ]
        destination_tax = self.env['account.tax'].create(
            destination_tax_vals,
        )
        destination_account = self._ensure_account(
            self.env, 'RECFPOS', 'Mapped recurring revenue', 'income_other',
        )
        fiscal_position_vals = {
            'name': 'Recurring fiscal map',
            'company_id': self.company.id,
            'account_ids': [Command.create({
                'account_src_id': self.account_revenue.id,
                'account_dest_id': destination_account.id,
            })],
        }
        if 'country_id' in self.env['account.fiscal.position']._fields:
            fiscal_position_vals['country_id'] = tax_country.id
        fiscal_position_vals['tax_ids'] = (
            [Command.set(destination_tax.ids)]
            if modern_tax_map else [Command.create({
                'tax_src_id': source_tax.id,
                'tax_dest_id': destination_tax.id,
            })]
        )
        fiscal_position = self.env['account.fiscal.position'].create(
            fiscal_position_vals,
        )
        template = self._make_template(
            code='fiscal_mapping',
            fiscal_position_id=fiscal_position.id,
            line_ids=[Command.create({
                'name': 'Mapped service',
                'account_id': self.account_revenue.id,
                'quantity': 1.0,
                'price_unit': 100.0,
                'tax_ids': [Command.set(source_tax.ids)],
            })],
        )

        template.action_generate_now()

        line = template.last_generated_move_id.invoice_line_ids
        self.assertEqual(line.tax_ids, destination_tax)
        self.assertEqual(line.account_id, destination_account)

    def test_template_company_and_deletion_freeze_after_generation(self):
        tpl = self._make_template(code='history_ownership_freeze')
        tpl.action_generate_now()
        move = tpl.last_generated_move_id
        other_company = self.env['res.company'].create({
            'name': 'Recurring ownership target',
        })
        with self.assertRaisesRegex(UserError, 'frozen'):
            tpl.write({'company_id': other_company.id})
        with self.assertRaisesRegex(UserError, 'cannot be deleted'):
            tpl.unlink()
        self.assertEqual(move.eh_recurring_template_id, tpl)
        self.assertEqual(
            self.env['account.move']._fields[
                'eh_recurring_template_id'
            ].ondelete,
            'restrict',
        )
        self.assertEqual(
            self.Template._fields['last_generated_move_id'].ondelete,
            'restrict',
        )

    def test_generate_with_auto_post_posts_invoice(self):
        tpl = self._make_template(code='auto_post_test', auto_post=True)
        tpl.action_activate()
        tpl.action_generate_now()
        self.assertEqual(tpl.last_generated_move_id.state, 'posted')
        self.assertTrue(tpl.last_generated_move_id.eh_sealed)

    def test_generate_advances_next_run(self):
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='advance_test',
            interval=1, interval_unit='month',
            next_run_date=today,
        )
        tpl.action_activate()
        tpl.action_generate_now()
        self.assertGreater(tpl.next_run_date, today)

    def test_generate_now_periods_are_distinct(self):
        """Each manual generation is for a distinct period. The row lock in
        action_generate_now ensures two concurrent calls cannot both read
        the same next_run_date and duplicate the same period; sequentially
        the two invoices fall one interval apart."""
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='distinct_periods', interval=1, interval_unit='month',
            next_run_date=today,
        )
        tpl.action_activate()
        tpl.action_generate_now()
        first = tpl.last_generated_move_id
        tpl.action_generate_now()
        second = tpl.last_generated_move_id
        self.assertNotEqual(first, second)
        self.assertNotEqual(first.invoice_date, second.invoice_date)
        self.assertEqual(tpl.count_generated, 2)

    def test_generate_without_lines_raises(self):
        tpl = self.Template.create({
            'name': 'Empty',
            'code': 'empty_lines',
            'partner_id': self.partner_a.id,
            'journal_id': self.sale_journal.id,
            'start_date': fields.Date.context_today(self.env['res.users']),
            'next_run_date': fields.Date.context_today(self.env['res.users']),
        })
        # Cannot activate with no lines, but we can try generate from draft.
        with self.assertRaises(UserError):
            tpl.action_generate_now()

    # ---- termination ----

    def test_count_total_finishes_template(self):
        tpl = self._make_template(
            code='count_cap_test',
            count_total=2,
        )
        tpl.action_activate()
        tpl.action_generate_now()
        self.assertEqual(tpl.state, 'active')
        tpl.action_generate_now()
        self.assertEqual(tpl.count_generated, 2)
        self.assertEqual(tpl.state, 'finished')

    def test_end_date_finishes_template(self):
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='end_date_test',
            interval=1, interval_unit='month',
            next_run_date=today,
            end_date=today + timedelta(days=15),
        )
        tpl.action_activate()
        # After one generation next_run_date will be today + 1 month, past
        # the end_date. Template should finish.
        tpl.action_generate_now()
        self.assertEqual(tpl.state, 'finished')

    def test_generation_preflight_blocks_date_beyond_end(self):
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='end_date_preflight',
            next_run_date=today,
            end_date=today - timedelta(days=1),
        )
        tpl.action_activate()
        tpl.action_generate_now()
        self.assertEqual(tpl.state, 'finished')
        self.assertEqual(tpl.count_generated, 0)
        self.assertFalse(tpl.generated_move_ids)

    def test_generation_preflight_blocks_lowered_count_cap(self):
        tpl = self._make_template(code='lowered_count_preflight')
        tpl.action_activate()
        tpl.action_generate_now()
        first = tpl.last_generated_move_id
        tpl.write({'count_total': 1})
        tpl.action_generate_now()
        self.assertEqual(tpl.state, 'finished')
        self.assertEqual(tpl.count_generated, 1)
        self.assertEqual(tpl.last_generated_move_id, first)

    def test_failed_last_end_date_period_is_retained_for_retry(self):
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='failed_final_period',
            next_run_date=today,
            end_date=today,
        )
        tpl.action_activate()
        original = type(tpl)._generate_one

        def _fail_target(record):
            if record == tpl:
                raise UserError("synthetic final-period failure")
            return original(record)

        self.patch(type(tpl), '_generate_one', _fail_target)
        self.Template._cron_generate_due()
        tpl.invalidate_recordset()
        self.assertEqual(tpl.count_generated, 0)
        self.assertEqual(tpl.state, 'paused')
        self.assertEqual(tpl.next_run_date, today)
        self.assertIn('synthetic final-period failure', tpl.last_error)

    def test_cron_pointer_failure_rolls_back_invoice_and_retains_period(self):
        """Invoice creation and schedule advance are one accounting unit."""
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='atomic_generation_pointer',
            next_run_date=today,
        )
        tpl.action_activate()
        original_advance = type(tpl)._advance_next_run

        def _fail_after_generation(record):
            if record == tpl:
                raise UserError("synthetic pointer failure")
            return original_advance(record)

        self.patch(type(tpl), '_advance_next_run', _fail_after_generation)
        self.Template._cron_generate_due()
        tpl.invalidate_recordset()

        self.assertEqual(tpl.state, 'paused')
        self.assertEqual(tpl.next_run_date, today)
        self.assertEqual(tpl.count_generated, 0)
        self.assertFalse(tpl.last_generated_move_id)
        self.assertFalse(self.env['account.move'].search_count([
            ('eh_recurring_template_id', '=', tpl.id),
        ]))
        self.assertIn('synthetic pointer failure', tpl.last_error)

    # ---- cron ----

    def test_cron_picks_up_due_active_templates(self):
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='cron_due',
            next_run_date=today - timedelta(days=1),
        )
        tpl.action_activate()
        self.Template._cron_generate_due()
        tpl.invalidate_recordset()
        self.assertEqual(tpl.count_generated, 1)

    def test_cron_skips_paused(self):
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='cron_paused',
            next_run_date=today - timedelta(days=1),
        )
        tpl.action_activate()
        tpl.action_pause()
        self.Template._cron_generate_due()
        tpl.invalidate_recordset()
        self.assertEqual(tpl.count_generated, 0)

    def test_cron_skips_future_runs(self):
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='cron_future',
            next_run_date=today + timedelta(days=15),
        )
        tpl.action_activate()
        self.Template._cron_generate_due()
        tpl.invalidate_recordset()
        self.assertEqual(tpl.count_generated, 0)

    def test_cron_generation_path_takes_row_lock(self):
        """The cron generation path must acquire the SAME row lock as the
        manual action_generate_now path, so a manual generate racing the
        cron cannot both read the same next_run_date and double-generate
        one period. We assert the lock is taken (by spying on
        _eh_lock_for_generate) and that exactly one invoice lands per
        period even when generation is driven from the cron.
        """
        today = fields.Date.context_today(self.env['res.users'])
        tpl = self._make_template(
            code='cron_lock',
            interval=1, interval_unit='month',
            next_run_date=today - timedelta(days=1),
        )
        tpl.action_activate()

        lock_calls = []
        original_lock = type(tpl)._eh_lock_for_generate

        def _spy_lock(self):
            lock_calls.append(self.id)
            return original_lock(self)

        self.patch(type(tpl), '_eh_lock_for_generate', _spy_lock)
        self.Template._cron_generate_due()
        tpl.invalidate_recordset()

        # The cron generation path took the row lock at least once for this
        # template (regression: previously it generated without locking).
        self.assertIn(
            tpl.id, lock_calls,
            "cron generation path did not acquire the row lock; a manual "
            "generate racing the cron could double-generate one period",
        )
        # Exactly one invoice for the single due period.
        self.assertEqual(tpl.count_generated, 1)
        self.assertEqual(len(tpl.generated_move_ids), 1)

    def test_cron_and_manual_do_not_double_generate_period(self):
        """A cron pass followed by a manual generate on the SAME originally
        due period must not both bill that period. Serialised by the shared
        row lock, the cron generates the due period and advances next_run;
        the subsequent manual generate observes the advanced next_run and
        bills the following period, so the two invoices are for distinct
        periods (one interval apart), never the same one.
        """
        today = fields.Date.context_today(self.env['res.users'])
        due_date = today - timedelta(days=1)
        tpl = self._make_template(
            code='cron_manual_race',
            interval=1, interval_unit='month',
            start_date=due_date,
            next_run_date=due_date,
        )
        tpl.action_activate()

        self.Template._cron_generate_due()
        tpl.invalidate_recordset()
        first = tpl.last_generated_move_id

        tpl.action_generate_now()
        tpl.invalidate_recordset()
        second = tpl.last_generated_move_id

        self.assertNotEqual(first, second)
        self.assertNotEqual(
            first.invoice_date, second.invoice_date,
            "cron and manual generated an invoice for the SAME period",
        )
        self.assertEqual(tpl.count_generated, 2)

    def test_cron_isolates_failure(self):
        today = fields.Date.context_today(self.env['res.users'])
        good = self._make_template(
            code='cron_good',
            next_run_date=today - timedelta(days=1),
        )
        good.action_activate()
        # Build a "bad" template by clearing its lines after activation.
        bad = self._make_template(
            code='cron_bad',
            next_run_date=today - timedelta(days=1),
        )
        failed_period = bad.next_run_date
        bad.action_activate()
        bad.line_ids.unlink()
        self.Template._cron_generate_due()
        good.invalidate_recordset()
        bad.invalidate_recordset()
        self.assertEqual(good.count_generated, 1)
        self.assertEqual(bad.count_generated, 0)
        self.assertTrue(bad.last_error)
        self.assertEqual(bad.state, 'paused')
        self.assertEqual(bad.next_run_date, failed_period)
        self.assertTrue(any(
            'period was not advanced' in (message.body or '')
            for message in bad.message_ids
        ))

        bad.write({'line_ids': [Command.create({
            'name': 'Repaired service',
            'account_id': self.account_revenue.id,
            'quantity': 1.0,
            'price_unit': 100.0,
        })]})
        bad.action_resume()
        self.Template._cron_generate_due()
        bad.invalidate_recordset()
        self.assertEqual(bad.count_generated, 1)
        self.assertFalse(bad.last_error)
        self.assertGreater(bad.next_run_date, failed_period)
        self.assertTrue(any(
            'generation recovered' in (message.body or '')
            for message in bad.message_ids
        ))

    # ---- traceability ----

    def test_generated_count_reflects_creates(self):
        tpl = self._make_template(code='gen_count_test')
        tpl.action_activate()
        tpl.action_generate_now()
        tpl.action_generate_now()
        tpl.invalidate_recordset(['generated_count'])
        self.assertEqual(tpl.generated_count, 2)

    def test_generated_invoices_carry_template_id(self):
        tpl = self._make_template(code='trace_test')
        tpl.action_activate()
        tpl.action_generate_now()
        move = tpl.last_generated_move_id
        self.assertEqual(move.eh_recurring_template_id, tpl)
        # And the reverse reference works.
        self.assertIn(move, tpl.generated_move_ids)

    # ---- MRR / ARR KPIs ----

    def _mrr_tpl(self, interval_unit, price, code, interval=1):
        return self._make_template(
            code=code, interval=interval, interval_unit=interval_unit,
            line_ids=[(0, 0, {
                'name': 'Recurring line',
                'account_id': self.account_revenue.id,
                'quantity': 1.0, 'price_unit': price,
            })],
        )

    def test_mrr_monthly(self):
        tpl = self._mrr_tpl('month', 100.0, 'mrr_m')
        tpl.action_activate()
        self.assertAlmostEqual(tpl.recurring_subtotal, 100.0, places=2)
        self.assertAlmostEqual(tpl.mrr, 100.0, places=2)
        self.assertAlmostEqual(tpl.arr, 1200.0, places=2)

    def test_mrr_quarterly(self):
        tpl = self._mrr_tpl('quarter', 300.0, 'mrr_q')
        tpl.action_activate()
        self.assertAlmostEqual(tpl.mrr, 100.0, places=2)

    def test_mrr_yearly(self):
        tpl = self._mrr_tpl('year', 1200.0, 'mrr_y')
        tpl.action_activate()
        self.assertAlmostEqual(tpl.mrr, 100.0, places=2)

    def test_mrr_zero_when_not_active(self):
        tpl = self._mrr_tpl('month', 100.0, 'mrr_draft')
        self.assertEqual(tpl.state, 'draft')
        self.assertAlmostEqual(tpl.mrr, 0.0, places=2)

    def test_total_mrr_sums_active_only(self):
        baseline = self.Template.eh_total_mrr(self.company)
        self._mrr_tpl('month', 100.0, 'mrr_t_a').action_activate()
        self._mrr_tpl('month', 50.0, 'mrr_t_b').action_activate()
        self._mrr_tpl('month', 999.0, 'mrr_t_draft')  # draft, excluded
        total = self.Template.eh_total_mrr(self.company)
        self.assertAlmostEqual(total - baseline, 150.0, places=2)
