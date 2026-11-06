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
Home Screen Theme - Menu Extensions
Provides methods to get app data for the home screen.
"""

import logging
from odoo import models, api

_logger = logging.getLogger(__name__)


class IrUiMenu(models.Model):
    """Extension of ir.ui.menu to provide home screen app data."""

    _inherit = 'ir.ui.menu'

    @api.model
    def get_home_screen_apps(self, debug=False):
        """Return root menu items (apps) visible to the current user.

        Uses the current user's load_menus (respects groups and ACLs)
        instead of sudo, so each user only sees apps they can access.
        Mirrors load_web_menus logic: for folder-type app roots (no direct
        action), resolve the action from the first visible child recursively.
        """
        try:
            menus = self.load_menus(debug=debug)
            root_menu = menus.get('root', {})
            app_ids = root_menu.get('children', [])
        except Exception:
            _logger.exception("Error loading menus for home screen")
            return []

        apps_data = []
        for app_id in app_ids:
            try:
                menu = menus.get(app_id)
                if not menu:
                    continue

                action_id = menu.get('action_id', False)
                action_model = menu.get('action_model', False)
                if not action_id:
                    child = menu
                    while child and not action_id:
                        action_id = child.get('action_id', False)
                        action_model = child.get('action_model', False)
                        children = child.get('children', [])
                        child = menus.get(children[0]) if children else None

                if not action_id:
                    continue

                icon_data = self._parse_menu_icon(menu)

                app_data = {
                    'id': menu['id'],
                    'name': menu['name'],
                    'xmlid': menu.get('xmlid', ''),
                    'action_id': action_id,
                    'action_model': action_model,
                    'icon_class': icon_data.get('icon_class', 'fa fa-th'),
                    'icon_color': icon_data.get('icon_color', '#FFFFFF'),
                    'background_color': icon_data.get('background_color', '#875A7B'),
                }

                if icon_data.get('web_icon_data'):
                    app_data['web_icon_data'] = icon_data['web_icon_data']
                    app_data['web_icon_data_mimetype'] = icon_data.get('web_icon_data_mimetype', 'image/png')

                if icon_data.get('module_icon_url'):
                    app_data['module_icon_url'] = icon_data['module_icon_url']

                apps_data.append(app_data)
            except Exception:
                _logger.exception("Error processing menu %s for home screen", app_id)
                continue

        self._add_app_categories(apps_data)
        return apps_data

    def _add_app_categories(self, apps):
        """Tag each app with its module category, for optional grouping on the
        home screen. The category is resolved from the app's module (the xmlid
        prefix of its root menu)."""
        modules = set()
        for app in apps:
            xmlid = app.get('xmlid') or ''
            module = xmlid.split('.')[0] if '.' in xmlid else ''
            app['_module'] = module
            if module:
                modules.add(module)

        cat_by_module = {}
        if modules:
            records = self.env['ir.module.module'].sudo().search([
                ('name', 'in', list(modules)),
            ])
            for mod in records:
                name = mod.category_id.name if mod.category_id else False
                cat_by_module[mod.name] = name or 'Other'

        for app in apps:
            app['category'] = cat_by_module.get(app.pop('_module', ''), 'Other')

    def _parse_menu_icon(self, menu):
        """
        Extract icon information from menu dictionary (from load_menus)
        Returns dict with icon_class, icon_color, background_color, web_icon_data, and module_icon_url
        """
        result = {
            'icon_class': 'fa fa-th',
            'icon_color': '#FFFFFF',
            'background_color': '#875A7B',
        }

        # Check if binary icon data exists (already base64 encoded from load_menus)
        if menu.get('web_icon_data'):
            result['web_icon_data'] = menu['web_icon_data']
            result['web_icon_data_mimetype'] = menu.get('web_icon_data_mimetype', 'image/png')
            return result

        # Parse web_icon field
        web_icon = menu.get('web_icon', '')
        if web_icon:
            # Parse web_icon format: "module,path" or "fa fa-icon,#bgcolor" or "fa fa-icon,#bgcolor,#color"
            parts = [p.strip() for p in web_icon.split(',')]

            if len(parts) >= 1:
                first_part = parts[0].strip()

                if first_part.startswith('fa '):
                    # FontAwesome icon: "fa fa-icon" or "fa fa-icon,#bgcolor" or "fa fa-icon,#bgcolor,#color"
                    result['icon_class'] = first_part

                    # Parse colors if provided
                    if len(parts) >= 2 and parts[1].strip().startswith('#'):
                        result['background_color'] = parts[1].strip()
                    if len(parts) >= 3 and parts[2].strip().startswith('#'):
                        result['icon_color'] = parts[2].strip()
                else:
                    # Module icon path: "module_name,path/to/icon.png"
                    # Construct the URL to the module's static file
                    module_name = first_part
                    icon_path = parts[1].strip() if len(parts) >= 2 else 'static/description/icon.png'
                    # Format: /module_name/path/to/icon.png (Odoo standard static file serving)
                    result['module_icon_url'] = f"/{module_name}/{icon_path}"

        return result

    @api.model
    def get_searchable_menus(self, debug=False):
        """Return all menu items the current user can access, with breadcrumb paths.
        Used by the home screen search feature.
        Odoo 19 load_menus format: action_model + action_id (not combined 'action' string).
        When debug mode is active, includes technical menus visible only in debug.
        """
        try:
            menus = self.load_menus(debug=debug)
        except Exception:
            _logger.exception("Error loading menus for search")
            return []

        root_menu = menus.get('root', {})
        root_children = root_menu.get('children', [])

        # Pre-parse root app icons so all submenus show their app icon
        app_icons = {}
        for app_id in root_children:
            app_menu = menus.get(app_id)
            if app_menu:
                app_icons[app_id] = self._parse_menu_icon(app_menu)

        results = []

        def _walk(menu_id, path_parts, root_app_id):
            menu = menus.get(menu_id)
            if not menu:
                return
            current_path = path_parts + [menu['name']]
            # Include menus that have an action (navigable)
            if menu.get('action_id'):
                icon_data = app_icons.get(root_app_id, {})
                results.append({
                    'id': menu['id'],
                    'name': menu['name'],
                    'full_path': ' / '.join(current_path),
                    'action_id': menu['action_id'],
                    'action_model': menu.get('action_model', 'ir.actions.act_window'),
                    'root_app_id': root_app_id,
                    # The heavy base64 app icon is deliberately NOT sent per
                    # row (it would repeat for every menu of the app, adding
                    # megabytes); clients rehydrate it from ``apps`` through
                    # ``root_app_id``.
                    'module_icon_url': icon_data.get('module_icon_url', False),
                    'icon_class': icon_data.get('icon_class', 'fa fa-th'),
                    'background_color': icon_data.get('background_color', '#875A7B'),
                })
            for child_id in menu.get('children', []):
                _walk(child_id, current_path, root_app_id)

        for app_id in root_children:
            app_menu = menus.get(app_id)
            if not app_menu:
                continue
            _walk(app_id, [], app_id)

        return results
