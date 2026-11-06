# -*- encoding: utf-8 -*-
import base64
import binascii

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.eh_account_einvoice_peppol.models.peppol_inbound import (
    _EH_INBOUND_ATTACHMENT_CAPABILITY,
    _EH_INBOUND_ATTACHMENT_INTERNAL,
)
from odoo.addons.eh_account_einvoice_peppol.tools import ubl_parser


class EhPeppolInboundUpload(models.TransientModel):
    _name = 'eh.peppol.inbound.upload'
    _description = "Upload inbound Peppol XML"

    xml_file = fields.Binary(string="UBL XML", required=True)
    filename = fields.Char(required=True, default='invoice.xml')
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
    )

    def action_upload(self):
        self.ensure_one()
        if not self.env.user.has_group('eh_account_base.group_eh_user'):
            raise AccessError(_(
                "Only ERP Heritage accounting users can upload Peppol XML."
            ))
        if self.company_id not in self.env.companies:
            raise AccessError(_(
                "Selected company is outside your allowed companies."
            ))
        try:
            raw = base64.b64decode(self.xml_file or b'', validate=True)
        except (binascii.Error, ValueError):
            raise UserError(_("Uploaded Peppol XML is not valid base64 data."))
        if not raw:
            raise UserError(_("Select a non-empty UBL XML file."))
        if len(raw) > ubl_parser.MAX_UBL_BYTES:
            raise UserError(_(
                "Uploaded XML exceeds maximum size of %d bytes.",
                ubl_parser.MAX_UBL_BYTES,
            ))
        attachment = self.env['ir.attachment'].with_context(**{
            _EH_INBOUND_ATTACHMENT_INTERNAL:
                _EH_INBOUND_ATTACHMENT_CAPABILITY,
        }).create({
            'name': self.filename or 'invoice.xml',
            'type': 'binary',
            'raw': raw,
            'mimetype': 'application/xml',
            'company_id': self.company_id.id,
            'eh_peppol_inbound_source': True,
        })
        inbound = self.env['eh.peppol.inbound'].with_company(
            self.company_id
        ).create({
            'company_id': self.company_id.id,
            'access_point_key': 'manual',
            'xml_attachment_id': attachment.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Peppol inbound"),
            'res_model': 'eh.peppol.inbound',
            'res_id': inbound.id,
            'view_mode': 'form',
            'target': 'current',
        }
