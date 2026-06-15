import json

import pytest

from hiera_gc.cli import main

CANARY = "CANARY_NEVER_PRINT_fx7"

BASE_YAML = f"""\
# base data, header comment must survive
sshd::port: '2222'
duplicated_key: 'sameval {CANARY}'
unused_simple: '{CANARY}_u1'
unused_block:
  a: '{CANARY}_u2'
  b:
    - '{CANARY}_u3'
unused_multiline: |
  {CANARY}_u4
  {CANARY}_u5
unused_anchor: &keep
  shared_bit: '{CANARY}_a'
keeper_alias: *keep
sshd::nope: '{CANARY}_sp'
"""

BASE_YAML_FIXED = f"""\
# base data, header comment must survive
sshd::port: '2222'
unused_anchor: &keep
  shared_bit: '{CANARY}_a'
keeper_alias: *keep
"""


@pytest.fixture
def code_dir(tmp_path):
    """Two environments plus shared data; every fixable, skippable and
    out-of-scope situation is represented, with canary values."""
    code = tmp_path / "code"
    shared = code / "hieradata"
    shared.mkdir(parents=True)
    (shared / "common.yaml").write_text(f"""\
shared_used: '{CANARY}'
shared_unused: '{CANARY}'
duplicated_key: 'sameval {CANARY}'
""")

    sshd = code / "modules" / "sshd"
    (sshd / "manifests").mkdir(parents=True)
    (sshd / "manifests" / "init.pp").write_text(
        "class sshd (String $port = '22') {\n"
        "  $x = lookup('shared_used')\n"
        "  $z = lookup('duplicated_key')\n"
        "}\n"
    )

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
    lookup_key: eyaml_lookup_key
    paths:
      - "nodes/%{trusted.certname}.yaml"
      - base.yaml
      - dupes.yaml
      - secrets.eyaml
      - extra.json
  - name: shared
    datadir: '@SHARED@'
    paths:
      - common.yaml
""".replace("@SHARED@", str(shared))
    )
    (env / "data" / "nodes").mkdir(parents=True)
    (env / "data" / "orphandir").mkdir(parents=True)
    (env / "data" / "base.yaml").write_text(BASE_YAML)
    (env / "data" / "dupes.yaml").write_text(
        f"dup_key: '{CANARY}_d1'\ndup_key: '{CANARY}_d2'\n"
    )
    (env / "data" / "secrets.eyaml").write_text(
        f"_unused_secret: ENC[GPG,{CANARY}base64==]\n"
    )
    (env / "data" / "extra.json").write_text(
        f'{{"unused_json_key": "{CANARY}"}}\n'
    )
    (env / "data" / "orphandir" / "dead.yaml").write_text(
        f"orphan_key: '{CANARY}'\n"
    )
    (env / "data" / "nodes" / "gone.example.yaml").write_text(
        f"node_key: '{CANARY}'\n"
    )

    staging = code / "environments" / "staging"
    (staging / "manifests").mkdir(parents=True)
    (staging / "manifests" / "site.pp").write_text("node default {}\n")
    (staging / "hiera.yaml").write_text("""\
version: 5
hierarchy:
  - name: env
    paths:
      - base.yaml
