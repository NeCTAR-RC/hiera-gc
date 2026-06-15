from hiera_gc.consumers.pp_tokens import tokenize


def kinds(text):
    return [(t.kind, t.value) for t in tokenize(text)]


def strings_of(text):
    return [t for t in tokenize(text) if t.kind == "string"]


def test_single_quoted_with_escapes():
    (token,) = strings_of(r"'it\'s \\ fine'")
    assert token.value == "it's \\ fine"
    assert not token.interpolated


def test_double_quoted_interpolation_kept_raw():
    (token,) = strings_of('"port ${lookup(\'sshd::port\')} end"')
    assert token.interpolated
    assert "${lookup('sshd::port')}" in token.value


def test_quotes_inside_interpolation_do_not_end_string():
    (token,) = strings_of('"${foo("a}b")} tail"')
    assert token.value.endswith(" tail")


def test_plain_var_interpolation_flag():
    (token,) = strings_of('"$role::port"')
    assert token.interpolated
    assert not strings_of("'$role'")[0].interpolated


def test_comments_are_discarded():
    tokens = kinds("a # comment 'with quotes' and (\nb /* block\nclass */ c")
    assert tokens == [("ident", "a"), ("ident", "b"), ("ident", "c")]


def test_hash_inside_string_is_not_comment():
    (token,) = strings_of("'#not-a-comment'")
    assert token.value == "#not-a-comment"


def test_heredoc_literal_body_is_opaque():
    text = (
        "$config = @(END)\n"
        "  class fake (\n"
        "  lookup('never')\n"
        "  END\n"
        "include real\n"
    )
    tokens = tokenize(text)
    heredoc = [t for t in tokens if t.kind == "string"][0]
    assert "class fake (" in heredoc.value
    assert not heredoc.interpolated
    idents = [t.value for t in tokens if t.kind == "ident"]
    assert "fake" not in idents
    assert "real" in idents
    include = [t for t in tokens if t.value == "include"][0]
    assert include.line == 5


def test_heredoc_interpolated_and_margin_markers():
    text = '$motd = @("EOT"/n)\n  Welcome ${fqdn}\n  | EOT\n$x = 1\n'
    heredoc = [t for t in tokenize(text) if t.kind == "string"][0]
    assert heredoc.interpolated
    assert "Welcome" in heredoc.value


def test_two_heredocs_on_one_line():
    text = (
        "foo { 'x': a => @(ONE), b => @(TWO) }\n"
        "first body\n"
        "ONE\n"
        "second body\n"
        "TWO\n"
    )
    bodies = [
        t.value
        for t in tokenize(text)
        if t.kind == "string" and t.value not in ("x",)
    ]
    assert bodies == ["first body", "second body"]


def test_regex_vs_division():
    tokens = kinds("$x = $a / 2\nif $y =~ /foo\\/bar/ { }")
    assert ("punct", "/") in tokens
    assert ("regex", "foo\\/bar") in tokens


def test_regex_in_pattern_type_with_brackets_inside():
    tokens = kinds("Pattern[/^(ssh|sftp)$/] $proto")
    assert ("regex", "^(ssh|sftp)$") in tokens
    assert ("var", "proto") in tokens


def test_unterminated_regex_falls_back_to_slash():
    tokens = kinds("$a = 1 / 2 / 3")
    assert tokens.count(("punct", "/")) == 2


def test_qualified_idents_and_variables():
    tokens = kinds("include nectar::role::compute $::osfamily $facts $1")
    assert ("ident", "nectar::role::compute") in tokens
    assert ("var", "osfamily") in tokens
    assert ("var", "facts") in tokens
    assert ("var", "1") in tokens


def test_case_and_selector_regexes():
    tokens = kinds("case $x { /^a$/: {} } $y = $z ? { /b/ => 1 }")
    assert ("regex", "^a$") in tokens
    assert ("regex", "b") in tokens


def test_operators():
    tokens = kinds("a => b == c =~ d !~ e")
    ops = [v for k, v in tokens if k == "op"]
    assert ops == ["=>", "==", "=~", "!~"]


def test_line_numbers():
    tokens = tokenize("a\nb\n  c\n")
    assert [(t.value, t.line) for t in tokens] == [
        ("a", 1),
        ("b", 2),
        ("c", 3),
    ]


def test_never_raises_on_garbage():
    tokenize("\x00 ~~~ `weird` \\ %% '")
    tokenize('"unterminated ${ also unterminated')
    tokenize("@(NEVERENDS)\nbody without end tag")
