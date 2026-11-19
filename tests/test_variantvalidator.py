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


class _Response:
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


def test_every_real_request_waits_on_the_throttle_and_failures_name_no_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.error
    import urllib.request

    waits: list[str] = []

    class Recorder:
        def wait(self) -> None:
            waits.append("wait")

    monkeypatch.setattr(vv, "_throttle", Recorder())
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Response())
    assert vv.urllib_fetch("https://example.invalid/x") == b"{}"
    assert waits == ["wait"]

    def refuse(*_a: object, **_k: object) -> None:
        raise urllib.error.URLError("secret-host")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(VariantValidatorError, match="could not reach VariantValidator: URLError"):
        vv.urllib_fetch("https://example.invalid/x")
    assert waits == ["wait", "wait"]


NR2F2 = "NM_021005.4"
OLDER = "NM_021005.3"
STEP1 = "nr2f2_v3_c288.json"
STEP2 = "nr2f2_genomic_on_mane.json"
GENOMIC = "15-96332393-C-A"


class _Fake:
    """A fetch answering by URL from named answers, counting every request."""

    def __init__(self, step1: dict[str, Any], step2: dict[str, Any]) -> None:
        self.by_url = {
            url(f"{OLDER}:c.288C>A", OLDER): step1,
            url(GENOMIC, NR2F2): step2,
        }
        self.asked: list[str] = []

    def __call__(self, request_url: str) -> bytes:
        self.asked.append(request_url)
        if request_url not in self.by_url:
            raise AssertionError(f"unexpected request {request_url}")
        return json.dumps(self.by_url[request_url]).encode()


def _fake() -> _Fake:
    return _Fake(_answer(STEP1), _answer(STEP2))


def test_an_older_version_is_placed_on_mane_through_its_genomic_position() -> None:
    fake = _fake()

    placed, metadata = place(f"{OLDER}:c.288C>A", NR2F2, fake)

    assert placed == Placed("NM_021005.4:c.288C>A", "NP_066285.1:p.(Cys96Ter)", "15:96332393:C:A")
    assert metadata == METADATA
    assert fake.asked == [url(f"{OLDER}:c.288C>A", OLDER), url(GENOMIC, NR2F2)]  # two requests


def test_the_mane_path_is_one_request() -> None:
    asked: list[str] = []

    def fetch(request_url: str) -> bytes:
        asked.append(request_url)
        return (FIXTURES / "tbx5_c835.json").read_bytes()

    place("NM_181486.4:c.835C>T", TBX5, fetch)

    assert len(asked) == 1


def test_a_reference_mismatch_on_the_papers_transcript_is_refused_after_one_request() -> None:
    asked: list[str] = []

    def fetch(request_url: str) -> bytes:
        asked.append(request_url)
        return (FIXTURES / "tbx5_v3_c998_warning.json").read_bytes()

    refused, _ = place("NM_000192.3:c.998G>T", TBX5, fetch)

    assert isinstance(refused, Refused)
    assert refused.warnings[0].startswith("ReferenceMismatchError:")
    assert len(asked) == 1


def test_only_a_transcript_version_warning_is_tolerated_and_only_at_step_one() -> None:
    only = _fake()
    assert only.by_url[url(f"{OLDER}:c.288C>A", OLDER)]["NM_021005.3:c.288C>A"][
        "validation_warnings"
    ] == [
        "TranscriptVersionWarning: A more recent version of the selected reference sequence "
        "NM_021005.3 is available for genome build GRCh38 (NM_021005.4)"
    ]
    assert isinstance(place(f"{OLDER}:c.288C>A", NR2F2, only)[0], Placed)

    extra = _fake()
    entry = extra.by_url[url(f"{OLDER}:c.288C>A", OLDER)]["NM_021005.3:c.288C>A"]
    entry["validation_warnings"].append("SomethingElse: a second warning")
    refused, _ = place(f"{OLDER}:c.288C>A", NR2F2, extra)
    assert refused == Refused(("SomethingElse: a second warning",))
    assert len(extra.asked) == 1

    late = _fake()
    late.by_url[url(GENOMIC, NR2F2)]["NM_021005.4:c.288C>A"]["validation_warnings"] = [
        "TranscriptVersionWarning: tolerated only at step one"
    ]
    refused, _ = place(f"{OLDER}:c.288C>A", NR2F2, late)
    assert refused == Refused(("TranscriptVersionWarning: tolerated only at step one",))

    prefixed = _fake()
    entry = prefixed.by_url[url(f"{OLDER}:c.288C>A", OLDER)]["NM_021005.3:c.288C>A"]
    entry["validation_warnings"] = ["Not a TranscriptVersionWarning: only a prefix counts"]
    assert isinstance(place(f"{OLDER}:c.288C>A", NR2F2, prefixed)[0], Refused)


def test_metadata_that_differs_between_the_steps_raises() -> None:
    fake = _fake()
    fake.by_url[url(GENOMIC, NR2F2)]["metadata"]["vvta_version"] = "vvta_2099_01"

    with pytest.raises(VariantValidatorError, match="versions changed"):
        place(f"{OLDER}:c.288C>A", NR2F2, fake)


def test_a_step_two_answer_off_mane_or_at_another_position_is_refused() -> None:
    off = _fake()
    off.by_url[url(GENOMIC, NR2F2)]["NM_021005.4:c.288C>A"]["hgvs_transcript_variant"] = (
        "NM_021005.3:c.288C>A"
    )
    refused, _ = place(f"{OLDER}:c.288C>A", NR2F2, off)
    assert isinstance(refused, Refused) and "not on NM_021005.4" in refused.warnings[0]

    moved = _fake()
    vcf = moved.by_url[url(GENOMIC, NR2F2)]["NM_021005.4:c.288C>A"]["primary_assembly_loci"][
        "grch38"
    ]["vcf"]
    vcf["pos"] = "96332394"
    refused, _ = place(f"{OLDER}:c.288C>A", NR2F2, moved)
    assert isinstance(refused, Refused)
    assert "15:96332393:C:A" in refused.warnings[0] and "15:96332394:C:A" in refused.warnings[0]
