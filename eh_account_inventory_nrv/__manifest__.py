# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
# All implementation work is original. The measurement rule (inventory is
# carried at the lower of cost and net realisable value, and a write-down is
# reversed when the net realisable value recovers, capped at the original
# write-down) is IAS 2 as published; no code or comments derive from any
# proprietary or third-party module.
#
##############################################################################
{
    'name': 'IAS 2 Inventory NRV',
    'summary': 'IAS 2 net realisable value write-down for Odoo 19 Community that measures inventory at the lower of cost and NRV on an item-by-item or grouped category basis, posts only the movement from the opening position, and reverses recoveries capped at the amount previously recognised. Odoo 19 inventory write down, IAS 2 net realisable value, lower of cost or NRV, inventory impairment, stock write-down allowance, NRV reversal, period-end inventory assessment, IAS 2.29 assessment basis.',
    'description': 'This module runs the IAS 2 measurement rule that inventories are carried at the lower of cost and net realisable value (IAS 2.9). A period-end run holds one line per inventory item or group, each with its cost and its net realisable value, the estimated selling price less the costs to complete and sell (IAS 2.6). Each run carries an explicit, audited assessment basis (IAS 2.29): on the default item-by-item basis the write-down is the excess of cost over NRV floored at zero per line; on the category basis similar or related items sharing a product category are assessed as one unit, so surpluses and deficits within the category are netted before the floor at zero is applied. The category is therefore never carried above its aggregate cost, and the netted requirement is allocated only over the lines with an item-level deficit (pro-rata by deficit), so no individual item is ever written up above its own cost or down below its own NRV. The basis is tracked in the chatter, locked once the run is posted, and disclosed on the posted movement entry. The run compares the closing position to the opening write-down and posts only the movement. An increase debits an inventory write-down expense and credits a write-down allowance; a recovery reverses the write-down but only up to the amount previously recognised, so inventory never rises above cost (IAS 2.33). Posting and reversal are gated to the EH Accounting Manager group, entries are sealed, and posted runs are frozen against silent edits (IAS 2.34).',
    'author': 'ERP Heritage',
    'website': 'https://www.erpheritage.com.au/',
    'license': 'LGPL-3',
    'category': 'Accounting/Accounting',
    'version': '19.0.1.2.2',
    'depends': ['eh_account_base', 'account', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/eh_isolation_rules.xml',
        'data/sequences.xml',
        'views/nrv_run_views.xml',
        'data/menus.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'images': ['static/description/banner.gif', 'static/description/inventory_nrv_01_run.png'],
}
