from rag.chunking import Chunk
from rag.scoring import check_evidence, compare, metrics, norm, summarize


def row(rank, lang="en", answerable=True, score=0.8):
    return {"id": "q", "lang": lang, "answerable": answerable, "rank": rank, "top_score": score}


def test_norm_ignores_case_and_line_wrapping():
    assert norm("repoints the `platform-docs`\n   ALIAS") == "repoints the `platform-docs` alias"


def test_missing_evidence_is_reported():
    chunks = [Chunk("README.md", "h", "Measured on EKS 1.36 with\none g4dn.xlarge")]
    questions = [{"id": "a", "evidence": ["eks 1.36 with one"]}, {"id": "b", "evidence": ["EKS 1.35"]}]
    assert check_evidence(questions, chunks) == ["b: 'EKS 1.35'"]


def test_metrics():
    # ranks 1, 3, miss -> hit@1 1/3, hit@k 2/3, mrr (1 + 1/3 + 0) / 3
    m = metrics([row(1), row(3), row(None)])
    assert m == {"n": 3, "hit@1": 0.333, "hit@k": 0.667, "mrr": 0.444}


def test_summary_splits_languages_and_skips_unanswerable():
    s = summarize([row(1), row(None, lang="es"), row(None, answerable=False, score=0.5)])
    assert s["all"]["n"] == 2
    assert s["en"]["hit@k"] == 1 and s["es"]["hit@k"] == 0


def test_threshold_separation():
    s = summarize([row(1, score=0.80), row(2, score=0.64), row(None, answerable=False, score=0.70)])
    # an unanswerable question outscores an answerable hit: no clean threshold
    assert s["threshold"] == {"max_unanswerable_score": 0.70, "min_hit_score": 0.64, "separation": -0.06}


def test_baseline_comparison():
    summary = {"all": {"hit@k": 0.8, "mrr": 0.6}, "es": {"hit@k": 0.4}}
    assert compare(summary, {"min": {"all.hit@k": 0.8, "es.hit@k": 0.4}}) == []
    assert compare(summary, {"min": {"all.mrr": 0.7}}) == ["all.mrr = 0.6 < 0.7"]
    assert compare(summary, {"min": {"fr.hit@k": 0.1}}) == ["fr.hit@k = None < 0.1"]
