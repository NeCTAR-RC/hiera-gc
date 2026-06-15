from hiera_gc.analysis import analyse
from hiera_gc.config import Environment, RunConfig


def build_tree(tmp_path):
    """A tree exercising every redundancy/shadowing rule.

    Hierarchy priority (high to low): nodes/<cert> -> base.yaml ->
    shared common.yaml -> module data.
    """
    code = tmp_path / "code"
    shared = code / "hieradata"
    shared.mkdir(parents=True)
    (shared / "common.yaml").write_text("""\
same_everywhere: 42
shadowed_in_env: 'shared value'
merged_hash: {a: 1}
enc_copy: ENC[GPG,identicalblob==]
""")

    sshd = code / "modules" / "sshd"
    (sshd / "manifests").mkdir(parents=True)
    (sshd / "manifests" / "init.pp").write_text(
        "class sshd ($port, $opts) {\n"
        "  $m = lookup('merged_hash', Hash, 'deep')\n"
        "  $s = lookup('same_everywhere')\n"
        "  $e = lookup('enc_copy')\n"
        "  $v = lookup('shadowed_in_env')\n"
        "  $n = lookup('node_same')\n"
        "  $i = lookup('intermediate_differs')\n"
        "}\n"
    )
    (sshd / "hiera.yaml").write_text(
        "version: 5\nhierarchy:\n  - name: defaults\n    path: common.yaml\n"
    )
    (sshd / "data").mkdir()
    (sshd / "data" / "common.yaml").write_text("sshd::port: 22\n")

    env = code / "environments" / "production"
    (env / "manifests").mkdir(parents=True)
    (env / "manifests" / "site.pp").write_text(
        "node /^web/ { include sshd }\n"
    )
    (env / "hiera.yaml").write_text(
        """\
version: 5
hierarchy:
  - name: env
    paths:
      - "nodes/%{trusted.certname}.yaml"
      - "groups/%{group}.yaml"
      - base.yaml
  - name: shared
    datadir: '@SHARED@'
    paths:
      - common.yaml
""".replace("@SHARED@", str(shared))
    )
    (env / "data" / "nodes").mkdir(parents=True)
    (env / "data" / "groups").mkdir(parents=True)
    (env / "data" / "base.yaml").write_text("""\
same_everywhere: 42
shadowed_in_env: 'env value'
merged_hash: {b: 2}
enc_copy: ENC[GPG,identicalblob==]
sshd::port: 22
node_same: 'x'
intermediate_differs: 'top'
""")
    (env / "data" / "groups" / "g1.yaml").write_text(
        "intermediate_differs: 'middle'\n"
    )
    (env / "data" / "nodes" / "web1.example.yaml").write_text("""\
node_same: 'x'
same_everywhere: 99
intermediate_differs: 'top'
""")
    return code


def run(tmp_path):
    code = build_tree(tmp_path)
    config = RunConfig(code_dir=code, global_hiera=None)
    return analyse(
        config,
        [Environment("production", code / "environments" / "production")],
    )


def redundant_map(result):
    return {(r.key, r.file.name): r for r in result.redundant}


def test_redundant_overrides(tmp_path):
    result = run(tmp_path)
    redundant = redundant_map(result)

    # env base.yaml duplicates shared common.yaml -> remove env copy.
    entry = redundant[("same_everywhere", "base.yaml")]
    assert entry.anchor_file.name == "common.yaml"
    assert entry.envs == ["production"]

    # byte-identical ENC blobs compare equal.
    assert ("enc_copy", "base.yaml") in redundant

    # env base.yaml duplicating a module-layer default.
    assert ("sshd::port", "base.yaml") in redundant

    # node file duplicating base.yaml (node level is conditional, base
    # is the always-loaded anchor).
    assert ("node_same", "web1.example.yaml") in redundant


def test_not_redundant_cases(tmp_path):
    result = run(tmp_path)
    redundant = redundant_map(result)

    # Different value at node level: a real override.
    assert ("same_everywhere", "web1.example.yaml") not in redundant

    # Deep-merged key: every level contributes.
    assert not any(key == "merged_hash" for key, _ in redundant)

    # An intermediate (group) level defines a different value between
    # the node copy and base.yaml: fact-dependent, not claimable.
    assert ("intermediate_differs", "web1.example.yaml") not in redundant


def test_shadowed_definition(tmp_path):
    result = run(tmp_path)
    shadowed = {(s.key, s.file.name): s for s in result.shadowed}
    entry = shadowed[("shadowed_in_env", "common.yaml")]
    assert entry.shadow_file.name == "base.yaml"
    # The shadowing pair must not also be reported as redundant.
    assert not any(r.key == "shadowed_in_env" for r in result.redundant)


def test_counts_include_redundancy(tmp_path):
    result = run(tmp_path)
    counts = result.counts()
    assert counts["redundant"] == len(result.redundant) > 0
    assert counts["shadowed"] == 1
    assert result.fails(["redundant"])
    assert not result.fails([])  # --fail-on none
