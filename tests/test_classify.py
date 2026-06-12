from pathlib import Path

from hiera_gc.analysis import analyse
from hiera_gc.config import Environment, RunConfig


def build_tree(tmp_path: Path) -> Path:
    code = tmp_path / "code"

    sshd = code / "modules" / "sshd"
    (sshd / "manifests").mkdir(parents=True)
    (sshd / "manifests" / "init.pp").write_text("""\
class sshd (
  Integer $port,
  String $banner = 'none',
) {
  $extras = lookup('sshd::extras')
}
""")
    (sshd / "manifests" / "conf.pp").write_text(
        "define sshd::conf (String $line) { }\n")
    (sshd / "templates").mkdir()
    (sshd / "templates" / "motd.erb").write_text(
        "Banner <%= scope['sshd::motd_text'] %>\n")
    (sshd / "hiera.yaml").write_text("""\
version: 5
hierarchy:
  - name: module defaults
    path: common.yaml
""")
    (sshd / "data").mkdir()
    (sshd / "data" / "common.yaml").write_text(
        "sshd::port: 22\nmodule_unused: 1\n")

    env = code / "environments" / "production"
    (env / "manifests").mkdir(parents=True)
    (env / "environment.conf").write_text(
        "modulepath = site:modules:$basemodulepath\n")
    (env / "manifests" / "site.pp").write_text("""\
# decommission note: mentioned_key still referenced by backup scripts
node /^web/ {
  include profile::web
}
$shared = lookup('shared_used')
$dyn = lookup("prefix::${facts['svc']}::setting")
$bad = lookup($oops)
""")
    (env / "site" / "profile" / "manifests").mkdir(parents=True)
    (env / "site" / "profile" / "manifests" / "web.pp").write_text(
        "class profile::web (String $vhost) {\n  include sshd\n}\n")

    shared = code / "hieradata"
    shared.mkdir()
    (shared / "common.yaml").write_text(
        "shared_used: 1\nshared_unused: 1\n")

    (env / "hiera.yaml").write_text("""\
version: 5
defaults:
  datadir: data
hierarchy:
  - name: env
    lookup_key: eyaml_lookup_key
    paths:
      - base.yaml
      - secrets.yaml
  - name: shared
    datadir: '%s'
    paths:
      - common.yaml
""" % shared)
    (env / "data").mkdir()
    (env / "data" / "base.yaml").write_text("""\
profile::web::vhost: 'a.example'
sshd::extras: [x]
sshd::motd_text: 'hi'
unused_key: 1
profile::web::gone: 1
sshd::conf::line: 1
prefix::foo::setting: 1
mentioned_key: 1
optioned_key: {}
real_secret: "%{alias('_hidden_secret')}"
lookup_options:
  optioned_key:
    merge: deep
""")
    (env / "data" / "secrets.yaml").write_text("""\
_hidden_secret: ENC[GPG,abcdef]
_orphan_secret: ENC[GPG,123456]
""")
    return code


def run(tmp_path, **kwargs):
    code = build_tree(tmp_path)
    config = RunConfig(code_dir=code, global_hiera=None, **kwargs)
    envs = [Environment("production", code / "environments" / "production")]
    return analyse(config, envs)


def statuses(result):
    return {f.key.name: f for f in result.keys}


def test_classification(tmp_path):
    result = run(tmp_path)
    by_name = statuses(result)

    def status(name):
        return by_name[name].status

    # Strong consumers.
    assert status("profile::web::vhost") == "USED"           # APL
    assert by_name["profile::web::vhost"].reason.kind == "apl"
    assert status("sshd::port") == "USED"                    # module data APL
    assert status("sshd::extras") == "USED"                  # lookup() in pp
    assert status("shared_used") == "USED"                   # shared layer
    assert status("_hidden_secret") == "USED"                # alias chain
    assert by_name["_hidden_secret"].reason.kind == "data_alias"
    assert status("lookup_options") == "USED"
    assert by_name["lookup_options"].reason.kind == "builtin"

    # Weak consumers.
    assert status("sshd::motd_text") == "POSSIBLY_USED"      # erb scope[]
    assert status("prefix::foo::setting") == "POSSIBLY_USED"
    assert by_name["prefix::foo::setting"].reason.kind == "dynamic_pattern"
    assert status("mentioned_key") == "POSSIBLY_USED"        # comment
    assert by_name["mentioned_key"].reason.kind == "mention"
    assert status("optioned_key") == "POSSIBLY_USED"
    assert by_name["optioned_key"].reason.kind == "lookup_options_ref"

    # Unused.
    assert status("unused_key") == "UNUSED"
    assert status("shared_unused") == "UNUSED"
    assert status("module_unused") == "UNUSED"
    # A key whose only textual occurrence is its own definition must not
    # be rescued by the mentions pass.
    assert status("_orphan_secret") == "UNUSED"

    # Annotations.
    assert status("profile::web::gone") == "UNUSED"
    assert "has no parameter $gone" in by_name["profile::web::gone"].stale_param
    assert status("sshd::conf::line") == "UNUSED"
    assert "defined type" in by_name["sshd::conf::line"].define_shape


def test_visible_envs_and_layers(tmp_path):
    result = run(tmp_path)
    by_name = statuses(result)
    assert by_name["shared_used"].key.layer == "shared"
    assert by_name["shared_used"].envs == ["production"]
    assert by_name["module_unused"].key.layer == "module"
    assert by_name["unused_key"].key.layer == "environment"


def test_dynamic_lookup_warning(tmp_path):
    result = run(tmp_path)
    dynamic = [w for w in result.warnings if w.kind == "dynamic_lookup"]
    assert len(dynamic) == 1
    assert dynamic[0].file.endswith("site.pp")


def test_allowlist(tmp_path):
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text("# comment\nunused_.*\n")
    result = run(tmp_path, allowlist=allowlist)
    by_name = statuses(result)
    assert by_name["unused_key"].allowlisted
    assert not by_name["shared_unused"].allowlisted
    counts = result.counts()
    assert counts["allowlisted"] == 1
    unused_names = [f.key.name for f in result.keys
                    if f.status == "UNUSED" and not f.allowlisted]
    assert "unused_key" not in unused_names
