# -*- encoding: utf-8 -*-
"""Backfill only explicit legacy evidence for derived order lines.

Historic down-payment and expense lines already carry core-owned Boolean
markers.  Copy those observable facts into the new durable discriminator on
bound inter-company orders.  Do not infer receipt adjustments: their legacy
shape cannot distinguish a real stock synchronization from a manual line, so
unprovable rows remain untouched for review.
"""

import logging


_logger = logging.getLogger(__name__)


def _columns(cr, table):
    cr.execute(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = to_regclass(%s) "
        "AND attnum > 0 AND NOT attisdropped",
        (table,),
    )
    return {row[0] for row in cr.fetchall()}


def migrate(cr, version):
    if not version:
        return

    counts = {}
    sale_columns = _columns(cr, 'sale_order_line')
    sale_order_columns = _columns(cr, 'sale_order')
    if {
        'order_id', 'eh_ic_derived_line_kind', 'is_downpayment',
    }.issubset(sale_columns) and {
        'id', 'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
    }.issubset(sale_order_columns):
        cr.execute("""
            UPDATE sale_order_line AS line
               SET eh_ic_derived_line_kind = 'downpayment'
              FROM sale_order AS sale
             WHERE sale.id = line.order_id
               AND (sale.eh_ic_mirror_po_id IS NOT NULL
                    OR sale.eh_ic_origin_po_id IS NOT NULL)
               AND line.eh_ic_derived_line_kind IS NULL
               AND COALESCE(line.is_downpayment, FALSE)
        """)
        counts['sale down payment'] = cr.rowcount
    if {
        'order_id', 'eh_ic_derived_line_kind', 'is_expense',
    }.issubset(sale_columns) and {
        'id', 'eh_ic_mirror_po_id', 'eh_ic_origin_po_id',
    }.issubset(sale_order_columns):
        cr.execute("""
            UPDATE sale_order_line AS line
               SET eh_ic_derived_line_kind = 'expense'
              FROM sale_order AS sale
             WHERE sale.id = line.order_id
               AND (sale.eh_ic_mirror_po_id IS NOT NULL
                    OR sale.eh_ic_origin_po_id IS NOT NULL)
               AND line.eh_ic_derived_line_kind IS NULL
               AND COALESCE(line.is_expense, FALSE)
        """)
        counts['sale expense'] = cr.rowcount

    purchase_columns = _columns(cr, 'purchase_order_line')
    purchase_order_columns = _columns(cr, 'purchase_order')
    if {
        'order_id', 'eh_ic_derived_line_kind', 'is_downpayment',
    }.issubset(purchase_columns) and {
        'id', 'eh_ic_mirror_so_id', 'eh_ic_origin_so_id',
    }.issubset(purchase_order_columns):
        cr.execute("""
            UPDATE purchase_order_line AS line
               SET eh_ic_derived_line_kind = 'downpayment'
              FROM purchase_order AS purchase
             WHERE purchase.id = line.order_id
               AND (purchase.eh_ic_mirror_so_id IS NOT NULL
                    OR purchase.eh_ic_origin_so_id IS NOT NULL)
               AND line.eh_ic_derived_line_kind IS NULL
               AND COALESCE(line.is_downpayment, FALSE)
        """)
        counts['purchase down payment'] = cr.rowcount

    _logger.warning(
        "SO/PO 1.1.4 preserved explicit derived-line evidence: %s; "
        "legacy receipt-like rows were not inferred.",
        ', '.join(
            '%s=%d' % item for item in sorted(counts.items())
        ) or 'no compatible legacy columns',
    )
