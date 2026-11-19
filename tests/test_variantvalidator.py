"""The VariantValidator client, with no network: ``fetch`` is a function over the real
responses copied into ``tests/fixtures/variantvalidator`` (taken 2026-11-17 and 2026-11-19), or
over a copy of one with a field changed. ``nr2f2_*`` are the paper-on-another-transcript
answers: step one on NM_021005.3, step two on MANE NM_021005.4."""

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
    url,
)
from chd_curator.variantvalidator import place as _place

FIXTURES = Path(__file__).parent / "fixtures" / "variantvalidator"
TBX5 = "NM_181486.4"
METADATA = Metadata(
    version="4.0.1.dev7+gbdab9c72f",
    hgvs_version="4.0.0",
    vvta="vvta_2025_02",
    vvdb="vvdb_2025_3",
    seqrepo="VV_SR_2025_02/master",
)


def place(description: str, transcript: str, fetch: Any, strand: str | None = None) -> Any:
    """`variantvalidator.place` on the gene's real strand: TBX5 is minus, NR2F2 plus."""
    return _place(description, transcript, fetch, strand or ("-" if transcript == TBX5 else "+"))


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
GENOMIC = "NC_000015.10:g.96332393C>A"
SNV = f"{OLDER}:c.288C>A"
DEL_GENOMIC = "NC_000015.10:g.96332202_96332208del"
DEL = f"{OLDER}:c.97_103delCCGCCCG"
DEL_ENTRY = f"{OLDER}:c.97_103del"
REDUNDANT = "VariantSyntaxError: Removing redundant reference bases from variant description"


class _Fake:
    """A fetch answering by URL from named answers, counting every request."""

    def __init__(
        self, description: str, step1: dict[str, Any], genomic: str, step2: dict[str, Any]
    ):
        self.by_url = {url(description, OLDER): step1, url(genomic, NR2F2): step2}
        self.asked: list[str] = []

    def __call__(self, request_url: str) -> bytes:
        self.asked.append(request_url)
        if request_url not in self.by_url:
            raise AssertionError(f"unexpected request {request_url}")
        return json.dumps(self.by_url[request_url]).encode()


def _fake() -> _Fake:
    return _Fake(SNV, _answer(STEP1), GENOMIC, _answer(STEP2))


def _del_fake(description: str = DEL) -> _Fake:
    return _Fake(
        description,
        _answer("nr2f2_v3_del_c97_103.json"),
        DEL_GENOMIC,
        _answer("nr2f2_genomic_del_on_mane.json"),
    )


def test_an_older_version_is_placed_on_mane_through_its_genomic_position() -> None:
    fake = _fake()

    placed, metadata = place(SNV, NR2F2, fake)

    assert placed == Placed("NM_021005.4:c.288C>A", "NP_066285.1:p.(Cys96Ter)", "15:96332393:C:A")
    assert metadata == METADATA
    assert fake.asked == [url(SNV, OLDER), url(GENOMIC, NR2F2)]  # two requests


def test_the_protein_written_is_the_mane_answers_not_the_papers_transcripts() -> None:
    fake = _fake()
    step1 = fake.by_url[url(SNV, OLDER)][SNV]
    step1["hgvs_predicted_protein_consequence"]["tlr"] = "NP_066285.0:p.(Cys96Tyr)"

    placed, _ = place(SNV, NR2F2, fake)

    assert isinstance(placed, Placed)
    assert placed.hgvs_p == "NP_066285.1:p.(Cys96Ter)"


def test_an_indel_on_an_older_version_is_placed_through_its_genomic_description() -> None:
    """Measured on one deletion: asked as a VCF key it drew an "automapped" warning; asked as
    the NC_ g. description it did not."""
    fake = _del_fake()

    placed, _ = place(DEL, NR2F2, fake)

    assert placed == Placed(
        "NM_021005.4:c.97_103del", "NP_066285.1:p.(Pro33AlafsTer77)", "15:96332196:GGCCCGCC:G"
    )
    assert fake.asked == [url(DEL, OLDER), url(DEL_GENOMIC, NR2F2)]


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
    assert refused.asked == "NM_000192.3:c.998G>T"
    assert len(asked) == 1


