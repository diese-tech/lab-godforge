"""Matcher coverage for the `.r67` passive detector (Issue #47, Gate 5)."""

import pytest

from utils.r67 import matcher

ELIGIBLE = [
    "67",
    "6 7",
    "six seven",
    "six-seven",
    "sixty seven",
    "sixty-seven",
    "6/7",
    "6-7",
    "6.7",
]

REJECTED = [
    "167",
    "670",
    "abc67",
    "67abc",
    "67th",
    "6.7.8",
    "6-7-8",
    "version 1.6.7 released",
    "version 1.67.0",
    "10.67.0.1",
    "id 6-67-8",
    "6.67",
    "build 2.67.3",
]


@pytest.mark.parametrize("text", ELIGIBLE)
def test_eligible_standalone_forms_match(text):
    assert matcher.is_qualifying(text) is True


@pytest.mark.parametrize("text", ELIGIBLE)
def test_eligible_forms_match_inside_a_sentence(text):
    assert matcher.is_qualifying(f"well {text} then") is True


@pytest.mark.parametrize("text", REJECTED)
def test_rejected_forms_do_not_match(text):
    assert matcher.is_qualifying(text) is False


def test_written_forms_are_case_insensitive():
    assert matcher.is_qualifying("SIX SEVEN") is True
    assert matcher.is_qualifying("Sixty-Seven") is True


def test_url_containing_67_is_ignored():
    assert matcher.is_qualifying("look at https://example.com/67 cool") is False
    assert matcher.is_qualifying("go to www.site67.com now") is False


def test_inline_code_containing_67_is_ignored():
    assert matcher.is_qualifying("run `value = 67` please") is False


def test_fenced_code_block_containing_67_is_ignored():
    assert matcher.is_qualifying("```\nx = 67\n```") is False
    assert matcher.is_qualifying("before ```67``` after") is False


def test_real_67_outside_code_still_matches():
    assert matcher.is_qualifying("`code` and also 67") is True


def test_empty_and_none_are_safe():
    assert matcher.is_qualifying("") is False
    assert matcher.is_qualifying(None) is False


def test_phone_like_and_id_strings_do_not_match():
    assert matcher.is_qualifying("call 555-6-7-890") is False
    assert matcher.is_qualifying("id 4670015") is False


def test_sentence_ending_period_after_67_still_matches():
    assert matcher.is_qualifying("it's 67.") is True
    assert matcher.is_qualifying("got there, 67!") is True
