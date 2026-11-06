# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The treatments (a change in accounting
# policy or the correction of a prior-period error applied retrospectively by
# restating comparatives, a change in estimate applied prospectively, and an
# adjusting versus non-adjusting event after the reporting period) are IAS 8
# and IAS 10 as published; no code or comments derive from any proprietary or
# third-party module.
#
##############################################################################
{
    'name': 'IAS 8 & 10 Events',
    'summary': 'Keep IAS 8 accounting changes and IAS 10 subsequent events on a controlled register in Odoo 19 Community, with retrospective restatement of comparatives and optional general ledger posting. Search terms: odoo 19 IAS 8, prior period error correction, change in accounting policy, retrospective restatement of comparatives, as previously reported adjustment as restated, change in accounting estimate prospective, opening retained earnings restatement, IAS 10 events after the reporting period, adjusting non-adjusting event, authorised for issue date, dividend declared after year end.',
    'description': 'This module maintains two linked registers. The IAS 8 register records a change in accounting policy, a change in accounting estimate, or the correction of a prior-period error, deriving retrospective versus prospective application from the change type, and building a per-line restatement trail of as previously reported, the adjustment, and the restated amount that totals into the opening retained-earnings impact. A separate restatement posting date identifies the opening-balance accounting date and an equity-presentation classification distinguishes retrospective application, error restatement, and prospective change. A retrospective change books one balanced opening-equity journal entry: in comparative mode, its affected-account line labels retain the entered prior-period labels, but the module does not create a separate posting for every prior period or regenerate prior statements. The IAS 10 register accepts only events strictly after the reporting date and, when an authorisation date is entered, on or before that cutoff. A later event is refused and must be registered against the following reporting period. The register computes adjusting or disclose-only treatment, records a company- and event-date-derived reference, captures who authorised the statements for issue, and distinguishes an estimated effect from a disclosure that the effect cannot be estimated. An adjusting event inside the window can book its balanced entry to the reporting date. Every posting is restricted to an EH Accounting Manager and is frozen once posted, correctable only through a Reset to Draft that reverses the entry. EH Accounting Users can work in both registers and EH Auditors receive read-only navigation matching their ACLs.',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.10',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/event_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/events_01_restatement.png'],
}
