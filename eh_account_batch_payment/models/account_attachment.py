# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""Protect and verify immutable batch bank-file field attachments."""

import base64
import binascii

from odoo import _, api, models
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.http import Stream

from .capabilities import has_authorized_batch_export_capability


_BATCH_EXPORT_CONTENT_FIELDS = ('raw', 'datas', 'db_datas')


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    def with_env(self, env):
        """Drop byte caches only when this attachment changes actor.

        Environment caches are shared by ``with_user``/``sudo`` recordsets.
        Protected export bytes therefore need an actor boundary, but declaring
        ``depends_context('uid')`` on core's global ``raw``/``datas`` computes
        partitions the cache of every attachment in Odoo.  Invalidate just the
        recordset crossing a user/superuser boundary instead.  Ordinary
        same-actor reads retain core's single cache entry.
        """
        if self.ids and (
            env.uid != self.env.uid
            or env.su != self.env.su
        ):
            self.invalidate_recordset(
                _BATCH_EXPORT_CONTENT_FIELDS,
                flush=False,
            )
        return super().with_env(env)

    @api.model
    def _eh_is_batch_export_vals(self, vals):
        return (
            vals.get('res_model') == 'eh.batch.payment'
            and vals.get('res_field') == 'export_file'
        )

    def _eh_batch_export_attachments(self):
        return self.filtered(
            lambda attachment: (
                attachment.res_model == 'eh.batch.payment'
                and attachment.res_field == 'export_file'
            ),
        )

    def _eh_batch_export_refs(self):
        protected = self._eh_batch_export_attachments()
        if not protected:
            return self.env['eh.batch.payment']
        return self.env['eh.batch.payment'].sudo().browse(
            protected.mapped('res_id'),
        ).exists()

    def _eh_authorized_export_refs(self):
        protected = self._eh_batch_export_attachments()
        batches = self._eh_batch_export_refs()
        if protected and any(
            not attachment.res_id
            or attachment.res_id not in batches.ids
            for attachment in protected
        ):
            raise AccessError(_(
                "An orphaned or mislinked batch bank export is never "
                "downloadable."
            ))
        if batches:
            real_batches = batches.sudo(False)
            real_batches._eh_check_access('read')
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise AccessError(_(
                    "Only EH Accounting Managers may read batch bank "
                    "exports."
                ))
        return batches

    def _eh_export_mutation_authorized(self):
        return has_authorized_batch_export_capability(self.env)

    def _eh_invalidate_export_content_cache(self):
        self.invalidate_recordset(
            _BATCH_EXPORT_CONTENT_FIELDS,
            flush=False,
        )

    def _eh_verify_loaded_export_content(self, field_name):
        for attachment in self._eh_batch_export_attachments():
            value = attachment[field_name]
            if field_name == 'db_datas' and not value:
                # Filestore-backed attachments legitimately have no database
                # payload. The raw compute verifies their actual file bytes.
                continue
            if field_name == 'datas':
                try:
                    value = base64.b64decode(
                        value or b'',
                        validate=True,
                    )
                except (binascii.Error, TypeError, ValueError) as exc:
                    raise UserError(_(
                        "The stored batch bank export could not be decoded."
                    )) from exc
            for batch in attachment._eh_authorized_export_refs():
                batch._eh_verify_export_content(
                    attachment=attachment,
                    content=value or b'',
                )

    def _fetch_field(self, field):
        if (
            field.name != 'db_datas'
            or not self._eh_batch_export_attachments()
            or self._eh_export_mutation_authorized()
        ):
            return super()._fetch_field(field)
        protected = self._eh_batch_export_attachments()
        try:
            protected._eh_authorized_export_refs()
            result = super()._fetch_field(field)
            if not (
                self.env.context.get('bin_size')
                or self.env.context.get('bin_size_db_datas')
            ):
                protected._eh_verify_loaded_export_content('db_datas')
            return result
        except Exception:
            protected._eh_invalidate_export_content_cache()
            raise

    @api.depends('store_fname', 'db_datas')
    def _compute_raw(self):
        protected = self._eh_batch_export_attachments()
        if not protected or self._eh_export_mutation_authorized():
            return super()._compute_raw()
        try:
            protected._eh_authorized_export_refs()
            result = super()._compute_raw()
            if not (
                self.env.context.get('bin_size')
                or self.env.context.get('bin_size_raw')
            ):
                protected._eh_verify_loaded_export_content('raw')
            return result
        except Exception:
            protected._eh_invalidate_export_content_cache()
            raise

    @api.depends('store_fname', 'db_datas', 'file_size')
    @api.depends_context('bin_size')
    def _compute_datas(self):
        """Verify protected bytes used by attached Binary field reads.

        ``fields.Binary.read()`` fetches ``attachment.datas`` directly on all
        supported series.  That path does not call ``read()``,
        ``_read_format()``, or the HTTP stream hooks below, so verification
        belongs at this computed-field seam too.
        """
        protected = self._eh_batch_export_attachments()
        if not protected or self._eh_export_mutation_authorized():
            return super()._compute_datas()
        try:
            protected._eh_authorized_export_refs()
            result = super()._compute_datas()
            if not self.env.context.get('bin_size'):
                protected._eh_verify_loaded_export_content('datas')
            return result
        except Exception:
            protected._eh_invalidate_export_content_cache()
            raise

    def _get_datas_related_values(self, data, mimetype):
        """Keep protected bank bytes out of shared stored-field cache."""
        values = super()._get_datas_related_values(data, mimetype)
        if data and self._eh_batch_export_attachments():
            # ``db_datas`` is stored and cannot be partitioned by uid. Force
            # protected exports into filestore even when global attachment
            # storage is configured as database; raw/datas are uid-partitioned
            # above and therefore cannot leak through a warmed cache.
            values['store_fname'] = self._file_write(
                data,
                values['checksum'],
            )
            values['db_datas'] = False
        return values

    @api.private
    def _eh_force_batch_export_filestore(self):
        """Move legacy protected database payloads into filestore once."""
        if not self._eh_export_mutation_authorized():
            raise AccessError(_(
                "Only the batch upgrade corridor may migrate bank exports."
            ))
        for attachment in self._eh_batch_export_attachments():
            payload = attachment.db_datas or b''
            if not payload:
                continue
            checksum = attachment.checksum or attachment._compute_checksum(
                payload,
            )
            store_fname = attachment._file_write(payload, checksum)
            # Core ir.attachment.write deliberately strips store_fname,
            # checksum, and file_size as server-owned fields. This private
            # storage migration is their owner, so update that narrow row
            # projection directly after writing the exact bytes to filestore.
            self.env.cr.execute(
                "UPDATE ir_attachment "
                "SET store_fname = %s, db_datas = NULL, checksum = %s, "
                "file_size = %s WHERE id = %s",
                (store_fname, checksum, len(payload), attachment.id),
            )
            attachment.invalidate_recordset([
                'store_fname', 'db_datas', 'checksum', 'file_size',
                'raw', 'datas',
            ], flush=False)
        return True

    @api.model_create_multi
    def create(self, vals_list):
        if (
            any(self._eh_is_batch_export_vals(vals) for vals in vals_list)
            and not self._eh_export_mutation_authorized()
        ):
            raise AccessError(_(
                "Batch bank-export attachments can only be created by the "
                "sealed Confirm/Post workflow."
            ))
        attachments = super().create(vals_list)
        protected = attachments._eh_batch_export_attachments()
        if protected:
            protected._eh_force_batch_export_filestore()
        return self.browse(attachments.ids)

    def write(self, vals):
        transitions_to_export = any(
            vals.get('res_model', attachment.res_model)
            == 'eh.batch.payment'
            and vals.get('res_field', attachment.res_field)
            == 'export_file'
            for attachment in self
        )
        if (
            (self._eh_batch_export_attachments() or transitions_to_export)
            and not self._eh_export_mutation_authorized()
        ):
            raise AccessError(_(
                "A sealed batch bank-export attachment is immutable and "
                "ordinary attachments cannot be relinked into that field."
            ))
        return super().write(vals)

    def unlink(self):
        if (
            self._eh_batch_export_attachments()
            and not self._eh_export_mutation_authorized()
        ):
            raise AccessError(_(
                "A sealed batch bank-export attachment cannot be deleted."
            ))
        return super().unlink()

    def _can_return_content(self, field_name=None, access_token=None):
        if self._eh_batch_export_attachments():
            self._eh_authorized_export_refs()
            return True
        return super()._can_return_content(field_name, access_token)

    def _read_format(self, fnames, load='_classic_read'):
        protected_formats = {'raw', 'datas', 'db_datas'} & set(fnames)
        protected = self._eh_batch_export_attachments()
        if (
            not protected_formats
            or not protected
            or self._eh_export_mutation_authorized()
        ):
            return super()._read_format(fnames, load=load)
        try:
            protected._eh_authorized_export_refs()
            result = super()._read_format(fnames, load=load)

            values_by_id = {values['id']: values for values in result}
            for attachment in protected:
                values = values_by_id.get(attachment.id, {})
                content = None
                if 'raw' in values and not (
                    self.env.context.get('bin_size')
                    or self.env.context.get('bin_size_raw')
                ):
                    content = values['raw'] or b''
                elif 'datas' in values and not (
                    self.env.context.get('bin_size')
                    or self.env.context.get('bin_size_datas')
                ):
                    try:
                        content = base64.b64decode(
                            values['datas'] or b'',
                            validate=True,
                        )
                    except (binascii.Error, TypeError, ValueError) as exc:
                        raise UserError(_(
                            "The stored batch bank export could not be decoded."
                        )) from exc
                elif values.get('db_datas') and not (
                    self.env.context.get('bin_size')
                    or self.env.context.get('bin_size_db_datas')
                ):
                    content = values['db_datas']
                if content is not None:
                    for batch in attachment._eh_authorized_export_refs():
                        batch._eh_verify_export_content(
                            attachment=attachment,
                            content=content,
                        )
            return result
        except Exception:
            protected._eh_invalidate_export_content_cache()
            raise

    def _to_http_stream(self):
        self.ensure_one()
        protected = self._eh_batch_export_attachments()
        if not protected or self._eh_export_mutation_authorized():
            return super()._to_http_stream()
        try:
            batches = self._eh_authorized_export_refs()
            content = self.sudo().raw or b''
            for batch in batches:
                batch._eh_verify_export_content(
                    attachment=self,
                    content=content,
                )
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
        except Exception:
            protected._eh_invalidate_export_content_cache()
            raise


class IrBinary(models.AbstractModel):
    _inherit = 'ir.binary'

    def _record_to_stream(self, record, field_name):
        """Route Odoo 16/17 /web/content through export verification too."""
        if (
            record._name == 'eh.batch.payment'
            and field_name == 'export_file'
        ):
            record._eh_check_access('read')
            if not self.env.user.has_group(
                'eh_account_base.group_eh_manager',
            ):
                raise AccessError(_(
                    "Only EH Accounting Managers may read batch bank exports."
                ))
            attachment = self.env['ir.attachment'].sudo().search([
                ('res_model', '=', record._name),
                ('res_id', '=', record.id),
                ('res_field', '=', field_name),
            ], limit=1)
            if not attachment:
                raise MissingError(_(
                    "The related batch bank-export attachment does not exist."
                ))
            return attachment._to_http_stream()
        if (
            record._name == 'ir.attachment'
            and field_name in ('raw', 'datas', 'db_datas')
            and record._eh_batch_export_attachments()
        ):
            return record._to_http_stream()
        return super()._record_to_stream(record, field_name)
