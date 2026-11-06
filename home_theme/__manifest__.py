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

{
    'name': 'Home Screen Theme',
    'version': '19.0.1.3.0',
    'category': 'Themes/Backend',
    'summary': 'Free Odoo backend theme: modern home screen & app launcher with full dark mode + dark palette customization, custom colors, wallpaper, quick app search and drag-and-drop app ordering',
    'description': """
Home Screen Theme
=================

A modern, customizable home screen for Odoo 19 with theme color management and 
personalized app organization.

Features
--------

**Custom Home Screen Dashboard**
- Clean, responsive grid layout for all installed apps
- Drag and drop to reorder apps (saved per user)
- Custom background image support
- Wallpaper gallery: nine bundled backgrounds, one click to apply
- Smooth animations and transitions

**Theme Color Customization**
- Brand color (logo, links)
- Primary color (buttons, selections)
- Info, Success, Warning, Danger colors
- Navbar background and text colors
- Home app names text color

**Full Backend Dark Mode**
- Complete dark recompilation of the Odoo backend (not just the navbar)
- Light / Dark / System switch, per user, right in the user menu
- Fully customizable dark palette (brand, primary, semantic colours, navbar)

**Smart Navigation**
- Desktop: Arrow-based navigation between Home and apps
- Mobile: Native sidebar toggle preserved
- Seamless state management across page loads

**Technical Highlights**
- Built with OWL (Odoo Web Library) components
- SCSS-based color theming with live preview
- User-specific app ordering stored in database
- Clean, documented codebase

Configuration
-------------
Go to Settings → Home Screen Theme to customize:
- Upload custom background image
- Adjust all theme colors
- Reset colors to defaults

Want More?
----------
Six one-click theme presets with a live Theme Studio, sidebar
navigation, a command palette and a per-user KPI dashboard are
available in the **Home Screen Theme Pro** add-on.

Compatibility
-------------
- Odoo 19.0 Community Edition
- All standard Odoo modules
    """,
    'author': 'SynthraTech SAS',
    'maintainer': 'SynthraTech SAS',
    'support': 'soporte.synthra@gmail.com',
    # Free AND open: use it, modify it, redistribute it (LGPL-3).
    'license': 'LGPL-3',
    'depends': ['web', 'base'],
    'data': [
        'security/ir.model.access.csv',
        'views/webclient_templates.xml',
        'views/home_screen_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'web._assets_primary_variables': [
            ('prepend', 'home_theme/static/src/scss/colors.scss'),
            (
                'before',
                'home_theme/static/src/scss/colors.scss',
                'home_theme/static/src/scss/colors_light.scss'
            ),
        ],
        # Full backend dark mode: the dark palette recompiles the backend and
        # the overrides fix the components that don't pick it up.
        'web.assets_web_dark': [
            (
                'after',
                'home_theme/static/src/scss/colors.scss',
                'home_theme/static/src/scss/colors_dark.scss'
            ),
            'home_theme/static/src/scss/dark_overrides.scss',
        ],
        'web.assets_backend': [
            'home_theme/static/src/scss/home_screen_colors.scss',
            'home_theme/static/src/scss/home_screen.scss',
            'home_theme/static/src/scss/color_field.scss',
            'home_theme/static/src/scss/color_settings.scss',
            'home_theme/static/src/xml/navbar.xml',
            'home_theme/static/src/xml/home_screen.xml',
            'home_theme/static/src/xml/color_field.xml',
            'home_theme/static/src/xml/color_scheme_menu.xml',
            'home_theme/static/src/js/home_menu_service.js',
            'home_theme/static/src/js/navbar_patch.js',
            'home_theme/static/src/js/webclient_patch.js',
            'home_theme/static/src/js/home_screen.js',
            'home_theme/static/src/js/color_field.js',
            'home_theme/static/src/js/color_scheme_service.js',
            'home_theme/static/src/js/color_scheme_menu.js',
        ],
    },
    # Store gallery (first image = card thumbnail). Lead with the designed banner,
    # matching the Pro / Dashboard / Website listings; screenshots follow.
    'images': [
        'static/description/banner.png',
        'static/description/theme_screenshot.png',
        'static/description/screenshot_dark.png',
        'static/description/screenshot_dark_home.png',
        'static/description/screenshot_dark_colors.png',
        'static/description/screenshot_wallpapers_light.png',
        'static/description/screenshot_1_screenshot.png',
        'static/description/screenshot_2.png',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'uninstall_hook': '_uninstall_cleanup',
}
