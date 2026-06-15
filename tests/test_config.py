from hiera_gc.config import RunConfig, discover_environments


def make_env(root, name):
    env = root / name
    (env / "data").mkdir(parents=True)
    return env


def test_default_root_only(tmp_path):
    code = tmp_path / "code"
    make_env(code / "environments", "production")
    make_env(code / "environments", "staging")
    envs = discover_environments(RunConfig(code_dir=code))
    assert [e.name for e in envs] == ["production", "staging"]


def test_extra_env_dir_discovered(tmp_path):
    code = tmp_path / "code"
    make_env(code / "environments", "production")
    extra = tmp_path / "extra-envs"
    make_env(extra, "research")
    make_env(extra, "testbed")

    problems = []
    envs = discover_environments(
        RunConfig(code_dir=code, env_dirs=[extra]), problems
    )
    names = {e.name for e in envs}
    assert names == {"production", "research", "testbed"}
    # Each environment keeps its real path, default root first.
    by_name = {e.name: e.path for e in envs}
    assert by_name["production"] == code / "environments" / "production"
    assert by_name["research"] == extra / "research"
    assert not problems


def test_name_clash_first_root_wins(tmp_path):
    code = tmp_path / "code"
    make_env(code / "environments", "production")
    extra = tmp_path / "extra-envs"
    make_env(extra, "production")

    problems = []
    envs = discover_environments(
        RunConfig(code_dir=code, env_dirs=[extra]), problems
    )
    # Only one 'production', and it is the default-root copy.
    assert [e.name for e in envs] == ["production"]
    assert envs[0].path == code / "environments" / "production"
    assert any("is ignored" in p for p in problems)


def test_missing_extra_root_recorded(tmp_path):
    code = tmp_path / "code"
    make_env(code / "environments", "production")
    problems = []
    envs = discover_environments(
        RunConfig(code_dir=code, env_dirs=[tmp_path / "nowhere"]), problems
    )
    assert [e.name for e in envs] == ["production"]
    assert any("does not exist" in p for p in problems)


def test_filters_apply_across_roots(tmp_path):
    code = tmp_path / "code"
    make_env(code / "environments", "production")
    extra = tmp_path / "extra-envs"
    make_env(extra, "research")
    make_env(extra, "testbed")

    glob_envs = discover_environments(
        RunConfig(code_dir=code, env_dirs=[extra], env_glob="*b*")
    )
    assert [e.name for e in glob_envs] == ["testbed"]

    named = discover_environments(
        RunConfig(code_dir=code, env_dirs=[extra], envs=["research"])
    )
    assert [e.name for e in named] == ["research"]


def test_duplicate_root_not_scanned_twice(tmp_path):
    code = tmp_path / "code"
    make_env(code / "environments", "production")
    extra = tmp_path / "extra-envs"
    make_env(extra, "research")
    problems = []
    envs = discover_environments(
        RunConfig(code_dir=code, env_dirs=[extra, extra]), problems
    )
    # Repeating the same root must not shadow its own environments.
    assert [e.name for e in envs] == ["production", "research"]
    assert not problems
