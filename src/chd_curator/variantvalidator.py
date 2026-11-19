"""A VariantValidator REST client: place one ``c.`` description on the MANE Select transcript.

``place`` answers either `Placed` (the MANE ``c.``, the predicted ``p.`` and the GRCh38 VCF key)
or `Refused` (what VariantValidator warned, and the description it was asked). A description on
the MANE transcript is one request; one on another transcript is two (ruling R-F9, see
``place``). Each response is one object holding the description as a key (or
``validation_warning_N`` when it refuses), plus ``flag`` and ``metadata``; fixtures of both live
in ``tests/fixtures/variantvalidator``, copied from real responses of 2026-11-17 and 2026-11-19.

A variant is refused unless ``flag`` is ``gene_variant``, no warning came back apart from the
two tolerated ones, and the returned ``hgvs_transcript_variant`` is on the transcript asked for:
an answer on another transcript would otherwise be written as if it were MANE's. Tolerated: a
TranscriptVersionWarning at step one of the two-step path, and (ruling R-F10) the exact
"Removing redundant reference bases" syntax warning, when the bases a ``c.…del<bases>`` lists
are the deletion in the returned VCF. VariantValidator itself refuses a wrong listing (measured
2026-11-19: a ReferenceMismatchError), so the second check is a guard on what curator writes.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
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
    # The description VariantValidator was asked when it refused; the genomic one at step 2.
    asked: str | None = field(default=None, compare=False)


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
    for name, key in _META_KEYS.items():
        value = meta.get(key)
        if not isinstance(value, str) or not value:
            raise VariantValidatorError(f"VariantValidator's metadata has no {key}")
        values[name] = value
    return Metadata(**values)


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(str(item) for item in value) if isinstance(value, list) else ()


_VERSION_WARNING: Final = "TranscriptVersionWarning:"
_REDUNDANT_BASES: Final = (
    "VariantSyntaxError: Removing redundant reference bases from variant description"
)
_LISTED_BASES: Final = re.compile(r"del([ACGTacgt]+)")


def _ask(
    description: str, select: str, fetch: Fetch
) -> tuple[dict[str, Any], dict[str, Any] | None, Metadata]:
    """One request: the answer, its single description entry and its metadata.

    The entry is None for an ``empty_result`` with no description at all, which the caller
    refuses for that variant; any other count is a failure of the run.
    """
    raw = fetch(url(description, select))
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
    if not entries and answer.get("flag") == "empty_result":
        return answer, None, metadata
    if len(entries) != 1 or not isinstance(entries[0], dict):
        raise VariantValidatorError(
            f"VariantValidator's answer holds {len(entries)} descriptions, not one"
        )
    return answer, entries[0], metadata


def _locus(entry: dict[str, Any]) -> dict[str, Any] | None:
    grch38 = (entry.get("primary_assembly_loci") or {}).get("grch38")
    return grch38 if isinstance(grch38, dict) else None


def _vcf(entry: dict[str, Any]) -> str | None:
    try:
        vcf = entry["primary_assembly_loci"]["grch38"]["vcf"]
        return f"{vcf['chr']}:{vcf['pos']}:{vcf['ref']}:{vcf['alt']}"
    except (KeyError, TypeError):
        return None


def _listed_bases_match(description: str, vcf: str | None) -> bool:
    """Whether the bases a ``c.…del<bases>`` lists are the ones the VCF deletes.

    VariantValidator itself checks listed bases against the reference (a wrong listing comes
    back with a ReferenceMismatchError); this checks them again against the VCF it returned. The
    deleted bases are the VCF's REF without its anchor base, or a rotation of them, because
    VariantValidator shifts a deletion within a repeat and the paper may have shifted it the
    other way. A VCF that is not a pure deletion cannot be checked, so it is not a match.
    """
    listed = _LISTED_BASES.search(description.split(":", 1)[-1])
    if listed is None or vcf is None:
        return False
    _, _, ref, alt = vcf.split(":")
    if len(alt) != 1 or not ref.startswith(alt) or len(ref) < 2:
        return False
    deleted, bases = ref[1:].upper(), listed[1].upper()
    return len(bases) == len(deleted) and bases in deleted + deleted


def _read(
    answer: dict[str, Any],
    entry: dict[str, Any] | None,
    transcript: str,
    asked: str,
    tolerate_version: bool = False,
    tolerate_automap: bool = False,
) -> Placed | Refused:
    """The entry as `Placed` on `transcript`, or `Refused`.

    A TranscriptVersionWarning is tolerated only when `tolerate_version`. The redundant-bases
    syntax warning is tolerated only when `asked` lists bases that match the returned VCF. With
    `tolerate_automap` (R-F11), so is exactly ``<asked, listed bases dropped> automapped to
    <the entry's own hgvs_transcript_variant>``: VariantValidator's 3' shift of the same variant.
    """
    if entry is None:
        return Refused(("VariantValidator returned no description (empty_result)",), asked)
    vcf = _vcf(entry)
    own = entry.get("hgvs_transcript_variant")
    automapped = (
        f"{_LISTED_BASES.sub('del', asked)} automapped to {own}"
        if tolerate_automap and isinstance(own, str) and own
        else None
    )
    blocking = []
    for warning in _strings(entry.get("validation_warnings")):
        if tolerate_version and warning.startswith(_VERSION_WARNING):
            continue
        if warning == automapped:
            continue
        if warning == _REDUNDANT_BASES and _listed_bases_match(asked, vcf):
            continue
        blocking.append(warning)
    flag = answer.get("flag")
    if flag != "gene_variant" or blocking:
        return Refused(tuple(blocking) or (f"VariantValidator flagged this {flag!r}",), asked)
    hgvs_c = entry.get("hgvs_transcript_variant")
    if not isinstance(hgvs_c, str) or hgvs_c.split(":", 1)[0] != transcript:
        return Refused((f"VariantValidator answered {hgvs_c!r}, not on {transcript}",), asked)
    if vcf is None:
        return Refused(("VariantValidator gave no GRCh38 position",), asked)
    protein = entry.get("hgvs_predicted_protein_consequence")
    tlr = protein.get("tlr") if isinstance(protein, dict) else None
    return Placed(hgvs_c, tlr if isinstance(tlr, str) and tlr else None, vcf)


def place(description: str, transcript: str, fetch: Fetch) -> tuple[Placed | Refused, Metadata]:
    """Place `description` on `transcript`, the MANE Select (versioned accession).

    A description on the MANE transcript is one request. A description on another transcript
    (the paper's own, ruling R-F9) is asked twice: on that transcript, where VariantValidator
    reads it and gives the GRCh38 position; then the genomic variant, as the answer's own
    ``hgvs_genomic_description``, on MANE, which gives the MANE `c.` and `p.`. (The VCF key
    would do for a substitution, but measured on one deletion and one duplication, VariantValidator
    answered it with an "automapped" warning.)
    The two positions must agree and the two answers must come from the same VariantValidator
    and databases. A refusal names the description it was asked, so a reader can tell the steps
    apart.
    """
    accession = description.split(":", 1)[0]
    if accession == transcript:
        answer, entry, metadata = _ask(description, transcript, fetch)
        return _read(answer, entry, transcript, description, tolerate_automap=True), metadata
    answer, entry, metadata = _ask(description, accession, fetch)
    first = _read(
        answer, entry, accession, description, tolerate_version=True, tolerate_automap=True
    )
    if not isinstance(first, Placed):
        return first, metadata
    locus = _locus(entry or {})
    genomic = locus.get("hgvs_genomic_description") if locus else None
    if not isinstance(genomic, str) or not genomic:
        return Refused(("VariantValidator gave no genomic description",), description), metadata
    answer2, entry2, metadata2 = _ask(genomic, transcript, fetch)
    if metadata2 != metadata:
        raise VariantValidatorError(
            "VariantValidator's versions changed between the two requests for one variant"
        )
    second = _read(answer2, entry2, transcript, genomic)
    if isinstance(second, Placed) and second.grch38 != first.grch38:
        return Refused(
            (
                f"VariantValidator placed {description} at {first.grch38} but the genomic "
                f"variant on {transcript} at {second.grch38}",
            ),
            genomic,
        ), metadata
    return second, metadata
