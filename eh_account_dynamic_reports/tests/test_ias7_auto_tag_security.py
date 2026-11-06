# -*- encoding: utf-8 -*-
"""Security regressions for IAS 7 automatic account tagging."""

from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged
from odoo.tests.common import TransactionCase


@tagged('eh_account_dynamic_reports', 'post_install', '-at_install')
class TestIas7AutoTagSecurity(TransactionCase):

    def test_basic_user_cannot_run_privileged_tagging(self):
        user = new_test_user(
            self.env,
            login='eh_ias7_basic_user',
            groups='eh_account_base.group_eh_user',
        )
        with self.assertRaises(AccessError):
            self.env['eh.noncash.transaction'].with_user(
                user
            ).action_eh_ias7_auto_tag()

    def test_manager_cannot_tag_foreign_company_account(self):
        company = self.env['res.company'].create({
            'name': 'IAS 7 Foreign Company',
        })
        Account = self.env['account.account'].with_company(company).sudo()
        vals = {
            'name': 'Foreign Depreciation Expense',
            'code': '991799',
            'account_type': 'expense_depreciation',
        }
        if 'company_ids' in Account._fields:
            vals['company_ids'] = [(6, 0, [company.id])]
        else:
            vals['company_id'] = company.id
        foreign_account = Account.create(vals)
        tag = self.env.ref(
            'eh_account_dynamic_reports.account_tag_noncash_depreciation'
        )
        foreign_account.write({'tag_ids': [(3, tag.id)]})

        manager = new_test_user(
            self.env,
            login='eh_ias7_scoped_manager',
            groups='eh_account_base.group_eh_manager',
            company_id=self.env.company.id,
            company_ids=[(6, 0, [self.env.company.id])],
        )
        self.env['eh.noncash.transaction'].with_user(manager).with_context(
            allowed_company_ids=[self.env.company.id],
        ).action_eh_ias7_auto_tag()

        foreign_account.invalidate_recordset(['tag_ids'])
        self.assertNotIn(tag, foreign_account.tag_ids)

    def test_branch_manager_tags_root_account_not_sibling_account(self):
        Account = self.env['account.account']
        if not callable(getattr(Account, '_check_company_domain', None)):
            self.skipTest(
                "Ancestor-owned accounts are not branch-shared on Odoo 16"
            )

        root = self.env.company
        branch = self.env['res.company'].create({
            'name': 'IAS 7 Allowed Branch',
            'parent_id': root.id,
        })
        sibling = self.env['res.company'].create({
            'name': 'IAS 7 Foreign Sibling',
            'parent_id': root.id,
        })

        def _depreciation_account(company, name, code):
            company_account = Account.with_company(company).sudo()
            vals = {
                'name': name,
                'code': code,
                'account_type': 'expense_depreciation',
            }
            if 'company_ids' in company_account._fields:
                vals['company_ids'] = [(6, 0, [company.id])]
            else:
                vals['company_id'] = company.id
            return company_account.create(vals)

        root_account = _depreciation_account(
            root, 'Root Depreciation Expense', '991797')
        sibling_account = _depreciation_account(
            sibling, 'Sibling Depreciation Expense', '991798')
        tag = self.env.ref(
            'eh_account_dynamic_reports.account_tag_noncash_depreciation'
        )
        (root_account | sibling_account).write({'tag_ids': [(3, tag.id)]})

        manager = new_test_user(
            self.env,
            login='eh_ias7_branch_manager',
            groups='eh_account_base.group_eh_manager',
            company_id=branch.id,
            company_ids=[(6, 0, [branch.id])],
        )
        self.env['eh.noncash.transaction'].with_user(manager).with_company(
            branch
        ).with_context(
            allowed_company_ids=[branch.id],
        ).action_eh_ias7_auto_tag()

        root_account.invalidate_recordset(['tag_ids'])
        sibling_account.invalidate_recordset(['tag_ids'])
        self.assertIn(tag, root_account.tag_ids)
        self.assertNotIn(tag, sibling_account.tag_ids)
