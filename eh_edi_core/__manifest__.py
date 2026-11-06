# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'post_init_hook': 'post_init_hook',
    'name': 'EH EDI Core',
    'version': '19.0.1.2.1',
    'summary': 'Canonical EN 16931 invoice semantics and versioned AES-256-GCM credential storage for ERP Heritage e-invoicing modules.',
    'description': """Internal technical dependency for the ERP Heritage e-invoicing modules (Mauritius MRA, France PDP / PPF Factur-X, Peppol BIS Billing). It gives the e-invoicing line two shared foundations so each format module stays small, consistent, and audited in one place.

AES-256-GCM credential vault. One audited authenticated-encryption implementation, parametrised per module by a key namespace, so API secrets for each tax authority or access point are encrypted once instead of a copy of the crypto per module. New ciphertext carries a format version and key id; configured historic keys keep legacy data readable and rotate_ciphertext re-encrypts it under the current key. The master key resolves from one of three sources in order (environment variable, a 0600 key file, or a system config parameter), and the vault refuses to operate when no master key resolves. A master-key file with group or other permissions is rejected outright (chmod 0600 required), the config-parameter source emits one least-secure warning, and tampered, truncated, wrong-key, unknown-version, or non-base64 ciphertext raises a vault error rather than returning garbage. Empty or unset credential fields pass through cleanly so they never raise.

Canonical EN16931 invoice model and account.move mapper. One mapper reads common financial semantics from account.move into a BT-code-annotated model: legal parties, lines, booked document-currency tax breakdown, totals and references. Format adapters consume that model, then add only jurisdiction or transport evidence such as validated participant identifiers and payment routing. Product-line detection works across Odoo versions, unit codes use Odoo's stable UN/ECE resolver, credit notes map to the correct document type, and optional cross-module fields are read defensively.

Not an end-user application. No user interface, no menu, no configuration of its own. Installed automatically as a dependency of the e-invoicing modules. Free and licensed LGPL-3.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'category': 'Accounting/Accounting',
    'license': 'LGPL-3',
    'depends': ['base'],
    'external_dependencies': {
        'python': ['cryptography'],
    },
    'data': [],
    'images': ['static/description/banner.gif'],
    'application': False,
    'installable': True,
    'auto_install': False,
}
