# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
WS3 annotation server-layer tests.

Covers the thin server additions that make the annotation popover work:

* add_annotation then render round-trips the note into the payload with an
  author, an ISO create date and a can_delete flag.
* can_delete is True for a manager and False for a plain report user, so the
  viewer gates the delete affordance correctly.
* delete_annotation removes the note (manager) and a re-render no longer
  shows it.
* A non-manager cannot delete: the append-only ACL (create+write but no
  unlink for group_eh_user) raises, preserving the audit posture.
* An annotation on company A is not injected when the report is scoped to
  company B only.
* show_annotations=False suppresses the injection without deleting the note.
"""

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import (
    EhAccountIntegrationTestCase,
)


@tagged('eh_account_dynamic_reports', 'integration',
        'post_install', '-at_install')
class TestReportAnnotations(EhAccountIntegrationTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.handler = cls.env[
            'eh.account.dynamic.report.handler.profit_and_loss'
        ]
        cls.report = cls.env['eh.account.dynamic.report'].search(
            [('code', '=', 'profit_and_loss')], limit=1,
        )
        if not cls.report:
            cls.report = cls.env['eh.account.dynamic.report'].create({
                'code': 'profit_and_loss',
                'name': 'Profit and Loss',
                'handler_model':
                    'eh.account.dynamic.report.handler.profit_and_loss',
            })

    def setUp(self):
        super().setUp()
        # Make the acting test user a manager so the can_delete flag and the
        # superuser-independent delete path are exercised as a real manager
        # would see them (admin is not in the custom eh manager group by
        # default). Odoo 19 renamed the relation to group_ids.
        self.env.user.group_ids = [
            (4, self.env.ref('eh_account_base.group_eh_manager').id)]
        self.options = {
            'date': {'date_from': '2026-01-01', 'date_to': '2026-12-31'},
            'company_ids': [self.company.id],
            'posted_only': True,
            'show_zero': False,
        }
        # Seed a single income move so net_profit is a real, drillable line.
        self.post_balanced_move(
            [{'account': self.account_revenue, 'credit': 1000.0},
             {'account': self.account_cash, 'debit': 1000.0}],
            date=fields.Date.from_string('2026-06-15'))

    @staticmethod
    def _line_by_id(payload, line_id):
        for line in payload['lines']:
            if line['id'] == line_id:
                return line
        return None

    # ---- create -> read round-trip ----

    def test_create_then_render_round_trips_with_meta(self):
        self.report.add_annotation('net_profit', 'Reviewed by CFO')
        payload = self.report.render(self.options, use_cache=False)
        net = self._line_by_id(payload, 'net_profit')
        notes = (net.get('meta') or {}).get('annotations') or []
        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note['text'], 'Reviewed by CFO')
        self.assertTrue(note['author'])
        # Enriched dict carries an ISO create date and a can_delete flag.
        self.assertIn('date', note)
        self.assertTrue(note['date'])
        self.assertIn('can_delete', note)
        # The seeding user is the admin/superuser, a manager -> can delete.
        self.assertTrue(note['can_delete'])

    def test_cell_level_note_lands_on_matching_column(self):
        self.report.add_annotation(
            'net_profit', 'Strong quarter', expression_label='amount')
        payload = self.report.render(self.options, use_cache=False)
        net = self._line_by_id(payload, 'net_profit')
        amount_col = next(
            c for c in net['columns']
            if c['expression_label'] == 'amount')
        self.assertIn('annotations', amount_col)
        self.assertEqual(
            amount_col['annotations'][0]['text'], 'Strong quarter')

    # ---- delete round-trip ----

    def test_delete_removes_note_from_next_render(self):
        ann = self.report.add_annotation('net_profit', 'Temporary note')
        payload = self.report.render(self.options, use_cache=False)
        net = self._line_by_id(payload, 'net_profit')
        self.assertTrue((net.get('meta') or {}).get('annotations'))

        ok = self.report.delete_annotation(ann.id)
        self.assertTrue(ok)
        self.assertFalse(ann.exists())

        payload2 = self.report.render(self.options, use_cache=False)
        net2 = self._line_by_id(payload2, 'net_profit')
        self.assertFalse((net2.get('meta') or {}).get('annotations'))

    def test_delete_unknown_id_is_a_safe_noop(self):
        # A stale UI id never errors the viewer; it returns False.
        self.assertFalse(self.report.delete_annotation(0))
        self.assertFalse(self.report.delete_annotation(999999999))

    def test_delete_rejects_note_from_other_report(self):
        # Belongs to a different report_code -> not deletable here.
        other = self.env['eh.account.report.annotation'].create({
            'report_code': 'balance_sheet',
            'line_id': 'net_profit',
            'text': 'Other report note',
            'company_id': self.company.id,
        })
        self.assertFalse(self.report.delete_annotation(other.id))
        self.assertTrue(other.exists())

    # ---- non-manager ACL ----

    def test_non_manager_cannot_delete(self):
        user = self.env['res.users'].create({
            'name': 'Plain Report User',
            'login': 'eh_ws3_plain_user',
            'group_ids': [
                (6, 0, [self.env.ref('eh_account_base.group_eh_user').id])],
        })
        ann = self.report.add_annotation('net_profit', 'Audit trail note')
        # The user group has create+write but NOT unlink (append-only); the
        # ORM raises AccessError on the unlink inside delete_annotation.
        with self.assertRaises(AccessError):
            self.report.with_user(user).delete_annotation(ann.id)
        # Note is intact: the audit posture held.
        self.assertTrue(ann.exists())

    def test_can_delete_false_for_non_manager_in_payload(self):
        user = self.env['res.users'].create({
            'name': 'Plain Report User 2',
            'login': 'eh_ws3_plain_user_2',
            'group_ids': [(6, 0, [
                self.env.ref('eh_account_base.group_eh_user').id,
                self.env.ref('account.group_account_user').id,
            ])],
        })
        self.report.add_annotation('net_profit', 'Reviewed')
        payload = self.report.with_user(user).render(
            self.options, use_cache=False)
        net = self._line_by_id(payload, 'net_profit')
        notes = (net.get('meta') or {}).get('annotations') or []
        self.assertEqual(len(notes), 1)
        self.assertFalse(notes[0]['can_delete'])

    # ---- company scoping ----

    def test_note_scoped_to_company_not_leaked(self):
        company_b = self.env['res.company'].create({'name': 'EH WS3 Co B'})
        # Note created on company B only.
        self.env['eh.account.report.annotation'].create({
            'report_code': self.report.code,
            'line_id': 'net_profit',
            'text': 'Company B only',
            'company_id': company_b.id,
        })
        # Render scoped to company A: the B note must not appear.
        payload = self.report.render(
            dict(self.options, company_ids=[self.company.id]),
            use_cache=False)
        net = self._line_by_id(payload, 'net_profit')
        notes = (net.get('meta') or {}).get('annotations') or []
        texts = [n['text'] for n in notes]
        self.assertNotIn('Company B only', texts)

    # ---- show_annotations opt-out ----

    def test_show_annotations_false_suppresses_without_deleting(self):
        ann = self.report.add_annotation('net_profit', 'Hidden but kept')
        payload = self.report.render(
            dict(self.options, show_annotations=False), use_cache=False)
        net = self._line_by_id(payload, 'net_profit')
        self.assertFalse((net.get('meta') or {}).get('annotations'))
        # The note still exists; it was only hidden.
        self.assertTrue(ann.exists())
