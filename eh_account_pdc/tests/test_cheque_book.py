# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Cheque book lifecycle and serial allocation tests."""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import EhPdcTestCase


@tagged('eh_account_pdc', 'integration', 'post_install', '-at_install')
class TestChequeBook(EhPdcTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        user_group = cls.env.ref('eh_account_base.group_eh_user')
        manager_group = cls.env.ref('eh_account_base.group_eh_manager')
        cls.book_user = cls.env['res.users'].create({
            'name': 'PDC Book Operator',
            'login': 'pdc_book_operator',
            'email': 'pdc-book-operator@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, user_group.ids)],
        })
        cls.book_manager = cls.env['res.users'].create({
            'name': 'PDC Book Manager',
            'login': 'pdc_book_manager',
            'email': 'pdc-book-manager@example.com',
            'company_id': cls.company.id,
            'company_ids': [(6, 0, cls.company.ids)],
            'group_ids': [(6, 0, manager_group.ids)],
        })

        cls.other_company = cls.env['res.company'].create({
            'name': 'PDC Foreign Company',
            'currency_id': cls.company.currency_id.id,
        })
        foreign_journals = cls.env['account.journal'].sudo().with_company(
            cls.other_company,
        )
        foreign_books = cls.env['eh.cheque.book'].sudo().with_company(
            cls.other_company,
        )
        cls.foreign_active_journal = foreign_journals.create({
            'name': 'PDC Foreign Active Bank',
            'code': 'PFAB',
            'type': 'bank',
            'company_id': cls.other_company.id,
        })
        cls.foreign_draft_journal = foreign_journals.create({
            'name': 'PDC Foreign Draft Bank',
            'code': 'PFDB',
            'type': 'bank',
            'company_id': cls.other_company.id,
        })
        cls.foreign_active_book = foreign_books.create({
            'name': 'Foreign Active Book',
            'journal_id': cls.foreign_active_journal.id,
            'start_number': 1,
            'end_number': 10,
            'company_id': cls.other_company.id,
        })
        cls.foreign_active_book.action_activate()
        cls.foreign_draft_book = foreign_books.create({
            'name': 'Foreign Draft Book',
            'journal_id': cls.foreign_draft_journal.id,
            'start_number': 20,
            'end_number': 30,
            'company_id': cls.other_company.id,
        })

    def test_default_next_number_is_start(self):
        book = self.env['eh.cheque.book'].create({
            'name': 'Book A',
            'journal_id': self.bank_journal.id,
            'start_number': 100,
            'end_number': 110,
        })
        self.assertEqual(book.next_number, 100)
        self.assertEqual(book.remaining_count, 11)
        self.assertEqual(book.state, 'draft')

    def test_activate_blocks_when_another_active(self):
        # self.book is already in_use
        new_book = self.env['eh.cheque.book'].create({
            'name': 'Book B',
            'journal_id': self.bank_journal.id,
            'start_number': 50,
            'end_number': 60,
        })
        with self.assertRaises(UserError):
            new_book.action_activate()

    def test_consume_next_serial(self):
        # Fresh book on a different journal so we don't clash with self.book.
        other_journal = self.env['account.journal'].create({
            'name': 'Other Bank',
            'code': 'OBNK',
            'type': 'bank',
            'company_id': self.company.id,
        })
        book = self.env['eh.cheque.book'].create({
            'name': 'Other Book',
            'journal_id': other_journal.id,
            'start_number': 1,
            'end_number': 2,
        })
        book.action_activate()
        s1 = book.consume_next_serial()
        s2 = book.consume_next_serial()
        self.assertEqual(s1, 1)
        self.assertEqual(s2, 2)
        self.assertEqual(book.state, 'exhausted')
        with self.assertRaises(UserError):
            book.consume_next_serial()

    def test_close_blocks_consume(self):
        self.book.action_close()
        with self.assertRaises(UserError):
            self.book.consume_next_serial()

    def test_non_manager_cannot_operate_or_consume_book(self):
        original_next = self.book.next_number

        with self.assertRaises(AccessError):
            self.book.with_user(self.book_user).action_close()
        with self.assertRaises(AccessError):
            self.book.with_user(self.book_user).consume_next_serial()

        self.book.invalidate_recordset(['state', 'next_number'])
        self.assertEqual(self.book.state, 'in_use')
        self.assertEqual(self.book.next_number, original_next)

    def test_manager_cannot_operate_cross_company_books(self):
        with self.assertRaises(AccessError):
            self.foreign_draft_book.with_user(
                self.book_manager,
            ).action_activate()
        with self.assertRaises(AccessError):
            self.foreign_active_book.with_user(
                self.book_manager,
            ).action_close()
        with self.assertRaises(AccessError):
            self.foreign_active_book.with_user(
                self.book_manager,
            ).consume_next_serial()

        self.foreign_active_book.invalidate_recordset([
            'state', 'next_number',
        ])
        self.foreign_draft_book.invalidate_recordset(['state'])
        self.assertEqual(self.foreign_active_book.state, 'in_use')
        self.assertEqual(self.foreign_active_book.next_number, 1)
        self.assertEqual(self.foreign_draft_book.state, 'draft')

    def test_next_number_is_server_owned(self):
        with self.assertRaises(AccessError):
            self.book.with_user(self.book_manager).write({
                'next_number': 7,
            })
        self.book.invalidate_recordset(['next_number'])
        self.assertEqual(self.book.next_number, 1)

        journal = self.env['account.journal'].create({
            'name': 'Server Cursor Bank',
            'code': 'SCBK',
            'type': 'bank',
            'company_id': self.company.id,
        })
        created = self.env['eh.cheque.book'].with_user(
            self.book_manager,
        ).create({
            'name': 'Server Cursor Book',
            'journal_id': journal.id,
            'start_number': 100,
            'end_number': 110,
            'next_number': 107,
            'company_id': self.company.id,
        })
        self.assertEqual(created.next_number, 100)

        created.action_activate()
        self.assertEqual(created.consume_next_serial(), 100)
        created.action_close()
        self.assertEqual(created.state, 'closed')
        self.assertEqual(created.next_number, 101)
