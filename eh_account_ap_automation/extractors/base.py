# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Vendor-bill line-item extractor base class.

The default AP automation pipeline reads supplier, total, due date with
a textbook regex parser. That covers the headers but not per-line data
(item description, quantity, unit price, line total, tax). This module
defines the extractor contract that a localization or partner package
plugs into to add per-line extraction.

Three reasons we ship the contract here rather than a default
implementation:

* Per-line extraction at production accuracy needs OCR plus an LLM /
  layout model. Both come with operational cost (per-call billing or
  on-prem GPU) and a vendor-data flow that is the deployment's call,
  not ours.
* Different jurisdictions read different document conventions (tax
  invoices, GST/VAT layouts, line-by-line vs schedule-attached). One
  default extractor would be wrong for many users.
* Treating it as a pluggable adapter keeps the suite licensable as
  LGPL-3: a deployment ships its own commercial extractor without
  forking ours.

Adapters subclass `LineItemExtractor`, set `EXTRACTOR_KEY` and
`EXTRACTOR_LABEL`, implement `extract(document_bytes, mime_type,
hints)`, and register via `register_extractor(...)` at import time.

The pipeline uses the extractor only when the user opts into a profile
that names a registered extractor key; absent any registered extractor,
the existing regex-only path runs unchanged.
"""

import dataclasses
from typing import Iterable, Optional


@dataclasses.dataclass
class ExtractedLine:
    """One extracted line from a vendor bill.

    Field semantics:

    * `description` -- human-readable line label (product or service).
    * `quantity` -- numeric; no unit conversion is performed.
    * `unit_price` -- pre-tax unit price.
    * `line_total` -- pre-tax total; the framework cross-checks
      against quantity * unit_price and discards the line if the
      mismatch exceeds the configured tolerance.
    * `tax_rate_pct` -- percentage as a number (10.0 for 10%); None
      means "tax not extracted, fall back to vendor default".
    * `confidence` -- 0.0 to 1.0; the framework rejects extractions
      below the confidence_threshold configured on the profile and
      surfaces them for manual review.
    * `extra` -- dict of model-specific metadata (page index, bbox,
      raw token spans). Persisted on the intake line for traceability
      but not used for matching.
    """

    description: str
    quantity: float
    unit_price: float
    line_total: float
    tax_rate_pct: Optional[float] = None
    confidence: float = 0.0
    extra: Optional[dict] = None


class ExtractorError(RuntimeError):
    """Raised when an extractor cannot process a document.

    The pipeline catches this per intake and records the message on
    the intake's chatter; the intake stays in the manual review queue
    rather than being silently dropped.
    """


class LineItemExtractor:
    """Base class for vendor-bill line-item extractors.

    The framework treats every extractor identically. Concrete
    implementations are responsible for whatever they call internally
    (OCR, LLM inference, classical layout analysis); the contract
    only requires that `extract(...)` returns an iterable of
    ExtractedLine records or raises ExtractorError.
    """

    EXTRACTOR_KEY = ''
    EXTRACTOR_LABEL = ''

    # If the extractor needs the document re-rendered to images first
    # (most layout models do), set this to True. The framework uses
    # this hint to decide whether to call a PDF-to-image preprocessor
    # before handing bytes to the extractor.
    REQUIRES_RASTERISATION = False

    def extract(
        self, document_bytes: bytes, mime_type: str,
        hints: Optional[dict] = None,
    ) -> Iterable[ExtractedLine]:
        """Return an iterable of ExtractedLine records.

        `hints` carries optional context the framework collects from the
        intake itself: vendor id, expected currency, the regex parser's
        own header read (so the extractor can prefer line totals that
        sum near the parsed total). All hints are advisory; an
        extractor that ignores them is still valid.
        """
        raise NotImplementedError(
            "%s must implement extract()" % type(self).__name__,
        )
