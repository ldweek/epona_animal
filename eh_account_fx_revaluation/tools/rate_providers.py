# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Pluggable foreign-exchange rate providers.

Each provider is a callable class with a single public method:

    fetch(base, quotes, on_date) -> ProviderRates

* base       three-letter ISO 4217 base currency (e.g. 'EUR' for ECB).
* quotes     iterable of ISO codes the caller wants quoted.
* on_date    a datetime.date. Most providers serve "the rate on or
             before this date"; the returned mapping uses the rate
             dated on_date if available, else the most recent prior
             publication.

Returns a dict-like :class:`ProviderRates` carrying quote currency code to
Decimal rate plus the provider's effective fixing date.  The rate expresses
1 unit of base currency in units of the quote currency.  A provider that has
no rate for a quote leaves it out; the caller treats the gap as "no rate
available" and surfaces it.  The fixing date is evidence, not decoration:
the ORM pipeline refuses to stamp a requested date that the source did not
serve.

Network errors raise RateProviderError. The Odoo cron wrapper catches
this and logs a chatter line so a one-off outage does not poison the
run record.

References:
* ECB daily reference rates XML feed:
  https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml
  https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml
* ECB legal terms permit free reuse with attribution; the URL above
  is the source of truth.

This module is original work; the parsers below implement the published
XML schema and do not derive from any third-party rate library.
"""

import datetime
import json
import logging
import re
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

from lxml import etree as ET

_logger = logging.getLogger(__name__)


# Known providers and their characteristics. The Odoo config field
# uses the keys of this dict, so adding a provider is purely a matter
# of registering a class below and adding an entry here.
_PROVIDERS = {}
_LABELS = {}
# Providers that require an operator-supplied API key. Surfaced so the
# UI can hint which sources need the key field populated.
_NEEDS_KEY = set()

# Sent on every outbound request. Some central-bank endpoints reject the
# default urllib agent, so we identify the module politely.
_USER_AGENT = (
    'eh_account_fx_revaluation/19.0 (+https://www.erpheritage.com.au)'
)
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
_SECRET_RE = re.compile(
    r'(?i)(access[_-]?token|api[_-]?key|access[_-]?key|app[_-]?id|'
    r'secret|password)(["\']?\s*[:=]\s*["\']?)([^"\',\s}&]+)'
)


def redact_url(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        safe = []
        for key, value in query:
            marker = key.lower().replace('-', '_')
            if any(part in marker for part in (
                    'token', 'key', 'secret', 'password', 'app_id',
                    'client_id')):
                value = '***'
            safe.append((key, value))
        return urllib.parse.urlunsplit(parsed._replace(
            query=urllib.parse.urlencode(safe)))
    except Exception:  # noqa: BLE001
        return '<redacted-url>'


def redact_secrets(message):
    return _SECRET_RE.sub(r'\1\2***', str(message or ''))


def build_url(url, params):
    """Return *url* with query parameters encoded as data, never syntax."""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(params.items())
    return urllib.parse.urlunsplit(parsed._replace(
        query=urllib.parse.urlencode(query, doseq=True),
    ))


def parse_xml_safely(raw, source="FX provider"):
    """Parse untrusted provider XML without DTDs, entities, or networking."""
    if isinstance(raw, str):
        payload = raw.encode('utf-8')
    elif isinstance(raw, (bytes, bytearray)):
        payload = bytes(raw)
    else:
        raise RateProviderError("%s returned a non-byte XML payload." % source)
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', payload, re.IGNORECASE):
        raise RateProviderError(
            "%s returned XML containing a forbidden DTD or entity."
            % source
        )
    parser = ET.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        huge_tree=False,
        recover=False,
    )
    try:
        return ET.fromstring(payload, parser=parser)
    except (ET.XMLSyntaxError, ValueError, TypeError) as exc:
        raise RateProviderError(
            "%s returned malformed XML: %s" % (source, exc),
        ) from exc


def _read_limited(response):
    payload = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise RateProviderError(
            "FX provider response exceeded %s bytes." % _MAX_RESPONSE_BYTES)
    return payload


def register(name, factory, label=None, needs_key=False):
    """Register a provider factory by short name. Idempotent.

    :param label: human label shown in the Odoo selection. Defaults to
        the upper-cased key.
    :param needs_key: True when the source needs an operator API key.
    """
    _PROVIDERS[name] = factory
    _LABELS[name] = label or name.upper()
    if needs_key:
        _NEEDS_KEY.add(name)
    else:
        _NEEDS_KEY.discard(name)


def get(name):
    """Return the factory registered under `name` or raise KeyError."""
    if name not in _PROVIDERS:
        raise KeyError("Unknown FX rate provider: %r" % name)
    return _PROVIDERS[name]


def known_providers():
    """Return the list of registered provider keys, sorted."""
    return sorted(_PROVIDERS.keys())


def provider_label(name):
    """Return the human label for a provider key."""
    return _LABELS.get(name, name.upper())


def provider_needs_key(name):
    """Return True when the provider requires an operator API key."""
    return name in _NEEDS_KEY


# Country (ISO 3166-1 alpha-2) -> preferred provider key. Used to default
# a new company's FX config to the most relevant source for its
# jurisdiction, the way an operator would expect. A country with no
# dedicated feed falls back to ECB, whose EUR base cross-derives into any
# company currency. The map is extended as new national feeds register.
_EUR_COUNTRIES = (
    'AT', 'BE', 'HR', 'CY', 'EE', 'FI', 'FR', 'DE', 'GR', 'IE', 'IT', 'LV',
    'LT', 'LU', 'MT', 'NL', 'PT', 'SK', 'SI', 'ES',
)
COUNTRY_PROVIDER = {country: 'ecb' for country in _EUR_COUNTRIES}
COUNTRY_PROVIDER.update({
    'CA': 'boc',        # Bank of Canada
    'PL': 'nbp',        # National Bank of Poland
    'RU': 'cbr',        # Bank of Russia
    'TR': 'tcmb',       # Central Bank of Turkey
    'AU': 'rba',        # Reserve Bank of Australia
    'BR': 'bcb',        # Central Bank of Brazil
    'GB': 'hmrc',       # HM Revenue and Customs (Odoo country code GB)
    'UK': 'hmrc',       # tolerate the non-ISO 'UK' alias some data carries
    'CZ': 'cnb',        # Czech National Bank
    'BG': 'bnb',        # Bulgarian National Bank
    'RO': 'bnr',        # National Bank of Romania
    'SE': 'srb',        # Sveriges Riksbank
    'KZ': 'nbkz',       # National Bank of Kazakhstan
    'UZ': 'cbu',        # Central Bank of Uzbekistan
    'MY': 'bnm',        # Bank Negara Malaysia
    'CO': 'banrepco',   # Bank of the Republic (Colombia TRM)
    'PE': 'bcrp',       # Central Reserve Bank of Peru
    'MX': 'banxico',    # Bank of Mexico (needs a free token)
    'UY': 'bcu',        # Central Bank of Uruguay (SOAP, open data)
    'KW': 'cbk',        # Central Bank of Kuwait (KWD floats, live feed)
    'BH': 'cbb',        # Central Bank of Bahrain (live feed)
    'SA': 'gcc_peg',    # Saudi Riyal, decree peg
    'AE': 'gcc_peg',    # UAE Dirham, decree peg
    'QA': 'gcc_peg',    # Qatari Riyal, decree peg
    'OM': 'gcc_peg',    # Omani Rial, decree peg
})


def provider_for_country(code, default='ecb'):
    """Return the preferred provider key for an ISO country code.

    Falls back to ``default`` (ECB) when the country has no dedicated
    feed, mirroring how an operator would pick the closest sensible
    source. ``code`` may be None or empty.
    """
    if not code:
        return default
    return COUNTRY_PROVIDER.get(code.upper(), default)


class RateProviderError(Exception):
    """Raised when a provider cannot deliver rates (network, parse)."""


class ProviderRates(dict):
    """Rate mapping plus source-proven effective fixing date.

    It intentionally subclasses ``dict`` so existing provider callers keep
    normal mapping behaviour while the accounting write path can preserve
    the source date.  ``effective_date`` may be ``None`` only for an empty
    response or a legacy provider; non-empty undated responses are rejected
    by the ORM trust gate.
    """

    def __init__(self, rates=None, *, effective_date=None):
        super().__init__(rates or {})
        self.effective_date = parse_rate_date(effective_date)


def parse_rate_date(raw):
    """Return a ``date`` from common central-bank date shapes.

    Sources use ISO, dotted/slashed national formats, RFC-style timestamps,
    or Unix seconds.  Unknown non-empty values return ``None`` so the caller
    fails closed instead of guessing.
    """
    if raw is None or raw == '':
        return None
    if isinstance(raw, datetime.datetime):
        return raw.date()
    if isinstance(raw, datetime.date):
        return raw
    if isinstance(raw, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(
                raw, tz=datetime.timezone.utc,
            ).date()
        except (OverflowError, OSError, TypeError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    # ISO dates and timestamps are the dominant response shape.
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError:
        pass
    for fmt in (
        '%d.%m.%Y', '%d/%m/%Y', '%m/%d/%Y', '%d-%b-%Y',
        '%d.%b.%y', '%Y%m%d%H%M%S',
    ):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, OverflowError):
        pass
    return None


# ---- Shared HTTP + cross-derivation helpers ----


def http_get(url, timeout, headers=None):
    """GET a URL and return the raw bytes, or raise RateProviderError.

    Centralises the urllib boilerplate so every provider gets the same
    timeout handling, user-agent, and error wrapping. Network and HTTP
    failures are wrapped into RateProviderError; the cron handler treats
    that as a recoverable per-config outage rather than a crash.
    """
    req_headers = {'User-Agent': _USER_AGENT}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return _read_limited(resp)
    except RateProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        safe_url = redact_url(url)
        detail = redact_secrets(str(exc).replace(url, safe_url))
        raise RateProviderError(
            "HTTP fetch failed for %s: %s" % (safe_url, detail),
        ) from exc


def http_post(url, body, timeout, headers=None):
    """POST a request body and return the raw response bytes.

    Used by SOAP providers, which must POST an XML envelope rather than
    GET a URL. Shares the timeout, user-agent, and error wrapping of
    http_get so a SOAP outage is a recoverable RateProviderError, not a
    crash. ``body`` may be str (encoded utf-8) or bytes.
    """
    if isinstance(body, str):
        body = body.encode('utf-8')
    req_headers = {'User-Agent': _USER_AGENT}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return _read_limited(resp)
    except RateProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        safe_url = redact_url(url)
        detail = redact_secrets(str(exc).replace(url, safe_url))
        raise RateProviderError(
            "HTTP POST failed for %s: %s" % (safe_url, detail),
        ) from exc


def to_decimal(raw):
    """Parse a provider rate string into Decimal, tolerating a decimal
    comma (used by several European central banks) and surrounding
    whitespace. Returns None when the value cannot be parsed so callers
    can skip a single bad row instead of failing the whole feed.
    """
    if raw is None:
        return None
    text = str(raw).strip().replace('\xa0', '').replace(' ', '')
    if not text:
        return None
    # Central banks such as CBR and TCMB publish "70,1234"; normalise to
    # a dot. A value never legitimately contains both separators here.
    if ',' in text and '.' not in text:
        text = text.replace(',', '.')
    try:
        value = Decimal(text)
    except (InvalidOperation, TypeError):
        return None
    return value if value.is_finite() and value > 0 else None


def cross_derive(native_base, native_rates, base, quotes):
    """Re-express a provider's native-base table against an arbitrary base.

    :param native_base: ISO code the provider publishes against (e.g.
        'PLN' for the National Bank of Poland).
    :param native_rates: dict {code -> Decimal} where each value is
        "1 unit of native_base expressed in units of code". The
        native_base itself is implicitly 1 and need not appear.
    :param base: the company currency the caller wants quotes against.
    :param quotes: iterable of ISO codes wanted.
    :return: dict {quote -> Decimal} where the value is "1 unit of base
        expressed in units of quote". Quotes the table cannot reach are
        omitted so the caller surfaces the gap.

    When base differs from native_base we pivot through native_base:
        rate(base -> quote) = rate(native -> quote) / rate(native -> base).
    A base the table does not carry cannot be pivoted, so we raise rather
    than guess.
    """
    native_base = (native_base or '').upper()
    base = (base or '').upper()
    wanted = {q.upper() for q in quotes}
    table = {}
    for code, value in native_rates.items():
        dec = value if isinstance(value, Decimal) else to_decimal(value)
        if dec is not None and dec.is_finite() and dec > 0:
            table[code.upper()] = dec

    if base == native_base:
        return {q: table[q] for q in wanted if q in table and q != base}

    if base not in table:
        raise RateProviderError(
            "Source base %s carries no rate for company currency %s; "
            "cannot derive cross rates." % (native_base or '?', base)
        )
    base_rate = table[base]
    if not base_rate:
        raise RateProviderError(
            "Source returned a zero rate for %s; cannot derive cross "
            "rates." % base
        )

    out = {}
    for quote in wanted:
        if quote == base:
            continue
        try:
            derived = (
                Decimal(1) / base_rate
                if quote == native_base else
                table[quote] / base_rate
                if quote in table else None
            )
        except (InvalidOperation, ArithmeticError) as exc:
            raise RateProviderError(
                "Source rates cannot derive a finite positive quote for %s."
                % quote
            ) from exc
        if derived is not None and derived.is_finite() and derived > 0:
            out[quote] = derived
    return out


class BaseHttpProvider:
    """Common scaffolding for HTTP rate providers.

    A subclass sets ``name``, ``native_base`` (the ISO code the source
    publishes against, or None for a multi-base API that takes the base
    as a parameter), and implements ``_fetch_native`` to return a
    {code -> Decimal} table expressed as "1 native_base in units of
    code". The base class handles the cross-derivation, the API-key
    guard, and the network seam.

    Multi-base sources (an API that serves any requested base directly)
    override ``fetch`` instead and skip ``cross_derive`` entirely.

    ``_download`` is the single network seam; unit tests replace it with
    a stub so the parsers are exercised hermetically with no live call.
    """

    name = None
    native_base = None
    needs_key = False

    def __init__(self, timeout=10, api_key=None, **kwargs):
        self.timeout = int(timeout or 10)
        self.api_key = api_key or None
        self.effective_date = None

    def _start_fetch(self):
        """Clear per-request fixing provenance before parsing a response."""
        self.effective_date = None

    def _set_effective_date(self, raw):
        """Record one coherent fixing date from source response data."""
        parsed = parse_rate_date(raw)
        if parsed is None:
            raise RateProviderError(
                "%s returned an invalid or unrecognised fixing date: %r"
                % (self.name, raw)
            )
        if self.effective_date and self.effective_date != parsed:
            raise RateProviderError(
                "%s returned rates from mixed fixing dates (%s and %s)."
                % (self.name, self.effective_date, parsed)
            )
        self.effective_date = parsed
        return parsed

    def _result(self, rates):
        return ProviderRates(rates, effective_date=self.effective_date)

    def _download(self, url, headers=None):
        """Network seam. Overridden in tests to serve canned bytes."""
        return http_get(url, self.timeout, headers)

    def _download_post(self, url, body, headers=None):
        """POST network seam for SOAP providers. Overridden in tests."""
        return http_post(url, body, self.timeout, headers)

    def _download_post_xml(self, url, body, headers=None):
        raw = self._download_post(url, body, headers)
        return parse_xml_safely(raw, self.name)

    def _download_json(self, url, headers=None):
        raw = self._download(url, headers)
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RateProviderError(
                "%s returned malformed JSON: %s" % (self.name, exc),
            ) from exc

    def _download_xml(self, url, headers=None):
        raw = self._download(url, headers)
        return parse_xml_safely(raw, self.name)

    def _require_key(self):
        if self.needs_key and not self.api_key:
            raise RateProviderError(
                "Provider %s requires an API key. Set it on the FX rate "
                "configuration." % self.name
            )

    def fetch(self, base, quotes, on_date):
        self._start_fetch()
        self._require_key()
        native = self._fetch_native(base, quotes, on_date)
        return self._result(
            cross_derive(self.native_base, native, base, quotes),
        )

    def _fetch_native(self, base, quotes, on_date):
        """Return {code -> Decimal} as '1 native_base in units of code'.

        The returned table should include an entry for ``base`` (unless
        base == native_base) so the cross derivation can pivot; most
        sources publish a full table so this is automatic.
        """
        raise NotImplementedError


# ---- ECB ----

_ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_ECB_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
# ECB XML namespaces. The Cube root sits under gesmes:Envelope/Cube.
_NS = {
    'gesmes': 'http://www.gesmes.org/xml/2002-08-01',
    'eurofxref':
        'http://www.ecb.int/vocabulary/2002-08-01/eurofxref',
}


class EcbRateProvider:
    """European Central Bank reference rates.

    Base currency is always EUR. The daily feed serves today's rates
    plus a fallback to the latest published business day. The 90-day
    feed serves a per-day series; we hit it for historical lookups so
    the user can backfill rates when the revaluation_date is older
    than today.

    Network calls go through urllib.request with a configurable timeout
    so a server with a flaky outbound connection does not hang an Odoo
    worker. The default timeout (10s) is comfortably above the ECB's
    typical 200ms response.
    """

    name = 'ecb'

    def __init__(self, timeout=10, api_key=None, **kwargs):
        self.timeout = timeout
        self.api_key = api_key or None
        self.effective_date = None

    def fetch(self, base, quotes, on_date):
        self.effective_date = None
        # ECB only publishes EUR-base rates. If the caller wants
        # something else, derive by inverse cross via EUR.
        base = (base or '').upper()
        wanted = {q.upper() for q in quotes}
        if base == 'EUR':
            return self._fetch_eur_base(wanted, on_date)
        # Cross via EUR. We need EUR/<base> AND EUR/<each-quote>; then
        # rate(base->quote) = (EUR/quote) / (EUR/base).
        bridge = {base, *wanted}
        eur_rates = self._fetch_eur_base(bridge, on_date)
        if base not in eur_rates:
            raise RateProviderError(
                "ECB has no rate for base currency %s on %s; cannot "
                "derive cross rates." % (base, on_date.isoformat())
            )
        eur_to_base = eur_rates[base]
        out = {}
        for quote in wanted:
            if quote == base:
                continue
            if quote not in eur_rates:
                continue
            out[quote] = eur_rates[quote] / eur_to_base
        return ProviderRates(
            out, effective_date=eur_rates.effective_date,
        )

    def _fetch_eur_base(self, wanted, on_date):
        today = datetime.date.today()
        # Pick the right feed: the 90d feed has the daily series, the
        # daily feed has only the latest business-day rates. If
        # on_date is more than ~5 days old, go straight to the 90d feed.
        days_back = (today - on_date).days if on_date <= today else 0
        if days_back <= 5:
            xml_bytes = self._download(_ECB_DAILY_URL)
            rates = self._parse_latest_day(xml_bytes, wanted)
            if (
                rates
                and rates.effective_date
                and rates.effective_date <= on_date
            ):
                self.effective_date = rates.effective_date
                return rates
            # Empty response, or daily feed fixing after requested date:
            # fall through to dated history instead of mis-stamping it.
        xml_bytes = self._download(_ECB_90D_URL)
        rates = self._parse_dated(xml_bytes, wanted, on_date)
        self.effective_date = rates.effective_date
        return rates

    def _download(self, url):
        return http_get(url, self.timeout)

    @staticmethod
    def _parse_latest_day(xml_bytes, wanted):
        root = parse_xml_safely(xml_bytes, "ECB")
        # The daily feed has a single dated Cube; pull rates straight off.
        for cube in root.iter('{%s}Cube' % _NS['eurofxref']):
            if cube.get('time'):
                return ProviderRates(
                    _extract_rates(cube, wanted),
                    effective_date=cube.get('time'),
                )
        return ProviderRates()

    @staticmethod
    def _parse_dated(xml_bytes, wanted, on_date):
        root = parse_xml_safely(xml_bytes, "ECB")
        # The 90d feed has one dated Cube per business day, newest
        # first. Walk back from `on_date` until we find a published
        # day; ECB does not publish on weekends and on TARGET2
        # holidays.
        target = on_date.isoformat()
        candidates = []
        for cube in root.iter('{%s}Cube' % _NS['eurofxref']):
            t = cube.get('time')
            if t:
                candidates.append((t, cube))
        if not candidates:
            return ProviderRates()
        candidates.sort(reverse=True)
        for cube_date, cube in candidates:
            if cube_date <= target:
                return ProviderRates(
                    _extract_rates(cube, wanted),
                    effective_date=cube_date,
                )
        # Requested date is older than every entry in the 90d feed.
        # Falling all the way through means the user is asking for a
        # rate older than 90 days; the ECB historical feed (daily
        # since 1999) is a separate, much larger XML and is left as
        # a future enhancement.
        return ProviderRates()


def _extract_rates(cube, wanted):
    out = {}
    for child in cube.findall('{%s}Cube' % _NS['eurofxref']):
        ccy = child.get('currency')
        rate = child.get('rate')
        if not ccy or not rate:
            continue
        if wanted and ccy not in wanted:
            continue
        try:
            out[ccy] = Decimal(rate)
        except (InvalidOperation, TypeError):
            continue
    return out


# ---- Manual / no-op provider ----


class ManualRateProvider:
    """Operator-managed: this provider never fetches. Useful for
    offline test environments and shops where finance enters rates
    by hand. The Odoo cron is a no-op when this provider is selected.
    """
    name = 'manual'

    def __init__(self, timeout=10, api_key=None, **kwargs):
        self.timeout = timeout
        self.api_key = api_key or None

    def fetch(self, base, quotes, on_date):
        return ProviderRates()


register('ecb', EcbRateProvider, label="ECB (European Central Bank)")
register('manual', ManualRateProvider, label="Manual (no automatic fetch)")

# Extra HTTP providers register themselves on import. Kept at the very
# bottom so the registry, helpers, and BaseHttpProvider are all defined
# before the submodules import them.
from . import fx_sources  # noqa: E402,F401
