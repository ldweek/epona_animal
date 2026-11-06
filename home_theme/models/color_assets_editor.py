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
Home Screen Theme - Color Assets Editor
Handles SCSS color variable management and asset replacement.
"""

import re
import base64

from odoo import models, api
from odoo.tools import misc

from odoo.addons.base.models.assetsbundle import EXTENSIONS


class ColorAssetsEditor(models.AbstractModel):
    """Utility model for managing SCSS color variables in theme assets."""

    _name = 'home_theme.color_assets_editor'
    _description = 'Color Assets Editor'

    # ----------------------------------------------------------
    # Helper
    # ----------------------------------------------------------

    @api.model
    def _get_custom_colors_url(self, url, bundle):
        return f'/_custom/{bundle}{url}'

    @api.model
    def _get_color_info_from_url(self, url):
        regex = re.compile(
            r'^(/_custom/([^/]+))?/(\w+)/([/\w]+\.\w+)$'
        )
        match = regex.match(url)
        if not match:
            return False
        return {
            'module': match.group(3),
            'resource_path': match.group(4),
            'customized': bool(match.group(1)),
            'bundle': match.group(2) or False
        }

    @api.model
    def _get_colors_attachment(self, custom_url):
        return self.env['ir.attachment'].search([
            ('url', '=', custom_url)
        ])

    @api.model
    def _get_colors_asset(self, custom_url):
        return self.env['ir.asset'].search([
            ('path', 'like', custom_url)
        ])

    @api.model
    def _get_colors_from_url(self, url, bundle):
        custom_url = self._get_custom_colors_url(url, bundle)
        url_info = self._get_color_info_from_url(custom_url)
        if url_info['customized']:
            attachment = self._get_colors_attachment(
                custom_url
            )
            if attachment:
                return base64.b64decode(attachment.datas)
        with misc.file_open(url.strip('/'), 'rb', filter_ext=EXTENSIONS) as f:
            return f.read()

    def _get_color_variable(self, content, variable):
        value = re.search(fr'\$theme_{variable}\:?\s(.*?);', content)
        return value and value.group(1)

    def _get_color_variables(self, content, variables):
        return {
            var: self._get_color_variable(content, var)
            for var in variables
        }

    def _replace_color_variables(self, content, variables):
        for variable in variables:
            content = re.sub(
                fr'{variable["name"]}\:?\s(.*?);',
                f'{variable["name"]}: {variable["value"]};',
                content
            )
        return content

    @api.model
    def _save_color_asset(self, url, bundle, content):
        custom_url = self._get_custom_colors_url(url, bundle)
        asset_url = url[1:] if url.startswith(('/', '\\')) else url
        datas = base64.b64encode((content or '\n').encode('utf-8'))
        custom_attachment = self._get_colors_attachment(
            custom_url
        )
        if custom_attachment:
            # public=True also HEALS attachments created before it was set:
            # the login page compiles bundles as the anonymous user, and a
            # private custom SCSS breaks that compilation with
            # "Could not get content for /_custom/...".
            custom_attachment.write({'datas': datas, 'public': True})
            self.env.registry.clear_cache('assets')
        else:
            attachment_values = {
                'name': url.split('/')[-1],
                'type': 'binary',
                'mimetype': 'text/scss',
                'datas': datas,
                'url': custom_url,
                # Anonymous pages (login) must be able to read it when the
                # bundle compiles — see the note on the write branch above.
                'public': True,
            }
            asset_values = {
                'path': custom_url,
                'target': url,
                'directive': 'replace',
            }
            target_asset = self._get_colors_asset(
                asset_url
            )
            if target_asset:
                asset_values['name'] = '%s override' % target_asset.name
                asset_values['bundle'] = target_asset.bundle
                asset_values['sequence'] = target_asset.sequence
            else:
                asset_values['name'] = '%s: replace %s' % (
                    bundle, custom_url.split('/')[-1]
                )
                asset_values['bundle'] = self.env['ir.asset']._get_related_bundle(
                    url, bundle
                )
            self.env['ir.attachment'].create(attachment_values)
            # Only create the override ir.asset if one does not already exist for
            # this custom url. Otherwise a stale state (asset kept while its
            # attachment was removed) would spawn a duplicate "replace" asset,
            # and two replaces of the same file in one bundle break compilation
            # with "Could not get content for ...".
            if not self._get_colors_asset(custom_url):
                self.env['ir.asset'].create(asset_values)

    # ----------------------------------------------------------
    # Functions
    # ----------------------------------------------------------

    def get_color_variables_values(self, url, bundle, variables):
        content = self._get_colors_from_url(url, bundle)
        return self._get_color_variables(
            content.decode('utf-8'), variables
        )

    def replace_color_variables_values(self, url, bundle, variables):
        original = self._get_colors_from_url(url, bundle).decode('utf-8')
        content = self._replace_color_variables(original, variables)
        self._save_color_asset(url, bundle, content)

    def reset_color_asset(self, url, bundle):
        custom_url = self._get_custom_colors_url(url, bundle)
        self._get_colors_attachment(custom_url).unlink()
        self._get_colors_asset(custom_url).unlink()
