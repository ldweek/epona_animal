# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Authorization boundary for the PDC cheque QWeb reports.

``ir.actions.report.group_ids`` controls whether a report appears in the
Print menu, but Odoo's report HTTP route resolves the action with ``sudo`` and
does not enforce that group.  These report-value models are consequently the
server-side gate for both HTML and PDF rendering, including guessed report
URLs.
"""

from odoo import _, api, models
from odoo.exceptions import AccessError


class _EhChequeReportGuard(models.AbstractModel):
    _name = 'report.eh_account_pdc.cheque_report_guard'
    _description = "PDC cheque report authorization"

    @api.model
    def _eh_cheque_report_values(self, docids):
        if not self.env.su and not self.env.user.has_group(
                'eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can print PDC cheque "
                "evidence.",
            ))
        docs = self.env['eh.cheque'].browse(docids)
        # Apply the real caller's ACL and company record rules before QWeb
        # touches any record.  In particular, a manager cannot print a
        # guessed cheque id belonging to a company they cannot access.
        docs._eh_check_access('read')
        return {
            'doc_ids': docids,
            'doc_model': 'eh.cheque',
            'docs': docs,
        }


class EhChequePrintReport(models.AbstractModel):
    _name = 'report.eh_account_pdc.report_cheque_print'
    _inherit = 'report.eh_account_pdc.cheque_report_guard'
    _description = "PDC physical cheque report values"

    @api.model
    def _get_report_values(self, docids, data=None):
        return self._eh_cheque_report_values(docids)


class EhChequeRegisterReport(models.AbstractModel):
    _name = 'report.eh_account_pdc.report_cheque_register'
    _inherit = 'report.eh_account_pdc.cheque_report_guard'
    _description = "PDC cheque register report values"

    @api.model
    def _get_report_values(self, docids, data=None):
        return self._eh_cheque_report_values(docids)
