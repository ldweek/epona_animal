# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Per-company FX rate provider configuration.

Wraps the pure-python provider registry in `tools.rate_providers` so
finance can pick a source and the cron can fetch on a schedule. Rates
are written into Odoo's standard res.currency.rate table; the
revaluation run reads them through res.currency._get_conversion_rate
exactly as before, so existing valuation paths are untouched.

One row per company. Multiple companies running different providers
(some on ECB, some manual) coexist; the cron iterates active configs.
"""

import datetime
import logging
import math
from decimal import Decimal

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.eh_account_fx_revaluation.tools import rate_providers

_logger = logging.getLogger(__name__)


class EhFxRateConfig(models.Model):
    _name = 'eh.fx.rate.config'
    _description = "FX rate provider configuration"
    _rec_name = 'company_id'

    company_id = fields.Many2one(
        'res.company', required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    provider = fields.Selection(
        selection='_selection_provider',
        required=True,
        default=lambda self: self._default_provider(),
        help=(
            "External source for daily rates. Central-bank sources (ECB, "
            "Bank of Canada, National Bank of Poland, Bank of Russia, "
            "Central Bank of Turkey, Reserve Bank of Australia, Central "
            "Bank of Brazil, HMRC) need no API key; cross-rates are "
            "derived into the company currency. Aggregator sources serve "
            "broad coverage; some need an API key. Manual disables "
            "automatic fetch so finance enters rates by hand."
        ),
    )
    provider_needs_key = fields.Boolean(
        compute='_compute_provider_needs_key',
        help="True when the selected provider requires an API key.",
    )
    api_key = fields.Char(
        groups='eh_account_base.group_eh_manager',
        help=(
            "API key for sources that require one (aggregator services). "
            "Central-bank sources ignore this field."
        ),
    )
    fallback_provider = fields.Selection(
        selection='_selection_provider',
        default='manual',
        help=(
            "Optional second source used to fill any currency the primary "
            "provider does not return, or to cover the run when the primary "
            "is temporarily unreachable. Leave on Manual to disable the "
            "fallback. A broad aggregator is a good choice here so a "
            "national feed that omits an exotic currency still resolves."
        ),
    )
    override_line_ids = fields.One2many(
        'eh.fx.rate.override', 'config_id',
        help=(
            "Manual per-currency rates. An override always wins over the "
            "fetched value, so finance can pin a currency that has no clean "
            "feed while every other currency keeps auto-updating."
        ),
    )
    enabled = fields.Boolean(
        default=True,
        help="Master toggle. Disable to suspend the cron without losing config.",
    )
    fetch_frequency = fields.Selection(
        [
            ('manual', "Manual only"),
            ('daily', "Daily"),
            ('weekly', "Weekly"),
            ('monthly', "Monthly"),
        ],
        default='daily', required=True,
        help=(
            "How often the scheduled job refreshes rates for this "
            "company. 'Manual only' never auto-fetches (finance pulls on "
            "demand); the others gate the daily cron on the elapsed time "
            "since the last successful fetch."
        ),
    )
    last_fetched_at = fields.Datetime(readonly=True)
    last_fetch_summary = fields.Char(readonly=True)
    last_error = fields.Char(readonly=True)
    rate_direction_review_required = fields.Boolean(
        string="Legacy rate review required",
        readonly=True,
        copy=False,
        help=(
            "Upgrade safety hold. Earlier releases wrote provider quotes in "
            "the reciprocal direction and did not record provenance on "
            "res.currency.rate. Finance must review affected historical "
            "currency-rate rows before re-enabling automatic updates or FX "
            "revaluation."
        ),
    )
    rate_direction_reviewed_at = fields.Datetime(
        readonly=True, copy=False,
    )
    rate_direction_reviewed_by_id = fields.Many2one(
        'res.users', readonly=True, copy=False,
    )
    timeout_seconds = fields.Integer(
        default=10,
        help="Network timeout in seconds for the provider HTTP call.",
    )

    _unique_per_company = models.Constraint(
        'unique(company_id)',
        'One FX rate config per company.',
    )

    @api.model
    def _selection_provider(self):
        # Driven by the registry so adding a provider in tools/
        # surfaces in the UI without view edits.
        return [
            (name, rate_providers.provider_label(name))
            for name in rate_providers.known_providers()
        ]

    @api.model
    def _default_provider(self):
        # Default a new config to the source most relevant to the
        # company's country, falling back to ECB. Mirrors the operator's
        # expectation that, say, a Canadian company starts on Bank of
        # Canada without hunting through the list.
        country = self.env.company.country_id
        return rate_providers.provider_for_country(
            country.code if country else None
        )

    @api.onchange('company_id')
    def _onchange_company_id_provider(self):
        # When the company changes on the form, re-suggest the country
        # default so the provider tracks the chosen company. The operator
        # can still override afterwards.
        for config in self:
            country = config.company_id.country_id
            config.provider = rate_providers.provider_for_country(
                country.code if country else None
            )

    @api.depends('provider')
    def _compute_provider_needs_key(self):
        for config in self:
            config.provider_needs_key = bool(
                config.provider
                and rate_providers.provider_needs_key(config.provider)
            )

    def _provider_instance(self, provider=None):
        self.ensure_one()
        provider = provider or self.provider
        try:
            factory = rate_providers.get(provider)
        except KeyError as exc:
            raise UserError(_(
                "Provider %s is not registered.",
            ) % provider) from exc
        return factory(
            timeout=int(self.timeout_seconds or 10),
            api_key=self.api_key or None,
        )

    def _override_map(self):
        """Return {currency_code -> Decimal} of active manual overrides.

        ``inverse_company_rate`` is retained as the legacy database field
        name, but its documented business value is the provider direction:
        units of listed currency per one unit of company currency.  That is
        Odoo's ``company_rate`` direction and merges into the pipeline
        directly.
        """
        self.ensure_one()
        out = {}
        for line in self.override_line_ids:
            if line.active and line.currency_id and line.inverse_company_rate:
                out[line.currency_id.name] = Decimal(
                    str(line.inverse_company_rate)
                )
        return out

    @api.private
    def fetch_rates(self, currency_codes=None, on_date=None,
                    raise_on_empty=True):
        """Fetch rates from the provider and write to res.currency.rate.

        :param currency_codes: iterable of ISO codes to fetch. When
            None, fetches every active foreign res.currency on the
            company.
        :param on_date: date for which to fetch. Defaults to today.
        :param raise_on_empty: when True, an empty, partial, undated, or
            otherwise invalid response raises UserError.  When False the
            failure is recorded and nothing is written.
        :return: dict {iso -> Decimal} of rates written.
        """
        self.ensure_one()
        if not self.enabled:
            return {}
        if self.rate_direction_review_required:
            raise UserError(_(
                "Automatic FX rates are on upgrade safety hold. Finance "
                "must review historical currency-rate rows and acknowledge "
                "the legacy rate-direction review first."
            ))
        on_date = on_date or fields.Date.context_today(self)
        if isinstance(on_date, str):
            on_date = datetime.date.fromisoformat(on_date[:10])
        rate_company = self._rate_company()
        company_ccy = rate_company.currency_id
        if currency_codes is None:
            target = self.env['res.currency'].search([
                ('active', '=', True),
                ('id', '!=', company_ccy.id),
            ])
            currency_codes = target.mapped('name')
        currency_codes = sorted({
            str(c).upper() for c in currency_codes
            if c and str(c).upper() != company_ccy.name
        })

        # 1) Primary provider. A provider error is remembered, not raised
        #    immediately, so a configured fallback can still cover the run.
        errors = []
        rates = {}
        effective_dates = {}
        try:
            rates, effective_dates = self._fetch_provider_batch(
                self.provider, company_ccy.name, currency_codes, on_date,
            )
        except rate_providers.RateProviderError as exc:
            errors.append("%s: %s" % (
                self.provider, rate_providers.redact_secrets(exc)))

        # 2) Fallback provider fills only the currencies still missing
        #    (or the whole run when the primary failed). The primary value
        #    always wins, so the fallback never overwrites a real fetch.
        fb = self.fallback_provider
        if fb and fb != self.provider:
            missing = [c for c in currency_codes if c not in rates]
            if missing:
                try:
                    fb_rates, fb_dates = self._fetch_provider_batch(
                        fb, company_ccy.name, missing, on_date,
                    )
                    for code, value in fb_rates.items():
                        if code not in rates:
                            rates[code] = value
                            effective_dates[code] = fb_dates[code]
                except rate_providers.RateProviderError as exc:
                    errors.append("fallback %s: %s" % (
                        fb, rate_providers.redact_secrets(exc)))

        # 3) Manual overrides win over everything fetched.
        overrides = {
            code: value for code, value in self._override_map().items()
            if code in currency_codes
        }
        rates.update(overrides)
        effective_dates.update({code: on_date for code in overrides})

        missing = [code for code in currency_codes if code not in rates]
        if missing:
            errors.append(_(
                "missing currencies: %s", ', '.join(missing),
            ))
        if not currency_codes:
            errors.append(_("no foreign currencies requested"))
        if errors and (missing or not rates):
            return self._fetch_failure(errors, on_date, raise_on_empty)

        prepared, invalid = self._prepare_rate_rows(
            rates, effective_dates, company_ccy,
        )
        if invalid or set(prepared) != set(currency_codes):
            invalid = sorted(set(invalid) | (set(currency_codes) - set(prepared)))
            errors.append(_(
                "invalid or unavailable currencies: %s", ', '.join(invalid),
            ))
            return self._fetch_failure(errors, on_date, raise_on_empty)

        written = self._upsert_rate_rows(prepared, company_ccy)
        fixing_dates = sorted({
            prepared[code]['name'] for code in written
        })
        if len(fixing_dates) == 1 and fixing_dates[0] == on_date:
            summary = _(
                "%(count)s rates written for %(date)s",
                count=len(written), date=on_date.isoformat(),
            )
        else:
            summary = _(
                "%(count)s rates written; requested %(requested)s; fixing "
                "date(s) %(fixings)s",
                count=len(written), requested=on_date.isoformat(),
                fixings=', '.join(date.isoformat() for date in fixing_dates),
            )
        extra = []
        if fb and fb != self.provider:
            extra.append(_("fallback %s", fb))
        if overrides:
            extra.append(_("%s manual override(s)", len(overrides)))
        if extra:
            summary = "%s (%s)" % (summary, ', '.join(extra))
        self.write({
            'last_error': '; '.join(errors)[:255],
            'last_fetched_at': fields.Datetime.now(),
            'last_fetch_summary': summary[:255],
        })
        return written

    def _rate_company(self):
        """Company whose rate rows Odoo will read for this config.

        Odoo 17+ stores rates only on a branch's root company. Odoo 16 has
        no ``root_id`` field and keeps the selected company.
        """
        self.ensure_one()
        Company = self.env['res.company']
        return (
            self.company_id.root_id
            if 'root_id' in Company._fields
            else self.company_id
        )

    def _fetch_provider_batch(self, provider, base, requested, on_date):
        """Fetch one source and require coherent fixing-date evidence."""
        result = self._provider_instance(provider=provider).fetch(
            base, requested, on_date,
        )
        rates = {
            str(code).upper(): value for code, value in dict(result or {}).items()
            if str(code).upper() in requested
        }
        if not rates:
            return {}, {}
        effective_date = rate_providers.parse_rate_date(
            getattr(result, 'effective_date', None),
        )
        if effective_date is None:
            raise rate_providers.RateProviderError(
                "Provider %s returned rates without a proven fixing date."
                % provider
            )
        if effective_date > on_date:
            raise rate_providers.RateProviderError(
                "Provider %s served fixing %s after requested date %s."
                % (provider, effective_date, on_date)
            )
        return rates, {code: effective_date for code in rates}

    def _fetch_failure(self, errors, on_date, raise_failure):
        """Record an unsuccessful attempt without suppressing next cron run."""
        note = '; '.join(str(error) for error in errors if error)
        note = note or _("No rates returned")
        self.write({
            'last_error': note[:255],
            'last_fetch_summary': _(
                "No rates written for %(date)s: %(error)s",
                date=on_date.isoformat(), error=note,
            )[:255],
        })
        if raise_failure:
            raise UserError(_(
                "FX rates were not written for %(date)s: %(error)s",
                date=on_date.isoformat(), error=note,
            ))
        return {}

    def _prepare_rate_rows(self, rates, effective_dates, company_ccy):
        """Validate a complete batch and calculate Odoo technical rates."""
        Currency = self.env['res.currency'].sudo().with_context(
            active_test=False,
        )
        rate_company = self._rate_company()
        currencies = Currency.search([('name', 'in', list(rates))])
        by_code = {currency.name: currency for currency in currencies}
        prepared = {}
        invalid = []
        for code, value in rates.items():
            ccy = by_code.get(code)
            fixing_date = rate_providers.parse_rate_date(
                effective_dates.get(code),
            )
            try:
                direct_rate = float(value)
            except (TypeError, ValueError, OverflowError):
                direct_rate = 0.0
            if (
                not ccy or ccy == company_ccy or not fixing_date
                or not math.isfinite(direct_rate) or direct_rate <= 0.0
            ):
                invalid.append(code)
                continue
            base_rates = company_ccy.sudo()._get_rates(
                rate_company, fixing_date,
            )
            base_technical_rate = float(
                base_rates.get(company_ccy.id) or 1.0
            )
            stored_rate = direct_rate * base_technical_rate
            if not math.isfinite(stored_rate) or stored_rate <= 0.0:
                invalid.append(code)
                continue
            prepared[code] = {
                'currency_id': ccy.id,
                'name': fixing_date,
                'company_id': rate_company.id,
                'rate': stored_rate,
                'provider_value': value,
                'direct_rate': direct_rate,
            }
        return prepared, invalid

    def _upsert_rate_rows(self, prepared, company_ccy):
        Rate = self.env['res.currency.rate'].sudo()
        written = {}
        for code, vals in prepared.items():
            provider_value = vals['provider_value']
            direct_rate = vals['direct_rate']
            rate_vals = {
                key: vals[key]
                for key in ('currency_id', 'name', 'company_id', 'rate')
            }
            existing = Rate.search([
                ('currency_id', '=', rate_vals['currency_id']),
                ('name', '=', rate_vals['name']),
                ('company_id', '=', rate_vals['company_id']),
            ], limit=1)
            if existing:
                existing.write(rate_vals)
            else:
                Rate.create(rate_vals)
            written[code] = provider_value
            _logger.info(
                "FX rate updated: %s -> %s = %s on %s (rate company %s)",
                company_ccy.name, code, direct_rate, rate_vals['name'],
                self._rate_company().name,
            )
        return written

    def _write_rates(self, rates, on_date, company_ccy,
                     effective_dates=None):
        """Upsert rates into res.currency.rate for this company.

        Provider returns rates as "1 unit of base in units of quote".
        That is Odoo's ``company_rate`` direction.  Technical ``rate`` is
        therefore ``provider quote * company-currency technical rate``;
        taking its reciprocal would silently reverse every conversion.

        ``effective_dates`` is per currency because primary, fallback, and
        manual sources may have different proven fixing dates.  Direct
        callers retain the legacy single-date behaviour.
        """
        effective_dates = effective_dates or {
            str(code).upper(): on_date for code in rates
        }
        normalized = {str(code).upper(): value for code, value in rates.items()}
        prepared, invalid = self._prepare_rate_rows(
            normalized, effective_dates, company_ccy,
        )
        if invalid or set(prepared) != set(normalized):
            return {}
        return self._upsert_rate_rows(prepared, company_ccy)

    def action_update_now(self):
        """Fetch rates immediately for this config and report the result.

        Gives the operator an on-demand pull instead of waiting for the
        daily cron. Surfaces an empty or failed run as a warning so the
        outcome is visible without leaving the form.
        """
        self.ensure_one()
        with self.env.cr.savepoint():
            return self._eh_action_update_now_atomic()

    def _eh_action_update_now_atomic(self):
        self.ensure_one()
        self._eh_check_access('write')
        written = self.fetch_rates(raise_on_empty=False)
        if written:
            message = _("%(count)s rates updated.", count=len(written))
            kind = 'success'
        else:
            message = self.last_fetch_summary or _("No rates returned.")
            kind = 'warning'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("FX rate update"),
                'message': message,
                'type': kind,
                'sticky': False,
            },
        }

    def action_acknowledge_rate_direction_review(self):
        """Release upgrade hold after finance reviews unproven legacy rows."""
        self._eh_check_access('write')
        if not self.env.user.has_group(
                'eh_account_base.group_eh_manager'):
            raise UserError(_(
                "Only an EH Accounting Manager may acknowledge the legacy "
                "FX rate-direction review."
            ))
        self.filtered('rate_direction_review_required').write({
            'rate_direction_review_required': False,
            'rate_direction_reviewed_at': fields.Datetime.now(),
            'rate_direction_reviewed_by_id': self.env.user.id,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("FX rate review acknowledged"),
                'message': _(
                    "Safety hold released. Automatic updates remain "
                    "disabled until enabled explicitly."
                ),
                'type': 'warning',
                'sticky': True,
            },
        }

    def _is_fetch_due(self, now=None):
        """Whether the scheduled fetch should run for this config now,
        per its frequency and last successful fetch."""
        self.ensure_one()
        if self.fetch_frequency == 'manual':
            return False
        if not self.last_fetched_at:
            return True
        now = now or fields.Datetime.now()
        elapsed = now - self.last_fetched_at
        thresholds = {
            # 20h, not 24h, so a fixed daily cron tick is never skipped
            # by a few minutes of drift.
            'daily': datetime.timedelta(hours=20),
            'weekly': datetime.timedelta(days=7),
            'monthly': datetime.timedelta(days=28),
        }
        return elapsed >= thresholds.get(
            self.fetch_frequency, datetime.timedelta(0))

    @api.model
    @api.private
    def _cron_fetch_daily(self):
        """Cron entry-point: fetch rates for every active config.

        Each company's fetch is wrapped in try/except so a single
        provider outage does not block the others. The error message
        is recorded on the config row for the next operator visit.
        """
        configs = self.search([('enabled', '=', True)])
        for config in configs:
            if not config._is_fetch_due():
                continue
            try:
                # A provider can fail after one or more rate writes, including
                # with a PostgreSQL error that aborts the current transaction.
                # Own the savepoint here so that partial rows roll back and
                # the outer cron cursor remains usable for later companies.
                with self.env.cr.savepoint():
                    config.fetch_rates(raise_on_empty=False)
            except Exception as exc:  # noqa: BLE001
                safe_error = rate_providers.redact_secrets(exc)[:255]
                _logger.warning(
                    "Daily rate fetch failed for company %s: %s",
                    config.company_id.name, safe_error,
                )
                try:
                    with self.env.cr.savepoint():
                        config.sudo().write({
                            'last_error': safe_error,
                        })
                except Exception:  # noqa: BLE001
                    _logger.exception(
                        "Could not persist FX fetch failure for company %s",
                        config.company_id.name,
                    )


class EhFxRateOverride(models.Model):
    _name = 'eh.fx.rate.override'
    _description = "FX manual rate override"

    config_id = fields.Many2one(
        'eh.fx.rate.config', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='config_id.company_id', store=True, index=True,
    )
    currency_id = fields.Many2one(
        'res.currency', required=True,
        help="The currency this manual rate applies to.",
    )
    inverse_company_rate = fields.Float(
        string="Units per company currency",
        digits=(16, 6), required=True,
        help=(
            "Units of this currency for one unit of the company currency "
            "(Odoo company-rate direction). For example, with an AUD "
            "company and a "
            "USD override, 0.65 means 1 AUD = 0.65 USD."
        ),
    )
    active = fields.Boolean(default=True)
    note = fields.Char()

    _unique_currency = models.Constraint(
        'unique(config_id, currency_id)',
        'One manual override per currency on a configuration.',
    )
