# -*- encoding: utf-8 -*-
##############################################################################
#
# ERP Heritage
# Copyright (C) 2026 (https://www.erpheritage.com.au/)
#
##############################################################################
"""
Shared OpenAI-compatible chat-completion call.

Both the hosted OpenAI provider and the local-endpoint provider speak
the same /chat/completions wire format, so the request building and
response unwrapping live here once. Anthropic's Messages API differs
and is handled in its own provider module.
"""

from . import llm_http
from .provider_registry import ProviderError


def openai_chat_completion(base_url, api_key, model, messages,
                           max_tokens=None, temperature=None, timeout=30,
                           allow_private=False):
    """Call an OpenAI-compatible /chat/completions endpoint.

    :param base_url: API root, e.g. 'https://api.openai.com/v1' or a
        local 'http://127.0.0.1:11434/v1'.
    :param api_key: bearer token, or falsy for keyless local servers.
    :param model: model identifier.
    :param messages: list of {'role', 'content'} dicts.
    :returns: the assistant message content string.
    :raises ProviderError: on transport error or unexpected shape.
    """
    url = base_url.rstrip('/') + '/chat/completions'
    headers = {}
    if api_key:
        headers['Authorization'] = 'Bearer %s' % api_key
    payload = {'model': model, 'messages': messages}
    if max_tokens:
        payload['max_tokens'] = max_tokens
    if temperature is not None:
        payload['temperature'] = temperature
    transport_options = {}
    if allow_private:
        transport_options['allow_private'] = True
    data = llm_http.http_post_json(
        url, payload, headers, timeout=timeout, **transport_options,
    )
    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(
            "OpenAI-compatible response missing choices/message "
            "content: %s" % exc,
        )
    if not isinstance(content, str):
        raise ProviderError(
            "OpenAI-compatible response content was not text.",
        )
    return content
