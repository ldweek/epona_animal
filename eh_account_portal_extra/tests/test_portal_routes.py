# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Customer portal card, statement route and settings regressions.
"""

import hashlib
import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from lxml import html

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.eh_account_portal_extra.controllers import (
    portal as portal_controller,
)
from odoo.addons.eh_account_portal_extra.models.res_config_settings import (
    DEFAULT_MAX_STATEMENT_DAYS,
    MAX_CONFIGURED_STATEMENT_DAYS,
    STATEMENT_DAYS_CONFIG_KEY,
)


@tagged(
    'eh_account_portal_extra', 'eh_portal_medium_low', 'integration',
    'post_install', '-at_install',
)
class TestPortalRoutes(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].create({
            'name': 'Portal Test Customer',
            'email': 'portal_test@example.com',
        })
        # Create a portal user bound to this partner.
        portal_group = cls.env.ref('base.group_portal')
        cls.portal_user = cls.env['res.users'].create({
            'name': 'Portal User',
            'login': 'portal_eh_test',
            'password': 'portal_eh_test_pass',
            'partner_id': cls.partner.id,
            'group_ids': [(6, 0, [portal_group.id])],
        })
        cls.partner.write({'company_id': cls.env.company.id})
        # res.users.create copies the user.name onto its partner. Re-write
        # the partner name AFTER the user is created so the test sees
        # the customer-facing name on the portal route response.
        cls.partner.write({'name': 'Portal Test Customer'})

    def test_removed_balance_route_has_no_dead_json_surface(self):
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open('/my/eh/balance', allow_redirects=False)
        self.assertEqual(result.status_code, 404)
        self.assertFalse(
            hasattr(portal_controller.EhAccountPortal, 'portal_eh_balance'),
        )

    def test_standard_counter_rpc_receives_no_unrequested_eh_keys(self):
        """The stock JS dereferences one placeholder per returned key."""
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open(
            '/my/counters',
            data=json.dumps({
                'jsonrpc': '2.0',
                'method': 'call',
                'params': {'counters': ['invoice_count']},
            }),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(result.status_code, 200)
        payload = result.json().get('result', {})
        self.assertFalse(
            [key for key in payload if key.startswith('eh_')],
            payload,
        )

    def test_open_invoice_counter_includes_child_contact_invoice(self):
        company = self.env.company
        Account = self.env['account.account'].with_company(company)

        def make_account(code, name, account_type, **extra):
            vals = {
                'code': code,
                'name': name,
                'account_type': account_type,
                **extra,
            }
            if 'company_ids' in Account._fields:
                vals['company_ids'] = [(6, 0, [company.id])]
            else:  # Odoo 16/17 backport
                vals['company_id'] = company.id
            return Account.create(vals)

        receivable = make_account(
            '991831', 'Portal Child Receivable', 'asset_receivable',
            reconcile=True,
        )
        income = make_account(
            '991832', 'Portal Child Revenue', 'income',
        )
        self.partner.with_company(company).property_account_receivable_id = (
            receivable
        )
        sale_journal = self.env['account.journal'].create({
            'name': 'Portal Child Sales',
            'code': 'PRTC',
            'type': 'sale',
            'company_id': company.id,
        })
        child = self.env['res.partner'].create({
            'name': 'Portal Test Billing Contact',
            'parent_id': self.partner.id,
            'type': 'invoice',
        })
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'journal_id': sale_journal.id,
            'partner_id': child.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'name': 'Child-contact invoice',
                'quantity': 1.0,
                'price_unit': 125.0,
                'account_id': income.id,
            })],
        })
        invoice.action_post()

        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open(
            '/my/counters',
            data=json.dumps({
                'jsonrpc': '2.0',
                'method': 'call',
                'params': {'counters': ['eh_open_invoice_count']},
            }),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            result.json().get('result', {}).get('eh_open_invoice_count'),
            1,
        )

    def test_portal_card_excludes_sister_company_invoice(self):
        """sudo reads must remain pinned to portal user's active company."""
        # A shared customer can transact with several companies. Keeping the
        # commercial partner company-neutral is also required by Odoo's own
        # company-consistency guard before the foreign invoice can be posted.
        self.partner.sudo().write({'company_id': False})
        foreign = self.env['res.company'].sudo().create({
            'name': 'Portal Foreign Company',
            'currency_id': self.env.company.currency_id.id,
        })
        self.assertNotIn(foreign, self.portal_user.company_ids)

        Account = self.env['account.account'].sudo().with_company(foreign)

        def account_vals(code, name, account_type, **extra):
            vals = {
                'code': code,
                'name': name,
                'account_type': account_type,
                **extra,
            }
            if 'company_ids' in Account._fields:
                vals['company_ids'] = [(6, 0, [foreign.id])]
            else:  # Odoo 16/17 backport
                vals['company_id'] = foreign.id
            return vals

        receivable = Account.create(account_vals(
            '991821', 'Portal Foreign Receivable', 'asset_receivable',
            reconcile=True,
        ))
        income = Account.create(account_vals(
            '991822', 'Portal Foreign Revenue', 'income',
        ))
        journal = self.env['account.journal'].sudo().with_company(
            foreign,
        ).create({
            'name': 'Portal Foreign Sales',
            'code': 'PRTF',
            'type': 'sale',
            'company_id': foreign.id,
        })
        due = fields.Date.today() - timedelta(days=30)
        self.partner.sudo().with_company(
            foreign,
        ).property_account_receivable_id = receivable
        move = self.env['account.move'].sudo().with_company(foreign).create({
            'move_type': 'out_invoice',
            'journal_id': journal.id,
            'partner_id': self.partner.id,
            'invoice_date': due,
            'invoice_date_due': due,
            'invoice_line_ids': [
                (0, 0, {
                    'name': 'Foreign invoice',
                    'account_id': income.id,
                    'quantity': 1.0,
                    'price_unit': 123.45,
                }),
            ],
        })
        move.action_post()

        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open('/my')
        self.assertEqual(result.status_code, 200)
        page = html.fromstring(result.content)
        self.assertFalse(page.xpath('//*[@data-eh-total-overdue]'))
        self.assertNotIn(b'123.45', result.content)

    def test_card_uses_positive_customer_invoices_and_exposes_form(self):
        """Credits, payments and other partners cannot net portal overdue."""
        company = self.env.company
        Account = self.env['account.account'].with_company(company)

        def make_account(code, name, account_type, **extra):
            values = {
                'code': code,
                'name': name,
                'account_type': account_type,
                **extra,
            }
            if 'company_ids' in Account._fields:
                values['company_ids'] = [(6, 0, [company.id])]
            else:
                values['company_id'] = company.id
            return Account.create(values)

        receivable = make_account(
            '991851', 'Portal Positive Receivable', 'asset_receivable',
            reconcile=True,
        )
        income = make_account(
            '991852', 'Portal Positive Revenue', 'income',
        )
        offset = make_account(
            '991853', 'Portal Payment Offset', 'equity',
        )
        sale_journal = self.env['account.journal'].create({
            'name': 'Portal Positive Sales',
            'code': 'PRPS',
            'type': 'sale',
            'company_id': company.id,
        })
        general_journal = self.env['account.journal'].create({
            'name': 'Portal Unapplied Payment',
            'code': 'PRPM',
            'type': 'general',
            'company_id': company.id,
        })
        other_partner = self.env['res.partner'].create({
            'name': 'Other Portal Customer',
        })
        for partner in (self.partner, other_partner):
            partner.with_company(company).property_account_receivable_id = (
                receivable
            )

        def post_invoice(partner, move_type, amount, due, label):
            move = self.env['account.move'].create({
                'move_type': move_type,
                'journal_id': sale_journal.id,
                'partner_id': partner.id,
                'invoice_date': due,
                'invoice_date_due': due,
                'invoice_line_ids': [(0, 0, {
                    'name': label,
                    'account_id': income.id,
                    'quantity': 1.0,
                    'price_unit': amount,
                    # Odoo 16 falls back to the company's seeded default sales
                    # tax when this computed field is omitted. This filtering
                    # regression intentionally compares tax-neutral residuals.
                    'tax_ids': [(6, 0, [])],
                })],
            })
            move.action_post()
            return move

        today = fields.Date.today()
        invoice_due = today - timedelta(days=20)
        post_invoice(
            self.partner, 'out_invoice', 125.0, invoice_due,
            'Portal customer invoice',
        )
        post_invoice(
            self.partner, 'out_refund', 60.0,
            today - timedelta(days=80), 'Older customer credit',
        )
        post_invoice(
            other_partner, 'out_invoice', 999.0,
            today - timedelta(days=100), 'Other customer invoice',
        )
        payment = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': general_journal.id,
            'date': today - timedelta(days=90),
            'line_ids': [
                (0, 0, {
                    'name': 'Unapplied customer payment',
                    'partner_id': self.partner.id,
                    'account_id': receivable.id,
                    'credit': 40.0,
                    'date_maturity': today - timedelta(days=90),
                }),
                (0, 0, {
                    'name': 'Payment offset',
                    'account_id': offset.id,
                    'debit': 40.0,
                }),
            ],
        })
        payment.action_post()

        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open('/my')
        self.assertEqual(result.status_code, 200)
        page = html.fromstring(result.content)
        overdue = page.xpath('//*[@data-eh-total-overdue]')
        self.assertEqual(len(overdue), 1)
        self.assertAlmostEqual(
            float(overdue[0].get('data-eh-total-overdue')),
            125.0,
        )
        oldest = page.xpath('//*[@data-eh-oldest-due-date]')
        self.assertEqual(len(oldest), 1)
        self.assertEqual(
            oldest[0].get('data-eh-oldest-due-date'),
            invoice_due.isoformat(),
        )
        self.assertIn('Oldest overdue invoice', oldest[0].text_content())
        self.assertNotIn(invoice_due.isoformat(), oldest[0].text_content())
        forms = page.xpath('//form[@action="/my/eh/statement"]')
        self.assertEqual(len(forms), 1)
        date_from_inputs = forms[0].xpath('.//input[@name="date_from"]')
        date_to_inputs = forms[0].xpath('.//input[@name="date_to"]')
        self.assertEqual(len(date_from_inputs), 1)
        self.assertEqual(len(date_to_inputs), 1)
        self.assertEqual(date_from_inputs[0].get('required'), 'required')
        self.assertEqual(date_to_inputs[0].get('required'), 'required')
        self.assertEqual(
            {
                option.get('value')
                for option in forms[0].xpath(
                    './/select[@name="statement_type"]/option'
                )
            },
            {'open_item', 'activity'},
        )

    def test_statement_pdf_is_exact_partner_scoped_and_private(self):
        """The controller must stream one exact PDF for the session partner."""
        other_partner = self.env['res.partner'].create({
            'name': 'Forbidden Statement Customer',
        })
        self.partner.name = 'Miyuki 雪\nLtd'
        report = self.env.ref(
            'eh_account_dynamic_reports.report_customer_statement',
        )
        pdf = b'%PDF-1.4\nportal-extra-exact\n%%EOF\n'
        captured = {}

        def render_ephemeral(_report, options):
            captured.update(options)
            return pdf

        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        with patch.object(
            type(report), '_eh_render_pdf_ephemeral', render_ephemeral,
        ):
            result = self.url_open(
                '/my/eh/statement?date_from=2026-01-01'
                '&date_to=2026-12-31&statement_type=activity'
                f'&partner_id={other_partner.id}',
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, pdf)
        self.assertEqual(result.headers['Content-Type'], 'application/pdf')
        self.assertEqual(
            result.headers['Cache-Control'],
            'private, no-store, max-age=0',
        )
        disposition = result.headers['Content-Disposition']
        self.assertIn('filename="Statement_Miyuki_Ltd_', disposition)
        self.assertIn("filename*=UTF-8''Statement_Miyuki_", disposition)
        self.assertIn('%E9%9B%AA%0ALtd', disposition)
        self.assertNotIn('\n', disposition)
        self.assertEqual(captured['partner_id'], self.partner.id)
        self.assertEqual(captured['company_ids'], [self.env.company.id])
        self.assertEqual(captured['primary_company_id'], self.env.company.id)
        self.assertEqual(captured['statement_type'], 'activity')
        self.assertEqual(captured['date'], {
            'mode': 'range',
            'date_from': '2026-01-01',
            'date_to': '2026-12-31',
        })

    def test_statement_invalid_date_renders_error_template(self):
        """A malformed date returns the invalid-dates page, not a 500."""
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open(
            '/my/eh/statement?date_from=not-a-date',
        )
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Invalid date range', result.content)
        self.assertIn('no-store', result.headers['Cache-Control'])

    def test_statement_inverted_date_range_renders_error(self):
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open(
            '/my/eh/statement?date_from=2026-12-31&date_to=2026-01-01',
        )
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Invalid date range', result.content)

    def test_statement_rejects_unbounded_date_range(self):
        self.env['ir.config_parameter'].sudo().set_param(
            STATEMENT_DAYS_CONFIG_KEY,
            10,
        )
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        result = self.url_open(
            '/my/eh/statement?date_from=2026-01-01&date_to=2026-01-31',
        )
        self.assertEqual(result.status_code, 200)
        self.assertIn(b'Statement period is too long', result.content)
        self.assertIn(b'10 days', result.content)
        self.assertNotIn(b'could not be parsed', result.content)
        self.assertIn('no-store', result.headers['Cache-Control'])

    def test_concurrent_render_returns_branded_private_429(self):
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        with patch.object(
            portal_controller.EhAccountPortal,
            '_eh_try_statement_lock',
            return_value=False,
        ):
            result = self.url_open(
                '/my/eh/statement?date_from=2026-01-01'
                '&date_to=2026-01-31',
            )
        self.assertEqual(result.status_code, 429)
        self.assertIn(
            b'Your statement is already being prepared',
            result.content,
        )
        self.assertIn(b'<html', result.content)
        self.assertEqual(result.headers['Retry-After'], '5')
        self.assertIn('no-store', result.headers['Cache-Control'])

    def test_render_failure_logs_traceback_and_returns_branded_503(self):
        report = self.env.ref(
            'eh_account_dynamic_reports.report_customer_statement',
        )

        def fail_render(_report, _options):
            raise RuntimeError('private-render-sentinel')

        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        with patch.object(
            type(report), '_eh_render_pdf_ephemeral', fail_render,
        ), patch.object(portal_controller._logger, 'exception') as logged:
            result = self.url_open(
                '/my/eh/statement?date_from=2026-01-01'
                '&date_to=2026-01-31',
            )
        self.assertEqual(result.status_code, 503)
        self.assertIn(b'We could not prepare your statement', result.content)
        self.assertNotIn(b'private-render-sentinel', result.content)
        self.assertIn('no-store', result.headers['Cache-Control'])
        logged.assert_called_once()

    def test_statement_keeps_audit_but_persists_no_cache_attachment(self):
        self.authenticate('portal_eh_test', 'portal_eh_test_pass')
        Execution = self.env['eh.account.report.execution'].sudo()
        before_execution_ids = set(Execution.search([
            ('report_code', '=', 'customer_statement'),
        ]).ids)
        pdf = b'%PDF-1.4\naudited-portal-render\n%%EOF\n'
        ReportActionClass = type(self.env['ir.actions.report'])

        def render_qweb_pdf(_actions, _report_ref, _res_ids=None, data=None):
            return pdf, 'pdf'

        with patch.object(
            ReportActionClass, '_render_qweb_pdf', render_qweb_pdf,
        ):
            result = self.url_open(
                '/my/eh/statement?date_from=2026-01-01'
                '&date_to=2026-01-31&statement_type=open_item',
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, pdf)
        new_executions = Execution.search([
            ('report_code', '=', 'customer_statement'),
            ('id', 'not in', list(before_execution_ids)),
        ])
        self.assertEqual(len(new_executions), 1)
        execution = new_executions.ensure_one()
        self.assertEqual(execution.state, 'done')
        self.assertEqual(execution.result_format, 'pdf')
        self.assertEqual(
            execution.result_hash,
            hashlib.sha256(pdf).hexdigest(),
        )
        self.assertFalse(execution.result_payload)
        self.assertFalse(execution.cache_trusted)
        options = json.loads(execution.options_snapshot)
        self.assertEqual(options['partner_id'], self.partner.id)
        self.assertEqual(options['company_ids'], [self.env.company.id])
        self.assertFalse(self.env['ir.attachment'].sudo().search([
            ('res_model', '=', 'eh.account.report.execution'),
            ('res_id', '=', execution.id),
            ('res_field', '=', 'result_payload'),
        ]))

    def test_routes_require_authentication(self):
        """Anonymous access to the portal routes is refused."""
        result = self.url_open('/my/eh/statement', allow_redirects=False)
        # Series/router variants either redirect to login or refuse before
        # database/auth routing. Both outcomes prove no portal payload leaked.
        self.assertIn(result.status_code, (301, 302, 303, 307, 404))


