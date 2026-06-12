import pytest

from hiera_gc.yamlloc import (
    DataFileError,
    iter_string_scalars,
    load_data_file,
)


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def keys_of(doc):
    return {k.name: k for k in doc.keys}


def test_top_level_keys_and_lines(tmp_path):
    doc = load_data_file(write(tmp_path, "a.yaml", (
        "first: 1\n"
        "second:\n"
        "  nested: true\n"
        "third::with::colons: x\n")))
    keys = keys_of(doc)
    assert set(keys) == {"first", "second", "third::with::colons"}
    assert keys["first"].line == 1
    assert keys["second"].line == 2
    assert keys["third::with::colons"].line == 4


def test_merge_keys_fold_anchor_keys_in(tmp_path):
    doc = load_data_file(write(tmp_path, "a.yaml", (
        "defaults: &defaults\n"
        "  from_anchor: 1\n"
        "  overridden: 2\n"
        "merged:\n"
        "  <<: *defaults\n"
        "  own: 3\n"
        "<<: *defaults\n"
        "overridden: 9\n")))
    keys = keys_of(doc)
    # 'from_anchor' arrives at top level via the top-level merge key.
    assert "from_anchor" in keys
    assert keys["from_anchor"].from_merge
    assert keys["from_anchor"].line == 2  # anchor definition line
    # Explicit keys win over merged ones.
    assert not keys["overridden"].from_merge
    assert keys["overridden"].line == 8


def test_duplicate_keys_flagged_last_wins(tmp_path):
    doc = load_data_file(write(tmp_path, "a.yaml", (
        "dup: 1\n"
        "other: 2\n"
        "dup: 3\n")))
    assert any("duplicate top-level key 'dup'" in p for p in doc.problems)
    assert keys_of(doc)["dup"].line == 3


def test_empty_list_root_and_multidoc_problems(tmp_path):
    assert load_data_file(write(tmp_path, "e.yaml", "")).problems == \
        ["empty data file"]
    assert "root is not a mapping (hiera expects a hash)" in \
        load_data_file(write(tmp_path, "l.yaml", "- a\n- b\n")).problems
    doc = load_data_file(write(tmp_path, "m.yaml",
                               "a: 1\n---\nb: 2\n"))
    assert any("multi-document" in p for p in doc.problems)
    assert set(keys_of(doc)) == {"a"}


def test_parse_error_raises_with_line(tmp_path):
    with pytest.raises(DataFileError) as exc:
        load_data_file(write(tmp_path, "bad.yaml", "a: 1\n  b: [unclosed\n"))
    assert exc.value.line >= 1


def test_json_fallback_for_tab_indented_json(tmp_path):
    doc = load_data_file(write(tmp_path, "a.json", '{\n\t"k": [1, 2]\n}\n'))
    keys = keys_of(doc)
    assert set(keys) == {"k"}
    assert any("json fallback" in p for p in doc.problems)
    assert keys["k"].digest()


def test_digest_order_insensitive_and_type_sensitive(tmp_path):
    doc = load_data_file(write(tmp_path, "a.yaml", (
        "h1: {a: 1, b: 2}\n"
        "h2: {b: 2, a: 1}\n"
        "int_value: 5\n"
        "str_value: '5'\n"
        "same_int: 5\n")))
    keys = keys_of(doc)
    assert keys["h1"].digest() == keys["h2"].digest()
    assert keys["int_value"].digest() == keys["same_int"].digest()
    assert keys["int_value"].digest() != keys["str_value"].digest()


def test_digest_handles_recursive_aliases(tmp_path):
    doc = load_data_file(write(tmp_path, "a.yaml",
                               "selfish: &s\n  inner: *s\n"))
    assert keys_of(doc)["selfish"].digest()


def test_iter_string_scalars(tmp_path):
    doc = load_data_file(write(tmp_path, "a.yaml", (
        "k:\n"
        "  - plain\n"
        "  - 42\n"
        "  - deeper:\n"
        "      v: \"quoted %{lookup('x')}\"\n")))
    strings = {value for value, _ in
               iter_string_scalars(keys_of(doc)["k"].node)}
    assert "plain" in strings
    assert "quoted %{lookup('x')}" in strings
    assert "42" not in strings  # int scalar, not a string
