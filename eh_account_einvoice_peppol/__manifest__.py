# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
{
    'name': 'Peppol eInvoicing',
    'summary': 'Two-way Peppol BIS Billing 3.0 eInvoicing for Odoo 19 Community: a namespace-accurate UBL 2.1 generator that exports a posted customer invoice or credit note with one button, plus an inbound UBL parser and a received to parsed to matched to posted bill-intake workflow that creates the vendor bill under manager-gated posting. Includes structural pre-flight validation (mandatory elements, single-currency consistency, line and tax reconciliation within 1 cent), real participant-identifier checksums (ABN, GS1 GLN, DE VAT, FR SIRET, MY TIN), SHA-256 duplicate detection, and a bring-your-own access-point adapter contract. UBL 2.1 invoice generator, UBL XML parser, EN 16931 electronic invoicing, Peppol inbound vendor bill, credit note CreditNote UBL, ABN GLN participant id validation, Peppol duplicate invoice detection, access point adapter, e-invoicing accounts payable and receivable.',
    'description': """Peppol eInvoicing renders a posted account.move into a Peppol BIS Billing 3.0 (UBL 2.1) Invoice or CreditNote XML, and ships the inbound side as well: a UBL parser and a bill-intake workflow that moves a received document through parsed, matched and posted to create the vendor bill.

Outbound is one button on a posted customer invoice or credit note. The generator emits the official OASIS UBL 2.1 namespaces (cbc CommonBasicComponents, cac CommonAggregateComponents, the Invoice-2 and CreditNote-2 roots) and carries UN/ECE rec-20 unit codes in the unitCode attribute. Before the payload is returned, it is checked against the high-frequency Peppol and EN 16931 invariants: mandatory elements present, a single document currency used consistently, and line totals and tax totals that reconcile within 1 cent. A failure names the offending rule instead of bouncing later at the access point. This is a structural pre-flight check, not full XSD schema validation.

Inbound is a real state machine. A received UBL document is parsed into header data, matched to a supplier, and only then posted as a vendor bill, with posting gated to accounting managers. An identical re-submission (same file hash, same supplier, same company) is detected and refused, so a poll that retries cannot create a duplicate bill. Where supplier resolution is ambiguous, the record is routed to an exception state for human review rather than guessing.

Country-profile validators for A-NZ, MY, DE and FR ship in tools as opt-in, unit-tested checks. They validate jurisdiction identifiers (ABN/NZBN, MY TIN, DE VAT and Leitweg-ID, FR VAT and SIRET) and allowed tax categories against the BIS 3.0 payload. They do not generate the MyInvois, XRechnung or Factur-X national formats, and they are not run by the default Export button.

Transport is bring-your-own. A real adapter registry and contract (submit and poll) ships in-module with a manual file-drop stub. Live AS4 transmission requires a separate ERP Heritage access-point adapter registered under the same key, which is not included here. The access-point key and inbox email are stored per company and read by the inbound poll cron, so a multi-company install can route each company to its own access point. LGPL-3 source on disk, no activation key, no phone-home.""",
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.9.0',
    'depends': [
        'eh_account_base',
        'eh_edi_core',
        'account',
        'purchase',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/peppol_inbound_data.xml',
        'views/account_tax_views.xml',
        'views/account_move_views.xml',
        'views/peppol_inbound_views.xml',
        'views/res_company_views.xml',
    ],
    'images': ['static/description/banner.gif'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
