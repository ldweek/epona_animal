# -*- encoding: utf-8 -*-
##############################################################################
# ERP Heritage  -  Copyright (C) 2026 (https://www.erpheritage.com.au/)
##############################################################################
"""
BAS run lifecycle: draft -> computed -> lodged. Lodged is read-only.
"""

from datetime import date
from unittest.mock import patch

from lxml import etree
from psycopg2 import errors

from odoo.exceptions import UserError, ValidationError

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


class BasRunLifecycleTest(EhAccountIntegrationTestCase):

    def setUp(self):
        super().setUp()
        au = self.env.ref('base.au')
        self.env.company.sudo().write({
            'country_id': au.id,
            'account_fiscal_country_id': au.id,
        })
        self.run = self.env['eh.bas.run'].create({
            'company_id': self.env.company.id,
            'date_from': date(2026, 4, 1),
            'date_to': date(2026, 6, 30),
            'name': 'BAS Q4-2026',
        })

    def test_initial_state_is_draft(self):
        self.assertEqual(self.run.state, 'draft')

    def test_compute_advances_to_computed(self):
        self.run.action_compute()
        self.assertEqual(self.run.state, 'computed')

    def test_recompute_on_lodged_raises(self):
        self.run.action_compute()
        self.run.action_mark_lodged()
        self.assertEqual(self.run.state, 'lodged')
        with self.assertRaises(UserError):
            self.run.action_compute()

    def test_original_is_unique_but_lodged_run_creates_amendment(self):
        with self.assertRaises(UserError):
            self.env['eh.bas.run'].create({
                'company_id': self.env.company.id,
                'date_from': date(2026, 4, 1),
                'date_to': date(2026, 6, 30),
                'name': 'BAS Q4-2026 duplicate',
            })
        self.run.action_compute()
        self.run.action_mark_lodged()

        action = self.run.action_create_amendment()
        amendment = self.env['eh.bas.run'].browse(action['res_id'])
        self.assertEqual(amendment.amendment_no, 1)
        self.assertEqual(amendment.amends_run_id, self.run)
        self.assertEqual(amendment.state, 'draft')

    def test_reset_and_recompute_preserve_manual_amount_and_notes(self):
        self.run.action_compute()
        manual = self.run.line_ids.filtered(
            lambda line: line.label_aggregation == 'manual'
        )[:1]
        self.assertTrue(manual)
        manual_label = manual.label_id
        manual.write({'amount': 123.45, 'notes': 'Reviewer adjustment'})

        self.run.action_reset_draft()
        self.assertEqual(manual.amount, 123.45)
        self.assertEqual(manual.notes, 'Reviewer adjustment')
        self.run.action_compute()

        rebuilt = self.run.line_ids.filtered(
            lambda line: line.label_id == manual_label
        )
        self.assertEqual(rebuilt.amount, 123.45)
        self.assertEqual(rebuilt.notes, 'Reviewer adjustment')

    def test_compute_does_not_lock_res_company_row(self):
        seen = []
        execute = self.env.cr.execute

        def record(*args, **kwargs):
            if args:
                seen.append(str(args[0]))
            return execute(*args, **kwargs)

        with patch.object(self.env.cr, 'execute', record):
            self.run.action_compute()
        self.assertFalse(any(
            'res_company' in sql and 'FOR UPDATE' in sql.upper()
            for sql in seen
        ))

    def test_lodgement_sets_tax_lock_and_rejects_draft_tax_entry(self):
        self.run.action_compute()
        tag = self.env['account.account.tag'].sudo()._get_tax_tags(
            'G1', self.env.ref('base.au').id,
        )[:1]
        draft = self.env['account.move'].create({
            'move_type': 'entry',
            'date': self.run.date_to,
            'journal_id': self.journal_misc.id,
            'line_ids': [(0, 0, {
                'name': 'Draft taxable sale',
                'account_id': self.account_revenue.id,
                'credit': 100.0,
                'tax_tag_ids': [(6, 0, tag.ids)],
            }), (0, 0, {
                'name': 'Draft taxable sale clearing',
                'account_id': self.account_cash.id,
                'debit': 100.0,
            })],
        })
        with self.assertRaises(UserError):
            self.run.action_mark_lodged()
        draft.unlink()

        self.run.action_mark_lodged()

        self.assertEqual(self.run.state, 'lodged')
        self.assertGreaterEqual(self.company.tax_lock_date, self.run.date_to)
        self.assertEqual(
            self.run.tax_lock_date_snapshot, self.company.tax_lock_date,
        )

    def test_audit_row_written_on_compute(self):
        before = self.env['eh.account.report.execution'].search_count([])
        self.run.action_compute()
        after = self.env['eh.account.report.execution'].search_count([])
        self.assertGreater(
            after, before,
            "Compute did not write a report.execution audit row",
        )

    def test_failed_compute_is_atomic_and_requests_durable_audit(self):
        self.run.action_compute()
        self.run.action_reset_draft()
        before_ids = self.run.line_ids.ids
        Execution = self.env['eh.account.report.execution']

        def fail_with_aborted_cursor(run):
            run.env.cr.execute("SELECT * FROM eh_bas_missing_failure_table")

        with patch.object(
            type(self.run), '_bas_automatic_amounts',
            new=fail_with_aborted_cursor,
        ), patch.object(
            type(Execution), 'record_failure_durable', return_value=True,
        ) as durable:
            with self.assertRaises(errors.UndefinedTable):
                self.run.action_compute()

        self.env.cr.execute("SELECT 1")
        self.run.invalidate_recordset(['line_ids', 'state'])
        self.assertEqual(self.run.state, 'draft')
        self.assertEqual(self.run.line_ids.ids, before_ids)
        durable.assert_called_once()

    def test_w5_is_computed_from_reviewed_w2_w3_w4(self):
        self.run.action_compute()
        for code, amount in (('W2', 10.0), ('W3', 20.0), ('W4', 30.0)):
            self.run.line_ids.filtered(
                lambda line, code=code: line.label_code == code
            ).write({'amount': amount})
        self.run.action_reset_draft()
        self.run.action_compute()
        w5 = self.run.line_ids.filtered(lambda line: line.label_code == 'W5')
        self.assertEqual(w5.label_aggregation, 'payg_withheld_total')
        self.assertEqual(w5.amount, 60.0)

    def test_simpler_bas_dynamic_report_renders_only_lodgeable_snapshot(self):
        self.run.is_simpler_bas = True
        self.run.action_compute()
        report = self.env.ref('eh_account_l10n_au_bas.report_au_bas')
        self.assertEqual(
            report.handler_model,
            'eh.account.dynamic.report.handler.au_bas',
        )
        payload = self.env[report.handler_model].compute({
            'date': {
                'date_from': self.run.date_from.isoformat(),
                'date_to': self.run.date_to.isoformat(),
            },
            'company_ids': [self.run.company_id.id],
        })
        codes = {
            line.get('meta', {}).get('label_code')
            for line in payload['lines']
            if line.get('meta', {}).get('kind') == 'bas_label'
        }
        self.assertTrue({'G1', '1A', '1B', 'W1', 'W2', 'W3', 'W4', 'W5'} <= codes)
        self.assertTrue(all(
            code in {'G1', '1A', '1B', 'W1', 'W2', 'W3', 'W4', 'W5'}
            for code in codes
        ))
        self.assertEqual(payload['meta']['run_id'], self.run.id)

    def test_lodgement_rejects_wrong_currency_snapshot(self):
        self.run.action_compute()
        other_currency = self.env['res.currency'].search([
            ('id', '!=', self.run.company_id.currency_id.id),
        ], limit=1)
        self.assertTrue(other_currency)
        line = self.run.line_ids[:1]
        self.env.cr.execute(
            "UPDATE eh_bas_run_line SET currency_id = %s WHERE id = %s",
            [other_currency.id, line.id],
        )
        line.invalidate_recordset(['currency_id'])
        with self.assertRaises(UserError):
            self.run._eh_validate_lodgement_evidence()

    def test_main_worksheet_is_readonly_and_manual_inputs_are_explicit(self):
        self.run.action_compute()
        arch = etree.fromstring(self.env.ref(
            'eh_account_l10n_au_bas.view_eh_bas_run_form'
        ).arch_db.encode())
        worksheet = arch.xpath(".//field[@name='line_ids']")[0]
        self.assertEqual(worksheet.get('readonly'), '1')
        inline = worksheet.xpath('./list|./tree')[0]
        self.assertFalse(inline.get('editable'))
        self.assertEqual(inline.get('create'), 'false')
        self.assertEqual(inline.get('delete'), 'false')
        action = self.run.action_open_manual_inputs()
        self.assertIn(('label_aggregation', '=', 'manual'), action['domain'])

    def test_context_date_drives_fy_and_quarter_defaults(self):
        with patch(
            'odoo.addons.eh_account_l10n_au_bas.models.bas_run.fields.Date.context_today',
            return_value=date(2026, 2, 15),
        ):
            run = self.env['eh.bas.run'].create({
                'company_id': self.company.id,
            })
        self.assertEqual(run.fy_label, '2025-26')
        self.assertEqual(run.quarter, 'q3')

    def test_fy_suffix_must_match_start_year(self):
        with self.assertRaises(ValidationError):
            self.env['eh.bas.run'].create({
                'company_id': self.company.id,
                'fy_label': '2025-27',
                'quarter': 'q2',
            })

    def test_name_excludes_mutable_raw_state_key(self):
        original = self.run.name
        self.run.action_compute()
        self.assertEqual(self.run.name, original)
        self.assertNotIn('draft', self.run.name)
        self.assertNotIn('computed', self.run.name)

    def test_reconciliation_auditor_acl_is_read_only(self):
        acl = self.env['ir.model.access'].search([
            ('model_id.model', '=', 'eh.bas.gst.recon.result'),
            ('group_id', '=', self.env.ref(
                'eh_account_base.group_eh_auditor'
            ).id),
        ], limit=1)
        self.assertTrue(acl.perm_read)
        self.assertFalse(acl.perm_write)
        self.assertFalse(acl.perm_create)
        self.assertFalse(acl.perm_unlink)

    def test_accruals_basis_computes(self):
        # The default (accruals) basis must still compute normally: the
        # cash-basis guard must not block the supported path.
        self.assertEqual(self.run.reporting_basis, 'accruals')
        self.run.action_compute()
        self.assertEqual(self.run.state, 'computed')

    def test_cash_basis_computes_without_refusing(self):
        # Cash basis recognises GST on the payment date, apportioned by the
        # paid fraction of each invoice. It must compute (never refuse) and
        # advance to computed. With no posted taxable activity on these
        # books the labels are zero rather than accrual figures under a
        # cash label.
        self.run.reporting_basis = 'cash'
        self.run.action_compute()
        self.assertEqual(self.run.state, 'computed')
        line_1a = self.env['eh.bas.run.line'].search([
            ('run_id', '=', self.run.id),
            ('label_id.code', '=', '1A'),
        ], limit=1)
        self.assertEqual(line_1a.amount, 0.0)
