import pytest

from hiera_gc.consumers.pp_classes import extract_definitions
from hiera_gc.consumers.pp_tokens import tokenize


def extract(text):
    return extract_definitions(tokenize(text))


def class_params(text, name=None):
    defs = extract(text)
    assert defs.classes, f"no class found in: {text}"
    found = (
        defs.classes[0]
        if name is None
        else [c for c in defs.classes if c.name == name][0]
    )
    return [p.name for p in found.params]


NASTY_HEADER = """\
# frozen
class sshd (
  String $banner = min(3, 4),          # function default
  Hash $config = { 'a' => [1, 2], 'b' => { 'c' => 3 } },
  Pattern[/^(ssh|sftp)$/] $protocol = 'ssh',
  Optional[String] $weird = 'class fake ( $phantom )',
  $untyped,
  Integer $port = 22,
) inherits sshd::params {
  $body = @(END)
    class heredoc_phantom ( $nope ) {}
    END
  notify { 'done': }
}
"""


def test_nasty_header():
    defs = extract(NASTY_HEADER)
    assert [c.name for c in defs.classes] == ["sshd"]
    assert class_params(NASTY_HEADER) == [
        "banner",
        "config",
        "protocol",
        "weird",
        "untyped",
        "port",
    ]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("class empty {}", []),
        ("class one::two::three ($a, $b) {}", ["a", "b"]),
        (
            "class t (String $x = 'a,b', Array $y = [1, [2, (3)]]) {}",
            ["x", "y"],
        ),
        ("class multi (\n  $a,\n  $b\n) {}", ["a", "b"]),
        ("class trail ($a,) {}", ["a"]),
    ],
)
def test_param_extraction(text, expected):
    assert class_params(text) == expected


def test_resource_style_class_declaration_skipped():
    defs = extract("class { 'sshd': port => 22 }")
    assert defs.classes == []


def test_class_keyword_as_hash_key_skipped():
    defs = extract("$h = { class => 'x' }\nfoo { 'a': class => 'b' }")
    assert defs.classes == []


def test_define_recorded_separately():
    defs = extract("define sshd::conf (String $line) { }")
    assert defs.classes == []
    assert [d.name for d in defs.defines] == ["sshd::conf"]
    assert [p.name for p in defs.defines[0].params] == ["line"]


def test_inherits_without_params():
    defs = extract(
        "class a::role::x inherits a::role::base {\n  include p::q\n}"
    )
    assert [c.name for c in defs.classes] == ["a::role::x"]
    assert defs.classes[0].params == []


def test_node_definitions():
    defs = extract("""
node 'cc5.example.com' {
  include role::x
}
node /^oc[1-2]\\.example\\.com$/, 'extra.example.com' {
  include role::y
}
node default {}
""")
    assert len(defs.nodes) == 3
    assert defs.nodes[0].patterns == [("literal", "cc5.example.com")]
    assert defs.nodes[1].patterns == [
        ("regex", "^oc[1-2]\\.example\\.com$"),
        ("literal", "extra.example.com"),
    ]
    assert defs.nodes[2].patterns == [("default", "default")]


def test_node_as_resource_attribute_not_a_nodedef():
    defs = extract("foo { 'a': node => 'x' }")
    assert defs.nodes == []


ARDC_SELECTOR = """\
$nodegroup = $facts['clientcert'] ? {
  /^oc[1-2]\\.svc\\.example$/  => 'artm-oc',
  /^cc[1-9]\\.svc\\.example$/  => 'artm-cn',
  'special.svc.example'      => 'artm-sp',
  default                    => 0,
}
"""


def test_selector_assignment_values_collected():
    defs = extract(ARDC_SELECTOR)
    (assign,) = defs.assignments
    assert assign.var == "nodegroup"
    assert assign.literal
    assert set(assign.values) == {"artm-oc", "artm-cn", "artm-sp", "0"}


def test_selector_with_expression_value_incomplete():
    defs = extract(
        "$hw = $facts['x'] ? { /a/ => 'one', default => $facts['y'] }"
    )
    (assign,) = defs.assignments
    assert not assign.literal


def test_simple_assignments():
    defs = extract("$a = 'plain'\n$b = \"interp${x}\"\n$c = $other\n$d = 5")
    by_var = {a.var: a for a in defs.assignments}
    assert by_var["a"].literal and by_var["a"].values == ["plain"]
    assert not by_var["b"].literal
    assert not by_var["c"].literal
    assert by_var["d"].literal and by_var["d"].values == ["5"]


def test_concatenation_not_treated_as_literal():
    defs = extract("$a = 'pre' + $x")
    (assign,) = defs.assignments
    assert not assign.literal


def test_class_keyword_inside_strings_and_comments_ignored():
    defs = extract("""
# class commented ( $a ) {}
$s = 'class stringy ( $b ) {}'
$d = "class dquoted ( $c ) {}"
class real ($yes) {}
""")
    assert [c.name for c in defs.classes] == ["real"]
