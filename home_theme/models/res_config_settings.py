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
Home Screen Theme - Configuration Settings
Manages theme colors and background image settings for light and dark modes.
"""

import base64

from odoo import api, fields, models
from odoo.tools.mimetypes import guess_mimetype
from odoo.tools import file_path


class ResConfigSettings(models.TransientModel):
    """Configuration settings for Home Screen Theme."""

    _inherit = 'res.config.settings'

    # ----------------------------------------------------------
    # Properties
    # ----------------------------------------------------------

    @property
    def COLOR_FIELDS(self):
        return [
            'color_brand',
            'color_primary',
            'color_success',
            'color_info',
            'color_warning',
            'color_danger',
            'color_navbar_bg',
            'color_navbar_text',
            'color_home_app_text',
        ]

    @property
    def COLOR_ASSET_LIGHT_URL(self):
        return '/home_theme/static/src/scss/colors_light.scss'

    @property
    def COLOR_BUNDLE_LIGHT_NAME(self):
        return 'web._assets_primary_variables'

    @property
    def COLOR_ASSET_DARK_URL(self):
        return '/home_theme/static/src/scss/colors_dark.scss'

    @property
    def COLOR_BUNDLE_DARK_NAME(self):
        return 'web.assets_web_dark'

    # ----------------------------------------------------------
    # Fields Light Mode
    # ----------------------------------------------------------

    color_brand_light = fields.Char(string='Brand Light Color')
    color_primary_light = fields.Char(string='Primary Light Color')
    color_success_light = fields.Char(string='Success Light Color')
    color_info_light = fields.Char(string='Info Light Color')
    color_warning_light = fields.Char(string='Warning Light Color')
    color_danger_light = fields.Char(string='Danger Light Color')
    color_navbar_bg_light = fields.Char(string='Navbar Background Light Color')
    color_navbar_text_light = fields.Char(string='Navbar Text Light Color')
    color_home_app_text_light = fields.Char(string='Home App Text Light Color')

    # ----------------------------------------------------------
    # Fields Dark Mode
    # ----------------------------------------------------------

    color_brand_dark = fields.Char(string='Brand Dark Color')
    color_primary_dark = fields.Char(string='Primary Dark Color')
    color_success_dark = fields.Char(string='Success Dark Color')
    color_info_dark = fields.Char(string='Info Dark Color')
    color_warning_dark = fields.Char(string='Warning Dark Color')
    color_danger_dark = fields.Char(string='Danger Dark Color')
    color_navbar_bg_dark = fields.Char(string='Navbar Background Dark Color')
    color_navbar_text_dark = fields.Char(string='Navbar Text Dark Color')
    color_home_app_text_dark = fields.Char(string='Home App Text Dark Color')

    # ----------------------------------------------------------
    # Home Screen Background Fields
    # ----------------------------------------------------------

    home_background_image = fields.Binary(
        string='Home Background Image',
        attachment=True
    )

    # ----------------------------------------------------------
    # Light Mode Helpers
    # ----------------------------------------------------------

    def _get_light_color_values(self):
        return self.env['home_theme.color_assets_editor'].get_color_variables_values(
            self.COLOR_ASSET_LIGHT_URL,
            self.COLOR_BUNDLE_LIGHT_NAME,
            self.COLOR_FIELDS
        )

    def _set_light_color_values(self, values):
        colors = self._get_light_color_values()
        for var, value in colors.items():
            values[f'{var}_light'] = value
        return values

    def _detect_light_color_change(self):
        colors = self._get_light_color_values()
        return any(
            self[f'{var}_light'] != val
            for var, val in colors.items()
        )

    def _replace_light_color_values(self):
        variables = [
            {'name': field, 'value': self[f'{field}_light']}
            for field in self.COLOR_FIELDS
        ]
        return self.env['home_theme.color_assets_editor'].replace_color_variables_values(
            self.COLOR_ASSET_LIGHT_URL,
            self.COLOR_BUNDLE_LIGHT_NAME,
            variables
        )

    def _reset_light_color_assets(self):
        self.env['home_theme.color_assets_editor'].reset_color_asset(
            self.COLOR_ASSET_LIGHT_URL,
            self.COLOR_BUNDLE_LIGHT_NAME,
        )

    # ----------------------------------------------------------
    # Actions
    # ----------------------------------------------------------

    def action_reset_light_color_assets(self):
        self._reset_light_color_assets()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    # ----------------------------------------------------------
    # Dark Mode Helpers
    # ----------------------------------------------------------

    def _get_dark_color_values(self):
        return self.env['home_theme.color_assets_editor'].get_color_variables_values(
            self.COLOR_ASSET_DARK_URL,
            self.COLOR_BUNDLE_DARK_NAME,
            self.COLOR_FIELDS
        )

    def _set_dark_color_values(self, values):
        colors = self._get_dark_color_values()
        for var, value in colors.items():
            values[f'{var}_dark'] = value
        return values

    def _detect_dark_color_change(self):
        colors = self._get_dark_color_values()
        return any(
            self[f'{var}_dark'] != val
            for var, val in colors.items()
        )

    def _replace_dark_color_values(self):
        variables = [
            {'name': field, 'value': self[f'{field}_dark']}
            for field in self.COLOR_FIELDS
        ]
        return self.env['home_theme.color_assets_editor'].replace_color_variables_values(
            self.COLOR_ASSET_DARK_URL,
            self.COLOR_BUNDLE_DARK_NAME,
            variables
        )

    def _reset_dark_color_assets(self):
        self.env['home_theme.color_assets_editor'].reset_color_asset(
            self.COLOR_ASSET_DARK_URL,
            self.COLOR_BUNDLE_DARK_NAME,
        )

    def action_reset_dark_color_assets(self):
        self._reset_dark_color_assets()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    # ----------------------------------------------------------
    # CRUD
    # ----------------------------------------------------------

    def get_values(self):
        res = super().get_values()
        res = self._set_light_color_values(res)
        res = self._set_dark_color_values(res)

        IrConfigParameter = self.env['ir.config_parameter'].sudo()
        attachment_id = IrConfigParameter.get_param(
            'home_theme.background_image_attachment_id', False
        )
        if attachment_id:
            attachment = self.env['ir.attachment'].sudo().browse(int(attachment_id))
            if attachment.exists():
                res['home_background_image'] = attachment.datas

        return res

    def set_values(self):
        res = super().set_values()

        if self._detect_light_color_change():
            self._replace_light_color_values()
        if self._detect_dark_color_change():
            self._replace_dark_color_values()

        IrConfigParameter = self.env['ir.config_parameter'].sudo()

        if self.home_background_image:
            attachment_id = IrConfigParameter.get_param(
                'home_theme.background_image_attachment_id', False
            )
            attachment_vals = {
                'name': 'home_theme_background_image',
                'datas': self.home_background_image,
                'mimetype': guess_mimetype(
                    base64.b64decode(self.home_background_image), 'image/png'
                ),
                'res_model': 'res.config.settings',
                'res_id': 0,
                'type': 'binary',
                'public': True,
            }
            if attachment_id:
                attachment = self.env['ir.attachment'].sudo().browse(int(attachment_id))
                if attachment.exists():
                    attachment.write(attachment_vals)
                else:
                    attachment = self.env['ir.attachment'].sudo().create(attachment_vals)
                    IrConfigParameter.set_param(
                        'home_theme.background_image_attachment_id', attachment.id
                    )
            else:
                attachment = self.env['ir.attachment'].sudo().create(attachment_vals)
                IrConfigParameter.set_param(
                    'home_theme.background_image_attachment_id', attachment.id
                )
        else:
            attachment_id = IrConfigParameter.get_param(
                'home_theme.background_image_attachment_id', False
            )
            if attachment_id:
                attachment = self.env['ir.attachment'].sudo().browse(int(attachment_id))
                if attachment.exists():
                    attachment.unlink()
                IrConfigParameter.set_param(
                    'home_theme.background_image_attachment_id', False
                )

        return res

    # ----------------------------------------------------------
    # Bundled wallpaper gallery (one-click home backgrounds).
    # The keys match the Pro quick-palette presets so Pro can pair
    # each palette with its wallpaper.
    # ----------------------------------------------------------

    HOME_WALLPAPERS = (
        # Palette-matched (same keys as the Pro quick presets)
        'hacienda', 'ocean', 'forest', 'sunset', 'plum', 'graphite',
        # Standalone signature backgrounds
        'midnight', 'aurora', 'slate',
        # Light, business-friendly backgrounds
        'daylight', 'porcelain', 'linen', 'horizon',
    )

    def action_apply_wallpaper(self):
        """Set the home-screen background to one of the bundled wallpapers
        (picked from context ``wallpaper``), then reload the page."""
        key = self.env.context.get('wallpaper')
        if key not in self.HOME_WALLPAPERS:
            return False
        path = file_path('home_theme/static/wallpapers/bg_%s.png' % key)
        with open(path, 'rb') as wallpaper_file:
            data = base64.b64encode(wallpaper_file.read())

        IrConfigParameter = self.env['ir.config_parameter'].sudo()
        attachment_vals = {
            'name': 'home_theme_background_image',
            'datas': data,
            'mimetype': 'image/png',
            'res_model': 'res.config.settings',
            'res_id': 0,
            'type': 'binary',
            'public': True,
        }
        attachment_id = IrConfigParameter.get_param(
            'home_theme.background_image_attachment_id', False
        )
        attachment = self.env['ir.attachment'].sudo().browse(int(attachment_id)) \
            if attachment_id else self.env['ir.attachment'].sudo()
        if attachment.exists():
            attachment.write(attachment_vals)
        else:
            attachment = self.env['ir.attachment'].sudo().create(attachment_vals)
            IrConfigParameter.set_param(
                'home_theme.background_image_attachment_id', attachment.id
            )
        return {'type': 'ir.actions.client', 'tag': 'reload'}
