# -*- encoding: utf-8 -*-
"""Fail closed before installing mirror uniqueness on legacy databases.

Old releases used search-before-create only. Duplicate legal documents must
never be deleted, relinked, or silently stripped of provenance by migration.
Stop before schema constraint installation and report every conflicting ID so
an operator can review and resolve each economic document explicitly.
"""


def _conflicts(cr, table, source_field):
    cr.execute(
        f"""
        SELECT {source_field}, company_id, array_agg(id ORDER BY id)
          FROM {table}
         WHERE {source_field} IS NOT NULL
         GROUP BY {source_field}, company_id
        HAVING count(*) > 1
         ORDER BY {source_field}, company_id
        """
    )
    return cr.fetchall()


def migrate(cr, version):
    if not version:
        return
    conflicts = []
    for model, table, field in (
        ('purchase.order', 'purchase_order', 'eh_ic_origin_so_id'),
        ('sale.order', 'sale_order', 'eh_ic_origin_po_id'),
    ):
        for source_id, company_id, order_ids in _conflicts(cr, table, field):
            conflicts.append(
                f"{model}:source={source_id}:company={company_id}:"
                f"orders={list(order_ids)}"
            )
    if conflicts:
        raise RuntimeError(
            "Inter-company mirror duplicates require explicit review before "
            "upgrade; no order or provenance was changed: "
            + "; ".join(conflicts)
        )
