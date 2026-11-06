# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Audit log of every bank statement import attempt.

Every wizard run inserts one log row, whether it succeeded, was a no-op
(file already imported), or failed. Auditors and operators get a single
queryable record of what landed and where, which file produced it, and
which exception (if any) was raised.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


_logger = logging.getLogger(__name__)


class EhBankStatementImportLog(models.Model):
    _name = 'eh.account.bank.statement.import.log'
    _description = "Bank statement import log"
    _order = 'imported_at desc, id desc'

    imported_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True,
    )
    imported_by_id = fields.Many2one(
        'res.users', required=True,
        default=lambda self: self.env.user,
    )
    journal_id = fields.Many2one('account.journal', required=True)
    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
    )
    profile_id = fields.Many2one(
        'eh.account.bank.statement.import.profile',
    )
    format_key = fields.Char(required=True)
    filename = fields.Char()
    file_hash = fields.Char(
        size=64, index=True,
        help="SHA-256 of the uploaded file content. Drives the idempotent reimport guard.",
    )
    statement_id = fields.Many2one(
        'account.bank.statement', ondelete='set null',
    )
    statement_ids = fields.Many2many(
        'account.bank.statement',
        'eh_bsi_log_statement_rel', 'log_id', 'statement_id',
        string="Imported statements",
        help=(
            "All statements created atomically from this source file. "
            "statement_id remains the legacy primary link."
        ),
    )
    source_attachment_ids = fields.Many2many(
        'ir.attachment',
        'eh_bsi_log_attachment_rel', 'log_id', 'attachment_id',
        string="Exact source files",
        help=(
            "Immutable attachments containing the exact uploaded bytes "
            "that produced the linked statements."
        ),
    )
    line_count = fields.Integer(default=0)
    skipped_count = fields.Integer(
        default=0,
        help="Lines skipped because they were already imported in a prior run.",
    )
    state = fields.Selection(
        [
            ('done', "Done"),
            ('duplicate', "Duplicate (no-op)"),
            ('error', "Error"),
        ],
        required=True, default='done', index=True,
    )
    error_message = fields.Text()

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(_(
                "Bank-import audit rows are created only by the importer."
            ))
        now = fields.Datetime.now()
        protected = []
        for incoming in vals_list:
            vals = dict(incoming or {})
            vals['imported_at'] = now
            protected.append(vals)
        return super().create(protected)

    def write(self, vals):
        raise UserError(_(
            "Bank-import audit rows are immutable."
        ))

    @api.ondelete(at_uninstall=False)
    def _unlink_immutable_audit(self):
        raise UserError(_(
            "Bank-import audit rows cannot be deleted."
        ))

    @api.model
    @api.private
    def record_failure_durable(self, vals):
        """Commit an error row outside a request transaction that will roll back.

        Runtime journal/profile/user rows are already committed and visible to
        this cursor. Test-only, uncommitted fixtures fall back to the caller's
        local transaction so integration tests remain hermetic.
        """
        vals = dict(vals or {})
        if vals.get('state') != 'error':
            raise ValueError("Durable bank-import logging accepts errors only")
        actor_id = int(vals.get('imported_by_id') or self.env.user.id)
        company_id = int(vals.get('company_id') or 0)
        journal_id = int(vals.get('journal_id') or 0)
        profile_id = int(vals.get('profile_id') or 0)
        durable_id = False
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SELECT id FROM res_users WHERE id = %s", [actor_id])
                actor_visible = bool(cr.fetchone())
                cr.execute(
                    "SELECT company_id FROM account_journal WHERE id = %s",
                    [journal_id],
                )
                journal_row = cr.fetchone()
                journal_visible = bool(
                    journal_row and journal_row[0] == company_id
                )
                profile_visible = True
                if profile_id:
                    cr.execute(
                        "SELECT id FROM eh_account_bank_statement_import_profile "
                        "WHERE id = %s",
                        [profile_id],
                    )
                    profile_visible = bool(cr.fetchone())
                if actor_visible and journal_visible and profile_visible:
                    isolated = api.Environment(
                        cr,
                        actor_id,
                        {'allowed_company_ids': [company_id]},
                    )
                    durable = isolated[self._name].sudo().create(vals)
                    durable_id = durable.id
                    cr.commit()
        except Exception:  # noqa: BLE001 - audit must not mask import error
            _logger.exception("Could not persist durable bank-import error")
        return durable_id
