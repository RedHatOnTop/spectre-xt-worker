"""Consume bounded renderer replies only outside an assignment's paste frame."""
import re

CSI = re.compile(r'\x1b\[(?:[?>][0-9;]*[cu]|[0-9]+;[0-9]+R)')
COLOR = re.compile(r'\x1b\](?:10|11);rgb:[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}/[0-9a-fA-F]{1,4}(?:\x07|\x1b\\)')
PREFIXES = ('\x1b[', '\x1b]10;', '\x1b]11;')
MAX_REPLY_BYTES = 128


def length(text):
    if not text.startswith('\x1b'):
        return 0
    for pattern in (CSI, COLOR):
        match = pattern.match(text)
        if match:
            return match.end()
    if len(text) > MAX_REPLY_BYTES:
        return 0
    if any(prefix.startswith(text) for prefix in PREFIXES):
        return -1
    if text.startswith('\x1b[') and re.fullmatch(r'\x1b\[[?>]?[0-9;]*', text):
        return -1
    if text.startswith(('\x1b]10;', '\x1b]11;')) and '\x07' not in text and '\x1b\\' not in text:
        return -1
    return 0