""")
    (staging / "data").mkdir()
    (staging / "data" / "base.yaml").write_text(
        f"staging_unused: '{CANARY}_s'\n"
    )
    return code


def run_cli(capsys, code_dir, *argv):
    code = main(
        [
            "--code-dir",
            str(code_dir),
            "--global-hiera",
            str(code_dir / "nope.yaml"),
        ]
        + list(argv)
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def tree_snapshot(code_dir):
    return {
        str(p.relative_to(code_dir)): p.read_text()
        for p in sorted(code_dir.rglob("*"))
        if p.is_file()
    }


def test_dry_run_changes_nothing(code_dir, capsys):
    before = tree_snapshot(code_dir)
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--env",
        "production",
        "--fix",
        "--dry-run",
        "--fail-on",
        "none",
    )
    assert code == 0
    assert tree_snapshot(code_dir) == before
    assert "FIXES  (environment: production, dry run)" in out
    assert "would remove key 'unused_simple'" in out
    assert "would delete file" in out
    assert "removed key" not in out


def test_fix_rewrites_only_the_target_environment(code_dir, capsys):
    code, out, _ = run_cli(
        capsys, code_dir, "--env", "production", "--fix", "--fail-on", "none"
    )
    assert code == 0
    env_data = code_dir / "environments" / "production" / "data"

    # Exact line-based edit: comments, used keys and the skipped
    # anchored pair survive; everything else is gone.
    assert (env_data / "base.yaml").read_text() == BASE_YAML_FIXED
    # eyaml key removed; the file is left behind, empty.
    assert (env_data / "secrets.eyaml").read_text() == ""
    # Orphaned and stale files deleted.
    assert not (env_data / "orphandir" / "dead.yaml").exists()
    assert not (env_data / "nodes" / "gone.example.yaml").exists()
    # Unsafe files untouched.
    assert "dup_key" in (env_data / "dupes.yaml").read_text()
    assert "unused_json_key" in (env_data / "extra.json").read_text()
    # Other environments and shared data untouched.
    assert (
        "staging_unused"
        in (
            code_dir / "environments" / "staging" / "data" / "base.yaml"
        ).read_text()
    )
    assert (
        "shared_unused" in (code_dir / "hieradata" / "common.yaml").read_text()
    )
    assert "removed key 'unused_simple'" in out
    assert "deleted file" in out


def test_fix_json_actions_skips_and_scope(code_dir, capsys):
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--env",
        "production",
        "--fix",
        "--format",
        "json",
        "--fail-on",
        "none",
    )
    assert code == 0
    fixes = json.loads(out)["fixes"]
    assert fixes["environment"] == "production"
    assert fixes["dry_run"] is False
    assert fixes["errors"] == []

    removed = {
        a["key"] for a in fixes["actions"] if a["action"] == "remove_key"
    }
    assert removed == {
        "unused_simple",
        "unused_block",
        "unused_multiline",
        "sshd::nope",
        "_unused_secret",
        "duplicated_key",
    }
    assert all(a["applied"] for a in fixes["actions"])
    findings = {a["key"] or a["file"]: a["finding"] for a in fixes["actions"]}
    assert findings["duplicated_key"] == "redundant"
    assert findings["sshd::nope"] == "stale_param"

    deleted = {
        a["file"] for a in fixes["actions"] if a["action"] == "delete_file"
    }
    assert {f.rsplit("/", 1)[-1] for f in deleted} == {
        "dead.yaml",
        "gone.example.yaml",
    }

    skipped = {(s["key"], s["reason"]) for s in fixes["skipped"]}
    assert {k for k, _ in skipped} == {
        "unused_anchor",
        "keeper_alias",
        "dup_key",
        "unused_json_key",
    }
    reasons = {k: r for k, r in skipped}
    assert "anchored" in reasons["unused_anchor"]
    assert "duplicate" in reasons["dup_key"]
    assert "flow-style" in reasons["unused_json_key"]

    # shared_unused is out of scope (shared layer); staging is not
    # analysed in a production run, so nothing else is counted.
    assert fixes["out_of_scope"] == {"unused": 1}


def test_fix_is_idempotent(code_dir, capsys):
    run_cli(
        capsys, code_dir, "--env", "production", "--fix", "--fail-on", "none"
    )
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--env",
        "production",
        "--fix",
        "--format",
        "json",
        "--fail-on",
        "none",
    )
    assert code == 0
    assert json.loads(out)["fixes"]["actions"] == []


def test_fix_other_env_collects_its_findings(code_dir, capsys):
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--env",
        "staging",
        "--fix",
        "--format",
        "json",
        "--fail-on",
        "none",
    )
    assert code == 0
    fixes = json.loads(out)["fixes"]
    removed = {a["key"] for a in fixes["actions"]}
    assert removed == {"staging_unused"}
    assert (
        "staging_unused"
        not in (
            code_dir / "environments" / "staging" / "data" / "base.yaml"
        ).read_text()
    )
    # Production files untouched in a staging run.
    assert (
        code_dir / "environments" / "production" / "data" / "base.yaml"
    ).read_text() == BASE_YAML


def test_fix_requires_a_single_env(code_dir, capsys):
    before = tree_snapshot(code_dir)
    # --fix with no --env: must refuse, so it can never fix all envs.
    with pytest.raises(SystemExit) as exc:
        main(["--code-dir", str(code_dir), "--fix"])
    assert exc.value.code == 2
    # --fix with more than one --env: ambiguous target, refused.
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--code-dir",
                str(code_dir),
                "--fix",
                "--env",
                "production",
                "--env",
                "staging",
            ]
        )
    assert exc.value.code == 2
    # --fix with --env-glob: could match several, refused.
    with pytest.raises(SystemExit) as exc:
        main(["--code-dir", str(code_dir), "--fix", "--env-glob", "prod*"])
    assert exc.value.code == 2
    assert tree_snapshot(code_dir) == before


def test_fix_env_must_exist(code_dir, capsys):
    before = tree_snapshot(code_dir)
    code, _, err = run_cli(capsys, code_dir, "--env", "bogus", "--fix")
    assert code == 2
    assert "no environments found" in err
    assert tree_snapshot(code_dir) == before


def test_fix_kinds_filter(code_dir, capsys):
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--env",
        "production",
        "--fix",
        "--fix-kinds",
        "stale_params",
        "--format",
        "json",
        "--fail-on",
        "none",
    )
    assert code == 0
    fixes = json.loads(out)["fixes"]
    assert {a["key"] for a in fixes["actions"]} == {"sshd::nope"}
    text = (
        code_dir / "environments" / "production" / "data" / "base.yaml"
    ).read_text()
    assert "sshd::nope" not in text
    assert "unused_simple" in text
    assert (
        code_dir
        / "environments"
        / "production"
        / "data"
        / "orphandir"
        / "dead.yaml"
    ).exists()


def test_fix_kinds_orphans_only(code_dir, capsys):
    code, _, _ = run_cli(
        capsys,
        code_dir,
        "--env",
        "production",
        "--fix",
        "--fix-kinds",
        "orphans",
        "--fail-on",
        "none",
    )
    assert code == 0
    env_data = code_dir / "environments" / "production" / "data"
    assert not (env_data / "orphandir" / "dead.yaml").exists()
    assert (env_data / "nodes" / "gone.example.yaml").exists()
    assert (env_data / "base.yaml").read_text() == BASE_YAML


def test_fix_refused_on_parse_errors(code_dir, capsys):
    bad = code_dir / "environments" / "production" / "data" / "bad.yaml"
    bad.write_text("a: 1\n  b: [broken\n")
    before = tree_snapshot(code_dir)
    code, _, err = run_cli(capsys, code_dir, "--env", "production", "--fix")
    assert code == 2
    assert "refusing to fix" in err
    assert tree_snapshot(code_dir) == before


@pytest.mark.parametrize(
    "mode",
    [
        ["--env", "production", "--fix"],
        ["--env", "production", "--fix", "--dry-run"],
        ["--env", "production", "--fix", "--format", "json"],
        [
            "--env",
            "production",
            "--fix",
            "--dry-run",
            "--format",
            "json",
            "-vv",
            "--stats",
        ],
    ],
)
def test_canary_never_leaks_with_fix(code_dir, capsys, mode):
    _, out, err = run_cli(capsys, code_dir, "--fail-on", "none", *mode)
    assert CANARY not in out
    assert CANARY not in err


def test_dry_run_requires_fix(code_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--code-dir", str(code_dir), "--dry-run"])
    assert exc.value.code == 2


def test_fix_kinds_requires_fix(code_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--code-dir", str(code_dir), "--fix-kinds", "unused"])
    assert exc.value.code == 2


def test_bad_fix_kind_rejected(code_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--code-dir",
                str(code_dir),
                "--env",
                "production",
                "--fix",
                "--fix-kinds",
                "shadowed",
            ]
        )
    assert exc.value.code == 2
