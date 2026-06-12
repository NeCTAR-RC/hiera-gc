import json

import pytest

from hiera_gc.cli import main

CANARY = "CANARY_NEVER_PRINT_a8f3"


@pytest.fixture
def code_dir(tmp_path):
    """A tree with every finding type and canary values planted in
    yaml, eyaml plaintext and json."""
    code = tmp_path / "code"
    shared = code / "hieradata"
    shared.mkdir(parents=True)
    (shared / "common.yaml").write_text("""\
shared_used: '%s'
shared_unused: '%s'
duplicated_key: 'sameval %s'
""" % (CANARY, CANARY, CANARY))

    sshd = code / "modules" / "sshd"
    (sshd / "manifests").mkdir(parents=True)
    (sshd / "manifests" / "init.pp").write_text(
        "class sshd (String $port = '22') {\n"
        "  $x = lookup('shared_used')\n"
        "  $y = lookup($runtime_key)\n"
        "}\n")

    env = code / "environments" / "production"
    (env / "manifests").mkdir(parents=True)
    (env / "manifests" / "site.pp").write_text(
        "node /^web/ { include sshd }\n")
    (env / "hiera.yaml").write_text("""\
version: 5
hierarchy:
  - name: env
    lookup_key: eyaml_lookup_key
    paths:
      - "nodes/%{trusted.certname}.yaml"
      - base.yaml
      - secrets.eyaml
      - extra.json
  - name: shared
    datadir: '@SHARED@'
    paths:
      - common.yaml
""".replace("@SHARED@", str(shared)))
    (env / "data" / "nodes").mkdir(parents=True)
    (env / "data" / "orphandir").mkdir(parents=True)
    (env / "data" / "base.yaml").write_text("""\
sshd::port: '2222'
duplicated_key: 'sameval %s'
unused_env_key: '%s'
""" % (CANARY, CANARY))
    (env / "data" / "secrets.eyaml").write_text(
        "_unused_secret: ENC[GPG,%sbase64==]\n" % CANARY)
    (env / "data" / "extra.json").write_text(
        '{"unused_json_key": "%s"}\n' % CANARY)
    (env / "data" / "orphandir" / "dead.yaml").write_text(
        "orphan_key: '%s'\n" % CANARY)
    (env / "data" / "nodes" / "gone.example.yaml").write_text(
        "node_key: '%s'\n" % CANARY)
    return code


def run_cli(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


ALL_MODES = [
    ["--format", "text"],
    ["--format", "json"],
    ["--format", "text", "-vv", "--stats"],
    ["--format", "json", "--show",
     "unused,possibly_used,redundant,shadowed,orphans,stale_files,"
     "warnings"],
]


@pytest.mark.parametrize("mode", ALL_MODES)
def test_canary_never_leaks(code_dir, capsys, mode):
    _, out, err = run_cli(
        capsys, "--code-dir", str(code_dir), "--global-hiera",
        str(code_dir / "nope.yaml"), *mode)
    assert CANARY not in out
    assert CANARY not in err


def test_findings_present_in_text_report(code_dir, capsys):
    code, out, _ = run_cli(capsys, "--code-dir", str(code_dir),
                           "--global-hiera", str(code_dir / "nope.yaml"))
    assert code == 1  # unused findings exist
    assert "UNUSED KEYS" in out
    assert "unused_env_key" in out
    assert "shared_unused" in out
    assert "_unused_secret" in out
    assert "unused_json_key" in out
    assert "REDUNDANT OVERRIDES" in out
    assert "duplicated_key" in out
    assert "ORPHANED DATA FILES" in out
    assert "dead.yaml" in out
    assert "STALE DATA FILES" in out
    assert "gone.example.yaml" in out
    assert "[dynamic_lookup]" in out
    assert "Summary:" in out
    # Used keys never show in the default report.
    assert "shared_used" not in out


def test_json_report_round_trips(code_dir, capsys):
    code, out, _ = run_cli(capsys, "--code-dir", str(code_dir),
                           "--format", "json",
                           "--global-hiera", str(code_dir / "nope.yaml"))
    assert code == 1
    doc = json.loads(out)
    assert doc["schema_version"] == 1
    names = {k["name"]: k for k in doc["keys"]}
    assert names["unused_env_key"]["status"] == "UNUSED"
    assert "shared_used" not in names
    assert doc["summary"]["unused"] >= 4
    assert any(r["key"] == "duplicated_key" for r in doc["redundant"])
    assert any("dead.yaml" in o["file"] for o in doc["orphaned_files"])


@pytest.mark.parametrize("fail_on,expected", [
    ("unused", 1),
    ("none", 0),
    ("stale_params", 0),  # tree has no stale params
    ("redundant", 1),
    ("orphans,stale_files", 1),
])
def test_fail_on_matrix(code_dir, capsys, fail_on, expected):
    code, _, _ = run_cli(capsys, "--code-dir", str(code_dir),
                         "--global-hiera", str(code_dir / "nope.yaml"),
                         "--fail-on", fail_on)
    assert code == expected


def test_output_to_file(code_dir, tmp_path, capsys):
    target = tmp_path / "report.json"
    code, out, _ = run_cli(capsys, "--code-dir", str(code_dir),
                           "--format", "json", "--output", str(target),
                           "--global-hiera", str(code_dir / "nope.yaml"))
    assert out == ""
    doc = json.loads(target.read_text())
    assert doc["schema_version"] == 1
    assert CANARY not in target.read_text()


def test_strict_mode_fails_on_parse_errors(code_dir, capsys):
    bad = code_dir / "environments" / "production" / "data" / "bad.yaml"
    bad.write_text("a: 1\n  b: [broken\n")
    code, _, _ = run_cli(capsys, "--code-dir", str(code_dir),
                         "--global-hiera", str(code_dir / "nope.yaml"),
                         "--strict")
    assert code == 2
