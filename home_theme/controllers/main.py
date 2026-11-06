# -*- coding: utf-8 -*-
###############################################################################
#
#    SynthraTech SAS
#    Copyright (C) 2026-TODAY SynthraTech SAS
#    Author: SynthraTech SAS (soporte.synthra@gmail.com)
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Lesser General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful, but
#    WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
#    Lesser General Public License for more details.
#
#    Full text: https://www.gnu.org/licenses/lgpl-3.0.html
#
###############################################################################

"""
Home Screen Theme - Controllers
Handles API endpoints for the custom home screen dashboard.
"""

import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

BACKGROUND_PARAM = 'home_theme.background_image_attachment_id'

# Apps that must always stay at the end of the home screen, so newly
# installed modules appear before them (in this order).
PINNED_LAST_XMLIDS = ('base.menu_management', 'base.menu_administration')


class HomeScreenController(http.Controller):
    """Controller for Home Screen Dashboard API endpoints."""

    @http.route('/web/home_screen/save_order', type='jsonrpc', auth='user')
    def save_app_order(self, app_ids):
        """Persist the custom order of apps for the current user.

        ``app_ids`` is the ordered list of ``ir.ui.menu`` ids as displayed
        on the home screen. Existing records are updated and missing ones
        created in a single batch to avoid one query per app.
        """
        try:
            menu_ids = [int(menu_id) for menu_id in (app_ids or [])]
        except (TypeError, ValueError):
            return {'success': False, 'error': 'invalid_app_ids'}

        if not menu_ids:
            return {'success': True}

        try:
            user = request.env.user
            HomeAppSequence = request.env['home.app.sequence'].sudo()

            existing = HomeAppSequence.search([
                ('user_id', '=', user.id),
                ('menu_id', 'in', menu_ids),
            ])
            existing_by_menu = {rec.menu_id.id: rec for rec in existing}

            to_create = []
            for index, menu_id in enumerate(menu_ids):
                record = existing_by_menu.get(menu_id)
                if record:
                    if record.sequence != index:
                        record.sequence = index
                else:
                    to_create.append({
                        'user_id': user.id,
                        'menu_id': menu_id,
                        'sequence': index,
                    })

            if to_create:
                HomeAppSequence.create(to_create)

            return {'success': True}
        except Exception as exc:  # noqa: BLE001 - surface a clean error to the client
            _logger.exception("Error saving home screen app order")
            return {'success': False, 'error': str(exc)}

    @http.route('/web/home_screen', type='jsonrpc', auth='user')
    def get_home_screen_data(self, light=False):
        """Return the data required to render the home screen dashboard."""
        user = request.env.user
        company = request.env.company

        try:
            IrUiMenu = request.env['ir.ui.menu']
            debug = bool(request.session.debug)

            apps = IrUiMenu.get_home_screen_apps(debug=debug)
            apps = self._apply_user_order(apps, user)
            apps = self._pin_system_apps_last(apps)
            background_url = background_mimetype = False
            if not light:
                # Icon-only callers (command palette) skip the wallpaper.
                background_url, background_mimetype = self._get_background_image()
            searchable_menus = IrUiMenu.get_searchable_menus(debug=debug)

            return {
                'apps': apps,
                'user_name': user.name,
                'company_name': company.name,
                'background_url': background_url,
                'background_mimetype': background_mimetype,
                'searchable_menus': searchable_menus,
            }
        except Exception:  # noqa: BLE001 - never break the client on render data
            _logger.exception("Error fetching home screen data")
            return {
                'apps': [],
                'user_name': user.name,
                'company_name': company.name,
                'background_url': False,
                'background_mimetype': False,
                'searchable_menus': [],
            }

    def _apply_user_order(self, apps, user):
        """Sort ``apps`` according to the user's saved sequence, if any."""
        sequences = request.env['home.app.sequence'].sudo().search([
            ('user_id', '=', user.id),
        ])
        if not sequences:
            return apps
        order = {seq.menu_id.id: seq.sequence for seq in sequences}
        return sorted(apps, key=lambda app: order.get(app['id'], 9999))

    def _pin_system_apps_last(self, apps):
        """Move the Apps and Settings menus to the end of the list.

        These utility apps should always sit after the regular apps so that
        newly installed modules appear before them, regardless of the user's
        saved order or the menu sequences.
        """
        pinned_ids = []
        for xmlid in PINNED_LAST_XMLIDS:
            menu = request.env.ref(xmlid, raise_if_not_found=False)
            if menu:
                pinned_ids.append(menu.id)
        if not pinned_ids:
            return apps

        rank = {menu_id: index for index, menu_id in enumerate(pinned_ids)}
        regular = [app for app in apps if app['id'] not in rank]
        pinned = sorted(
            (app for app in apps if app['id'] in rank),
            key=lambda app: rank[app['id']],
        )
        return regular + pinned

    def _get_background_image(self):
        """Return ``(url, mimetype)`` for the configured background.

        The wallpaper is served through ``/web/image`` (with the attachment
        checksum as cache buster) instead of being embedded as base64 in the
        JSON payload: the browser downloads it once and caches it across
        sessions, shrinking every /web/home_screen response by megabytes."""
        params = request.env['ir.config_parameter'].sudo()
        attachment_id = params.get_param(BACKGROUND_PARAM, False)
        if not attachment_id:
            return False, False

        attachment = request.env['ir.attachment'].sudo().browse(int(attachment_id))
        if not attachment.exists() or not attachment.datas:
            return False, False

        if not attachment.public:
            # One-time migration for wallpapers saved before this version:
            # the image is decorative, publishing lets the browser cache it.
            attachment.public = True
        url = '/web/image/%s?unique=%s' % (
            attachment.id, (attachment.checksum or '')[:16])
        return url, attachment.mimetype or 'image/png'
