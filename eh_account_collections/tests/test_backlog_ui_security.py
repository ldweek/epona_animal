# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Focused metadata and real-actor regressions for the collections UI."""

from lxml import etree

from odoo.exceptions import AccessError
from odoo.tests import tagged

from odoo.addons.eh_account_base.tests.common import EhAccountIntegrationTestCase


@tagged('eh_account_collections', 'integration', 'post_install', '-at_install')
class TestCollectionsBacklogUiSecurity(EhAccountIntegrationTestCase):

    def _arch(self, xmlid):
        view = self.env.ref(xmlid)
        return etree.fromstring(view.arch_db.encode())

    def test_followup_and_stage_controls_are_configurable(self):
        form = self._arch(
            'eh_account_collections.view_eh_followup_level_form',
        )
        listed = self._arch(
            'eh_account_collections.view_eh_followup_level_list',
        )
        for field_name in (
            'also_send_sms', 'sms_template_id', 'responsible_type',
        ):
            self.assertTrue(form.xpath(".//field[@name='%s']" % field_name))
            self.assertTrue(listed.xpath(".//field[@name='%s']" % field_name))
        for xmlid in (
            'eh_account_collections.view_eh_collections_stage_tree',
            'eh_account_collections.view_eh_collections_stage_form',
        ):
            self.assertTrue(
                self._arch(xmlid).xpath(".//field[@name='is_escalated']"),
            )

    def test_action_log_is_readonly_and_days_are_not_summed(self):
        form = self._arch('eh_account_collections.view_eh_collections_case_form')
        embedded = form.xpath(
            ".//field[@name='action_ids']/*[self::list or self::tree]",
        )[0]
        self.assertNotIn('editable', embedded.attrib)
        self.assertEqual(embedded.get('create'), '0')
        self.assertEqual(embedded.get('delete'), '0')
        for field in embedded.xpath(
            "./field[not(@column_invisible) and not(@invisible='1')]",
        ):
            self.assertEqual(field.get('readonly'), '1')
        for xmlid in (
            'eh_account_collections.view_eh_collections_action_tree',
            'eh_account_collections.view_eh_collections_action_form',
        ):
            action_view = self._arch(xmlid)
            root = action_view
            self.assertEqual(root.get('create'), '0')
            self.assertEqual(root.get('edit'), '0')
            self.assertEqual(root.get('delete'), '0')
        listing = self._arch('eh_account_collections.view_eh_collections_case_tree')
        days = listing.xpath(".//field[@name='days_overdue_max']")[0]
        self.assertNotIn('sum', days.attrib)

    def test_shipped_ladder_uses_branded_templates(self):
        mapping = {
            'followup_level_1': 'email_template_eh_dunning_friendly',
            'followup_level_2': 'email_template_eh_dunning_firm',
            'followup_level_3': 'email_template_eh_dunning_final',
        }
        for level_xmlid, template_xmlid in mapping.items():
            level = self.env.ref('eh_account_collections.' + level_xmlid)
            template = self.env.ref('eh_account_collections.' + template_xmlid)
            self.assertEqual(level.mail_template_id, template)
            self.assertNotIn('\u2014', template.name)
            self.assertNotIn('\u2014', template.subject)

    def test_followup_menu_group_matches_manager_acl(self):
        menu = self.env.ref('eh_account_collections.menu_eh_followup_level')
        self.assertIn(
            self.env.ref('eh_account_base.group_eh_manager'),
            menu.group_ids,
        )

    def test_auditor_can_read_complete_timeline_and_visible_menus(self):
        level = self.env.ref('eh_account_collections.followup_level_1')
        case = self.env['eh.collections.case'].create({
            'partner_id': self.partner_a.id,
            'days_overdue_max': 20,
        })
        action = case.action_log_action('note', 'Auditor evidence')
        auditor = self.env['res.users'].create({
            'name': 'Collections Auditor',
            'login': 'collections_auditor@test',
            'company_id': self.company.id,
            'company_ids': [(6, 0, self.company.ids)],
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('eh_account_base.group_eh_auditor').id,
            ])],
        })
        self.assertEqual(
            self.env['eh.collections.case'].with_user(auditor).search([
                ('id', '=', case.id),
            ]),
            case,
        )
        self.assertEqual(
            self.env['eh.collections.action'].with_user(auditor)
            .search([('id', '=', action.id)]),
            action,
        )
        self.assertTrue(
            self.env['eh.collections.stage'].with_user(auditor).search([], limit=1),
        )
        self.assertEqual(
            self.env['eh.collections.followup.level'].with_user(auditor)
            .search([('id', '=', level.id)]),
            level,
        )
        with self.assertRaises(AccessError):
            case.with_user(auditor).write({'priority': '3'})
        visible = self.env['ir.ui.menu'].with_user(auditor)._visible_menu_ids()
        for xmlid in (
            'menu_eh_collections_root',
            'menu_eh_collections_workbench',
            'menu_eh_collections_action_log',
        ):
            self.assertIn(
                self.env.ref('eh_account_collections.' + xmlid).id,
                visible,
            )
