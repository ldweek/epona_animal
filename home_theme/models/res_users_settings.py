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
Home Screen Theme - User Settings
Per-user colour scheme (Light / Dark / System) for the backend dark mode.
"""

from odoo import fields, models


class ResUsersSettings(models.Model):
    _inherit = 'res.users.settings'

    color_scheme = fields.Selection(
        selection=[
            ('light', 'Light'),
            ('dark', 'Dark'),
            ('auto', 'System'),
            # Dark within a per-user time window (e.g. 19:00–07:00). The
            # window fields + UI live in the Pro theme; the free service
            # still resolves the value so the schemes stay in one place.
            ('schedule', 'Scheduled'),
        ],
        string='Color Scheme',
        default='light',
    )
