# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Tolerant JSON extraction for LLM responses.

LLMs frequently wrap JSON in prose or in ```json fences. These helpers
recover the first JSON object / array embedded in a string and never
raise: a response that cannot be parsed yields None / [] so the
capability layer falls back to its deterministic result.
"""

import json


def _strip_fences(text):
    text = (text or '').strip()
    if text.startswith('```'):
        # drop the opening fence line and any trailing fence
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _extract_span(text, open_ch, close_ch):
    """Return the substring spanning the first balanced open/close pair."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json_object(text):
    """Return the first JSON object as a dict, or None."""
    candidate = _strip_fences(text)
    if not candidate:
        return None
    span = _extract_span(candidate, '{', '}')
    if span is None:
        return None
    try:
        data = json.loads(span)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def extract_json_array(text):
    """Return the first JSON array as a list, or []."""
    candidate = _strip_fences(text)
    if not candidate:
        return []
    span = _extract_span(candidate, '[', ']')
    if span is None:
        return []
    try:
        data = json.loads(span)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []
