from pathlib import Path

from hiera_gc.consumers.epp import extract_epp, mask_epp
from hiera_gc.consumers.erb import extract_erb
from hiera_gc.consumers.ruby_plugins import extract_ruby

FILE = Path("template")

EPP = """\
# A config file
<%- |$listen_port, $banner| -%>
port = <%= $listen_port %>
banner = <%= lookup('sshd::motd') %>
<%# comment with lookup('not::this') %>
<% if $banner { %>
extras = <%= lookup('sshd::extras', Array, 'unique') %>
<% } %>
plain text lookup('also::not::this') outside tags
"""


def test_epp_lookups_and_lines():
    consumers = extract_epp(EPP, FILE)
    by_key = {c.key: c for c in consumers}
    assert set(by_key) == {"sshd::motd", "sshd::extras"}
    assert by_key["sshd::motd"].line == 4
    assert by_key["sshd::extras"].merge
    assert all(c.kind == "epp_lookup" for c in consumers)


def test_epp_mask_preserves_line_count():
    assert mask_epp(EPP).count("\n") == EPP.count("\n")


ERB = """\
# managed by puppet
Port <%= scope['sshd::port'] %>
Banner <%= scope.call_function('lookup', ['sshd::banner']) %>
Legacy <%= scope.function_hiera(['sshd::legacy']) %>
Merged <%= scope.function_hiera_hash(['sshd::merged']) %>
Old <%= scope.lookupvar('sshd::old') %>
<%# Comment <%= scope['sshd::commented'] %1> %>
"""


def test_erb_extraction():
    consumers = extract_erb(ERB.replace("%1>", "%>"), FILE)
    by_key = {c.key: c for c in consumers}
    assert set(by_key) == {
        "sshd::port",
        "sshd::banner",
        "sshd::legacy",
        "sshd::merged",
        "sshd::old",
    }
    assert by_key["sshd::port"].kind == "erb_var"
    assert by_key["sshd::banner"].kind == "erb_lookup"
    assert by_key["sshd::banner"].line == 3
    assert by_key["sshd::merged"].merge
    assert by_key["sshd::old"].kind == "erb_var"


RUBY = """\
Puppet::Functions.create_function(:'sshd::pick_key') do
  def pick_key
    val = call_function('lookup', ['sshd::ruby_key', nil, nil, 'dflt'])
    other = call_function('lookup', 'sshd::plain_key')
    legacy = scope.lookupvar('sshd::ruby_var')
  end
end
"""


def test_ruby_extraction():
    consumers = extract_ruby(RUBY, FILE)
    by_key = {c.key: c for c in consumers}
    assert set(by_key) == {
        "sshd::ruby_key",
        "sshd::plain_key",
        "sshd::ruby_var",
    }
    assert by_key["sshd::ruby_key"].kind == "ruby_lookup"
    assert by_key["sshd::ruby_key"].line == 3
    assert by_key["sshd::ruby_var"].kind == "ruby_var"