def test_an_unversioned_accession_is_refused_for_that_variant() -> None:
    """Measured 2026-11-19: VariantValidator answers a warning entry, not an empty result."""
    refused, _ = place(
        "NM_000192:c.510+1G>A", TBX5, lambda _u: (FIXTURES / "tbx5_unversioned.json").read_bytes()
    )

    assert isinstance(refused, Refused)
    assert refused.warnings[0] == "RefSeq variant accession numbers MUST include a version number"


def test_only_a_transcript_version_warning_is_tolerated_and_only_at_step_one() -> None:
    only = _fake()
    assert only.by_url[url(SNV, OLDER)][SNV]["validation_warnings"] == [
        "TranscriptVersionWarning: A more recent version of the selected reference sequence "
        "NM_021005.3 is available for genome build GRCh38 (NM_021005.4)"
    ]
    assert isinstance(place(SNV, NR2F2, only)[0], Placed)

    extra = _fake()
    extra.by_url[url(SNV, OLDER)][SNV]["validation_warnings"].append("SomethingElse: a warning")
    refused, _ = place(SNV, NR2F2, extra)
    assert refused == Refused(("SomethingElse: a warning",))
    assert len(extra.asked) == 1

    late = _fake()
    late.by_url[url(GENOMIC, NR2F2)]["NM_021005.4:c.288C>A"]["validation_warnings"] = [
        "TranscriptVersionWarning: tolerated only at step one"
    ]
    refused, _ = place(SNV, NR2F2, late)
    assert refused == Refused(("TranscriptVersionWarning: tolerated only at step one",))
    assert refused.asked == GENOMIC  # the note can say which step refused

    prefixed = _fake()
    prefixed.by_url[url(SNV, OLDER)][SNV]["validation_warnings"] = [
        "Not a TranscriptVersionWarning: only a prefix counts"
    ]
    assert isinstance(place(SNV, NR2F2, prefixed)[0], Refused)


def test_metadata_that_differs_between_the_steps_raises() -> None:
    fake = _fake()
    fake.by_url[url(GENOMIC, NR2F2)]["metadata"]["vvta_version"] = "vvta_2099_01"

    with pytest.raises(VariantValidatorError, match="versions changed"):
        place(SNV, NR2F2, fake)


def test_a_step_two_answer_off_mane_or_at_another_position_is_refused() -> None:
    off = _fake()
    off.by_url[url(GENOMIC, NR2F2)]["NM_021005.4:c.288C>A"]["hgvs_transcript_variant"] = (
        "NM_021005.3:c.288C>A"
    )
    refused, _ = place(SNV, NR2F2, off)
    assert isinstance(refused, Refused) and "not on NM_021005.4" in refused.warnings[0]

    moved = _fake()
    entry = moved.by_url[url(GENOMIC, NR2F2)]["NM_021005.4:c.288C>A"]
    entry["primary_assembly_loci"]["grch38"]["vcf"]["pos"] = "96332394"
    refused, _ = place(SNV, NR2F2, moved)
    assert isinstance(refused, Refused)
    assert "15:96332393:C:A" in refused.warnings[0] and "15:96332394:C:A" in refused.warnings[0]


