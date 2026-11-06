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
Home Screen Theme - App Sequence Model
Stores user-specific app ordering for the home screen.
"""

from odoo import models, fields


class HomeAppSequence(models.Model):
    """Stores the custom order of apps per user in the home screen."""

    _name = 'home.app.sequence'
    _description = 'Home Screen App Sequence'
    _order = 'sequence, id'

    user_id = fields.Many2one(
        'res.users',
        string='User',
        required=True,
        ondelete='cascade',
        index=True
    )
    menu_id = fields.Many2one(
        'ir.ui.menu',
        string='Menu/App',
        required=True,
        ondelete='cascade'
    )
    sequence = fields.Integer(string='Sequence', default=10)

    _user_menu_unique = models.Constraint(
        'unique(user_id, menu_id)',
        'Each app can only have one sequence per user!',
    )
