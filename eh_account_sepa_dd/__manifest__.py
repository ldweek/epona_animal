# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The module is built directly
# against the public ISO 20022 PAIN.008.001.02 specification published
# at https://www.iso20022.org/ and the SEPA Direct Debit Scheme
# Rulebook published by the European Payments Council. No code or
# comments derive from any proprietary or third-party Odoo module.
#
##############################################################################
{
    'name': 'SEPA Direct Debit',
    'summary': 'SEPA Direct Debit collection for Odoo 19 Community. Generate ISO 20022 PAIN.008.001.02 XML, run reachable FRST, RCUR, FNAL, and OOFF mandate lifecycles, validate creditor identifiers, enforce CORE/B2B rules, and block non-compliant exports before they reach the bank.',
    'description': """SEPA Direct Debit for Odoo 19 Community collects EUR payments from customer bank accounts via the SEPA Direct Debit scheme. It pairs with the Recurring Invoices module for subscription collection and with Batch Payment for ad-hoc collection runs.

The PAIN.008.001.02 XML is original work, written strictly from the public ISO 20022 specification and the SEPA Direct Debit Scheme Rulebook. Tests parse the generated XML back with lxml and assert the scheme structure: namespace, sequence type, local instrument, the mandate-related block, creditor identifier, charge bearer, and control sum.

The mandate state machine encodes the FRST, RCUR, FNAL, OOFF transitions exactly as the rulebook describes. The first collection on a mandate renders as FRST, and an atomic SQL counter increment flips it to RCUR automatically thereafter, so concurrent collection attempts cannot race the transition. Mandate states are Draft, Active, Completed, Revoked, and Expired.

The scheme's 36-month dormancy rule is double-enforced: on the collection path and on a daily cron that processes each mandate inside its own savepoint, so one bad record never aborts the batch.

IBAN and BIC are validated locally against ISO 13616 and ISO 9362. Creditor identifiers are canonicalised and checked with ISO 7064 mod-97-10. Active EPC CORE and B2B instruments are supported; withdrawn COR1 is migrated to CORE and cannot be selected. B2B activation requires a business debtor and recorded debtor-bank confirmation. Per-company creditor configuration also controls pre-notification and batch-booking preference.

Every generated file carries a SHA-256 fingerprint. A live file blocks duplicate generation. Controlled re-cut requires explicitly voiding the prior file, preserves the original payment consumption snapshot, and retains every retired export as immutable audit evidence.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.0',
    'depends': [
        'eh_account_base',
        'eh_account_batch_payment',
        'eh_account_sepa_ct',
        'account',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'data/cron.xml',
        'views/sepa_creditor_views.xml',
        'views/sepa_mandate_views.xml',
        'views/batch_payment_views.xml',
        'views/sepa_dd_export_views.xml',
        'data/menus.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
