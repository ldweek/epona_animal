# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
PDF rendering smoke tests for dynamic reports.

Each test renders one of the seven v1.0 reports to PDF, verifies that the
output starts with the PDF magic bytes, and is large enough to contain the
table content. The tests rely on wkhtmltopdf being available in the
testing environment, which is the standard Odoo CI assumption.

Detailed visual layout is verified manually rather than asserted in code:
parsing PDF text and asserting positions is brittle and adds no value over
manual inspection. The smoke tests catch broken templates, missing
abstract model registrations, malformed paperformats, and crashes inside
the formatting helpers.
"""

import hashlib
from pathlib import Path
from unittest.mock import patch

from lxml import etree

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_dynamic_reports', 'integration', 'pdf', 'post_install', '-at_install')
class TestPdfRendering(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        DynRep = cls.env['eh.account.dynamic.report']
        cls.reports = {
            r.code: r for r in DynRep.search([])
        }
        # Seed enough activity to make every report non trivial.
        cls.post_balanced_move(
            [
                {'account': cls.account_revenue, 'credit': 1000.0,
                 'partner': cls.partner_a},
                {'account': cls.account_cash, 'debit': 1000.0},
            ],
            date=fields.Date.from_string('2026-06-15'),
        )
        cls.post_balanced_move(
            [
                {'account': cls.account_expense, 'debit': 200.0,
                 'partner': cls.partner_b},
                {'account': cls.account_cash, 'credit': 200.0},
            ],
            date=fields.Date.from_string('2026-07-01'),
        )

    def setUp(self):
        super().setUp()
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }

    def _assert_pdf_bytes(self, content, label):
        # In --test-enable mode Odoo returns fully rendered HTML instead of
        # spawning wkhtmltopdf against the test HTTP server. Convert that
        # already-rendered document locally, then enforce real PDF magic;
        # HTML alone is never a passing PDF smoke result.
        self.assertIsInstance(content, (bytes, bytearray))
        is_html = content[:9].lower().startswith(b'<!doctype') or \
            content[:5].lower().startswith(b'<html')
        if is_html:
            content = self.env['ir.actions.report']._run_wkhtmltopdf(
                [bytes(content).decode('utf-8')])
        self.assertEqual(content[:4], b'%PDF',
                         "%s did not produce PDF magic" % label)
        self.assertGreater(
            len(content), 100,
            "%s render is suspiciously small (%d bytes)"
            % (label, len(content)),
        )

    def test_trial_balance_pdf(self):
        report = self.reports.get('trial_balance')
        self.assertTrue(report)
        content = report.render_pdf(self.options)
        self._assert_pdf_bytes(content, 'Trial Balance')

    def test_profit_and_loss_pdf(self):
        report = self.reports.get('profit_and_loss')
        self.assertTrue(report)
        content = report.render_pdf(self.options)
        self._assert_pdf_bytes(content, 'Profit and Loss')

    def test_balance_sheet_pdf(self):
        report = self.reports.get('balance_sheet')
        self.assertTrue(report)
        content = report.render_pdf(self.options)
        self._assert_pdf_bytes(content, 'Balance Sheet')

    def test_general_ledger_pdf(self):
        report = self.reports.get('general_ledger')
        self.assertTrue(report)
        content = report.render_pdf(self.options)
        self._assert_pdf_bytes(content, 'General Ledger')

    def test_partner_ledger_pdf(self):
        report = self.reports.get('partner_ledger')
        self.assertTrue(report)
        content = report.render_pdf(self.options)
        self._assert_pdf_bytes(content, 'Partner Ledger')

    def test_aged_receivable_pdf(self):
        report = self.reports.get('aged_receivable')
        self.assertTrue(report)
        content = report.render_pdf(self.options)
        self._assert_pdf_bytes(content, 'Aged Receivable')

    def test_aged_payable_pdf(self):
        report = self.reports.get('aged_payable')
        self.assertTrue(report)
        content = report.render_pdf(self.options)
        self._assert_pdf_bytes(content, 'Aged Payable')

    def test_cash_flow_pdf(self):
        report = self.reports.get('cash_flow')
        self.assertTrue(report)
        content = report.render_pdf(self.options)
        self._assert_pdf_bytes(content, 'Cash Flow Statement')

    def test_pdf_execution_hashes_exact_bytes_without_nested_render(self):
        report = self.reports.get('trial_balance')
        self.assertTrue(report)
        Execution = self.env['eh.account.report.execution']
        previous = Execution.search([], limit=1, order='id desc').id or 0
        options = dict(self.options, audit_nonce='real-pdf-result-hash')

        # QWeb must consume the payload already produced by the private export
        # lifecycle. Calling public render() here would create a second JSON
        # execution and repeat report computation.
        with patch.object(
            type(report),
            'render',
            side_effect=AssertionError("PDF performed a nested JSON render"),
        ):
            content = report.render_pdf(options, use_cache=False)

        self._assert_pdf_bytes(content, 'Audited Trial Balance')
        executions = Execution.search([
            ('id', '>', previous),
            ('report_code', '=', report.code),
        ])
        self.assertEqual(len(executions), 1)
        execution = executions.ensure_one()
        self.assertEqual(execution.result_format, 'pdf')
        self.assertEqual(
            execution.result_hash,
            hashlib.sha256(content).hexdigest(),
        )
        self.assertTrue(execution.cache_trusted)
        self.assertTrue(execution.result_payload)

    def test_export_pdf_attachment_returns_download_action(self):
        report = self.reports.get('trial_balance')
        action = report.export_pdf_attachment(self.options)
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertIn('/web/content/', action['url'])
        attachment_id = int(
            action['url'].split('/web/content/')[1].split('?')[0]
        )
        attachment = self.env['ir.attachment'].browse(attachment_id)
        self.assertTrue(attachment.exists())
        self.assertEqual(attachment.mimetype, 'application/pdf')
        self.assertTrue(attachment.name.endswith('.pdf'))
        self.assertEqual(
            attachment.res_model,
            'eh.account.report.wizard',
        )
        owner = self.env[attachment.res_model].browse(attachment.res_id)
        self.assertEqual(owner.create_uid, self.env.user)

        other_user = new_test_user(
            self.env,
            login='eh_pdf_export_other_user',
            groups='eh_account_base.group_eh_user',
        )
        with self.assertRaises(AccessError):
            attachment.with_user(other_user).read(['name', 'datas'])

    def test_read_only_auditor_can_create_private_pdf_export(self):
        report = self.reports.get('trial_balance')
        auditor = new_test_user(
            self.env,
            login='eh_pdf_export_auditor',
            groups='eh_account_base.group_eh_auditor',
        )
        with patch.object(type(report), 'render_pdf', return_value=b'%PDF-audit'):
            action = report.with_user(auditor).export_pdf_attachment(
                self.options,
            )
        attachment_id = int(
            action['url'].split('/web/content/')[1].split('?')[0]
        )
        attachment = self.env['ir.attachment'].sudo().browse(attachment_id)
        self.assertEqual(attachment.create_uid, auditor)
        self.assertEqual(attachment.res_model, 'eh.account.report.wizard')
        owner = self.env[attachment.res_model].sudo().browse(
            attachment.res_id,
        )
        self.assertEqual(owner.create_uid, auditor)
        self.assertTrue(
            self.env['ir.attachment'].with_user(auditor).search([
                ('id', '=', attachment.id),
            ])
        )


@tagged('eh_account_dynamic_reports', 'unit')
class TestPdfFormattingHelpers(EhAccountIntegrationTestCase):
    """Unit tests for the PDF abstract model's formatting helpers.

    These do not call wkhtmltopdf and run quickly; they verify the
    transformations from raw payload values into display strings.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.helper = cls.env[
            'report.eh_account_dynamic_reports.report_dynamic_pdf_template'
        ]

    def test_format_monetary_positive(self):
        self.assertEqual(self.helper._format_value(1234.56, 'monetary'),
                         '1,234.56')

    def test_format_monetary_negative(self):
        self.assertEqual(self.helper._format_value(-1234.56, 'monetary'),
                         '(1,234.56)')

    def test_format_monetary_zero(self):
        self.assertEqual(self.helper._format_value(0.0, 'monetary'), '0.00')

    def test_format_integer(self):
        self.assertEqual(self.helper._format_value(1234567, 'integer'),
                         '1,234,567')

    def test_format_zero_decimal_currency(self):
        self.assertEqual(
            self.helper._format_value(
                1234.4,
                'monetary',
                {
                    'symbol': '¥',
                    'position': 'before',
                    'decimal_places': 0,
                    'multi_currency': False,
                },
            ),
            '¥ 1,234',
        )

    def test_format_string_passthrough(self):
        self.assertEqual(self.helper._format_value('hello', 'string'), 'hello')

    def test_format_none_yields_empty(self):
        self.assertEqual(self.helper._format_value(None, 'monetary'), '')
        self.assertEqual(self.helper._format_value('', 'string'), '')

    def test_line_css_class_for_section_header(self):
        line = {'level': 0, 'meta': {'kind': 'section_header'}}
        css = self.helper._line_css_class(line)
        self.assertIn('eh_pdf_section_row', css)
        self.assertIn('eh_pdf_section_header', css)

    def test_line_css_class_for_balance_check(self):
        line = {'level': 0, 'meta': {'kind': 'balance_check'}}
        css = self.helper._line_css_class(line)
        self.assertIn('eh_pdf_balance_check', css)

    def test_line_css_class_for_data_row(self):
        line = {'level': 1, 'meta': {'kind': 'aml'}}
        css = self.helper._line_css_class(line)
        self.assertIn('eh_pdf_data_row', css)

    def test_render_lines_produces_cells(self):
        payload = {
            'columns': [
                {'expression_label': 'account', 'name': 'Account',
                 'figure_type': 'string'},
                {'expression_label': 'amount', 'name': 'Amount',
                 'figure_type': 'monetary'},
            ],
            'lines': [
                {
                    'id': 'l1', 'name': 'Cash', 'level': 1,
                    'columns': [
                        {'expression_label': 'amount', 'value': 1234.56},
                    ],
                    'meta': {'kind': 'aml'},
                },
            ],
        }
        rendered = self.helper._render_lines(payload)
        self.assertEqual(len(rendered), 1)
        line = rendered[0]
        self.assertEqual(line['name'], 'Cash')
        self.assertEqual(len(line['cells']), 1)
        self.assertEqual(line['cells'][0]['display'], '1,234.56')
        self.assertTrue(line['cells'][0]['align_right'])

    def test_grouped_pdf_headers_preserve_valid_spans(self):
        columns = [
            {'name': 'Account'}, {'name': 'Current A'},
            {'name': 'Current B'}, {'name': 'Prior A'},
            {'name': 'Prior B'},
        ]
        rows = [
            [
                {'name': 'Account', 'rowspan': 2},
                {'name': 'Current', 'colspan': 2},
                {'name': 'Prior', 'colspan': 2},
            ],
            [
                {'name': 'A'}, {'name': 'B'},
                {'name': 'A'}, {'name': 'B'},
            ],
        ]
        normalised = self.helper._normalise_column_header_rows(
            columns, rows,
        )
        self.assertEqual(len(normalised), 2)
        self.assertEqual(normalised[0][0], {
            'name': 'Account', 'rowspan': 2, 'colspan': 1,
            'is_label': True,
        })
        self.assertEqual(normalised[0][1]['colspan'], 2)

    def test_malformed_pdf_headers_fail_closed(self):
        columns = [{'name': 'Account'}, {'name': 'Amount'}]
        malformed = (
            [[{'name': 'Overflow', 'colspan': 3}]],
            [[{'name': 'Account'}, {'name': 'Bad', 'rowspan': 2}]],
            [[{'name': 'Boolean span', 'colspan': True}]],
        )
        for rows in malformed:
            with self.subTest(rows=rows):
                self.assertEqual(
                    self.helper._normalise_column_header_rows(columns, rows),
                    [],
                )

    def test_pdf_template_renders_grouped_header_branch(self):
        template = self.env.ref(
            'eh_account_dynamic_reports.report_dynamic_pdf_template'
        )
        root = etree.fromstring(template.arch_db.encode('utf-8'))
        self.assertEqual(
            len(root.xpath(
                ".//thead//tr["
                "@t-foreach=\"chunk.get('column_header_rows')\"]"
            )),
            1,
        )
        self.assertEqual(
            len(root.xpath(
                ".//thead//th[@t-att-colspan="
                "\"header_cell.get('colspan')\"]"
            )),
            1,
        )

    def test_pdf_single_chunk_preserves_legacy_column_order(self):
        columns = [
            {'expression_label': 'account', 'name': 'Account'},
            {'expression_label': 'debit', 'name': 'Debit'},
            {'expression_label': 'credit', 'name': 'Credit'},
        ]
        lines = [{
            'name': 'Cash',
            'cells': [
                {'display': '10.00'}, {'display': '2.00'},
            ],
        }]
        chunks = self.helper._build_pdf_table_chunks(columns, lines)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]['columns'], columns)
        self.assertEqual(chunks[0]['lines'][0]['cells'], lines[0]['cells'])
        self.assertEqual(chunks[0]['value_from'], 1)
        self.assertEqual(chunks[0]['value_to'], 2)
        self.assertEqual(chunks[0]['column_header_rows'], [])

    def test_pdf_48_value_columns_are_legibly_chunked_without_duplicates(self):
        columns = [
            {'expression_label': 'account', 'name': 'Account'},
        ] + [
            {'expression_label': 'value_%02d' % index,
             'name': 'Value %02d' % index}
            for index in range(1, 49)
        ]
        lines = [{
            'name': 'Cash',
            'cells': [
                {'display': str(index)} for index in range(1, 49)
            ],
        }]
        chunks = self.helper._build_pdf_table_chunks(columns, lines)
        self.assertEqual(len(chunks), 6)
        self.assertTrue(all(len(chunk['columns']) == 9 for chunk in chunks))
        self.assertTrue(all(
            chunk['columns'][0]['expression_label'] == 'account'
            for chunk in chunks
        ))
        expressions = [
            column['expression_label']
            for chunk in chunks
            for column in chunk['columns'][1:]
        ]
        self.assertEqual(
            expressions,
            ['value_%02d' % index for index in range(1, 49)],
        )
        displays = [
            cell['display']
            for chunk in chunks
            for cell in chunk['lines'][0]['cells']
        ]
        self.assertEqual(displays, [str(index) for index in range(1, 49)])
        self.assertEqual(
            [(chunk['value_from'], chunk['value_to']) for chunk in chunks],
            [(1, 8), (9, 16), (17, 24), (25, 32), (33, 40), (41, 48)],
        )

    def test_pdf_chunks_clip_grouped_headers_to_printed_values(self):
        columns = [{'name': 'Account'}] + [
            {'name': 'Value %d' % index} for index in range(1, 11)
        ]
        header_rows = self.helper._normalise_column_header_rows(
            columns,
            [
                [
                    {'name': 'Account', 'rowspan': 2},
                    {'name': 'Period A', 'colspan': 5},
                    {'name': 'Period B', 'colspan': 5},
                ],
                [
                    {'name': 'A%d' % index} for index in range(1, 6)
                ] + [
                    {'name': 'B%d' % index} for index in range(1, 6)
                ],
            ],
        )
        chunks = self.helper._build_pdf_table_chunks(
            columns, [], header_rows,
        )
        self.assertEqual(len(chunks), 2)
        self.assertEqual(
            [(cell['name'], cell['colspan'])
             for cell in chunks[0]['column_header_rows'][0]],
            [('Account', 1), ('Period A', 5), ('Period B', 3)],
        )
        self.assertEqual(
            [(cell['name'], cell['colspan'])
             for cell in chunks[1]['column_header_rows'][0]],
            [('Account', 1), ('Period B', 2)],
        )
        self.assertEqual(
            [cell['name']
             for cell in chunks[1]['column_header_rows'][1]],
            ['B4', 'B5'],
        )

    def test_pdf_template_repeats_label_column_in_page_chunks(self):
        template = self.env.ref(
            'eh_account_dynamic_reports.report_dynamic_pdf_template'
        )
        root = etree.fromstring(template.arch_db.encode('utf-8'))
        self.assertEqual(
            len(root.xpath(
                ".//div[@t-foreach=\"r.get('table_chunks') or []\"]"
            )),
            1,
        )
        css = ''.join(root.xpath('.//style/text()'))
        self.assertIn('.eh_pdf_column_chunk_break', css)
        self.assertIn('page-break-before: always', css)
        self.assertIn('table-layout: fixed', css)

    def test_pdf_header_company_follows_rendered_scope(self):
        company_b = self.env['res.company'].with_context(
            default_group_rfq='default',
        ).create({'name': 'PDF Scope Company B'})
        self.env.user.sudo().write({'company_ids': [(4, company_b.id)]})
        # Deliberately keep company A as the only active context company.
        # Report scope was authorised against user.company_ids already and a
        # PDF for company B must not inherit company A branding from context.
        helper = self.helper.with_context(
            allowed_company_ids=[self.company.id],
        )

        company, companies = helper._resolve_pdf_company_scope({
            'meta': {'company_ids': [company_b.id]},
        })
        self.assertEqual(company, company_b)
        self.assertEqual(companies, company_b)

        company, companies = helper._resolve_pdf_company_scope(
            {'meta': {'company_ids': [self.company.id, company_b.id]}},
            {'primary_company_id': company_b.id},
        )
        self.assertEqual(company, company_b)
        self.assertEqual(set(companies.ids), {self.company.id, company_b.id})

    def test_render_lines_honours_per_cell_figure_types(self):
        payload = {
            'columns': [
                {'expression_label': 'metric', 'name': 'Metric',
                 'figure_type': 'string'},
                {'expression_label': 'value', 'name': 'Value',
                 'figure_type': 'string'},
            ],
            'currency': {
                'name': 'AUD', 'symbol': '$', 'position': 'before',
                'decimal_places': 2,
            },
            'lines': [
                {'id': 'money', 'name': 'Revenue', 'level': 1,
                 'columns': [{
                     'expression_label': 'value', 'value': 1234.56,
                     'figure_type': 'monetary',
                 }]},
                {'id': 'ratio', 'name': 'Margin', 'level': 1,
                 'columns': [{
                     'expression_label': 'value', 'value': 0.25,
                     'figure_type': 'percentage',
                 }]},
                {'id': 'undefined', 'name': 'Undefined', 'level': 1,
                 'columns': [{
                     'expression_label': 'value', 'value': 'n/a',
                     'figure_type': 'percentage',
                 }]},
            ],
        }
        rendered = self.helper._render_lines(payload)
        self.assertEqual(rendered[0]['cells'][0]['display'], '$ 1,234.56')
        self.assertEqual(rendered[1]['cells'][0]['display'], '25.00%')
        self.assertEqual(rendered[2]['cells'][0]['display'], 'n/a')
        self.assertTrue(all(
            line['cells'][0]['align_right'] for line in rendered
        ))

    def test_dynamic_pdf_uses_only_fixed_layout_footer(self):
        """Do not append duplicate company footer in flowing page body.

        ``eh_clean_layout`` owns fixed company/page footer.  Adding
        ``eh_report_footer`` inside ``.eh_pdf_page`` can create a blank
        trailing page when report table reaches printable boundary.
        """
        template = self.env.ref(
            'eh_account_dynamic_reports.report_dynamic_pdf_template'
        )
        root = etree.fromstring(template.arch_db.encode('utf-8'))
        self.assertEqual(
            root.xpath(
                ".//t[@t-call='eh_account_base.eh_report_footer']"
            ),
            [],
        )
        self.assertEqual(
            len(root.xpath(
                ".//t[@t-call='eh_account_base.eh_clean_layout']"
            )),
            1,
        )

    def test_mobile_filter_width_does_not_stretch_checkboxes(self):
        scss = (
            Path(__file__).parents[1] / 'static' / 'src' / 'components'
            / 'dynamic_report' / 'dynamic_report.scss'
        ).read_text()
        self.assertIn(
            '.eh_dr .eh_dr_filter input:not([type="checkbox"])', scss,
        )
        self.assertNotIn('.eh_dr .eh_dr_filter input {', scss)

    def test_wide_axis_matrix_scroll_stays_inside_report_body(self):
        scss = (
            Path(__file__).parents[1] / 'static' / 'src' / 'components'
            / 'dynamic_report' / 'dynamic_report.scss'
        ).read_text()
        self.assertIn('overflow: auto;', scss)
        self.assertIn('width: max-content;', scss)
        self.assertIn('min-width: 100%;', scss)
        self.assertIn('.eh_dr_axis_group', scss)

    def test_grouped_sticky_header_uses_one_enforced_row_height(self):
        component_dir = (
            Path(__file__).parents[1] / 'static' / 'src' / 'components'
            / 'dynamic_report'
        )
        scss = (component_dir / 'dynamic_report.scss').read_text()
        xml = (component_dir / 'dynamic_report.xml').read_text()
        self.assertIn('--eh-dr-header-row-height: 34px;', scss)
        self.assertIn(
            'height: var(--eh-dr-header-row-height);', scss,
        )
        self.assertIn(
            'calc(var(--eh-dr-header-row-height) * '
            '{{headerRow_index}})',
            xml,
        )
        self.assertNotIn('headerRow_index * 33', xml)


