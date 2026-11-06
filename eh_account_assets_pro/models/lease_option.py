# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
eh.lease.option: extension / termination / purchase options on an IFRS 16
lease contract.

IFRS 16.18-19: the lease term comprises the non-cancellable period plus
periods covered by an extension option the lessee is REASONABLY CERTAIN
to exercise (and periods after a termination option it is reasonably
certain NOT to exercise). IFRS 16.27 includes in the lease payments the
exercise price of a purchase option, and penalties for terminating, when
the corresponding exercise is reasonably certain.

Only options flagged reasonably_certain affect measurement:

* extension: its months extend the term used for the schedule and the
  liability PV.
* termination: its penalty is added to the final period's payment and
  its present value to the initial liability.
* purchase: its price is added to the final period's payment and its
  present value to the initial liability; the ROU asset is then
  depreciated over the underlying asset's useful life rather than the
  lease term (IFRS 16.32).

Options are frozen once the lease leaves draft: the schedule and opening
entry were measured off them, so later changes must go through the
modification wizard, never an in-place edit.
"""

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class EhLeaseOption(models.Model):
    _name = 'eh.lease.option'
    _description = "Lease term option (IFRS 16)"
    _order = 'lease_id, id'

    lease_id = fields.Many2one(
        'eh.lease.contract', required=True, ondelete='cascade', index=True,
        check_company=True,
    )
    company_id = fields.Many2one(
        related='lease_id.company_id', store=True, readonly=True,
    )
    currency_id = fields.Many2one(
        related='lease_id.currency_id', store=True, readonly=True,
    )
    option_type = fields.Selection(
        [
            ('extension', "Extension option"),
            ('termination', "Termination option"),
            ('purchase', "Purchase option"),
        ],
        required=True, default='extension',
    )
    extension_months = fields.Integer(
        help=(
            "Months added to the lease term when this extension option "
            "is reasonably certain to be exercised (IFRS 16.18)."
        ),
    )
    termination_penalty = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Penalty payable on termination. Included in the lease "
            "liability (as part of the final period's payment) when "
            "exercise is reasonably certain (IFRS 16.27(e))."
        ),
    )
    purchase_price = fields.Monetary(
        currency_field='currency_id',
        help=(
            "Exercise price of the purchase option. Included in the "
            "lease liability (as part of the final period's payment) "
            "when exercise is reasonably certain (IFRS 16.27(d)); the "
            "ROU asset is then depreciated over the underlying asset's "
            "useful life (IFRS 16.32)."
        ),
    )
    reasonably_certain = fields.Boolean(
        default=False,
        help=(
            "Management's assessment that exercise (or, for a "
            "termination option, that exercise of the penalty path) is "
            "reasonably certain. Only reasonably-certain options enter "
            "the lease term and liability measurement; the others are "
            "disclosure-only."
        ),
    )
    note = fields.Char(
        help="Assessment basis: incentives, leasehold improvements, "
             "business plans supporting the reasonably-certain call.",
    )

    @api.constrains('option_type', 'extension_months',
                    'termination_penalty', 'purchase_price')
    def _check_option_values(self):
        for opt in self:
            if opt.option_type == 'extension' and opt.extension_months <= 0:
                raise ValidationError(_(
                    "An extension option needs a positive number of "
                    "extension months.",
                ))
            if (opt.option_type == 'termination'
                    and opt.termination_penalty <= 0):
                raise ValidationError(_(
                    "A termination option needs a positive penalty "
                    "amount (a penalty-free exit needs no option row).",
                ))
            if opt.option_type == 'purchase' and opt.purchase_price <= 0:
                raise ValidationError(_(
                    "A purchase option needs a positive exercise price.",
                ))

    # Options measure the schedule; once the lease has left draft they are
    # frozen so the recorded term/liability basis cannot drift from the
    # entries already posted. Re-assessments go through the modification
    # wizard (which remeasures and posts the IFRS 16.44-46 adjustment).
    def _check_lease_in_draft(self):
        for opt in self:
            if opt.lease_id.state != 'draft':
                raise UserError(_(
                    "Lease options are frozen once the lease is active; "
                    "the schedule and opening entry were measured off "
                    "them. Use the modification wizard to remeasure the "
                    "lease instead.",
                ))

    @api.model_create_multi
    def create(self, vals_list):
        leases = self.env['eh.lease.contract'].browse({
            vals.get('lease_id') for vals in vals_list if vals.get('lease_id')
        })
        if not self.env.su:
            leases._eh_check_access('write')
        leases._eh_lock_for_transition()
        if leases.filtered(lambda lease: lease.state != 'draft'):
            raise UserError(_(
                "Lease options can only be added to draft leases."
            ))
        records = super().create(vals_list)
        records._check_lease_in_draft()
        # Adding a reasonably-certain extension / purchase option can
        # break the lease's exemption eligibility (IFRS 16.5/18); the
        # lease-side constraint does not fire on child creates, so
        # re-validate explicitly.
        records.lease_id._check_exemption()
        leases.mapped('schedule_line_ids').sudo().unlink()
        return records

    def write(self, vals):
        leases = self.mapped('lease_id')
        if vals.get('lease_id'):
            leases |= self.env['eh.lease.contract'].browse(vals['lease_id'])
        if not self.env.su:
            self._eh_check_access('write')
            leases._eh_check_access('write')
        leases._eh_lock_for_transition()
        if leases.filtered(lambda lease: lease.state != 'draft'):
            raise UserError(_(
                "Lease options can only be changed on draft leases."
            ))
        res = super().write(vals)
        self._check_lease_in_draft()
        self.lease_id._check_exemption()
        leases.mapped('schedule_line_ids').sudo().unlink()
        return res

    def unlink(self):
        self._check_lease_in_draft()
        leases = self.lease_id
        if not self.env.su:
            self._eh_check_access('unlink')
            leases._eh_check_access('write')
        leases._eh_lock_for_transition()
        self._check_lease_in_draft()
        res = super().unlink()
        leases._check_exemption()
        leases.mapped('schedule_line_ids').sudo().unlink()
        return res
