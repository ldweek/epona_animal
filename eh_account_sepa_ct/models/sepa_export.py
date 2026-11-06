# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.sepa.export: audit row per generated PAIN.001 file.

XML payload stays immutable as attachment evidence. State starts generated,
changes to downloaded only when user requests file, and changes to superseded
only through controlled void-for-recut workflow. SHA-256 binds every served
byte to audit row.
"""

import base64
import hashlib
import hmac
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.http import Stream

_logger = logging.getLogger(__name__)


SEPA_EXPORT_CONTEXT = 'eh_sepa_export_capability'
SEPA_EXPORT_CAPABILITY = object()
SEPA_ATTACHMENT_CONTEXT = 'eh_sepa_attachment_capability'
SEPA_ATTACHMENT_CAPABILITY = object()
_SEPA_EXPORT_OWNED_FIELDS = frozenset({
    'batch_id', 'company_id', 'message_id', 'generated_at',
    'generated_by_id', 'transaction_count', 'control_sum', 'file_hash',
    'state', 'attachment_id', 'provenance_status',
})


class EhSepaExport(models.Model):
    _name = 'eh.sepa.export'
    _description = "SEPA Credit Transfer export file"
    _inherit = ['mail.thread', 'mail.activity.mixin', 'eh.workflow.guard']
    _order = 'generated_at desc, id desc'
    _rec_name = 'message_id'

    # The complete generated-file identity is immutable, not just its state.
    # A user may annotate ``notes`` but cannot repoint the batch/attachment or
    # rewrite message, control, hash, actor, or timestamp audit facts.
    _eh_guarded_fields = (
        'batch_id',
        'company_id',
        'message_id',
        'generated_at',
        'generated_by_id',
        'transaction_count',
        'control_sum',
        'file_hash',
        'state',
        'attachment_id',
        'provenance_status',
    )

    batch_id = fields.Many2one(
        'eh.batch.payment', required=True,
        ondelete='restrict', index=True,
    )
    company_id = fields.Many2one(
        related='batch_id.company_id', store=True, readonly=True,
    )
    message_id = fields.Char(
        required=True, copy=False, index=True,
        help="GrpHdr/MsgId stamped on the XML. Globally unique.",
    )
    generated_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True,
    )
    generated_by_id = fields.Many2one(
        'res.users', required=True,
        default=lambda self: self.env.user,
    )
    transaction_count = fields.Integer(readonly=True)
    control_sum = fields.Float(
        digits=(16, 2), readonly=True,
        help="Sum of every transaction amount in the file.",
    )
    file_hash = fields.Char(
        size=64, readonly=True, index=True,
        help="SHA-256 of the rendered XML bytes.",
    )
    state = fields.Selection(
        [
            ('generated', "Generated"),
            ('downloaded', "Downloaded"),
            ('superseded', "Superseded"),
        ],
        default='generated', required=True, tracking=True,
    )
    attachment_id = fields.Many2one(
        'ir.attachment', readonly=True, ondelete='set null', index=True,
    )
    provenance_status = fields.Selection(
        [
            ('verified', "Generator verified"),
            ('legacy_unverified', "Legacy - unverified"),
        ],
        default='verified', required=True, readonly=True, copy=False,
        index=True,
        help=(
            "Only exports generated after server-side provenance controls "
            "were installed are downloadable. Pre-control rows are retained "
            "as audit evidence but quarantined because their origin cannot "
            "be proven."
        ),
    )

    notes = fields.Text()

    @api.model_create_multi
    def create(self, vals_list):
        # Export rows are evidence of a file produced by the PAIN generator.
        # A client may not manufacture that evidence with model.create().
        if self.env.context.get(SEPA_EXPORT_CONTEXT) is not (
            SEPA_EXPORT_CAPABILITY
        ):
            raise AccessError(_(
                "SEPA Credit Transfer exports can only be created by the "
                "batch file generator.",
            ))
        # Even a trusted generator call cannot accidentally inherit a caller-
        # supplied legacy/trust label: creation itself is the proof.
        vals_list = [dict(vals, provenance_status='verified')
                     for vals in vals_list]
        records = super().create(vals_list)
        records.mapped('attachment_id').sudo().with_context(**{
            SEPA_ATTACHMENT_CONTEXT: SEPA_ATTACHMENT_CAPABILITY,
        }).write({'eh_sepa_ct_export_evidence': True})
        # When a new export is generated for a batch that already has
        # a generated/downloaded export, mark the prior ones superseded
        # so the audit trail shows which file is the active one.
        for rec in records:
            siblings = self.search([
                ('batch_id', '=', rec.batch_id.id),
                ('id', '!=', rec.id),
                ('state', 'in', ('generated', 'downloaded')),
            ])
            # Server-initiated state transition: route through sudo so it
            # passes the workflow guard (env.su), regardless of who ran the
            # export that spawned this new row.
            siblings.sudo().with_context(**{
                SEPA_EXPORT_CONTEXT: SEPA_EXPORT_CAPABILITY,
            }).write({'state': 'superseded'})
        return records

    def write(self, vals):
        if _SEPA_EXPORT_OWNED_FIELDS.intersection(vals) and (
            self.env.context.get(SEPA_EXPORT_CONTEXT)
            is not SEPA_EXPORT_CAPABILITY
        ):
            raise AccessError(_(
                "SEPA Credit Transfer export evidence is engine-owned and "
                "immutable."
            ))
        return super().write(vals)

    def _eh_lock_transitions(self, access='write'):
        """Authorize and serialize export evidence state transitions."""
        if not self.env.su:
            self._eh_check_access(access)
        ids = sorted(self.ids)
        if not ids:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_sepa_export WHERE id IN %s "
            "ORDER BY id FOR UPDATE",
            (tuple(ids),),
        )
        self.invalidate_recordset()
        return self

    def _eh_lock_batches_for_void(self):
        """Use the same batch-then-export lock order as file generation."""
        batches = self.mapped('batch_id')
        if not self.env.su:
            batches._eh_check_access('write')
        ids = sorted(batches.ids)
        if ids:
            batches.flush_recordset()
            self.env.cr.execute(
                "SELECT id FROM eh_batch_payment WHERE id IN %s "
                "ORDER BY id FOR UPDATE",
                (tuple(ids),),
            )
            batches.invalidate_recordset()
        return batches

    def action_download(self):
        self.ensure_one()
        # Downloads are available to managers and read-only auditors, but a
        # guessed export id must never cross ACL/company boundaries before the
        # sanctioned sudo state stamp.
        self._eh_lock_transitions(access='read')
        self._verify_attachment_integrity()
        # Read-only auditors are allowed to verify/download evidence.  The
        # explicit read check above proves access to this exact export; only
        # the engine-owned telemetry stamp is elevated here.
        self.sudo().with_context(**{
            SEPA_EXPORT_CONTEXT: SEPA_EXPORT_CAPABILITY,
        }).write({'state': 'downloaded'})
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % self.attachment_id.id,
            'target': 'download',
        }

    def action_send_confirmation(self):
        """Open mail composer with exact verified XML attached."""
        self.ensure_one()
        self._eh_check_access('read')
        self._verify_attachment_integrity()
        template = self.env.ref(
            'eh_account_sepa_ct.email_template_eh_sepa_ct_confirmation',
        )
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mail.compose.message',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_model': self._name,
                'default_res_id': self.id,
                'default_res_ids': [self.id],
                'default_composition_mode': 'comment',
                'default_use_template': True,
                'default_template_id': template.id,
                'default_attachment_ids': [
                    (6, 0, self.attachment_id.ids),
                ],
            },
        }

    def _assert_downloadable(self):
        """Fail closed when an export has been retired from circulation."""
        self.ensure_one()
        if self.provenance_status != 'verified':
            raise UserError(_(
                "Export %s predates generator provenance controls. Its row "
                "and XML are retained unchanged as legacy audit evidence, "
                "but the bank instruction is quarantined and cannot be "
                "downloaded or served. Void it through the controlled re-cut "
                "workflow if a replacement is required.",
                self.message_id,
            ))
        if self.state not in ('generated', 'downloaded'):
            raise UserError(_(
                "Export %s has been superseded and is retired. Its XML "
                "file cannot be downloaded or served; use the active "
                "replacement export instead.",
                self.message_id,
            ))

    def _verify_attachment_integrity(self, attachment=None, content=None):
        """Return verified XML bytes, or fail closed on any mismatch.

        ``content`` lets HTTP/RPC callers verify the exact byte string they
        are about to return.  Without that binding, a privileged or external
        storage mutation between a hash check and the subsequent stream read
        could substitute different bytes (a classic check/use race).
        """
        self.ensure_one()
        self._assert_downloadable()
        attachment = attachment or self.attachment_id
        if not attachment or attachment.id != self.attachment_id.id:
            raise UserError(_(
                "Export %s has no valid attached XML file. Generate a fresh "
                "export from the batch.",
                self.message_id,
            ))
        # The caller's right to the export was established by
        # action_download() or the attachment byte-boundary helpers.  The
        # binary is deliberately attached to the batch, whose generic
        # ir.attachment record rule need not grant a read-only auditor access;
        # inspect that server-owned linkage under sudo only after the export
        # ACL/company rule has passed.
        attachment = attachment.sudo()
        if attachment.res_model != 'eh.batch.payment' \
                or attachment.res_id != self.batch_id.id:
            raise UserError(_(
                "The XML attachment for export %s is not linked to its "
                "source batch. Download was blocked.",
                self.message_id,
            ))
        if content is None:
            content = attachment.sudo().raw or b''
        if isinstance(content, bytearray):
            content = bytes(content)
        if not isinstance(content, bytes):
            raise UserError(_(
                "The stored XML for export %s is not a byte stream.",
                self.message_id,
            ))
        actual_hash = self.compute_hash(content)
        if not self.file_hash or not hmac.compare_digest(
                self.file_hash, actual_hash):
            raise UserError(_(
                "The stored XML for export %s no longer matches its SHA-256 "
                "audit fingerprint. Download was blocked because the file "
                "may have been replaced or corrupted.",
                self.message_id,
            ))
        return content

    def action_void_for_recut(self):
        """Explicitly retire this export so its batch may be re-cut.

        The batch's export action refuses to generate a second PAIN.001 while
        an active (generated/downloaded) file exists, because two files for one
        posted batch are two independent bank instructions (a double supplier
        payment). When a file was genuinely NOT submitted to the bank and must
        be regenerated, a manager voids it here first: this flips the audit row
        to 'superseded' and records who did it, so the trail shows the file was
        retired on purpose rather than silently overwritten. Voiding does NOT
        regenerate; the manager then triggers a fresh export deliberately.
        """
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can void a SEPA export.",
            ))
        self._eh_lock_batches_for_void()
        self._eh_lock_transitions()
        for rec in self:
            if rec.state not in ('generated', 'downloaded'):
                raise UserError(_(
                    "Export %s is already retired and cannot be voided "
                    "again.",
                    rec.message_id,
                ))
        # Sanctioned state transition: run the guarded write as su, but post
        # the audit note as the real manager so the trail names who voided it.
        self.sudo().with_context(**{
            SEPA_EXPORT_CONTEXT: SEPA_EXPORT_CAPABILITY,
        }).write({'state': 'superseded'})
        for rec in self:
            rec.message_post(body=_(
                "SEPA export voided for re-cut by %(user)s. This file must "
                "NOT be submitted to the bank; a fresh export supersedes it.",
                user=self.env.user.display_name,
            ))
        return True

    @staticmethod
    def compute_hash(content_bytes):
        return hashlib.sha256(content_bytes).hexdigest()

    def unlink(self):
        if not self.env.su:
            raise AccessError(_(
                "SEPA Credit Transfer export audit rows cannot be deleted.",
            ))
        return super().unlink()


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    eh_sepa_ct_export_evidence = fields.Boolean(
        default=False,
        copy=False,
        index=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        if any(vals.get('eh_sepa_ct_export_evidence') for vals in vals_list):
            if self.env.context.get(
                    SEPA_ATTACHMENT_CONTEXT) is not SEPA_ATTACHMENT_CAPABILITY:
                raise AccessError(_(
                    "SEPA attachment evidence markers are server-owned."
                ))
        return super().create(vals_list)

    def _eh_ct_export_refs(self):
        marked = self.filtered('eh_sepa_ct_export_evidence')
        if not marked:
            return self.env['eh.sepa.export']
        return self.env['eh.sepa.export'].sudo().search([
            ('attachment_id', 'in', marked.ids),
        ])

    def _eh_ct_authorized_export_refs(self):
        """Return refs only after checking the real caller's export ACL."""
        exports = self._eh_ct_export_refs()
        if exports:
            # Attachment routes may hand us a sudo record after
            # _can_return_content(). sudo(False) restores the same real user
            # and enforces export ACLs/company rules again at the byte boundary.
            exports.sudo(False)._eh_check_access('read')
        return exports

    def _can_return_content(self, field_name=None, access_token=None):
        exports = self._eh_ct_export_refs()
        if exports:
            exports.sudo(False)._eh_check_access('read')
            return True
        return super()._can_return_content(field_name, access_token)

    def write(self, vals):
        if 'eh_sepa_ct_export_evidence' in vals and self.env.context.get(
                SEPA_ATTACHMENT_CONTEXT) is not SEPA_ATTACHMENT_CAPABILITY:
            raise AccessError(_(
                "SEPA attachment evidence markers are immutable."
            ))
        if not self.env.su and self._eh_ct_export_refs():
            raise AccessError(_(
                "A generated SEPA Credit Transfer XML attachment is "
                "immutable.",
            ))
        return super().write(vals)

    def unlink(self):
        if not self.env.su and self._eh_ct_export_refs():
            raise AccessError(_(
                "A generated SEPA Credit Transfer XML attachment cannot be "
                "deleted.",
            ))
        return super().unlink()

    def _read_format(self, fnames, load='_classic_read'):
        protected_formats = {'raw', 'datas', 'db_datas'} & set(fnames)
        if protected_formats:
            # Reject retired evidence before the storage layer loads any
            # bytes. The later integrity check remains bound to the exact
            # bytes returned for active exports.
            for attachment in self:
                for export in attachment._eh_ct_authorized_export_refs():
                    export._assert_downloadable()
        result = super()._read_format(fnames, load=load)
        if protected_formats:
            values_by_id = {values['id']: values for values in result}
            for attachment in self:
                values = values_by_id.get(attachment.id, {})
                content = None
                if 'raw' in values and not (
                        self.env.context.get('bin_size')
                        or self.env.context.get('bin_size_raw')):
                    content = values['raw'] or b''
                elif 'datas' in values and not (
                        self.env.context.get('bin_size')
                        or self.env.context.get('bin_size_datas')):
                    try:
                        content = base64.b64decode(
                            values['datas'] or b'', validate=True,
                        )
                    except (TypeError, ValueError) as exc:
                        raise UserError(_(
                            "The stored XML attachment could not be decoded."
                        )) from exc
                elif values.get('db_datas'):
                    content = values['db_datas']
                for export in attachment._eh_ct_authorized_export_refs():
                    export._verify_attachment_integrity(
                        attachment=attachment,
                        content=content,
                    )
        return result

    def _to_http_stream(self):
        # Covers direct /web/content/<attachment-id> requests as well as the
        # model button, so a caller cannot bypass hash verification by keeping
        # or guessing the attachment URL.
        self.ensure_one()
        exports = self._eh_ct_authorized_export_refs()
        if not exports:
            return super()._to_http_stream()
        for export in exports:
            export._assert_downloadable()
        content = self.sudo().raw or b''
        for export in exports:
            export._verify_attachment_integrity(
                attachment=self,
                content=content,
            )
        # Serve the same in-memory bytes that were hashed.  Do not let the
        # base implementation reopen a filestore path after verification.
        return Stream(
            type='data',
            data=content,
            mimetype=self.mimetype,
            download_name=self.name,
            etag=self.checksum,
            last_modified=self.write_date,
            size=len(content),
            public=self.public,
        )


class IrBinary(models.AbstractModel):
    _inherit = 'ir.binary'

    def _record_to_stream(self, record, field_name):
        """Route Odoo 16/17 /web/content through PAIN.001 verification."""
        if (
            record._name == 'ir.attachment'
            and field_name in ('raw', 'datas', 'db_datas')
            and record._eh_ct_export_refs()
        ):
            return record._to_http_stream()
        return super()._record_to_stream(record, field_name)
