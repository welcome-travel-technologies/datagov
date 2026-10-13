import json

from django.conf import settings
from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser


class BoundedJSONParser(BaseParser):
    media_type = 'application/json'

    def parse(self, stream, media_type=None, parser_context=None):
        limit = settings.DOCUMENT_EXPLORER_MAX_RESULT_BYTES
        payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ParseError('The document service payload exceeds its configured limit.')
        try:
            result = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise ParseError('Invalid JSON.')
        if not isinstance(result, dict):
            raise ParseError('Expected a JSON object.')
        return result