def test_an_empty_result_or_a_missing_genomic_description_refuses_that_variant_only() -> None:
    empty = _fake()
    empty.by_url[url(GENOMIC, NR2F2)] = {
        "flag": "empty_result",
        "metadata": _answer(STEP2)["metadata"],
    }
    refused, _ = place(SNV, NR2F2, empty)
    assert isinstance(refused, Refused) and "empty_result" in refused.warnings[0]
    assert refused.asked == GENOMIC

    first = _fake()
    first.by_url[url(SNV, OLDER)] = {"flag": "empty_result", "metadata": _answer(STEP1)["metadata"]}
    refused, _ = place(SNV, NR2F2, first)
    assert isinstance(refused, Refused) and refused.asked == SNV

    bare = _fake()
    del bare.by_url[url(SNV, OLDER)][SNV]["primary_assembly_loci"]["grch38"][
        "hgvs_genomic_description"
    ]
    refused, _ = place(SNV, NR2F2, bare)
    assert refused == Refused(("VariantValidator gave no genomic description",))


def test_listed_deleted_bases_are_tolerated_only_when_they_are_the_vcf_deletion() -> None:
    """The real indel: REF GGCCCGCC minus its anchor is GCCCGCC; the paper lists CCGCCCG, a
    rotation (VariantValidator shifted the deletion within the repeat)."""
    assert isinstance(place(DEL, NR2F2, _del_fake())[0], Placed)  # rotation
    same = f"{OLDER}:c.97_103delGCCCGCC"  # equal to REF minus the anchor
    assert isinstance(place(same, NR2F2, _del_fake(same))[0], Placed)

    wrong = f"{OLDER}:c.97_103delTTTTTTT"
    refused, _ = place(wrong, NR2F2, _del_fake(wrong))
    assert isinstance(refused, Refused) and refused.warnings == (REDUNDANT,)
    short = f"{OLDER}:c.97_103delCCGCCC"
    assert isinstance(place(short, NR2F2, _del_fake(short))[0], Refused)


def test_the_redundant_bases_warning_is_matched_exactly_and_on_the_mane_path_too() -> None:
    other = _del_fake()
    entry = other.by_url[url(DEL, OLDER)][DEL_ENTRY]
    entry["validation_warnings"][0] = REDUNDANT + " (changed)"
    assert isinstance(place(DEL, NR2F2, other)[0], Refused)

    single = _answer("tbx5_c835.json")
    single["NM_181486.4:c.835C>T"]["validation_warnings"] = [REDUNDANT]
    # c.835C>T lists no deleted bases, so the warning has nothing to be checked against.
    refused, _ = place("NM_181486.4:c.835C>T", TBX5, _fetch(single))
    assert refused == Refused((REDUNDANT,))


def test_vv_checks_listed_bases_itself_and_that_refuses_the_variant() -> None:
    """Measured 2026-11-19: a wrong listing comes back with a ReferenceMismatchError."""
    refused, _ = place(
        f"{OLDER}:c.97_103delTTTTTTT",
        NR2F2,
        lambda _u: (FIXTURES / "nr2f2_c97_103_wrong_bases.json").read_bytes(),
    )
    assert isinstance(refused, Refused)
    assert any(w.startswith("ReferenceMismatchError:") for w in refused.warnings)


DUP = f"{OLDER}:c.220_222dup"
DUP_TARGET = f"{OLDER}:c.222_224dup"
AUTOMAP = f"{DUP} automapped to {DUP_TARGET}"


def _dup_fake() -> _Fake:
    """Step one is the real answer; step two is derived from it (re-keyed onto MANE, no warning)."""
    step1 = _answer("nr2f2_v3_dup_c220_222.json")
    step2 = json.loads(json.dumps(step1).replace(OLDER, NR2F2))
    step2[f"{NR2F2}:c.222_224dup"]["validation_warnings"] = []
    return _Fake(DUP, step1, "NC_000015.10:g.96332327_96332329dup", step2)


def test_vvs_3_prime_shift_of_the_same_variant_is_tolerated_at_step_one() -> None:
    """R-F11: measured 2026-11-19 on NM_021005.3:c.220_222dup."""
    placed, _ = place(DUP, NR2F2, _dup_fake())

    assert isinstance(placed, Placed) and placed.hgvs_c == "NM_021005.4:c.222_224dup"


