# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Manual reminder wizard.

Lets a collector compose and send an ad-hoc reminder over email and/or
SMS to the case partner, with an editable subject and body, outside the
automated follow-up ladder. Every send is written to the case action
log so the manual touch sits alongside the automated ones.
"""

from odoo import _, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import plaintext2html


class EhCollectionsReminderWizard(models.TransientModel):
    _name = 'eh.collections.reminder.wizard'
    _description = "Collections manual reminder"

    case_id = fields.Many2one(
        'eh.collections.case', required=True, ondelete='cascade',
    )
    partner_id = fields.Many2one('res.partner', required=True)
    channel = fields.Selection(
        [('email', "Email"), ('sms', "SMS"), ('both', "Email and SMS")],
        default='email', required=True,
    )
    subject = fields.Char()
    body = fields.Text(required=True)

    def action_send(self):
        self.ensure_one()
        case = self.case_id
        case._eh_check_access('write')
        if case.company_id not in self.env.companies:
            raise AccessError(_(
                "You cannot send reminders for this case's company.",
            ))
        partner = case.partner_id.commercial_partner_id or case.partner_id
        if self.partner_id != partner:
            raise AccessError(_(
                "The reminder recipient must be the partner on the case.",
            ))
        if (
            self.channel in ('email', 'both')
            and not partner.email_normalized
        ):
            raise UserError(_(
                "%s has no valid email address for a reminder.",
                partner.display_name,
            ))
        number = partner.phone or (
            partner['mobile'] if 'mobile' in partner._fields else False)
        if self.channel in ('sms', 'both') and not number:
            raise UserError(_(
                "%s has no mobile or phone number for SMS.",
                partner.display_name,
            ))
        sent = []
        # Keep a combined email/SMS request atomic even when an API caller
        # catches the raised exception instead of aborting its transaction.
        with self.env.cr.savepoint():
            if self.channel in ('email', 'both'):
                case.message_post(
                    body=plaintext2html(self.body),
                    subject=self.subject or _("Payment reminder"),
                    partner_ids=partner.ids,
                    message_type='comment',
                    subtype_xmlid='mail.mt_comment',
                )
                sent.append('email')
            if self.channel in ('sms', 'both'):
                # sms.sms creation is elevated only after the writable case,
                # company and exact case recipient have all been validated.
                self.env['sms.sms'].sudo().create({
                    'partner_id': partner.id,
                    'number': number,
                    'body': self.body,
                })
                sent.append('sms')
            case.action_log_action(
                action_type='email' if 'email' in sent else 'sms',
                summary=_("Manual reminder queued (%s)") % ', '.join(sent),
                # Queue acceptance is not proof that the partner was reached.
                contact_made=False,
            )
        return {'type': 'ir.actions.act_window_close'}