@tagged(
    'eh_account_portal_extra', 'eh_portal_medium_low', 'integration',
    'post_install', '-at_install',
)
class TestPortalConfiguration(TransactionCase):

    def test_statement_limit_migration_is_defaulted_and_idempotent(self):
        """Upgrade must distinguish a missing key from explicit one day."""
        module_root = Path(portal_controller.__file__).resolve().parents[1]
        migration_files = sorted(
            (module_root / 'migrations').glob(
                '*.0.1.0.8/post-migration.py',
            ),
        )
        self.assertEqual(len(migration_files), 1, migration_files)
        spec = importlib.util.spec_from_file_location(
            'eh_account_portal_extra_migration_1_0_8_test',
            str(migration_files[0]),
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        parameters = self.env['ir.config_parameter'].sudo()
        parameters.search([
            ('key', '=', STATEMENT_DAYS_CONFIG_KEY),
        ]).unlink()
        migration.migrate(self.env.cr, '19.0.1.0.6')
        self.assertEqual(
            parameters.get_param(STATEMENT_DAYS_CONFIG_KEY),
            str(DEFAULT_MAX_STATEMENT_DAYS),
        )

        parameter_class = type(parameters)
        with patch.object(parameter_class, 'set_param') as set_param:
            migration.migrate(self.env.cr, '19.0.1.0.6')
        set_param.assert_not_called()

        parameters.set_param(STATEMENT_DAYS_CONFIG_KEY, '1')
        with patch.object(parameter_class, 'set_param') as set_param:
            migration.migrate(self.env.cr, '19.0.1.0.6')
        set_param.assert_not_called()
        self.assertEqual(
            parameters.get_param(STATEMENT_DAYS_CONFIG_KEY),
            '1',
        )

    def test_statement_limit_is_documented_persisted_and_bounded(self):
        field = self.env['res.config.settings']._fields[
            'eh_portal_max_statement_days'
        ]
        self.assertEqual(field.config_parameter, STATEMENT_DAYS_CONFIG_KEY)
        self.assertEqual(
            self.env['res.config.settings'].default_get([
                'eh_portal_max_statement_days',
            ])['eh_portal_max_statement_days'],
            DEFAULT_MAX_STATEMENT_DAYS,
        )

        settings = self.env['res.config.settings'].create({
            'eh_portal_max_statement_days': 45,
        })
        settings.set_values()
        self.assertEqual(
            self.env['ir.config_parameter'].sudo().get_param(
                STATEMENT_DAYS_CONFIG_KEY,
            ),
            '45',
        )
        with self.assertRaises(ValidationError):
            self.env['res.config.settings'].create({
                'eh_portal_max_statement_days': 0,
            })
        with self.assertRaises(ValidationError):
            self.env['res.config.settings'].create({
                'eh_portal_max_statement_days': (
                    MAX_CONFIGURED_STATEMENT_DAYS + 1
                ),
            })

        settings_view = self.env.ref(
            'eh_account_portal_extra.'
            'res_config_settings_view_form_portal_extra',
        )
        self.assertIn('eh_portal_max_statement_days', settings_view.arch_db)
        self.assertIn('Maximum Statement Period', settings_view.arch_db)

    def test_portal_card_is_a_website_customize_option(self):
        portal_view = self.env.ref(
            'eh_account_portal_extra.portal_my_home_eh_card',
        )
        self.assertTrue(portal_view.customize_show)
