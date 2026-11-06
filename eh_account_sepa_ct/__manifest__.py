# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The module is built directly
# against the public ISO 20022 PAIN.001.001.03 specification published
# at https://www.iso20022.org/. No code or comments derive from any
# proprietary or third-party Odoo module.
#
##############################################################################
{
    'name': 'SEPA Credit Transfer',
    'summary': 'Generate ISO 20022 SEPA Credit Transfer XML (PAIN.001.001.03 and PAIN.001.001.09) from a batch of posted vendor payments in Odoo 19 Community. Pure Python IBAN mod-97 and BIC validation with no external library, single currency EUR enforcement, SEPA Latin character sanitisation, and a per export SHA-256 audit record. SEPA credit transfer Odoo 19, PAIN.001.001.03 generator, PAIN.001.001.09 ISO 20022, SEPA XML batch vendor payment, IBAN BIC validation, EUR pay run bank upload file.',
    'description': """SEPA Credit Transfer builds ISO 20022 credit-transfer files for posted vendor-payment batches. Each batch explicitly opts into SEPA controls; merely configuring an originator never changes ordinary outbound batches. PAIN.001.001.09 is the default for new originators, while .03 remains available for banks that still require it.

Validation happens before the file is written, not after the bank rejects it. IBAN is checked with a pure Python mod-97 (ISO 13616) routine that also verifies the country is in the SEPA zone and the length matches that country exactly. BIC is checked against ISO 9362 structure rules, with 8 character codes padded to 11 with the XXX branch. There is no external validation library to install. If a batch mixes currencies, generation refuses and names the offending currencies so you split the batch before the bank does.

Names and remittance text are run through a SEPA Latin character set pass that transliterates accents and strips characters the scheme does not allow, so a supplier name with diacritics does not bounce at the gateway. Each transaction renders as its own entry for cleaner reconciliation on the bank side.

Every export writes an immutable audit row with actor, timestamp, message ID, quantised control sum and SHA-256. Generation leaves state at generated; requesting the verified attachment changes it to downloaded. A second live instruction is blocked until a manager explicitly voids the unsent file for re-cut. Two-person maker/checker and stricter three-person policies are configurable per originator.

Built directly from the public ISO 20022 specification. Original work, not a derivative. Pairs with the batch payment module: build the batch, generate the SEPA file, upload to the bank.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.3.0',
    'depends': ['eh_account_base', 'eh_account_batch_payment', 'account'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/email_templates.xml',
        'views/sepa_originator_views.xml',
        'views/batch_payment_views.xml',
        'views/sepa_export_views.xml',
        'data/menus.xml',
    ],
    'assets': {
        'web.assets_backend': ['eh_account_sepa_ct/static/src/js/tours/sepa_ct_tour.js'],
    },
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