def test_an_automap_to_another_variant_or_an_other_warning_still_refuses() -> None:
    other = _dup_fake()
    entry = other.by_url[url(DUP, OLDER)][DUP_TARGET]
    entry["validation_warnings"][0] = f"{DUP} automapped to {OLDER}:c.999_1001dup"
    refused, _ = place(DUP, NR2F2, other)
    assert isinstance(refused, Refused) and "c.999_1001dup" in refused.warnings[0]

    echo = _dup_fake()
    echo.by_url[url(DUP, OLDER)][DUP_TARGET]["validation_warnings"][0] = AUTOMAP.replace(
        "220", "221"
    )
    assert isinstance(place(DUP, NR2F2, echo)[0], Refused)

    loose = _dup_fake()
    loose.by_url[url(DUP, OLDER)][DUP_TARGET]["validation_warnings"][0] = "A new warning"
    assert isinstance(place(DUP, NR2F2, loose)[0], Refused)

    # Tolerated at step one only: the same warning on the MANE answer refuses.
    late = _dup_fake()
    late.by_url[url("NC_000015.10:g.96332327_96332329dup", NR2F2)][f"{NR2F2}:c.222_224dup"][
        "validation_warnings"
    ] = [f"NC_000015.10:g.96332327_96332329dup automapped to {NR2F2}:c.222_224dup"]
    assert isinstance(place(DUP, NR2F2, late)[0], Refused)


def test_the_automap_tolerance_ignores_listed_deleted_bases_in_the_echo() -> None:
    """The log's NM_021005.3:c.92_98delGCCCGCC was echoed as c.92_98del."""
    fake = _del_fake(f"{OLDER}:c.92_98delGCCCGCC")
    entry = fake.by_url[url(f"{OLDER}:c.92_98delGCCCGCC", OLDER)][DEL_ENTRY]
    entry["validation_warnings"][0:0] = [f"{OLDER}:c.92_98del automapped to {DEL_ENTRY}"]

    placed, _ = place(f"{OLDER}:c.92_98delGCCCGCC", NR2F2, fake)

    assert isinstance(placed, Placed)


def test_an_answer_with_no_entries_and_no_empty_result_flag_raises() -> None:
    """Only a flagged empty_result is a per-variant refusal; any other empty answer is a failure
    of the run, because it is not VariantValidator's stated answer for this variant."""
    answer = {"flag": "gene_variant", "metadata": _answer("tbx5_c835.json")["metadata"]}

    with pytest.raises(VariantValidatorError, match="holds 0 descriptions"):
        place("NM_181486.4:c.835C>T", TBX5, _fetch(answer))


def _tbx5_del(listed: str = "A", strand: str | None = None) -> Any:
    answer = _answer("tbx5_c905delA.json")
    return place(f"NM_181486.4:c.905del{listed}", TBX5, _fetch(answer), strand)


def test_a_minus_strand_deletion_is_read_on_the_transcripts_strand() -> None:
    """Measured 2026-11-19: TBX5 c.905delA is REF CT, ALT C on the forward strand; the listed A
    is the reverse complement of the deleted T."""
    placed, _ = _tbx5_del()

    assert placed == Placed(
        "NM_181486.4:c.905del", "NP_852259.1:p.(Gln302ArgfsTer92)", "12:114366241:CT:C"
    )


def test_a_wrong_listed_base_on_the_minus_strand_is_refused_by_curators_check() -> None:
    # T is the forward-strand base: read on the minus strand it is the wrong one.
    refused, _ = _tbx5_del("T")

    assert refused == Refused((REDUNDANT,))


def test_the_plus_strand_check_is_unchanged_and_the_strand_must_be_one_of_two() -> None:
    assert isinstance(place(DEL, NR2F2, _del_fake(), "+")[0], Placed)
    refused, _ = place(DEL, NR2F2, _del_fake(), "-")  # the same listing read on the wrong strand
    assert isinstance(refused, Refused)
    with pytest.raises(VariantValidatorError, match="strand"):
        place(DEL, NR2F2, _del_fake(), "?")
