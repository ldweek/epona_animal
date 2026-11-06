{
    "name": "ICA Responsive",
    "summary": "Responsive backend theme, home menu, navbar, and dark mode enhancements.",
    "category": "Technical",
    "version": "19.0.1.0.0",
    "author": "Agga, IdeaCode Academy",
    "maintainer": "IdeaCode Academy",
    "website": "https://www.linkedin.com/company/ideacode-academy",
    "support": "hanzawnyineonline@gmail.com",
    "depends": ["web", "base_setup"],
    "auto_install": False,
    "data": [
        "views/webclient_templates.xml",
    ],
    "assets": {
        "web._assets_primary_variables": [
            (
                "after",
                "web/static/src/scss/primary_variables.scss",
                "ica_web_responsive/static/src/**/*.variables.scss",
            ),
            (
                "before",
                "web/static/src/scss/primary_variables.scss",
                "ica_web_responsive/static/src/scss/primary_variables.scss",
            ),
        ],
        "web._assets_secondary_variables": [
            (
                "before",
                "web/static/src/scss/secondary_variables.scss",
                "ica_web_responsive/static/src/scss/secondary_variables.scss",
            ),
        ],
        "web._assets_backend_helpers": [
            (
                "before",
                "web/static/src/scss/bootstrap_overridden.scss",
                "ica_web_responsive/static/src/scss/bootstrap_overridden.scss",
            ),
        ],
        "web.assets_frontend": [
            "ica_web_responsive/static/src/webclient/home_menu/home_menu_background.scss",  # used by login page
            "ica_web_responsive/static/src/webclient/navbar/navbar.scss",
        ],
        "web.assets_backend": [
            "ica_web_responsive/static/src/webclient/**/*.scss",
            "ica_web_responsive/static/src/views/**/*.scss",
            "ica_web_responsive/static/src/core/**/*",
            "ica_web_responsive/static/src/webclient/**/*.js",
            (
                "after",
                "web/static/src/views/list/list_renderer.xml",
                "ica_web_responsive/static/src/views/list/list_renderer_desktop.xml",
            ),
            "ica_web_responsive/static/src/webclient/**/*.xml",
            "ica_web_responsive/static/src/views/**/*.js",
            "ica_web_responsive/static/src/views/**/*.xml",
            ("remove", "ica_web_responsive/static/src/views/pivot/**"),
            # Don't include dark mode files in light mode
            ("remove", "ica_web_responsive/static/src/**/*.dark.scss"),
        ],
        "web.assets_backend_lazy": [
            "ica_web_responsive/static/src/views/pivot/**",
        ],
        "web.assets_backend_lazy_dark": [
            ("include", "web.dark_mode_variables"),
            # web._assets_backend_helpers
            (
                "before",
                "ica_web_responsive/static/src/scss/bootstrap_overridden.scss",
                "ica_web_responsive/static/src/scss/bootstrap_overridden.dark.scss",
            ),
            (
                "after",
                "web/static/lib/bootstrap/scss/_functions.scss",
                "ica_web_responsive/static/src/scss/bs_functions_overridden.dark.scss",
            ),
        ],
        "web.assets_web": [
            (
                "replace",
                "web/static/src/main.js",
                "ica_web_responsive/static/src/main.js",
            ),
        ],
        # ========= Dark Mode =========
        "web.dark_mode_variables": [
            # web._assets_primary_variables
            (
                "before",
                "ica_web_responsive/static/src/scss/primary_variables.scss",
                "ica_web_responsive/static/src/scss/primary_variables.dark.scss",
            ),
            (
                "before",
                "ica_web_responsive/static/src/**/*.variables.scss",
                "ica_web_responsive/static/src/**/*.variables.dark.scss",
            ),
            # web._assets_secondary_variables
            (
                "before",
                "ica_web_responsive/static/src/scss/secondary_variables.scss",
                "ica_web_responsive/static/src/scss/secondary_variables.dark.scss",
            ),
        ],
        "web.assets_web_dark": [
            ("include", "web.dark_mode_variables"),
            # web._assets_backend_helpers
            (
                "before",
                "ica_web_responsive/static/src/scss/bootstrap_overridden.scss",
                "ica_web_responsive/static/src/scss/bootstrap_overridden.dark.scss",
            ),
            (
                "after",
                "web/static/lib/bootstrap/scss/_functions.scss",
                "ica_web_responsive/static/src/scss/bs_functions_overridden.dark.scss",
            ),
            # assets_backend
            "ica_web_responsive/static/src/**/*.dark.scss",
        ],
    },
    "description": """
ICA Responsive
==============

Enhance the Odoo backend with responsive web client behavior, updated navbar,
home menu improvements, share URL tools, and light/dark styling assets.

Key Features
------------
* Responsive backend web client customizations.
* Updated navbar and burger menu behavior.
* Home menu layout and background styling.
* Light and dark mode SCSS assets.
* List, kanban, pivot, search, and core UI style improvements.
""",
    "images": ["static/description/img.png"],
    "license": "LGPL-3",
}
