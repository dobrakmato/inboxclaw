import pytest
from src.utils.filtering import normalize_text, matches_filter

class MockFilter:
    def __init__(self, contains=None, regex=None):
        self.contains = contains
        self.regex = regex

def test_normalize_text():
    assert normalize_text("  hello  world  ") == "hello world"
    assert normalize_text("line1\nline2\r\nline3") == "line1 line2 line3"
    assert normalize_text("tab\tspace") == "tab space"
    assert normalize_text("multiple     spaces") == "multiple spaces"
    assert normalize_text("&quot;quoted&quot;") == '"quoted"'
    assert normalize_text("  &lt;tag&gt;  \n  next line  ") == "<tag> next line"
    assert normalize_text(None) == ""
    assert normalize_text("") == ""

def test_matches_filter_contains():
    f = MockFilter(contains="hello")
    assert matches_filter("hello world", f) is True
    assert matches_filter("HELLO world", f) is True
    assert matches_filter("  he&rho;  ", MockFilter(contains="heρ")) is True # Testing entities if they come up
    
    # Test normalized match
    f_norm = MockFilter(contains="a b")
    assert matches_filter("a\n  b", f_norm) is True
    assert matches_filter("a     b", f_norm) is True

def test_matches_filter_regex():
    f = MockFilter(regex=r"^start")
    assert matches_filter("start here", f) is True
    assert matches_filter("  start here", f) is True # Leading whitespace stripped by normalize
    assert matches_filter("START here", f) is True # re.IGNORECASE by default in our helper
    
    f2 = MockFilter(regex=r"e.d$")
    assert matches_filter("the end", f2) is True
    assert matches_filter("the end  ", f2) is True # Trailing whitespace stripped
    
    # Test regex with multiple lines
    f3 = MockFilter(regex=r"first.*second")
    assert matches_filter("first\nsecond", f3) is True # Normalized to "first second"

def test_matches_filter_empty():
    f = MockFilter(contains="foo")
    assert matches_filter("", f) is False
    assert matches_filter(None, f) is False
    assert matches_filter("   ", f) is False
