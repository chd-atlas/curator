"""The VariantValidator client, with no network: ``fetch`` is a function over the two real
responses copied into ``tests/fixtures/variantvalidator`` (taken 2026-11-17)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chd_curator import variantvalidator as vv
from chd_curator.variantvalidator import (
    Metadata,
    Placed,
    Refused,
    VariantValidatorError,
    place,
    url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "variantvalidator"
TBX5 = "NM_181486.4"
METADATA = Metadata(
    version="4.0.1.dev7+gbdab9c72f",
    hgvs_version="4.0.0",
    vvta="vvta_2025_02",
    vvdb="vvdb_2025_3",
    seqrepo="VV_SR_2025_02/master",
)


def _answer(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return loaded


def _fetch(answer: dict[str, Any]) -> Any:
    return lambda _url: json.dumps(answer).encode()


def test_url_is_the_url_only_json_form() -> None:
    assert url("NM_181486.4:c.835C>T", TBX5) == (
        "https://rest.variantvalidator.org/VariantValidator/variantvalidator/GRCh38/"
        "NM_181486.4%3Ac.835C%3ET/NM_181486.4?content-type=application/json"
    )


def test_place_reads_vvs_answer_for_tbx5_c835() -> None:
    asked: list[str] = []

    def fetch(request_url: str) -> bytes:
        asked.append(request_url)
        return (FIXTURES / "tbx5_c835.json").read_bytes()

    placed, metadata = place("NM_181486.4:c.835C>T", TBX5, fetch)

    assert placed == Placed("NM_181486.4:c.835C>T", "NP_852259.1:p.(Arg279Ter)", "12:114366312:G:A")
    assert metadata == METADATA
    assert asked == [url("NM_181486.4:c.835C>T", TBX5)]


def test_place_refuses_on_a_reference_mismatch() -> None:
    refused, metadata = place(
        "NM_181486.4:c.998G>T", TBX5, _fetch(_answer("tbx5_c998_warning.json"))
    )

    assert isinstance(refused, Refused)
    assert refused.warnings[0] == (
        "ReferenceMismatchError: NM_181486.4:c.998G>T: Variant reference (G) does not agree "
        "with reference sequence (C)"
    )
    assert len(refused.warnings) == 5
    assert metadata == METADATA


def test_place_refuses_a_wrong_transcript_a_non_gene_flag_and_a_missing_locus() -> None:
    other = _answer("tbx5_c835.json")
    other["NM_181486.4:c.835C>T"]["hgvs_transcript_variant"] = "NM_000192.4:c.835C>T"
    refused, _ = place("NM_181486.4:c.835C>T", TBX5, _fetch(other))
    assert isinstance(refused, Refused)
    assert "NM_000192.4:c.835C>T" in refused.warnings[0]

    flagged = _answer("tbx5_c835.json")
    flagged["flag"] = "intergenic"
    refused, _ = place("NM_181486.4:c.835C>T", TBX5, _fetch(flagged))
    assert refused == Refused(("VariantValidator flagged this 'intergenic'",))

    no_locus = _answer("tbx5_c835.json")
    no_locus["NM_181486.4:c.835C>T"]["primary_assembly_loci"] = {}
    refused, _ = place("NM_181486.4:c.835C>T", TBX5, _fetch(no_locus))
    assert refused == Refused(("VariantValidator gave no GRCh38 position",))


def test_a_warning_on_an_otherwise_good_gene_variant_refuses_it() -> None:
    warned = _answer("tbx5_c835.json")
    warned["NM_181486.4:c.835C>T"]["validation_warnings"] = ["a warning"]
    refused, _ = place("NM_181486.4:c.835C>T", TBX5, _fetch(warned))
    assert refused == Refused(("a warning",))


def test_an_empty_protein_consequence_is_none() -> None:
    answer = _answer("tbx5_c835.json")
    answer["NM_181486.4:c.835C>T"]["hgvs_predicted_protein_consequence"]["tlr"] = ""
    placed, _ = place("NM_181486.4:c.835C>T", TBX5, _fetch(answer))
    assert isinstance(placed, Placed)
    assert placed.hgvs_p is None


@pytest.mark.parametrize("body", [b"not json", b"[1]", b"{}"])
def test_an_unusable_answer_raises(body: bytes) -> None:
    with pytest.raises(VariantValidatorError):
        place("NM_181486.4:c.835C>T", TBX5, lambda _url: body)


def test_the_throttle_spaces_requests_one_second_apart() -> None:
    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = vv._Throttle(lambda: now[0], sleep)
    throttle.wait()  # the first request waits for nothing
    now[0] += 0.25
    throttle.wait()
    now[0] += 3.0
    throttle.wait()  # already more than a second since the last: no wait
    assert slept == [pytest.approx(0.75)]
    assert vv._Throttle.GAP == 1.0
