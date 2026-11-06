# -*- encoding: utf-8 -*-

from odoo import _, api, models
from odoo.exceptions import UserError


class EhEpsChildMixin(models.AbstractModel):
    _name = 'eh.eps.child.mixin'
    _description = "Shared EPS child mutation guard"

    @api.model
    def _eh_prepare_runs(self, run_ids):
        """Lock each parent once and validate caller scope.

        Every EPS evidence child exposes ``run_id`` (directly or as a
        stored related field), so one implementation closes the same
        create/write/unlink race for movements, potentials, observations,
        and restatement events.
        """
        runs = self.env['eh.eps.run'].browse(
            sorted({run_id for run_id in run_ids if run_id}),
        )
        runs._eh_check_caller_scope('read')
        runs._eh_lock()
        return runs

    def _eh_check_run_frozen(self, evidence_label):
        frozen = self.filtered(lambda record: record.run_id.state != 'draft')
        if frozen:
            run = frozen[0].run_id
            raise UserError(_(
                "%(evidence)s are frozen while EPS run %(run)s is "
                "%(state)s. Set the run to draft before changing evidence "
                "(IAS 33).",
                evidence=evidence_label,
                run=run.display_name,
                state=run.state,
            ))
