"""A VariantValidator REST client: place one ``c.`` description on the MANE Select transcript.

``place`` asks for the description on one transcript and answers either `Placed` (the
normalised ``c.``, the predicted ``p.`` and the GRCh38 VCF key) or `Refused` (what
VariantValidator warned). The response is one object holding the description as a key (or
``validation_warning_N`` when it refuses), plus ``flag`` and ``metadata``; fixtures of both
live in ``tests/fixtures/variantvalidator``, copied from real responses of 2026-11-17.

A variant is refused unless ``flag`` is ``gene_variant``, no warning came back, and the
returned ``hgvs_transcript_variant`` is on the asked-for transcript: an answer on another
transcript would otherwise be written as if it were MANE's.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

BASE: Final = "https://rest.variantvalidator.org/VariantValidator/variantvalidator/GRCh38"
TIMEOUT_SECONDS: Final = 60
_META_KEYS: Final = {
    "version": "variantvalidator_version",
    "hgvs_version": "variantvalidator_hgvs_version",
    "vvta": "vvta_version",
    "vvdb": "vvdb_version",
    "seqrepo": "vvseqrepo_db",
}

Fetch = Callable[[str], bytes]


class VariantValidatorError(Exception):
    """VariantValidator could not be reached or its answer was not usable (exit 1 at the CLI)."""


@dataclass(frozen=True)
class Placed:
    hgvs_c: str
    hgvs_p: str | None
    grch38: str


@dataclass(frozen=True)
class Refused:
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class Metadata:
    version: str
    hgvs_version: str
    vvta: str
    vvdb: str
    seqrepo: str


def url(description: str, transcript: str) -> str:
    return f"{BASE}/{quote(description, safe='')}/{transcript}?content-type=application/json"


class _Throttle:
    """Spaces requests: VariantValidator asks for at most one a second."""

    GAP: Final = 1.0

    def __init__(self, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self._last + self.GAP - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


_throttle = _Throttle(time.monotonic, time.sleep)


def urllib_fetch(request_url: str) -> bytes:
    """GET `request_url` with a timeout, at most one request a second."""
    _throttle.wait()
    try:
        with urllib.request.urlopen(request_url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            body: bytes = response.read()
            return body
    except urllib.error.HTTPError as exc:
        raise VariantValidatorError(
            f"VariantValidator answered HTTP {exc.code} {exc.reason}"
        ) from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise VariantValidatorError(
            f"could not reach VariantValidator: {type(exc).__name__}"
        ) from None


def _metadata(answer: dict[str, Any]) -> Metadata:
    meta = answer.get("metadata")
    if not isinstance(meta, dict):
        raise VariantValidatorError("VariantValidator's answer has no metadata")
    values: dict[str, str] = {}
    for field, key in _META_KEYS.items():
        value = meta.get(key)
        if not isinstance(value, str) or not value:
            raise VariantValidatorError(f"VariantValidator's metadata has no {key}")
        values[field] = value
    return Metadata(**values)


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def place(description: str, transcript: str, fetch: Fetch) -> tuple[Placed | Refused, Metadata]:
    """Ask VariantValidator for `description` on `transcript` (versioned accession)."""
    raw = fetch(url(description, transcript))
    try:
        answer = json.loads(raw)
    except ValueError as exc:
        raise VariantValidatorError(
            f"VariantValidator's answer is not JSON: {raw[:200]!r}"
        ) from exc
    if not isinstance(answer, dict):
        raise VariantValidatorError("VariantValidator's answer is not a JSON object")
    metadata = _metadata(answer)
    entries = [v for k, v in sorted(answer.items()) if k not in ("flag", "metadata")]
    if len(entries) != 1 or not isinstance(entries[0], dict):
        raise VariantValidatorError(
            f"VariantValidator's answer holds {len(entries)} descriptions, not one"
        )
    entry = entries[0]
    warnings = _strings(entry.get("validation_warnings"))
    flag = answer.get("flag")
    if flag != "gene_variant" or warnings:
        return Refused(warnings or (f"VariantValidator flagged this {flag!r}",)), metadata
    hgvs_c = entry.get("hgvs_transcript_variant")
    if not isinstance(hgvs_c, str) or hgvs_c.split(":", 1)[0] != transcript:
        return Refused((f"VariantValidator answered {hgvs_c!r}, not on {transcript}",)), metadata
    try:
        vcf = entry["primary_assembly_loci"]["grch38"]["vcf"]
        grch38 = f"{vcf['chr']}:{vcf['pos']}:{vcf['ref']}:{vcf['alt']}"
    except (KeyError, TypeError):
        return Refused(("VariantValidator gave no GRCh38 position",)), metadata
    protein = entry.get("hgvs_predicted_protein_consequence")
    tlr = protein.get("tlr") if isinstance(protein, dict) else None
    return Placed(hgvs_c, tlr if isinstance(tlr, str) and tlr else None, grch38), metadata
