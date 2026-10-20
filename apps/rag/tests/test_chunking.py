from rag.chunking import chunk_document, split_sections, window


def test_sections_follow_headings():
    md = "intro line\n\n# Title\nbody one\n\n## Sub\nbody two\n"
    assert split_sections(md) == [
        ("", "intro line"),
        ("Title", "# Title\nbody one"),
        ("Sub", "## Sub\nbody two"),
    ]


def test_shell_comments_in_code_fences_are_not_headings():
    md = "# Setup\n```bash\n# not a heading\nkubectl get pods\n```\n"
    sections = split_sections(md)
    assert len(sections) == 1
    assert "# not a heading" in sections[0][1]


def test_empty_sections_are_dropped():
    assert split_sections("# Empty\n\n# Full\ntext\n") == [("Full", "# Full\ntext")]


def test_short_text_is_one_piece():
    assert window("short", size=100, overlap=10) == ["short"]


def test_long_text_splits_on_paragraphs_with_overlap():
    paragraphs = [f"paragraph {i} " + "x" * 80 for i in range(10)]
    pieces = window("\n\n".join(paragraphs), size=300, overlap=40)
    assert len(pieces) > 1
    assert all(len(p) <= 300 + 40 + 2 for p in pieces)
    # every paragraph survives somewhere
    for para in paragraphs:
        assert any(para in p for p in pieces)
    # consecutive pieces overlap
    for prev, nxt in zip(pieces, pieces[1:]):
        assert prev[-40:] in nxt


def test_oversized_paragraph_is_cut():
    pieces = window("y" * 1000, size=300, overlap=50)
    assert all(len(p) <= 300 + 50 + 2 for p in pieces)
    assert "".join(pieces).count("y") >= 1000


def test_chunks_carry_source_and_heading():
    chunks = chunk_document("docs/a.md", "no heading text\n\n# Costs\n$1.48\n")
    assert [(c.source, c.heading) for c in chunks] == [("docs/a.md", "docs/a.md"), ("docs/a.md", "Costs")]
