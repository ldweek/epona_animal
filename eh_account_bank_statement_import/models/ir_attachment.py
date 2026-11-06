# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################

from odoo import _, api, models
from odoo.exceptions import UserError


_SOURCE_EVIDENCE_FIELDS = {
    'name', 'type', 'datas', 'db_datas', 'store_fname', 'checksum', 'url',
    'res_model', 'res_id', 'res_field', 'company_id', 'mimetype',
}
_SOURCE_STATEMENT_DELETE_CONTEXT = 'eh_bank_source_statement_delete'
_SOURCE_STATEMENT_DELETE_CAPABILITY = object()


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    def _eh_bank_import_source_evidence(self):
        if not self:
            return self
        logs = self.env['eh.account.bank.statement.import.log'].sudo().search([
            ('source_attachment_ids', 'in', self.ids),
        ])
        linked_ids = logs.source_attachment_ids.ids
        return self.browse(set(self.ids).intersection(linked_ids))

    def write(self, vals):
        if _SOURCE_EVIDENCE_FIELDS.intersection(vals):
            if self._eh_bank_import_source_evidence():
                raise UserError(_(
                    "Exact bank-import source attachments are immutable."
                ))
        return super().write(vals)

    def unlink(self):
        protected = self._eh_bank_import_source_evidence()
        internal_delete = (
            self.env.context.get(_SOURCE_STATEMENT_DELETE_CONTEXT)
            is _SOURCE_STATEMENT_DELETE_CAPABILITY
        )
        if protected and internal_delete:
            Log = self.env['eh.account.bank.statement.import.log'].sudo()
            for attachment in protected:
                log = Log.search([
                    ('source_attachment_ids', 'in', attachment.id),
                ], order='id', limit=1)
                # Keep exact bytes under their immutable audit owner while the
                # user intentionally deletes the recoverable statement.
                super(IrAttachment, attachment).write({
                    'res_model': Log._name,
                    'res_id': log.id,
                })
            remaining = self - protected
            if remaining:
                super(IrAttachment, remaining).unlink()
            return True
        return super().unlink()

    @api.ondelete(at_uninstall=False)
    def _unlink_bank_import_source_evidence(self):
        if self._eh_bank_import_source_evidence():
            raise UserError(_(
                "Exact bank-import source attachments cannot be deleted."
            ))
