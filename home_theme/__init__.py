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


from . import controllers
from . import models


def _uninstall_cleanup(env):
    """Clean up color assets on module uninstall"""
    try:
        env['res.config.settings']._reset_light_color_assets()
    except Exception:
        pass
    try:
        env['res.config.settings']._reset_dark_color_assets()
    except Exception:
        pass