@tagged('eh_account_dynamic_reports', 'integration', 'post_install',
        '-at_install')
class TestDynamicReportRoleVisibility(EhAccountIntegrationTestCase):

    MENU_XMLIDS = (
        'menu_executive_summary',
        'menu_trial_balance',
        'menu_profit_and_loss',
        'menu_balance_sheet',
        'menu_general_ledger',
        'menu_partner_ledger',
        'menu_aged_receivable',
        'menu_aged_payable',
        'menu_cash_flow',
        'menu_deferred_revenue',
        'menu_deferred_expense',
        'menu_bank_reconciliation',
        'menu_eh_report_analytic_balance',
        'menu_eh_noncash_transaction',
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plain_accountant = new_test_user(
            cls.env,
            login='eh_report_plain_accountant',
            groups='account.group_account_user',
        )
        cls.eh_user = new_test_user(
            cls.env,
            login='eh_report_role_user',
            groups='eh_account_base.group_eh_user',
        )
        cls.eh_auditor = new_test_user(
            cls.env,
            login='eh_report_role_auditor',
            groups='eh_account_base.group_eh_auditor',
        )
        cls.menus = cls.env['ir.ui.menu'].browse([
            cls.env.ref(
                'eh_account_dynamic_reports.%s' % xmlid,
            ).id
            for xmlid in cls.MENU_XMLIDS
        ])

    def test_menu_groups_match_dynamic_report_model_access(self):
        expected = {
            self.env.ref('eh_account_base.group_eh_user').id,
            self.env.ref('eh_account_base.group_eh_auditor').id,
        }
        for menu in self.menus:
            self.assertEqual(
                set(menu.group_ids.ids),
                expected,
                '%s must use the EH report roles' % menu.complete_name,
            )

    def test_menu_data_unlinks_legacy_account_role_on_upgrade(self):
        """Keep XML upgrade semantics, not only fresh-install end state."""
        module_root = Path(__file__).resolve().parents[1]
        menu_nodes = {}
        for relative_path in (
                'data/menus.xml',
                'data/reports.xml',
                'views/noncash_transaction_views.xml'):
            root = etree.parse(str(module_root / relative_path))
            for node in root.xpath('.//menuitem[@id]'):
                menu_nodes[node.get('id')] = node

        expected_groups = {
            '-account.group_account_user',
            'eh_account_base.group_eh_user',
            'eh_account_base.group_eh_auditor',
        }
        for xmlid in self.MENU_XMLIDS:
            self.assertIn(xmlid, menu_nodes)
            self.assertEqual(
                set(menu_nodes[xmlid].get('groups', '').split(',')),
                expected_groups,
                '%s must unlink the legacy accountant menu role on upgrade'
                % xmlid,
            )

    def test_plain_accountant_has_no_dead_report_menus(self):
        visible = self.env['ir.ui.menu'].with_user(
            self.plain_accountant,
        )._visible_menu_ids()
        self.assertFalse(set(self.menus.ids) & set(visible))

    def test_eh_user_and_auditor_can_reach_report_menus(self):
        for user in (self.eh_user, self.eh_auditor):
            visible = self.env['ir.ui.menu'].with_user(
                user,
            )._visible_menu_ids()
            self.assertEqual(
                set(self.menus.ids) - set(visible),
                set(),
                '%s is missing an authorised EH report menu' % user.login,
            )

    def test_partner_statement_buttons_follow_the_same_roles(self):
        view = self.env.ref(
            'eh_account_dynamic_reports.view_partner_statement_buttons',
        )
        for user, expected in (
            (self.plain_accountant, False),
            (self.eh_user, True),
            (self.eh_auditor, True),
        ):
            arch = self.env['res.partner'].with_user(user).get_view(
                view_id=view.id,
                view_type='form',
            )['arch']
            self.assertEqual(
                'action_print_customer_statement' in arch,
                expected,
                'partner statement role mismatch for %s' % user.login,
            )
