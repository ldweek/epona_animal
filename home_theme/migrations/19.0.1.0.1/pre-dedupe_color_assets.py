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

"""Remove duplicate colour-override assets.

The in-app colour editor could, in an inconsistent state (an ir.asset kept
while its attachment was removed), create a SECOND ``replace`` ir.asset for the
same custom file in the same bundle. Two ``replace`` directives for one file
break asset generation: the first one consumes/removes the target from the
bundle, so the second fails with
``ValueError: File(s) .../colors_light.scss not found in bundle ...`` -> HTTP 500
on every backend page.

This pre-migration drops the duplicate(s), keeping the original (lowest id), so
exactly one ``replace`` remains and the customised colours are preserved. It is
a pre-migration so the cleanup happens before assets are regenerated during the
module update. The editor itself has been hardened to no longer create the
duplicate.
"""


def migrate(cr, version):
    cr.execute(
        """
        DELETE FROM ir_asset a
        USING ir_asset b
        WHERE a.directive = 'replace'
          AND a.path LIKE '/_custom/%'
          AND a.path = b.path
          AND a.target = b.target
          AND a.bundle = b.bundle
          AND a.id > b.id
        """
    )
