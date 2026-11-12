"""Canonical UTF-8 JSON for normalized domain records."""

import json
from hashlib import sha256

from pydantic import BaseModel


def canonical_json(record: BaseModel) -> bytes:
    return json.dumps(
        record.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def fingerprint(record: BaseModel, *, prefix: bytes = b"") -> str:
    return sha256(prefix + canonical_json(record)).hexdigest()
