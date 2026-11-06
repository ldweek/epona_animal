# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Partner action: print a customer or vendor statement PDF.

Adds two methods on res.partner so the statement can be triggered from a
button on the partner form. The methods resolve the right report record
(customer or vendor) and delegate to the dynamic report's PDF pipeline,
returning a download action the user can click through.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    def action_print_customer_statement(self):
        return self._print_statement(
            xml_id='eh_account_dynamic_reports.report_customer_statement',
            label="Customer Statement",
        )

    def action_print_vendor_statement(self):
        return self._print_statement(
            xml_id='eh_account_dynamic_reports.report_vendor_statement',
            label="Vendor Statement",
        )

    def _print_statement(self, xml_id, label):
        self.ensure_one()
        report = self.env.ref(xml_id, raise_if_not_found=False)
        if not report:
            raise UserError(_(
                "Statement report %s is not registered. Reinstall "
                "eh_account_dynamic_reports to fix this.",
            ) % xml_id)
        today = fields.Date.context_today(self)
        options = report.get_default_options()
        options['partner_id'] = self.id
        options['date'] = {
            'mode': 'range',
            'date_from': today.replace(day=1).isoformat(),
            'date_to': today.isoformat(),
        }
        content = report.render_pdf(options)
        return report._eh_private_download_action(
            content=content,
            filename="%s_%s_%s.pdf" % (
                label.replace(' ', '_'),
                self.display_name.replace(' ', '_'),
                today.isoformat(),
            ),
            mimetype='application/pdf',
            options=options,
        )
