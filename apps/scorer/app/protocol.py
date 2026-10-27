"""WordHeat internal embedding protocol v1. See README for byte contract."""
import hashlib
import hmac
import json
import math
import re

import rfc8785

PATH = '/internal/v1/embeddings'
SIGNATURE_HEADER = 'x-wordheat-signature'


def canonical(value):
    return rfc8785.dumps(value)


def parse_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('NONFINITE_JSON')))


def sign(secret, direction, request_id, body, manifest_sha256, issued_at):
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError('BOOT_SECRET_REQUIRED')
    if direction not in ('request', 'response'):
        raise ValueError('INVALID_DIRECTION')
    message = canonical({'protocol': 'wordheat-embeddings/1', 'direction': direction,
                         'method': 'POST', 'path': PATH, 'request_id': request_id,
                         'body_sha256': hashlib.sha256(body).hexdigest(),
                         'manifest_sha256': manifest_sha256, 'issued_at': issued_at})
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify(secret, direction, request_id, body, manifest_sha256, signature, issued_at):
    return (isinstance(signature, str) and bool(re.fullmatch('[0-9a-f]{64}', signature))
            and hmac.compare_digest(sign(secret, direction, request_id, body, manifest_sha256, issued_at), signature))


def validate_vectors(vectors, count, dimension, normalize):
    if len(vectors) != count:
        raise ValueError('VECTOR_COUNT_MISMATCH')
    result = []
    for vector in vectors:
        if len(vector) != dimension:
            raise ValueError('VECTOR_DIMENSION_MISMATCH')
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise ValueError('VECTOR_NONFINITE')
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm <= 1e-12 or (normalize and abs(norm - 1.) > .001):
            raise ValueError('VECTOR_NORM_INVALID')
        result.append([float(value) for value in vector])
    return result
