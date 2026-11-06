# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Customer portal statement controller and home-card values.

Two authenticated surfaces:

* /my/eh/statement (GET): renders the statement PDF for the logged-in
  partner over a bounded period range and serves bytes inline without
  persisting sensitive statement copies in the filestore.
* CustomerPortal._prepare_home_portal_values is overridden to surface
  a strictly positive overdue-invoice summary and the statement form on
  the normal portal home render.

Security: the route resolves the partner exclusively from
``request.env.user.partner_id.commercial_partner_id`` and ignores arbitrary
query-string keys. There is no caller-selected partner or company path.
"""

import logging
import re
import unicodedata
from urllib.parse import quote

from odoo import fields, http
from odoo.http import request

from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.addons.eh_account_base.tools.orm_compat import read_group_compat
from odoo.addons.eh_account_portal_extra.models.res_config_settings import (
    DEFAULT_MAX_STATEMENT_DAYS,
    MAX_CONFIGURED_STATEMENT_DAYS,
    STATEMENT_DAYS_CONFIG_KEY,
)

_logger = logging.getLogger(__name__)

_STATEMENT_LOCK_NAMESPACE = 1162036327


class EhAccountPortal(CustomerPortal):

    @staticmethod
    def _eh_statement_filename_fallback(filename):
        """Return a quoted-header-safe ASCII fallback for RFC 6266 clients."""
        ascii_name = unicodedata.normalize('NFKD', filename).encode(
            'ascii', 'ignore',
        ).decode('ascii')
        ascii_name = re.sub(r'[^A-Za-z0-9._-]+', '_', ascii_name)
        ascii_name = re.sub(r'_+', '_', ascii_name)
        ascii_name = ascii_name.strip('._')
        return ascii_name or 'customer_statement.pdf'

    @staticmethod
    def _eh_private_response(response, status=None, headers=None):
        """Mark every statement response as private and non-cacheable."""
        if status is not None:
            response.status_code = status
        response.headers['Cache-Control'] = 'private, no-store, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        for name, value in headers or ():
            response.headers[name] = value
        return response

    def _eh_render_statement_page(
        self, template, values=None, status=200, headers=None,
    ):
        return self._eh_private_response(
            request.render(template, values or {}),
            status=status,
            headers=headers,
        )

    @staticmethod
    def _eh_max_statement_days():
        raw_max_days = request.env['ir.config_parameter'].sudo().get_param(
            STATEMENT_DAYS_CONFIG_KEY,
            str(DEFAULT_MAX_STATEMENT_DAYS),
        )
        try:
            max_days = int(raw_max_days)
        except (TypeError, ValueError):
            max_days = DEFAULT_MAX_STATEMENT_DAYS
        return min(max(max_days, 1), MAX_CONFIGURED_STATEMENT_DAYS)

    @staticmethod
    def _eh_try_statement_lock(partner, company):
        """Avoid concurrent renders for one customer/company corridor."""
        request.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(%s, hashtext(%s))",
            (
                _STATEMENT_LOCK_NAMESPACE,
                f"{partner.id}:{company.id}",
            ),
        )
        return bool(request.env.cr.fetchone()[0])

    @http.route()
    def home(self, **kw):
        """Populate the card on every supported portal-home implementation.

        Odoo 18+ calls ``_prepare_home_portal_values([])`` while rendering
        ``/my``. Odoo 16 and 17 only call that hook from the counter RPC, so
        an inherited template otherwise receives none of the card/form
        values. Keep the standard controller response and fill its lazy QWeb
        context only when the active series did not already do so.
        """
        response = super().home(**kw)
        qcontext = getattr(response, 'qcontext', None)
        if qcontext is not None and 'eh_partner_id' not in qcontext:
            qcontext.update(self._prepare_home_portal_values([]))
        return response

    def _prepare_home_portal_values(self, counters):
        """Add open invoice count and balance to the portal home.

        The standard Odoo portal home shows counters per related model
        (invoices, sales, etc). We layer in:

        * eh_open_invoice_count: partner's posted not-paid / partial
          invoices.
        * eh_total_overdue: positive residual on overdue customer invoices.
        * eh_oldest_due_date: earliest overdue invoice due date.

        Returns the values dict consumed by the portal's QWeb home
        template via the inherited template patch.
        """
        values = super()._prepare_home_portal_values(counters)
        partner = request.env.user.partner_id.commercial_partner_id
        company = request.env.company
        Move = request.env['account.move'].sudo()
        AML = request.env['account.move.line'].sudo()

        domain = [
            ('commercial_partner_id', '=', partner.id),
            ('company_id', '=', company.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'in', ('not_paid', 'partial')),
        ]
        open_invoice_count = Move.search_count(domain)

        # ``/my/counters`` calls this hook with a non-empty list and its JS
        # assumes every returned key names a requested ``data-placeholder``.
        # Returning the balance/card keys there makes the selector null and
        # crashes the standard portal counter refresh. The home method above
        # guarantees an empty-list call on all supported Odoo series.
        if counters:
            if 'eh_open_invoice_count' in counters:
                values['eh_open_invoice_count'] = open_invoice_count
            return values

        values['eh_open_invoice_count'] = open_invoice_count

        today = fields.Date.context_today(request.env.user)
        # One SQL pass for the overdue total + oldest due date. Routed through
        # read_group_compat: the raw _read_group signature here is 17+, so a
        # direct call crashes /my for every portal customer on Odoo 16 (whose
        # _read_group is a different, private signature). The compat helper
        # normalises both series to the 17+ tuple shape.
        overdue_rows = read_group_compat(
            AML,
            [
                ('move_id.commercial_partner_id', '=', partner.id),
                ('company_id', '=', company.id),
                ('account_id.account_type', '=', 'asset_receivable'),
                ('move_id.move_type', '=', 'out_invoice'),
                ('parent_state', '=', 'posted'),
                ('amount_residual', '>', 0),
                ('date_maturity', '<', today),
                ('date_maturity', '!=', False),
            ],
            [],
            ['amount_residual:sum', 'date_maturity:min'],
        )
        if overdue_rows and overdue_rows[0][0]:
            values['eh_total_overdue'] = overdue_rows[0][0]
            values['eh_oldest_due_date'] = overdue_rows[0][1]
            values['eh_days_overdue_max'] = (
                today - values['eh_oldest_due_date']
            ).days if values['eh_oldest_due_date'] else 0
        else:
            values['eh_total_overdue'] = 0.0
            values['eh_oldest_due_date'] = False
            values['eh_days_overdue_max'] = 0
        values['eh_partner_id'] = partner.id
        # amount_residual is company-currency money. Never label it with a
        # partner preference currency, and never aggregate sister-company
        # receivables into this active-company portal card.
        values['eh_partner_currency'] = company.currency_id
        values['eh_statement_date_from'] = today.replace(day=1)
        values['eh_statement_date_to'] = today
        return values

    @http.route(
        ['/my/eh/statement'],
        type='http', auth='user', website=True,
    )
    def portal_eh_statement(self, date_from=None, date_to=None,
                            statement_type='open_item', **kw):
        """Render the customer statement PDF and serve it inline.

        Query parameters:
        * date_from (YYYY-MM-DD) optional, defaults to first of current month.
        * date_to (YYYY-MM-DD) optional, defaults to today.
        * statement_type ('open_item' default | 'activity').

        The route never accepts a caller-selected partner or company. Invalid
        input and runtime failures return private branded responses.
        """
        partner = request.env.user.partner_id.commercial_partner_id
        today = fields.Date.context_today(request.env.user)
        try:
            df = (
                fields.Date.from_string(date_from)
                if date_from else today.replace(day=1)
            )
            dt = (
                fields.Date.from_string(date_to)
                if date_to else today
            )
        except (ValueError, TypeError):
            return self._eh_render_statement_page(
                'eh_account_portal_extra.portal_invalid_dates',
            )
        if df > dt:
            return self._eh_render_statement_page(
                'eh_account_portal_extra.portal_invalid_dates',
            )
        max_days = self._eh_max_statement_days()
        if (dt - df).days + 1 > max_days:
            return self._eh_render_statement_page(
                'eh_account_portal_extra.portal_range_too_large',
                {'max_statement_days': max_days},
            )
        if statement_type not in ('open_item', 'activity'):
            statement_type = 'open_item'

        report = request.env.ref(
            'eh_account_dynamic_reports.report_customer_statement',
            raise_if_not_found=False,
        )
        if not report:
            return self._eh_render_statement_page(
                'eh_account_portal_extra.portal_report_missing',
            )
        company = request.env.company
        # One expensive render per partner/company at a time. A non-blocking
        # transaction advisory lock rejects concurrent refresh storms instead
        # of queuing duplicate PDFs and tying up HTTP workers.
        if not self._eh_try_statement_lock(partner, company):
            return self._eh_render_statement_page(
                'eh_account_portal_extra.portal_statement_busy',
                status=429,
                headers=[('Retry-After', '5')],
            )
        report_engine = report.sudo().with_company(company).with_context(
            allowed_company_ids=[company.id],
        )
        options = report_engine.get_default_options()
        # sudo is needed because portal users have no back-office report ACL.
        # Pin scope explicitly: relying on sudo's company set would expose
        # sister-company ledger rows for a shared commercial partner.
        options['company_ids'] = [company.id]
        options['primary_company_id'] = company.id
        options['partner_id'] = partner.id
        options['date'] = {
            'mode': 'range',
            'date_from': df.isoformat(),
            'date_to': dt.isoformat(),
        }
        options['statement_type'] = statement_type
        try:
            # Keep the immutable execution metadata and exact PDF hash, but
            # do not persist the statement source payload in the report
            # cache's attachment-backed Binary field.  Portal delivery is a
            # private, ephemeral rendering corridor.
            pdf_bytes = report_engine._eh_render_pdf_ephemeral(options)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "Customer portal statement render failed: partner_id=%s "
                "company_id=%s",
                partner.id, company.id,
            )
            return self._eh_render_statement_page(
                'eh_account_portal_extra.portal_render_failed',
                status=503,
            )

        download_name = "Statement_%s_%s_to_%s.pdf" % (
            (partner.name or str(partner.id)).replace(' ', '_'),
            df.isoformat(),
            dt.isoformat(),
        )
        # RFC 5987 encode the filename: partner.name may contain non-Latin-1,
        # quote, newline or
        # control characters that would produce an invalid or injectable
        # Content-Disposition header (werkzeug rejects non-Latin-1 header
        # values outright). filename* carries the full UTF-8 name safely.
        disposition = "inline; filename=\"%s\"; filename*=UTF-8''%s" % (
            self._eh_statement_filename_fallback(download_name),
            quote(download_name, safe=''),
        )
        return self._eh_private_response(
            request.make_response(pdf_bytes, headers=[
                ('Content-Type', 'application/pdf'),
                ('Content-Disposition', disposition),
                ('Content-Length', str(len(pdf_bytes))),
                ('X-Content-Type-Options', 'nosniff'),
            ]),
        )
