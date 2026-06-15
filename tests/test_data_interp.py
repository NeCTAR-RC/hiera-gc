from hiera_gc.consumers.data_interp import extract_data_consumers
from hiera_gc.yamlloc import load_data_file


def load(tmp_path, content, name="data.yaml"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return load_data_file(path)


def extract(tmp_path, content, name="data.yaml"):
    return extract_data_consumers(load(tmp_path, content, name))


def test_lookup_alias_hiera_interpolations(tmp_path):
    consumers, _, warnings = extract(
        tmp_path,
        """\
a: "%{lookup('other_key')}"
b: "%{alias('_secret_key')}"
c: "%{hiera('legacy_key')}"
d: "%{literal('%')}{escaped}"
""",
    )
    by_key = {c.key: c for c in consumers}
    assert set(by_key) == {"other_key", "_secret_key", "legacy_key"}
    assert by_key["other_key"].kind == "data_lookup"
    assert by_key["other_key"].line == 1
    assert by_key["_secret_key"].kind == "data_alias"
    assert warnings == []


def test_embedded_alias_warns(tmp_path):
    consumers, _, warnings = extract(
        tmp_path, 'a: "prefix %{alias(\'x\')} suffix"\n'
    )
    assert consumers[0].key == "x"
    assert any("embedded in a longer string" in w.message for w in warnings)


def test_variable_interpolations(tmp_path):
    consumers, _, _ = extract(
        tmp_path,
        """\
a: "%{::profile::base::dns}"
b: "%{scope('nectar::profile::common::region')}"
c: "%{facts.os.family} %{trusted.certname} %{nodegroup}"
d: "%{environment}"
""",
    )
    keys = {c.key for c in consumers}
    # Facts and bare top-scope variables are not hiera keys.
    assert keys == {"profile::base::dns", "nectar::profile::common::region"}
    assert all(c.kind == "data_var_interp" for c in consumers)


def test_enc_values_are_opaque(tmp_path):
    consumers, _, warnings = extract(
        tmp_path,
        """\
secret: ENC[GPG,looksLike%{lookup('nope')}base64]
block: >
  ENC[PKCS7,alsoOpaque%{alias('nada')}]
""",
    )
    assert consumers == []
    assert warnings == []


def test_interpolation_in_nested_values_and_seq(tmp_path):
    consumers, _, _ = extract(
        tmp_path,
        """\
top:
  nested:
    - "%{lookup('deep_key')}"
  other: "%{lookup('other.dotted')}"
""",
    )
    keys = {c.key for c in consumers}
    assert keys == {"deep_key", "other.dotted"}


def test_lookup_options_entries(tmp_path):
    _, entries, warnings = extract(
        tmp_path,
        """\
lookup_options:
  exact::key:
    merge: deep
  "^profile::.*::extras$":
    merge:
      strategy: deep
      knockout_prefix: "--"
  no_merge::key:
    convert_to: Sensitive
""",
    )
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {
        "exact::key",
        "profile::.*::extras$",
        "no_merge::key",
    }
    assert not by_name["exact::key"].regex
    assert by_name["exact::key"].merge == "deep"
    assert by_name["profile::.*::extras$"].regex
    assert by_name["profile::.*::extras$"].merge == "deep"
    assert by_name["no_merge::key"].merge is None
    assert by_name["exact::key"].line == 2
    assert warnings == []


def test_lookup_options_not_consuming_other_keys(tmp_path):
    consumers, entries, _ = extract(
        tmp_path,
        """\
lookup_options:
  some::key:
    merge: unique
""",
    )
    assert consumers == []  # entries are annotations, not consumers
    assert len(entries) == 1


def test_non_hash_lookup_options_warns(tmp_path):
    _, entries, warnings = extract(tmp_path, "lookup_options: not-a-hash\n")
    assert entries == []
    assert any("not a hash" in w.message for w in warnings)
