import pytest

from hiera_gc.analysis import analyse
from hiera_gc.checks import hiera_pattern_to_regex
from hiera_gc.config import Environment, RunConfig


@pytest.mark.parametrize(
    "pattern,is_glob,path,matches",
    [
        (
            "nodes/%{trusted.certname}.yaml",
            False,
            "nodes/a.example.yaml",
            True,
        ),
        (
            "nodes/%{trusted.certname}.yaml",
            False,
            "nodes/a.example.yml",
            False,
        ),
        ("base.yaml", False, "base.yaml", True),
        ("base.yaml", False, "subdir/base.yaml", False),
        (
            "os/%{facts.os.name}/%{facts.os.release.major}.yaml",
            False,
            "os/Ubuntu/22.04.yaml",
            True,
        ),
        ("globs/*.yaml", True, "globs/a.yaml", True),
        ("globs/*.yaml", True, "globs/sub/a.yaml", False),
        ("deep/**.yaml", True, "deep/sub/dir/a.yaml", True),
        ("svc/{web,db}.yaml", True, "svc/web.yaml", True),
        ("svc/{web,db}.yaml", True, "svc/mq.yaml", False),
        ("file[0-9].yaml", True, "file7.yaml", True),
    ],
)
def test_hiera_pattern_to_regex(pattern, is_glob, path, matches):
    import re

    regex = re.compile(hiera_pattern_to_regex(pattern, is_glob))
    assert bool(regex.fullmatch(path)) == matches


def build_tree(tmp_path, with_default_node=False):
    code = tmp_path / "code"
    env = code / "environments" / "production"
    for sub in ("nodes", "nodegroups", "leftover"):
        (env / "data" / sub).mkdir(parents=True)
    (env / "manifests").mkdir()

    (env / "hiera.yaml").write_text("""\
version: 5
hierarchy:
  - name: env
    paths:
      - "nodes/%{trusted.certname}.yaml"
      - "nodegroups/%{nodegroup}.yaml"
      - "hw/%{hardwaregroup}.yaml"
      - base.yaml
""")
    node_block = "node default {}\n" if with_default_node else ""
    (env / "manifests" / "site.pp").write_text(f"""\
$nodegroup = $facts['clientcert'] ? {{
  /^web/  => 'group-web',
  default => 0,
}}
$hardwaregroup = $facts['model']
node 'web1.example' {{ }}
node /^db[1-3]\\.example$/ {{ }}
{node_block}""")

    data = env / "data"
    (data / "base.yaml").write_text("k: 1\n")
    (data / "stray.yml").write_text("orphan_key: 1\n")  # .yml vs .yaml
    (data / "leftover" / "old.yaml").write_text("dead: 1\n")
    (data / "nodes" / "web1.example.yaml").write_text("a: 1\n")
    (data / "nodes" / "db2.example.yaml").write_text("b: 1\n")
    (data / "nodes" / "gone.example.yaml").write_text("c: 1\n")
    (data / "nodegroups" / "group-web.yaml").write_text("d: 1\n")
    (data / "nodegroups" / "group-old.yaml").write_text("e: 1\n")
    return code


def run(tmp_path, **kwargs):
    code = build_tree(tmp_path, **kwargs)
    config = RunConfig(code_dir=code, global_hiera=None)
    envs = [Environment("production", code / "environments" / "production")]
    return analyse(config, envs)


def test_orphan_files(tmp_path):
    result = run(tmp_path)
    orphans = {o.file.name for o in result.orphans}
    assert orphans == {"stray.yml", "old.yaml"}


def test_stale_node_and_group_files(tmp_path):
    result = run(tmp_path)
    stale = {s.file.name: s.message for s in result.stale_files}
    assert set(stale) == {"gone.example.yaml", "group-old.yaml"}
    assert "no node definition" in stale["gone.example.yaml"]
    assert "never assigned the value 'group-old'" in stale["group-old.yaml"]


def test_node_default_suppresses_node_check(tmp_path):
    result = run(tmp_path, with_default_node=True)
    stale = {s.file.name for s in result.stale_files}
    assert "gone.example.yaml" not in stale
    assert "group-old.yaml" in stale  # group check unaffected


def test_uncollectible_var_noted(tmp_path):
    code = build_tree(tmp_path)
    env = code / "environments" / "production"
    (env / "data" / "hw").mkdir()
    (env / "data" / "hw" / "metal.yaml").write_text("f: 1\n")
    config = RunConfig(code_dir=code, global_hiera=None)
    result = analyse(config, [Environment("production", env)])
    # $hardwaregroup is fact-derived; its files must not be flagged.
    assert "metal.yaml" not in {s.file.name for s in result.stale_files}
    assert any(
        w.kind == "stale_check_skipped" and "hardwaregroup" in w.message
        for w in result.warnings
    )


def test_stale_lookup_options_entry(tmp_path):
    code = build_tree(tmp_path)
    env = code / "environments" / "production"
    (env / "data" / "base.yaml").write_text("""\
k: 1
lookup_options:
  k:
    merge: deep
  vanished::key:
    merge: deep
""")
    config = RunConfig(code_dir=code, global_hiera=None)
    result = analyse(config, [Environment("production", env)])
    stale = [w for w in result.warnings if w.kind == "stale_lookup_options"]
    assert len(stale) == 1
    assert "vanished::key" in stale[0].message
