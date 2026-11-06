# Home Screen Theme

A modern, customizable home screen module for Odoo 19 Community Edition.

## Features

### 🏠 Custom Home Screen Dashboard
- Clean, responsive grid layout displaying all installed apps
- Drag and drop to reorder apps (order saved per user)
- Custom background image support
- Smooth animations and hover effects
- Transparent app icons for better visual integration

### 🎨 Theme Color Customization
All colors are managed via SCSS and applied system-wide:

| Color | Description |
|-------|-------------|
| **Brand** | Logo, main accent color, links |
| **Primary** | Buttons, active states, selections |
| **Info** | Information badges, tooltips |
| **Success** | Success messages, confirmations |
| **Warning** | Warnings, pending states |
| **Danger** | Errors, delete actions |
| **Navbar Background** | Navigation bar background |
| **Navbar Text** | Navigation bar text |
| **Home App Names** | App name text in home screen |

### 🔍 Instant Menu Search
- Search every menu, report and option you can access
- `Ctrl+K` to focus, full keyboard navigation (↑ ↓ ↵ Esc)
- Results show breadcrumb paths and their app icon

### 🌗 Light / Dark Mode
- Per-user color scheme: Light, Dark or System (follows OS)
- Selector available in the user menu
- Independent color palettes for light and dark

### 🧭 Smart Navigation
- **Desktop**: Arrow-based navigation
  - Inside app: Shows app icon (hover reveals ← arrow to go Home)
  - On Home after visiting app: Shows → arrow to return to app
  - Initial Home load: No button shown
- **Mobile**: Native sidebar toggle preserved

### 📱 Responsive Design
- 6 apps per row on large screens
- 5 apps per row on medium screens
- 4 apps per row on tablets
- 3 apps per row on mobile
- 2 apps per row on small mobile

## Installation

1. Copy the `home_theme` folder to your Odoo addons directory
2. Update the apps list in Odoo
3. Install "Home Screen Theme" from the Apps menu
4. Refresh your browser

## Configuration

Navigate to **Settings → Home Screen Theme** to customize:

### Home Screen Background
- Upload a custom background image (recommended: 1920x1080 or larger)

### Theme Colors
- Click on any color picker to change the color
- Changes require a page reload to take effect
- Use "Reset Colors" to restore defaults

## Technical Details

### File Structure
```
home_theme/
├── __init__.py                      # imports + uninstall hook (color cleanup)
├── __manifest__.py
├── README.md
├── controllers/
│   └── main.py                      # /web/home_screen + save_order endpoints
├── i18n/
│   └── es_CO.po                     # Spanish (Colombia) translations
├── models/
│   ├── color_assets_editor.py       # SCSS color variable replacement
│   ├── home_app_sequence.py         # Per-user app ordering model
│   ├── ir_http.py                   # color_scheme cookie override
│   ├── ir_ui_menu.py                # Home apps + searchable menus data
│   ├── res_config_settings.py       # Theme color & background settings
│   └── res_users_settings.py        # Per-user color_scheme field
├── security/
│   └── ir.model.access.csv
├── static/
│   ├── description/
│   │   ├── icon.png
│   │   └── index.html
│   └── src/
│       ├── css/
│       │   └── home_screen.css
│       ├── js/
│       │   ├── color_scheme_menu.js     # User-menu light/dark selector
│       │   ├── color_scheme_service.js  # Applies the saved color scheme
│       │   ├── home_menu_service.js     # Home ↔ app navigation state
│       │   ├── home_screen.js           # Dashboard OWL component
│       │   ├── navbar_patch.js          # Navbar arrow-navigation behavior
│       │   └── webclient_patch.js       # Loads Home as the default app
│       ├── scss/
│       │   ├── colors.scss              # Ordering anchor
│       │   ├── colors_light.scss        # Light palette
│       │   ├── colors_dark.scss         # Dark palette
│       │   ├── home_screen_colors.scss  # App-name color
│       │   └── home_screen_dark.scss    # Dashboard dark overrides
│       └── xml/
│           ├── color_scheme_menu.xml
│           ├── home_screen.xml
│           └── navbar.xml
└── views/
    ├── home_screen_views.xml
    ├── res_config_settings_views.xml
    └── webclient_templates.xml
```

### Key Components

- **HomeScreenDashboard**: OWL component for the home screen
- **home_menu service**: Manages navigation state between Home and apps
- **NavBar patch**: Customizes navbar behavior for arrow navigation
- **color_assets_editor**: Handles SCSS variable replacement for theme colors

### API Endpoints

- `POST /web/home_screen` - Get home screen data (apps, background)
- `POST /web/home_screen/save_order` - Save user's app order

## Dependencies

- `web`
- `base`

## Compatibility

- Odoo 19.0 Community Edition
- All standard Odoo modules

## License

LGPL-3

## Author

**Hacienda Los Sauces**  
Development Team

## Changelog

### Version 19.0.4.0.0
- Provide a full backend dark mode for Community Edition (which has none natively):
  `colors_dark.scss` now injects a dark base palette (gray scale, surfaces, text,
  Bootstrap body/borders) early in the dark bundle, so the whole backend recompiles
  dark and Odoo's built-in `*.dark.scss` component styles activate correctly
- Dashboard styles moved to `home_screen.scss` and driven by Odoo theme variables
  (`$o-gray-*`, `$o-view-background-color`, `$o-brand-primary`), so the home screen
  adapts natively to light/dark and to the configured brand color
- Removed the standalone `home_screen_dark.scss` (no longer needed)

### Version 19.0.3.0.0
- Batched the app-order save endpoint (removed per-app N+1 queries)
- Removed dead code and switched to lazy (`%s`) logging in models
- Dashboard component now uses a root `t-ref` instead of global DOM lookups
- Localized the greeting and date; background image now serves its real MIME type
- Unified the navbar template to English source strings (cleaner translations)
- Refactored the dashboard styles onto CSS design tokens (single source of truth)
- Completed dark mode: every element is themed through token overrides instead of
  duplicated selectors, and the previously unreadable app-name color is fixed
- Aligned focus/selection accents with the brand color; added `:focus-visible`
  outlines and `prefers-reduced-motion` support
- Rewrote the App Store description page and refreshed this documentation

### Version 19.0.2.0.0
- Simplified theme configuration (removed dark mode)
- Added Home App Names color customization
- Improved navbar arrow navigation
- Transparent app icon backgrounds
- Code cleanup and documentation
- Performance optimizations

### Version 19.0.1.0.0
- Initial release
- Basic home screen with app grid
- Drag and drop ordering
- Background customization
