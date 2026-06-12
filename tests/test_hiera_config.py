from hiera_gc.hiera_config import parse_hiera_config


def write(tmp_path, content):
    path = tmp_path / "hiera.yaml"
    path.write_text(content, encoding="utf-8")
    return path


ARDC_STYLE = """\
---
version: 5

defaults:
  datadir: 'data'
  data_hash: 'yaml_data'

hierarchy:
  - name: 'environment hiera'
    lookup_key: eyaml_lookup_key
    options:
      gpg_gnupghome: /opt/puppetlabs/server/data/puppetserver/.gnupg
    paths:
      - "nodes/%{trusted.certname}.yaml"
      - "nodegroups/%{nodegroup}.yaml"
      - "base.yaml"
      - "secrets.yaml"
  - name: 'nectar shared'
    lookup_key: eyaml_lookup_key
    datadir: '/etc/puppetlabs/code/hieradata/'
    paths:
      - 'testing.yaml'
      - 'common.yaml'
"""


def test_parses_ardc_style_config(tmp_path):
    config = parse_hiera_config(write(tmp_path, ARDC_STYLE))
    assert config.usable
    assert len(config.entries) == 2
    env_entry, shared_entry = config.entries
    assert env_entry.datadir_raw == "data"
    assert env_entry.backend_name == "eyaml_lookup_key"
    assert env_entry.backend_kind == "yaml"
    assert env_entry.patterns[0] == "nodes/%{trusted.certname}.yaml"
    assert shared_entry.datadir_raw == "/etc/puppetlabs/code/hieradata/"
    assert shared_entry.index == 1
    assert not config.warnings


def test_glob_globs_and_mapped_paths(tmp_path):
    config = parse_hiera_config(write(tmp_path, """\
version: 5
hierarchy:
  - name: globby
    glob: "globs/*.yaml"
  - name: mapped
    mapped_paths: [service_names, name, "services/%{name}.yaml"]
"""))
    globby, mapped = config.entries
    assert globby.patterns == ["globs/*.yaml"]
    assert globby.glob_flags == [True]
    assert globby.backend_name == "yaml_data"  # hiera's default
    assert mapped.patterns == ["services/%{name}.yaml"]
    assert mapped.glob_flags == [False]


def test_unknown_and_hocon_backends_warn(tmp_path):
    config = parse_hiera_config(write(tmp_path, """\
version: 5
hierarchy:
  - name: vault
    lookup_key: hiera_vault
  - name: hocon
    data_hash: hocon_data
    path: stuff.conf
"""))
    assert config.usable
    assert not config.entries[0].scannable
    assert not config.entries[1].scannable
    kinds = [w.kind for w in config.warnings]
    assert kinds.count("unknown_backend") == 2


def test_hiera3_and_wrong_version_rejected(tmp_path):
    v3 = parse_hiera_config(write(tmp_path, ":backends:\n  - yaml\n"))
    assert not v3.usable
    assert any("hiera 3" in w.message for w in v3.warnings)

    v4 = parse_hiera_config(write(tmp_path, "version: 4\nhierarchy: []\n"))
    assert not v4.usable


def test_entry_without_paths_warns(tmp_path):
    config = parse_hiera_config(write(tmp_path, """\
version: 5
hierarchy:
  - name: pathless
    data_hash: yaml_data
"""))
    assert any("no path" in w.message for w in config.warnings)
