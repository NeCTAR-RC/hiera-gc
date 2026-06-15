from pathlib import Path

from hiera_gc.consumers.pp_lookups import extract_lookups
from hiera_gc.consumers.pp_tokens import tokenize

FILE = Path("test.pp")


def extract(text):
    return extract_lookups(tokenize(text), FILE)


def test_simple_lookup_functions():
    consumers = extract("""
$a = lookup('app::port')
$b = hiera('legacy::key')
$c = hiera_array('array::key')
$d = hiera_hash('hash::key')
hiera_include('classes')
""")
    by_key = {c.key: c for c in consumers}
    assert set(by_key) == {
        "app::port",
        "legacy::key",
        "array::key",
        "hash::key",
        "classes",
    }
    assert not by_key["app::port"].merge
    assert by_key["array::key"].merge
    assert by_key["hash::key"].merge
    assert by_key["classes"].detail == "hiera_include()"
    assert by_key["app::port"].line == 2


def test_lookup_with_array_of_keys():
    consumers = extract("$x = lookup(['first::key', 'second::key'])")
    assert sorted(c.key for c in consumers) == ["first::key", "second::key"]


def test_lookup_hash_form():
    consumers = extract(
        "$x = lookup({'name' => 'hash::form', 'merge' => 'deep'})"
    )
    (consumer,) = consumers
    assert consumer.key == "hash::form"
    assert consumer.merge


def test_lookup_with_merge_argument():
    consumers = extract("$x = lookup('k', Hash, 'deep')")
    assert consumers[0].merge
    consumers = extract("$y = lookup('k2', Hash)")
    assert not consumers[0].merge


def test_interpolated_key_becomes_pattern():
    consumers = extract('$x = lookup("${service}::port")')
    (consumer,) = consumers
    assert consumer.key is None
    assert consumer.pattern == "*::port"

    consumers = extract('$y = lookup("nectar::profile::${name}::tag")')
    assert consumers[0].pattern == "nectar::profile::*::tag"


def test_variable_key_is_opaque_dynamic():
    consumers = extract("$x = lookup($keyvar)")
    (consumer,) = consumers
    assert consumer.kind == "dynamic"
    assert consumer.key is None and consumer.pattern is None


def test_deferred_lookup():
    consumers = extract("$pw = Deferred('lookup', ['secret::password'])")
    (consumer,) = consumers
    assert consumer.key == "secret::password"


def test_lookup_in_class_param_default():
    consumers = extract("class p (String $x = lookup('p::extra')) {}")
    assert consumers[0].key == "p::extra"


def test_dotted_key_recorded_verbatim():
    consumers = extract("$x = lookup('top.sub.key')")
    assert consumers[0].key == "top.sub.key"


def test_method_call_named_lookup_skipped():
    assert extract("$x = $registry.lookup('not::hiera')") == []


def test_nested_lookup_in_default_argument():
    consumers = extract("$x = lookup('outer', String, undef, lookup('inner'))")
    keys = {c.key for c in consumers}
    assert keys == {"outer", "inner"}
