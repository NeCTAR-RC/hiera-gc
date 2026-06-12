import json

import pytest

from hiera_gc.cli import main


@pytest.fixture
def code_dir(tmp_path):
    (tmp_path / "environments" / "production" / "data").mkdir(parents=True)
    (tmp_path / "environments" / "staging" / "data").mkdir(parents=True)
    (tmp_path / "modules").mkdir()
    return tmp_path


def run_cli(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_runs_clean_on_empty_tree(code_dir, capsys):
    code, out, err = run_cli(
        capsys, "--code-dir", str(code_dir), "--format", "json",
        "--global-hiera", str(code_dir / "missing-hiera.yaml"))
    assert code == 0
    doc = json.loads(out)
    assert doc["schema_version"] == 1
    assert doc["environments"] == ["production", "staging"]


def test_env_filter(code_dir, capsys):
    code, out, _ = run_cli(
        capsys, "--code-dir", str(code_dir), "--format", "json",
        "--env", "staging")
    assert code == 0
    assert json.loads(out)["environments"] == ["staging"]


def test_env_glob(code_dir, capsys):
    code, out, _ = run_cli(
        capsys, "--code-dir", str(code_dir), "--format", "json",
        "--env-glob", "prod*")
    assert code == 0
    assert json.loads(out)["environments"] == ["production"]


def test_missing_code_dir_is_config_error(tmp_path, capsys):
    code, _, err = run_cli(capsys, "--code-dir", str(tmp_path / "nope"))
    assert code == 2
    assert "code dir not found" in err


def test_no_environments_is_config_error(tmp_path, capsys):
    code, _, err = run_cli(capsys, "--code-dir", str(tmp_path))
    assert code == 2
    assert "no environments" in err


def test_text_format_summary(code_dir, capsys):
    code, out, _ = run_cli(capsys, "--code-dir", str(code_dir))
    assert code == 0
    assert "Summary:" in out


def test_bad_show_section_rejected(code_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--code-dir", str(code_dir), "--show", "bogus"])
    assert exc.value.code == 2
