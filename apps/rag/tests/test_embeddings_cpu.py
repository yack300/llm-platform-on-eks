import sys
import types

# cpu_limit doesn't need the model: stub fastembed so the test runs without it.
sys.modules.setdefault("fastembed", types.SimpleNamespace(TextEmbedding=object))

from rag.embeddings import cpu_limit  # noqa: E402


def test_quota_is_rounded_up(tmp_path):
    f = tmp_path / "cpu.max"
    f.write_text("150000 100000\n")
    assert cpu_limit(str(f)) == 2


def test_fraction_of_a_cpu_is_one_thread(tmp_path):
    f = tmp_path / "cpu.max"
    f.write_text("50000 100000\n")
    assert cpu_limit(str(f)) == 1


def test_no_limit_falls_back_to_visible_cores(tmp_path):
    f = tmp_path / "cpu.max"
    f.write_text("max 100000\n")
    assert cpu_limit(str(f)) >= 1


def test_missing_file_falls_back(tmp_path):
    assert cpu_limit(str(tmp_path / "nope")) >= 1
