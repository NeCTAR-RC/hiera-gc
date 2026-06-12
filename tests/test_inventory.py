from pathlib import Path

from hiera_gc.config import Environment, RunConfig
from hiera_gc.inventory import build_inventory


ENV_HIERA_TEMPLATE = """\
version: 5
defaults:
  datadir: data
  data_hash: yaml_data
hierarchy:
  - name: env
    lookup_key: eyaml_lookup_key
    paths:
      - "nodes/%{trusted.certname}.yaml"
      - base.yaml
  - name: shared
    lookup_key: eyaml_lookup_key
    datadir: '@SHARED@'
    paths:
      - common.yaml
"""


def make_tree(tmp_path, shared_datadir=None):
    code = tmp_path / "code"
    shared = code / "hieradata"
    shared.mkdir(parents=True)
    (shared / "common.yaml").write_text("shared_key: 1\n")

    for env_name in ("production", "staging"):
        env = code / "environments" / env_name
        (env / "data" / "nodes").mkdir(parents=True)
        (env / "hiera.yaml").write_text(ENV_HIERA_TEMPLATE.replace(
            "@SHARED@", shared_datadir or str(shared)))
        (env / "data" / "base.yaml").write_text(
            "%s_key: 1\ncommon_to_both: 1\n" % env_name)
    (code / "environments" / "production" / "data" / "nodes" /
     "web1.example.yaml").write_text("node_key: ENC[GPG,abc123]\n")
    (code / "environments" / "production" / "data" /
     "hiera-eyaml-gpg.recipients").write_text("not yaml at all {{{\n")
    return code


def envs_of(code):
    root = code / "environments"
    return [Environment(name=p.name, path=p) for p in sorted(root.iterdir())]


def run(code, **kwargs):
    config = RunConfig(code_dir=code, global_hiera=None, **kwargs)
    return build_inventory(config, envs_of(code))


def test_env_and_shared_layers(tmp_path):
    code = make_tree(tmp_path)
    inv = run(code)
    by_name = inv.keys_by_name()

    shared_key = by_name["shared_key"][0]
    assert shared_key.layer == "shared"
    assert shared_key.env is None
    # Both environments reference the shared datadir.
    (shared_dir,) = [d for d in inv.shared_refs]
    assert sorted(inv.shared_refs[shared_dir]) == ["production", "staging"]
    # Shared keys are inventoried once even with two referencing envs.
    assert len(by_name["shared_key"]) == 1

    prod_key = by_name["production_key"][0]
    assert prod_key.layer == "environment"
    assert prod_key.env == "production"
    assert len(by_name["common_to_both"]) == 2

    node_key = by_name["node_key"][0]
    assert node_key.file.name == "web1.example.yaml"
    assert node_key.line == 1


def test_non_data_files_ignored(tmp_path):
    code = make_tree(tmp_path)
    inv = run(code)
    assert not any("recipients" in str(k.file) for k in inv.keys)
    assert not any(w.kind == "parse_error" for w in inv.warnings)


def test_absolute_datadir_rebased_under_code_dir(tmp_path):
    code = make_tree(tmp_path,
                     shared_datadir="/etc/puppetlabs/code/hieradata/")
    if Path("/etc/puppetlabs/code/hieradata").is_dir():
        return  # machine has a real puppet tree; rebase will not trigger
    inv = run(code)
    assert "shared_key" in inv.keys_by_name()
    assert any("rebased under code dir" in w.message for w in inv.warnings)


def test_missing_absolute_datadir_warns(tmp_path):
    code = make_tree(tmp_path, shared_datadir=str(tmp_path / "nowhere"))
    inv = run(code)
    assert "shared_key" not in inv.keys_by_name()
    assert any("does not exist" in w.message for w in inv.warnings)


def test_interpolated_datadir_globbed(tmp_path):
    code = tmp_path / "code"
    env = code / "environments" / "production"
    (env / "data" / "production").mkdir(parents=True)
    (env / "hiera.yaml").write_text("""\
version: 5
hierarchy:
  - name: env
    datadir: "data/%{environment}"
    paths: [common.yaml]
""")
    (env / "data" / "production" / "common.yaml").write_text("k: 1\n")
    inv = run(code)
    assert "k" in inv.keys_by_name()


def test_missing_env_hiera_uses_default_hierarchy(tmp_path):
    code = tmp_path / "code"
    env = code / "environments" / "production"
    (env / "data").mkdir(parents=True)
    (env / "data" / "common.yaml").write_text("k: 1\n")
    inv = run(code)
    assert "k" in inv.keys_by_name()
    assert any("no hiera.yaml" in w.message for w in inv.warnings)


def test_unparseable_data_file_warns(tmp_path):
    code = make_tree(tmp_path)
    bad = code / "environments" / "production" / "data" / "broken.yaml"
    bad.write_text("a: 1\n  b: [unclosed\n")
    inv = run(code)
    warning = [w for w in inv.warnings if w.kind == "parse_error"]
    assert len(warning) == 1
    assert warning[0].file.endswith("broken.yaml")
