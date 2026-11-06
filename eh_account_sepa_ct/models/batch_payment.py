# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Extend eh.batch.payment with a SEPA Credit Transfer export action.

The action assembles the PAIN.001 input dict from the batch's payments
and delegates to the pure-python generator. Output is stored as an
ir.attachment and an eh.sepa.export audit row.
"""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.eh_account_sepa_ct.tools import pain_001
from odoo.addons.eh_account_sepa_ct.tools.sepa_charset import (
    sanitize_sepa_text,
)
from .sepa_export import (
    SEPA_ATTACHMENT_CAPABILITY,
    SEPA_ATTACHMENT_CONTEXT,
    SEPA_EXPORT_CAPABILITY,
    SEPA_EXPORT_CONTEXT,
)
from odoo.addons.eh_account_sepa_ct.tools.iban_validator import (
    validate_iban, IbanValidationError,
)
from odoo.addons.eh_account_sepa_ct.tools.bic_validator import (
    validate_bic, BicValidationError,
)
from odoo.addons.eh_account_batch_payment.models.batch_payment import (
    POSTED_STATES,
)


_CT_SOURCE_FINGERPRINT_FIELD = 'sepa_ct_source_fingerprint'
_CT_SOURCE_FINGERPRINT_CONTEXT = 'eh_authorized_sepa_ct_source_fingerprint'
_CT_SOURCE_FINGERPRINT_CAPABILITY = object()
_CT_SOURCE_APPROVAL_CONTEXT = 'eh_authorized_sepa_ct_source_approval'
_CT_SOURCE_APPROVAL_CAPABILITY = object()
_CT_SERVER_OWNED_FIELDS = frozenset({
    _CT_SOURCE_FINGERPRINT_FIELD,
    'last_sepa_export_id',
    'sepa_ct_source_approved_at',
    'sepa_ct_source_approved_by_id',
})


class EhBatchPayment(models.Model):
    _inherit = 'eh.batch.payment'

    sepa_export_ids = fields.One2many(
        'eh.sepa.export', 'batch_id',
        copy=False,
    )
    last_sepa_export_id = fields.Many2one(
        'eh.sepa.export',
        compute='_compute_last_export', store=True,
    )
    sepa_ct_source_fingerprint = fields.Char(
        copy=False,
        readonly=True,
        groups='eh_account_base.group_eh_manager',
        help=(
            "SHA-256 of the approved originator, creditor bank accounts, "
            "and creditor names. Export recomputes it under row locks and "
            "fails closed if any source changed after confirmation."
        ),
    )
    sepa_ct_source_approval_ids = fields.One2many(
        'eh.sepa.ct.source.approval', 'batch_id', readonly=True, copy=False)
    sepa_ct_source_approved_at = fields.Datetime(readonly=True, copy=False)
    sepa_ct_source_approved_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False)
    sepa_ct_reapproval_reason = fields.Text(
        copy=False,
        help="Manager explanation required to approve changed source banking "
             "data on an already-posted batch.")
    sepa_ct_enabled = fields.Boolean(
        string="SEPA Credit Transfer",
        default=False,
        copy=False,
        help=(
            "Explicitly route this outbound batch through SEPA source "
            "approval and PAIN.001 export controls. A journal originator "
            "alone never changes ordinary non-SEPA batches."
        ),
    )

    @api.model_create_multi
    def create(self, vals_list):
        touched = set().union(*(set(vals) for vals in vals_list))
        if touched.intersection(_CT_SERVER_OWNED_FIELDS):
            if not (
                self.env.context.get(_CT_SOURCE_FINGERPRINT_CONTEXT)
                is _CT_SOURCE_FINGERPRINT_CAPABILITY
            ):
                raise AccessError(_(
                    "SEPA Credit Transfer approval fingerprints are "
                    "server-owned and cannot be supplied by a client."
                ))
        return super().create(vals_list)

    def write(self, vals):
        if _CT_SERVER_OWNED_FIELDS.intersection(vals) and not (
            self.env.context.get(_CT_SOURCE_FINGERPRINT_CONTEXT)
            is _CT_SOURCE_FINGERPRINT_CAPABILITY
        ):
            raise AccessError(_(
                "SEPA Credit Transfer approval fingerprints are immutable."
            ))
        if 'sepa_ct_enabled' in vals and any(
                batch.state != 'draft' for batch in self):
            raise AccessError(_(
                "SEPA routing is frozen after batch confirmation."
            ))
        return super().write(vals)

    def action_confirm(self):
        """Capture the SEPA payment targets inside the approval transaction.

        Ordinary outbound batches remain outside SEPA controls even when their
        journal has an originator. An explicitly enabled SEPA batch requires
        every target to be explicit and trusted when the batch is approved.
        """
        with self.env.cr.savepoint():
            result = super().action_confirm()
            for batch in self.filtered(
                    lambda rec: rec.batch_type == 'outbound'
                    and rec.sepa_ct_enabled):
                originator = batch._eh_ct_originator()
                fingerprint = False
                if originator:
                    fingerprint = batch._eh_ct_source_fingerprint(
                        originator, lock=True,
                    )
                batch.sudo().with_context(
                    **{
                        _CT_SOURCE_FINGERPRINT_CONTEXT:
                            _CT_SOURCE_FINGERPRINT_CAPABILITY,
                    }
                ).write({
                    _CT_SOURCE_FINGERPRINT_FIELD: fingerprint,
                    'sepa_ct_source_approved_at': fields.Datetime.now()
                        if fingerprint else False,
                    'sepa_ct_source_approved_by_id': self.env.user.id
                        if fingerprint else False,
                })
                if fingerprint:
                    batch._eh_record_ct_source_approval(
                        previous_fingerprint=False,
                        fingerprint=fingerprint,
                        reason=_("Captured during batch confirmation."),
                    )
            return result

    @api.depends('sepa_export_ids', 'sepa_export_ids.state')
    def _compute_last_export(self):
        for batch in self:
            active = batch.sepa_export_ids.filtered(
                lambda e: e.state in ('generated', 'downloaded'),
            ).sorted('generated_at', reverse=True)
            batch.last_sepa_export_id = active[:1]

    def _eh_ct_originator(self):
        self.ensure_one()
        return self.env['eh.sepa.originator'].search(
            [
                ('journal_id', '=', self.journal_id.id),
                ('journal_id.company_id', 'in',
                 [self.company_id.id, False]),
            ],
            limit=1,
        )

    def _eh_lock_ct_source_rows(self, originator):
        """Prevent the exact source rows from changing during hash/render.

        The batch and its payment pointers are already row-locked by the
        confirmation/export workflow.  Lock the referenced configuration,
        partner-bank, bank/BIC and partner-name rows before reading payload
        values so the fingerprint and emitted XML observe one database state.
        """
        self.ensure_one()
        payments = self.payment_ids.sorted('id')
        bank_ids = sorted(set(payments.mapped('partner_bank_id').ids))

        self.env['eh.sepa.originator'].flush_model()
        self.env['res.partner.bank'].flush_model()
        self.env['res.partner'].flush_model()
        self.env['res.bank'].flush_model()
        self.env.cr.execute(
            "SELECT id FROM eh_sepa_originator WHERE id = %s FOR SHARE",
            (originator.id,),
        )
        if bank_ids:
            self.env.cr.execute(
                "SELECT id FROM res_partner_bank WHERE id IN %s "
                "ORDER BY id FOR SHARE",
                (tuple(bank_ids),),
            )
        banks = self.env['res.partner.bank'].browse(bank_ids)
        banks.invalidate_recordset()

        partner_ids = set(payments.mapped('partner_id').ids)
        partner_ids.update(banks.mapped('partner_id').ids)
        if partner_ids:
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE id IN %s "
                "ORDER BY id FOR SHARE",
                (tuple(sorted(partner_ids)),),
            )
        partners = self.env['res.partner'].browse(sorted(partner_ids))
        partners.invalidate_recordset()
        commercial_ids = set(partners.mapped('commercial_partner_id').ids)
        extra_commercial_ids = commercial_ids - partner_ids
        if extra_commercial_ids:
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE id IN %s "
                "ORDER BY id FOR SHARE",
                (tuple(sorted(extra_commercial_ids)),),
            )
        bank_model_ids = sorted(set(banks.mapped('bank_id').ids))
        if bank_model_ids:
            self.env.cr.execute(
                "SELECT id FROM res_bank WHERE id IN %s "
                "ORDER BY id FOR SHARE",
                (tuple(bank_model_ids),),
            )
        self.env['res.bank'].browse(bank_model_ids).invalidate_recordset()
        originator.invalidate_recordset()
        payments.invalidate_recordset()
        return payments

    def _eh_assert_ct_bank_target(self, payment):
        """Return the explicit trusted bank target or fail closed."""
        self.ensure_one()
        bank = payment.partner_bank_id
        if not bank:
            raise UserError(_(
                "Payment %(payment)s has no explicit recipient bank account. "
                "Select and verify partner_bank_id before confirming the "
                "batch; SEPA export never guesses the first partner bank.",
                payment=payment.display_name,
            ))
        if not bank.active:
            raise UserError(_(
                "Payment %(payment)s targets archived bank account %(bank)s.",
                payment=payment.display_name,
                bank=bank.display_name,
            ))
        if not bank.allow_out_payment:
            raise UserError(_(
                "Payment %(payment)s targets bank account %(bank)s, which "
                "has not been verified for outgoing payments.",
                payment=payment.display_name,
                bank=bank.display_name,
            ))
        payment_owner = payment.partner_id.commercial_partner_id
        bank_owner = bank.partner_id.commercial_partner_id
        if bank_owner != payment_owner:
            raise UserError(_(
                "Payment %(payment)s bank account belongs to %(bank_owner)s, "
                "not the payment counterparty %(payment_owner)s.",
                payment=payment.display_name,
                bank_owner=bank_owner.display_name,
                payment_owner=payment_owner.display_name,
            ))
        if bank.company_id and bank.company_id != self.company_id:
            raise UserError(_(
                "Payment %(payment)s bank account belongs to company "
                "%(bank_company)s, not batch company %(batch_company)s.",
                payment=payment.display_name,
                bank_company=bank.company_id.display_name,
                batch_company=self.company_id.display_name,
            ))
        return bank

    @staticmethod
    def _eh_ct_counterparty_name(payment):
        # ``display_name`` is context-sensitive; different exporter language
        # or display flags must not change either the fingerprint or bank file.
        return (
            payment.partner_id.complete_name
            or payment.partner_id.name
            or ''
        ).strip()

    def _eh_ct_source_fingerprint(self, originator, lock=False):
        """Hash the exact mutable source values used by PAIN.001."""
        self.ensure_one()
        originator._eh_assert_journal_bank_account()
        payments = (
            self._eh_lock_ct_source_rows(originator)
            if lock else self.payment_ids.sorted('id')
        )
        source = {
            'originator': {
                'id': originator.id,
                'journal_id': originator.journal_id.id,
                'initiating_party_name': originator.initiating_party_name or '',
                'initiating_party_identifier': (
                    originator.initiating_party_identifier or ''
                ),
                'iban': originator.iban or '',
                'bic': originator.bic or '',
                'requested_execution_offset_days': (
                    originator._eh_validated_requested_execution_offset_days()
                ),
                'pain_001_version': originator.pain_001_version or '09',
                'segregation_policy': originator.segregation_policy,
                'charge_bearer': originator.charge_bearer,
                'instruction_priority': originator.instruction_priority or '',
                'batch_booking': bool(originator.batch_booking),
            },
            'payments': [],
        }
        for payment in payments:
            bank = self._eh_assert_ct_bank_target(payment)
            # Validate canonical financial identifiers while approval is still
            # reversible.  The generator validates again defensively.
            try:
                iban = validate_iban(bank.acc_number)
            except IbanValidationError as exc:
                raise UserError(_(
                    "Payment %(payment)s: creditor IBAN invalid (%(error)s).",
                    payment=payment.display_name,
                    error=str(exc),
                ))
            bic = ''
            if bank.bank_bic:
                try:
                    bic = validate_bic(
                        bank.bank_bic,
                        version=originator.pain_001_version or '09',
                    )
                except BicValidationError as exc:
                    raise UserError(_(
                        "Payment %(payment)s: creditor BIC invalid "
                        "(%(error)s).",
                        payment=payment.display_name,
                        error=str(exc),
                    ))
            source['payments'].append({
                'payment_id': payment.id,
                'partner_id': payment.partner_id.id,
                'commercial_partner_id': (
                    payment.partner_id.commercial_partner_id.id
                ),
                'creditor_name': self._eh_ct_counterparty_name(payment),
                'bank_id': bank.id,
                'bank_owner_id': bank.partner_id.id,
                'bank_company_id': bank.company_id.id or 0,
                'active': bool(bank.active),
                'allow_out_payment': bool(bank.allow_out_payment),
                'iban': iban,
                'bic': bic,
                'structured_reference': (
                    payment.sepa_ct_structured_reference or ''
                ),
                'payment_date': fields.Date.to_string(payment.date),
            })
        canonical = json.dumps(
            source, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        ).encode('utf-8')
        return hashlib.sha256(canonical).hexdigest()

    def _eh_assert_ct_sources_unchanged(self, originator):
        self.ensure_one()
        approved = self.sepa_ct_source_fingerprint
        if not approved:
            raise UserError(_(
                "This posted batch has no SEPA source fingerprint captured "
                "at confirmation. Legacy/unapproved payment targets cannot "
                "be exported; review and re-approve the batch through a "
                "controlled source re-approval."
            ))
        current = self._eh_ct_source_fingerprint(originator, lock=True)
        if not hmac.compare_digest(approved, current):
            raise UserError(_(
                "SEPA source data changed after batch confirmation (recipient "
                "bank, trust flag, creditor name, or originator details). "
                "Export was blocked; do not cut a bank file from inputs the "
                "approvers did not review. Record a re-approval reason and "
                "use Re-approve SEPA Sources."
            ))
        return True

    def _eh_record_ct_source_approval(
        self, previous_fingerprint, fingerprint, reason,
    ):
        self.ensure_one()
        return self.env['eh.sepa.ct.source.approval'].sudo().with_context(**{
            _CT_SOURCE_APPROVAL_CONTEXT: _CT_SOURCE_APPROVAL_CAPABILITY,
        }).create({
            'batch_id': self.id,
            'previous_fingerprint': previous_fingerprint or False,
            'source_fingerprint': fingerprint,
            'reason': reason,
            'approved_by_id': self.env.user.id,
        })

    def action_reapprove_sepa_ct_sources(self):
        """Approve current bank-file inputs without changing booked cash."""
        self.ensure_one()
        self._eh_check_access('write')
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise AccessError(_(
                "Only an EH Accounting Manager can re-approve SEPA sources."
            ))
        self._eh_lock_batch_for_export()
        if (
            self.state != 'posted'
            or self.batch_type != 'outbound'
            or not self.sepa_ct_enabled
        ):
            raise UserError(_(
                "Only a posted outbound batch can re-approve SEPA Credit "
                "Transfer sources."
            ))
        originator = self._eh_ct_originator()
        if not originator:
            raise UserError(_(
                "The batch journal has no SEPA originator to approve."
            ))
        if (
            originator.segregation_policy == 'three_person'
            and (
                self.confirmed_by_id == self.env.user
                or self.posted_by_id == self.env.user
            )
        ):
            raise UserError(_(
                "Segregation of duties: the confirmer or poster cannot also "
                "approve changed bank-file sources."
            ))
        if self.sepa_export_ids.filtered(
                lambda export: export.state in ('generated', 'downloaded')):
            raise UserError(_(
                "Void any live SEPA file before re-approving its source data."
            ))
        reason = ' '.join((self.sepa_ct_reapproval_reason or '').split())
        if len(reason) < 20:
            raise UserError(_(
                "Record at least 20 characters explaining the changed source "
                "data and evidence reviewed."
            ))
        previous = self.sepa_ct_source_fingerprint
        current = self._eh_ct_source_fingerprint(originator, lock=True)
        approved_at = fields.Datetime.now()
        self.sudo().with_context(**{
            _CT_SOURCE_FINGERPRINT_CONTEXT: _CT_SOURCE_FINGERPRINT_CAPABILITY,
        }).write({
            _CT_SOURCE_FINGERPRINT_FIELD: current,
            'sepa_ct_source_approved_at': approved_at,
            'sepa_ct_source_approved_by_id': self.env.user.id,
        })
        self._eh_record_ct_source_approval(previous, current, reason)
        self.write({'sepa_ct_reapproval_reason': False})
        self.message_post(body=_(
            "SEPA Credit Transfer source revision approved by %(user)s. "
            "Reason: %(reason)s",
            user=self.env.user.display_name, reason=reason,
        ))
        return True

    def action_export_sepa_ct(self):
        """Generate the PAIN.001 XML and persist it as an attachment.

        Strict prerequisites:
        * Batch is outbound (SEPA CT pays out).
        * Batch is posted (we do not export drafts that have not been
          authorised).
        * Originator config exists for the batch's journal.
        * Every payment has a partner with a primary IBAN bank account
          on the company.
        * Every IBAN and (optional) BIC validates locally.
        """
        self.ensure_one()
        # Resolve ACLs/company record rules before the raw row lock or any
        # generator sudo.  A guessed cross-company batch id must not become a
        # lock/sudo side channel merely because the caller is a manager in a
        # different legal entity.
        self._eh_check_access('write')
        # Generating PAIN.001 is money-moving. Manager access always applies;
        # selected originator policy controls two- versus three-person SoD.
        if not self.env.user.has_group('eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager can generate the SEPA "
                "Credit Transfer bank file.",
            ))
        # Serialize immediately after authorization, then re-read every
        # workflow/source pointer from the locked batch before validating it.
        self._eh_lock_batch_for_export()
        if not self.sepa_ct_enabled:
            raise UserError(_(
                "Enable SEPA Credit Transfer on the draft batch before "
                "confirmation. Journal configuration alone never opts an "
                "ordinary batch into SEPA controls."
            ))
        originator = self._eh_ct_originator()
        if not originator:
            raise UserError(_(
                "Journal %s has no SEPA originator configured. Add "
                "one under Configuration > SEPA Originators.",
                self.journal_id.display_name,
            ))
        if originator.segregation_policy == 'three_person':
            if self.confirmed_by_id and self.confirmed_by_id == self.env.user:
                raise UserError(_(
                    "Three-person policy: the confirmer cannot generate the "
                    "SEPA bank file."
                ))
            if self.posted_by_id and self.posted_by_id == self.env.user:
                raise UserError(_(
                    "Three-person policy: the poster cannot generate the "
                    "SEPA bank file."
                ))
        if (
            self.sepa_ct_source_approved_by_id
            and self.sepa_ct_source_approved_by_id == self.env.user
        ):
            raise UserError(_(
                "Segregation of duties: the manager who approved the current "
                "SEPA source revision cannot also generate the bank file."
            ))
        if self.batch_type != 'outbound':
            raise UserError(_(
                "SEPA Credit Transfer is for outbound batches only.",
            ))
        if self.state != 'posted':
            raise UserError(_(
                "Export the SEPA file only after the batch is posted. "
                "This guarantees the bank receives the same payments "
                "the GL has booked.",
            ))
        # Re-lock and re-hash the mutable recipient/config rows immediately
        # before rendering.  Payment/batch FKs are frozen after confirmation,
        # but their referenced partner/bank/originator rows are not.
        self._eh_assert_ct_sources_unchanged(originator)

        # Single-currency enforcement. ISO 20022 PAIN.001 expresses one
        # control sum at the message level, derived from amounts in a
        # single currency. Banks reject mixed-currency files; we refuse
        # at submission time so the user fixes the batch before the
        # bank does.
        currencies = self.payment_ids.filtered(
            lambda p: p.state in POSTED_STATES,
        ).mapped('currency_id')
        if len(currencies) > 1:
            raise UserError(_(
                "Batch contains payments in %(count)s different "
                "currencies (%(names)s). Split the batch by currency "
                "before exporting; SEPA Credit Transfer requires one "
                "currency per file.",
                count=len(currencies),
                names=', '.join(currencies.mapped('name')),
            ))
        # Euro enforcement. SEPA Credit Transfer is a euro-denominated
        # scheme and the generated pain.001 declares EUR at the amount
        # level. Refuse a non-euro batch here rather than emit a file
        # that mislabels the amounts as EUR.
        if currencies and currencies.name != 'EUR':
            raise UserError(_(
                "SEPA Credit Transfer files are euro-denominated, but "
                "this batch is in %(name)s. Convert the payments to EUR "
                "or use a non-SEPA payment method.",
                name=currencies.name,
            ))

        # Idempotency guard. The PAIN.001 IS the instruction the bank
        # executes. Regenerating it for the same posted batch mints a fresh
        # MsgId (so the two files never collide) while carrying the SAME
        # EndToEndIds and amounts - the bank treats them as two independent
        # instructions and, if both are submitted, pays every supplier twice.
        # Take a row lock on the batch BEFORE inspecting prior exports so two
        # managers who click Export at the same instant serialise rather than
        # both observe "no active export" and both cut a file. A genuine
        # re-cut (a file that was never sent) goes through the explicit,
        # audited action_void_for_recut on the prior export, which retires it
        # before a fresh export is allowed.
        active_export = self.sepa_export_ids.filtered(
            lambda e: e.state in ('generated', 'downloaded'),
        )
        if active_export:
            raise UserError(_(
                "This batch already has an active SEPA Credit Transfer file "
                "(Message ID: %(id)s, generated %(when)s by %(who)s). "
                "Re-exporting would create a second, independent bank "
                "instruction for the same payments and risk paying the "
                "suppliers twice. If that file was NOT submitted to the bank "
                "and you must re-cut, void it first from the SEPA exports "
                "tab, then export again.",
                id=active_export[0].message_id,
                when=active_export[0].generated_at,
                who=active_export[0].generated_by_id.display_name,
            ))

        payload = self._eh_build_pain_001_payload(originator)
        version = originator.pain_001_version or '09'
        xml_bytes = pain_001.render(payload, version=version)
        attachment = self._eh_persist_sepa_attachment(payload, xml_bytes)
        # Creation under sudo is the server-side provenance proof enforced by
        # eh.sepa.export.create(); sudo keeps the real exporter as env.user.
        export = self.env['eh.sepa.export'].sudo().with_context(**{
            SEPA_EXPORT_CONTEXT: SEPA_EXPORT_CAPABILITY,
        }).create({
            'batch_id': self.id,
            'message_id': payload['message_id'],
            'transaction_count': sum(
                len(p['transactions']) for p in payload['payments']
            ),
            'control_sum': float(sum(
                pain_001.quantize_amount(tx['amount'])
                for p in payload['payments']
                for tx in p['transactions']
            )),
            'file_hash': self.env['eh.sepa.export'].compute_hash(xml_bytes),
            'attachment_id': attachment.id,
        })
        self.message_post(body=_(
            "SEPA Credit Transfer file generated. Message ID: %s. "
            "Transactions: %d. Control sum: %.2f.",
            export.message_id,
            export.transaction_count,
            export.control_sum,
        ))
        # Generation and download are distinct audit events. Open the evidence
        # row; only its Download button stamps the actual retrieval request.
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'eh.sepa.export',
            'res_id': export.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def _eh_build_pain_001_payload(self, originator):
        self.ensure_one()
        payments = self.payment_ids.filtered(
            lambda p: p.state in POSTED_STATES,
        )
        if not payments:
            raise UserError(_(
                "Batch contains no posted payments to export.",
            ))

        # Normalise originator IBAN/BIC (already validated at write time
        # by the originator's @api.constrains). The validator returns
        # the canonicalised form so we re-normalise defensively.
        try:
            dbtr_iban = validate_iban(originator.iban)
        except IbanValidationError as exc:
            raise UserError(_(
                "Originator IBAN failed validation: %s. Fix the SEPA "
                "originator config and retry.",
            ) % str(exc))
        dbtr_bic = None
        if originator.bic:
            try:
                dbtr_bic = validate_bic(
                    originator.bic,
                    version=originator.pain_001_version or '09',
                )
            except BicValidationError as exc:
                raise UserError(_(
                    "Originator BIC failed validation: %s.",
                ) % str(exc))
        execution_offset = (
            originator._eh_validated_requested_execution_offset_days()
        )

        transactions = []
        for idx, payment in enumerate(payments, start=1):
            partner_bank = self._eh_resolve_creditor_bank(payment)
            try:
                cdtr_iban = validate_iban(partner_bank.acc_number)
            except IbanValidationError as exc:
                raise UserError(_(
                    "Payment %(name)s: creditor IBAN invalid (%(err)s).",
                    name=payment.display_name,
                    err=str(exc),
                ))
            cdtr_bic = None
            if partner_bank.bank_bic:
                try:
                    cdtr_bic = validate_bic(
                        partner_bank.bank_bic,
                        version=originator.pain_001_version or '09',
                    )
                except BicValidationError as exc:
                    raise UserError(_(
                        "Payment %(name)s: creditor BIC invalid "
                        "(%(err)s).",
                        name=payment.display_name,
                        err=str(exc),
                    ))
            structured_reference = payment.sepa_ct_structured_reference or None
            if structured_reference:
                try:
                    structured_reference = (
                        pain_001.normalize_structured_reference(
                            structured_reference,
                        )
                    )
                except pain_001.Pain001GenerationError as exc:
                    raise UserError(_(
                        "Payment %(name)s: invalid structured creditor "
                        "reference (%(error)s).",
                        name=payment.display_name,
                        error=str(exc),
                    ))
            transactions.append({
                'end_to_end_id': self._eh_clip(
                    payment.name or "PMT%05d" % idx, 35,
                ),
                'amount': Decimal(str(payment.amount)),
                'creditor': {
                    'name': self._eh_clip(
                        self._eh_ct_counterparty_name(payment), 70,
                    ),
                    'iban': cdtr_iban,
                    'bic': cdtr_bic,
                },
                'remittance_info': self._eh_clip(
                    payment.memo or '', 140,
                ) or None,
                'structured_reference': structured_reference,
            })

        return {
            'message_id': self._eh_sepa_msg_id(),
            'creation_datetime': datetime.utcnow(),
            'initiating_party': {
                'name': self._eh_clip(originator.initiating_party_name, 70),
                'identifier': self._eh_clip(
                    originator.initiating_party_identifier or '', 35,
                ) or None,
            },
            'payments': [
                {
                    'payment_info_id': self._eh_clip(
                        self.name.replace('/', '-'), 35,
                    ),
                    'requested_execution_date': (
                        self._eh_requested_execution_date(
                            payments, execution_offset,
                        )
                    ),
                    'debtor': {
                        'name': self._eh_clip(
                            originator.initiating_party_name, 70,
                        ),
                        'iban': dbtr_iban,
                        'bic': dbtr_bic,
                    },
                    'charge_bearer': originator.charge_bearer or 'SLEV',
                    'instruction_priority': (
                        originator.instruction_priority or None
                    ),
                    'batch_booking': bool(originator.batch_booking),
                    'transactions': transactions,
                },
            ],
        }

    def _eh_requested_execution_date(self, payments, offset_days):
        """Clamp execution to today and every included payment date."""
        self.ensure_one()
        candidates = [
            fields.Date.context_today(self),
            fields.Date.to_date(self.payment_date),
        ]
        candidates.extend(
            fields.Date.to_date(payment.date)
            for payment in payments
            if payment.date
        )
        return max(candidates) + timedelta(days=offset_days)

    def _eh_resolve_creditor_bank(self, payment):
        """Return only the explicitly approved and trusted payment target."""
        return self._eh_assert_ct_bank_target(payment)

    @staticmethod
    def _eh_clip(value, max_length):
        if not value:
            return value
        return sanitize_sepa_text(value)[:max_length]

    def _eh_sepa_msg_id(self):
        """Globally unique SEPA MsgId (max 35 chars): a short sanitised
        batch name, a UTC-clock timestamp, and a uuid fragment so two
        exports in the same second never collide. The name part is capped
        so the length limit can never truncate the uuid.
        """
        self.ensure_one()
        name_part = sanitize_sepa_text(
            (self.name or 'EH').replace('/', '-'),
        )[:11]
        ts = fields.Datetime.now().strftime('%Y%m%d%H%M%S')
        return ("%s-%s-%s" % (name_part, ts, uuid.uuid4().hex[:8]))[:35]

    def _eh_lock_batch_for_export(self):
        """Take a row lock on this batch and drop cached state so a serialised
        concurrent export re-reads the committed exports rather than a stale
        pre-export snapshot.

        Closes the double-submit race in which two managers both observe no
        active eh.sepa.export, both render a PAIN.001 and both create an export
        row - two independent bank instructions for one posted batch, i.e. a
        double supplier payment. Mirrors eh_account_fx_revaluation's
        _eh_lock_for_post.
        """
        self.ensure_one()
        self.flush_recordset()
        self.env.cr.execute(
            "SELECT id FROM eh_batch_payment WHERE id = %s FOR UPDATE",
            (self.id,),
        )
        self.invalidate_recordset()

    def _eh_persist_sepa_attachment(self, payload, xml_bytes):
        self.ensure_one()
        filename = "%s_%s.xml" % (
            self.name.replace('/', '_'),
            fields.Datetime.now().strftime('%Y%m%d%H%M%S'),
        )
        # The persisted bank instruction is generator-owned. Once its export
        # audit row is created, the attachment model makes it immutable.
        return self.env['ir.attachment'].sudo().with_context(**{
            SEPA_ATTACHMENT_CONTEXT: SEPA_ATTACHMENT_CAPABILITY,
        }).create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(xml_bytes),
            'mimetype': 'application/xml',
            'res_model': self._name,
            'res_id': self.id,
            'company_id': self.company_id.id,
            'eh_sepa_ct_export_evidence': True,
        })


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    sepa_ct_structured_reference = fields.Char(
        string="Structured creditor reference",
        copy=False,
        help=(
            "Optional ISO 11649 RF creditor reference. When set, PAIN.001 "
            "renders structured SCOR remittance instead of free text."
        ),
    )

    def write(self, vals):
        if 'sepa_ct_structured_reference' in vals and any(
            payment.eh_batch_payment_id
            and payment.eh_batch_payment_id.state != 'draft'
            for payment in self
        ):
            raise AccessError(_(
                "Structured creditor reference is frozen after batch "
                "confirmation."
            ))
        return super().write(vals)


class EhSepaCtSourceApproval(models.Model):
    _name = 'eh.sepa.ct.source.approval'
    _description = "SEPA Credit Transfer source approval"
    _order = 'approved_at desc, id desc'

    batch_id = fields.Many2one(
        'eh.batch.payment', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(
        related='batch_id.company_id', store=True, readonly=True, index=True)
    previous_fingerprint = fields.Char(readonly=True)
    source_fingerprint = fields.Char(required=True, readonly=True, index=True)
    reason = fields.Text(required=True, readonly=True)
    approved_at = fields.Datetime(
        required=True, readonly=True, default=fields.Datetime.now)
    approved_by_id = fields.Many2one(
        'res.users', required=True, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(
                _CT_SOURCE_APPROVAL_CONTEXT) is not _CT_SOURCE_APPROVAL_CAPABILITY:
            raise AccessError(_(
                "SEPA source approval evidence can only be created by the "
                "controlled approval workflow."
            ))
        return super().create(vals_list)

    def write(self, vals):
        raise AccessError(_("SEPA source approval evidence is append-only."))

    def unlink(self):
        raise AccessError(_("SEPA source approval evidence cannot be deleted."))
