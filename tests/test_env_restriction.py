"""A run narrowed to a single environment reports and fails on only
that environment's own data; an all-environments run is unchanged."""

import json

import pytest

from hiera_gc.cli import main


@pytest.fixture
def code_dir(tmp_path):
    """Two environments sharing a datadir, each with an unused key of
    its own, plus an unused key in the shared layer."""
    code = tmp_path / "code"
    shared = code / "hieradata"
    shared.mkdir(parents=True)
    (shared / "common.yaml").write_text("shared_unused: 1\n")

    # Global modules (visible to every environment) that each raise a
    # warning of their own: an empty data file, a runtime-keyed lookup in
    # a module manifest, and a module hiera.yaml naming a missing datadir.
    mod = code / "modules" / "emptymod"
    (mod / "data").mkdir(parents=True)
    (mod / "hiera.yaml").write_text(
        "version: 5\nhierarchy:\n  - name: data\n    path: common.yaml\n"
    )
    (mod / "data" / "common.yaml").write_text("")  # empty: warns

    dyn = code / "modules" / "dynmod" / "manifests"
    dyn.mkdir(parents=True)
    (dyn / "init.pp").write_text(
        "class dynmod {\n  $x = lookup($runtime_key)\n}\n"  # dynamic lookup
    )

    baddir = code / "modules" / "baddir"
    baddir.mkdir(parents=True)
    (baddir / "hiera.yaml").write_text(
        "version: 5\nhierarchy:\n"
        "  - name: c\n    datadir: nope\n    path: x.yaml\n"  # missing datadir
    )

    for name, own_key in (
        ("production", "prod_unused"),
        ("staging", "staging_unused"),
    ):
        env = code / "environments" / name
        (env / "manifests").mkdir(parents=True)
        # The environment's own manifest also does a runtime-keyed lookup.
        (env / "manifests" / "site.pp").write_text(
            "node default { $own = lookup($runtime_key) }\n"
        )
        (env / "hiera.yaml").write_text(
            """\
version: 5
hierarchy:
  - name: env
    paths:
      - base.yaml
  - name: shared
    datadir: '@SHARED@'
    paths:
      - common.yaml
""".replace("@SHARED@", str(shared))
        )
        (env / "data").mkdir()
        (env / "data" / "base.yaml").write_text(f"{own_key}: 1\n")
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


def test_single_env_hides_shared_and_other_environments(code_dir, capsys):
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--env",
        "production",
        "--format",
        "json",
        "--fail-on",
        "none",
    )
    assert code == 0
    doc = json.loads(out)
    names = {k["name"] for k in doc["keys"]}
    # Only production's own data is reported.
    assert names == {"prod_unused"}
    assert "shared_unused" not in names  # shared layer, out of scope
    assert "staging_unused" not in names  # another environment
    assert doc["restricted_to"] == "production"
    # The shared-layer unused key is tallied as hidden.
    assert doc["restricted_suppressed"]["unused"] == 1


def test_all_environments_run_is_unchanged(code_dir, capsys):
    code, out, _ = run_cli(
        capsys, code_dir, "--format", "json", "--fail-on", "none"
    )
    assert code == 0
    doc = json.loads(out)
    names = {k["name"] for k in doc["keys"]}
    assert names == {"prod_unused", "staging_unused", "shared_unused"}
    assert doc["restricted_to"] is None
    assert doc["restricted_suppressed"] == {}


def test_env_glob_matching_one_environment_restricts(code_dir, capsys):
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--env-glob",
        "prod*",
        "--format",
        "json",
        "--fail-on",
        "none",
    )
    assert code == 0
    doc = json.loads(out)
    assert doc["restricted_to"] == "production"
    assert {k["name"] for k in doc["keys"]} == {"prod_unused"}


def test_env_glob_matching_many_is_not_restricted(code_dir, capsys):
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--env-glob",
        "*",
        "--format",
        "json",
        "--fail-on",
        "none",
    )
    assert code == 0
    doc = json.loads(out)
    assert doc["restricted_to"] is None
    assert {k["name"] for k in doc["keys"]} == {
        "prod_unused",
        "staging_unused",
        "shared_unused",
    }


def test_single_env_hides_warnings_about_other_layers(code_dir, capsys):
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--env",
        "production",
        "--show",
        "warnings",
        "--format",
        "json",
        "--fail-on",
        "none",
    )
    assert code == 0
    warnings = json.loads(out)["warnings"]
    files = [w["file"] for w in warnings]
    messages = [w["message"] for w in warnings]
    # No warning about a module file (empty data file, module manifest
    # dynamic lookup, module hiera.yaml datadir) survives a single-env run.
    assert not any("/modules/" in f for f in files)
    assert not any("empty data file" in m for m in messages)
    # The environment's own manifest lookup is still reported: it is part
    # of production's tree and relevant to its analysis.
    assert any(
        f.endswith("environments/production/manifests/site.pp") for f in files
    )


def test_all_environments_run_keeps_module_warnings(code_dir, capsys):
    code, out, _ = run_cli(
        capsys,
        code_dir,
        "--show",
        "warnings",
        "--format",
        "json",
        "--fail-on",
        "none",
    )
    assert code == 0
    files = [w["file"] for w in json.loads(out)["warnings"]]
    # Every module's warning is present when no environment filter applies.
    assert any("modules/emptymod" in f for f in files)
    assert any("modules/dynmod" in f for f in files)
    assert any("modules/baddir" in f for f in files)


def test_single_env_text_report_notes_the_restriction(code_dir, capsys):
    code, out, _ = run_cli(
        capsys, code_dir, "--env", "production", "--fail-on", "none"
    )
    assert code == 0
    assert "Restricted to environment 'production'" in out
    assert "prod_unused" in out
    assert "shared_unused" not in out
    assert "staging_unused" not in out


def test_single_env_exit_status_ignores_shared_findings(tmp_path, capsys):
    """Production's own data is clean; only the shared layer has an
    unused key. A production-only run passes; the full run fails."""
    code = tmp_path / "code"
    shared = code / "hieradata"
    shared.mkdir(parents=True)
    (shared / "common.yaml").write_text("shared_unused: 1\n")
    (code / "modules").mkdir()
    env = code / "environments" / "production"
    (env / "manifests").mkdir(parents=True)
    (env / "manifests" / "site.pp").write_text("node default {}\n")
    (env / "hiera.yaml").write_text(
        """\
version: 5
hierarchy:
  - name: shared
    datadir: '@SHARED@'
    paths:
      - common.yaml
""".replace("@SHARED@", str(shared))
    )

    restricted, _, _ = run_cli(
        capsys, code, "--env", "production", "--fail-on", "unused"
    )
    assert restricted == 0  # nothing unused in production's own data

    full, _, _ = run_cli(capsys, code, "--fail-on", "unused")
    assert full == 1  # the shared-layer key is unused across the tree
