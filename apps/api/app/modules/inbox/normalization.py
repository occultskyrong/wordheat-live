import unicodedata
from app.errors import DomainError


def normalize_word(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).strip()
    def is_han(char):
        code = ord(char)
        return (0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF
                or 0x20000 <= code <= 0x2EBEF or 0x30000 <= code <= 0x323AF)
    if not 2 <= len(value) <= 4 or len(value.encode()) > 32 or not all(map(is_han, value)):
        raise DomainError('INPUT_INVALID', 'Expected one 2–4 Han word', 422)
    return value


def parse_official_guess(text):
    """Split instruction/code before word normalization; ordinary chat is ignored."""
    import re
    if not isinstance(text,str) or not text.startswith('猜'): return None
    match=re.fullmatch(r'猜\s*([A-Z0-9]{2,4})\s*(.+)',text)
    if not match: raise DomainError('INPUT_INVALID','Expected guess instruction and round code',422)
    return match.group(1),normalize_word(match.group(2))
