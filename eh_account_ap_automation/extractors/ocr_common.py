# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared, dependency-free helpers for the LLM/vision line-item extractors.

The provider-specific transport (OpenAI vision, Anthropic documents,
local endpoint) lives in the separate paid extractor packages. The
prompt, credential parsing, and JSON-to-ExtractedLine mapping are
identical across them and live here so the adapters stay thin. This
module imports nothing outside the standard library and this package,
so the core AP module keeps its provider-free footprint.
"""

import base64
import json
import math

from .base import ExtractedLine, ExtractorError


MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_CREDENTIAL_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_LINES = 1000

OCR_SYSTEM_PROMPT = (
    "You are an accounts-payable document parser. Extract the line "
    "items from the supplied vendor bill or invoice. Return ONLY a "
    "JSON array and nothing else. Each element is an object with keys: "
    "description (string), quantity (number), unit_price (number, "
    "pre-tax), line_total (number, pre-tax), tax_rate_pct (number or "
    "null), confidence (number between 0 and 1). Do not include "
    "headers, totals, or commentary; only the individual line items."
)


def build_user_text(hints):
    """Build the textual instruction, folding in advisory hints."""
    hints = hints or {}
    parts = ["Extract the line items from this vendor document."]
    parsed_total = hints.get('parsed_total')
    if parsed_total:
        parts.append(
            "The document total is approximately %s; prefer line "
            "totals that sum near it." % parsed_total
        )
    if hints.get('vendor_reference'):
        parts.append("Vendor reference: %s." % hints['vendor_reference'])
    return ' '.join(parts)


def build_openai_user_content(document_bytes, mime_type, hints):
    """Build OpenAI-compatible multimodal user content for the document.

    Text documents become a single text part; images become an
    image_url data URI; everything else (PDF) becomes an OpenAI file
    input. Used by both the hosted OpenAI and the local-endpoint
    extractors, which share the same wire format.
    """
    _validate_document(document_bytes)
    text = build_user_text(hints)
    mime = mime_type or 'application/octet-stream'
    if mime.startswith('text/'):
        document = (document_bytes or b'').decode('utf-8', 'replace')
        return [{'type': 'text',
                 'text': '%s\n\nDocument:\n%s' % (text, document)}]
    encoded = base64.b64encode(document_bytes or b'').decode('ascii')
    data_uri = 'data:%s;base64,%s' % (mime, encoded)
    if mime.startswith('image/'):
        return [
            {'type': 'text', 'text': text},
            {'type': 'image_url', 'image_url': {'url': data_uri}},
        ]
    return [
        {'type': 'text', 'text': text},
        {'type': 'file',
         'file': {'filename': 'document.pdf', 'file_data': data_uri}},
    ]


def build_anthropic_user_blocks(document_bytes, mime_type, hints):
    """Build Anthropic Messages content blocks for the document.

    Images use an image block; PDFs a document block; text is appended
    to the instruction text block.
    """
    _validate_document(document_bytes)
    text = build_user_text(hints)
    mime = mime_type or 'application/octet-stream'
    if mime.startswith('text/'):
        document = (document_bytes or b'').decode('utf-8', 'replace')
        return [{'type': 'text',
                 'text': '%s\n\nDocument:\n%s' % (text, document)}]
    encoded = base64.b64encode(document_bytes or b'').decode('ascii')
    if mime.startswith('image/'):
        block = {'type': 'image', 'source': {
            'type': 'base64', 'media_type': mime, 'data': encoded,
        }}
    else:
        block = {'type': 'document', 'source': {
            'type': 'base64', 'media_type': 'application/pdf', 'data': encoded,
        }}
    return [{'type': 'text', 'text': text}, block]


def parse_credentials(raw, required=()):
    """Coerce the credentials value to a dict and check required keys.

    :param raw: dict, JSON string, or None.
    :raises ExtractorError: invalid JSON, wrong type, or missing key.
    """
    if isinstance(raw, str) and len(raw.encode('utf-8')) > MAX_CREDENTIAL_BYTES:
        raise ExtractorError("OCR credentials exceed the 64 KiB safety limit.")
    if isinstance(raw, dict):
        data = raw
    elif raw is None:
        data = {}
    elif isinstance(raw, str):
        if not raw.strip():
            data = {}
        else:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ExtractorError(
                    "OCR credentials are not valid JSON: %s" % exc,
                )
    else:
        raise ExtractorError(
            "OCR credentials must be a dict or JSON string.",
        )
    if not isinstance(data, dict):
        raise ExtractorError("OCR credentials must be a JSON object.")
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ExtractorError(
            "OCR credentials missing required field(s): %s"
            % ', '.join(missing),
        )
    return data


def _validate_document(document_bytes):
    if not isinstance(document_bytes, (bytes, bytearray)):
        raise ExtractorError("OCR document payload must be bytes.")
    if len(document_bytes) > MAX_DOCUMENT_BYTES:
        raise ExtractorError("OCR document exceeds the 20 MiB safety limit.")


def _extract_json_array(text):
    if text is not None and not isinstance(text, str):
        return []
    candidate = (text or '').strip()
    if len(candidate.encode('utf-8')) > MAX_RESPONSE_BYTES:
        return []
    if candidate.startswith('```'):
        lines = candidate.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    start = candidate.find('[')
    if start == -1:
        return []
    depth = 0
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(candidate[start:i + 1])
                except (TypeError, ValueError):
                    return []
                if not isinstance(data, list) or len(data) > MAX_RESPONSE_LINES:
                    return []
                return data
    return []


def lines_from_text(raw):
    """Parse an LLM response into a list of ExtractedLine records.

    Tolerant: a response that is not a JSON array, or whose items are
    malformed, yields an empty list so the pipeline records a no-result
    run and falls back rather than crashing.
    """
    out = []
    for item in _extract_json_array(raw):
        if not isinstance(item, dict):
            continue
        description = str(item.get('description') or '').strip()
        try:
            quantity = float(item.get('quantity') or 0.0)
            unit_price = float(item.get('unit_price') or 0.0)
            line_total = float(
                item.get('line_total')
                if item.get('line_total') is not None
                else quantity * unit_price
            )
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (
                quantity, unit_price, line_total)):
            continue
        if not description and not line_total:
            continue
        tax = item.get('tax_rate_pct')
        try:
            tax = float(tax) if tax is not None else None
        except (TypeError, ValueError):
            tax = None
        if tax is not None and not math.isfinite(tax):
            tax = None
        confidence = item.get('confidence')
        try:
            confidence = float(confidence) if confidence is not None else 0.8
        except (TypeError, ValueError):
            confidence = 0.8
        if not math.isfinite(confidence):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        out.append(ExtractedLine(
            description=description or 'Line item',
            quantity=quantity,
            unit_price=unit_price,
            line_total=line_total,
            tax_rate_pct=tax,
            confidence=confidence,
            extra={'source': 'llm_ocr'},
        ))
    return out
