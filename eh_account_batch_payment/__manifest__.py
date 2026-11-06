# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Batch Payment',
    'summary': 'Group inbound or outbound payments into one atomic, maker-checker-controlled payment batch on Odoo 19 Community, with partial and mixed-currency settlement, locked bank instructions, pre-post CSV preview and standard Odoo reconciliation. Search terms: Odoo 19 batch payment, Odoo Community vendor payment run, batch vendor payments, group customer receipts, mixed currency payment batch, partial vendor payment, AP payment run, batch payment CSV preview bank portal, manager approval payment posting, bulk account.payment, payment batch audit trail.',
    'description': """Run a single batch instead of N individual payments. Open the build wizard, pick the open invoices or vendor bills to clear, choose whether to aggregate, and the wizard creates one draft account.payment per source or one net payment per compatible partner, receivable/payable account, currency and reviewed recipient bank. The source's payment/vendor reference and bank account are carried into the instruction. Pre-existing draft payments can join a draft batch from the Batch field on the payment record.

Posting is atomic: the batch header, every member payment and every source document are locked and revalidated immediately before cash moves. A single validation or posting failure rolls the whole attempt back and leaves the batch confirmed for correction, so a reviewed payment run can never become partially posted. Confirmed financial inputs and member payment values are frozen until an allowed reset.

Output is plain standard account.payment records, with only a nullable batch link added, so reconciliation, the vendor ledger, follow-ups and reporting all see the payments natively. A reviewed payment may settle only part of its source residual. Company-currency bank journals may carry several payment currencies in one batch; the header converts each member at its payment date for a meaningful company-currency total, while an explicitly foreign-currency journal remains single-currency. The build wizard refuses to mix companies, blocks a source document with no partner, and warns on the chatter when a document looks already or partially settled. Confirmation is restricted to an EH Accounting Manager, and posting requires a second, different EH Accounting Manager. This mandatory two-person maker-checker control is intentionally not configurable. The confirmed, posted, cancelled and reversed transitions stamp the acting user and timestamp, and every transition is tracked on the chatter.

Confirmation freezes the exact CSV rows and gives the checker a non-posting preview before cash moves. Posting seals the same approved bytes as an immutable downloadable attachment. The CSV writes one row per payment (reference, partner, bank account, amount, currency, payment date, memo), ordered by partner, for upload to your bank portal. The export layout can be subclassed for banks that need a different column set. Batch references use separate company/direction counters and the reviewed payment date's year.

Pairs naturally with the post-dated cheque module: cheques can be presented on their value date and then batched.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.1.17',
    'depends': ['eh_account_base', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/batch_payment_views.xml',
        'wizards/build_batch_wizard_views.xml',
        'views/account_payment_views.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
