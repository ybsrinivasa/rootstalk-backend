"""Unit tests for `app.services.i18n_cosh.pick_translation`.

Pure function — no DB / fixtures needed. The DB-backed
`resolve_names_by_cosh_id` is exercised end-to-end through the
biological-name resolution tests (see the diagnosis / discover suites).
"""
from app.services.i18n_cosh import pick_translation


def test_lang_present_returns_lang():
    assert (
        pick_translation({"en": "Paddy", "hi": "धान"}, "hi", "FB")
        == "धान"
    )


def test_lang_absent_falls_back_to_english():
    assert (
        pick_translation({"en": "Paddy"}, "hi", "FB")
        == "Paddy"
    )


def test_neither_lang_nor_english_falls_back_to_fallback():
    # 'ta' requested, only 'hi' present → fallback fires (we never
    # cross-translate by guessing a sibling language).
    assert (
        pick_translation({"hi": "धान"}, "ta", "FB")
        == "FB"
    )


def test_legacy_english_alias_key():
    # Pre-2026-05 some resolvers wrote both 'en' and 'English'. Tolerate
    # the alias when 'en' is missing.
    assert (
        pick_translation({"English": "Paddy"}, "hi", "FB")
        == "Paddy"
    )


def test_lang_matches_takes_precedence_over_english_alias():
    assert (
        pick_translation({"English": "Paddy", "hi": "धान"}, "hi", "FB")
        == "धान"
    )


def test_none_translations():
    assert pick_translation(None, "hi", "FB") == "FB"


def test_empty_dict():
    assert pick_translation({}, "hi", "FB") == "FB"


def test_empty_lang_value_falls_through_to_english():
    # Empty string in the target lang is treated as missing — Cosh
    # has been seen to ship empty strings on partially-translated rows.
    assert (
        pick_translation({"hi": "", "en": "Paddy"}, "hi", "FB")
        == "Paddy"
    )


def test_empty_fallback_returns_empty_string():
    # Default fallback is empty string; useful when the caller wants
    # to detect "no name" with a truthy check.
    assert pick_translation({}, "hi") == ""


def test_default_lang_is_english():
    # Resolving with lang="en" still works (no special case needed).
    assert (
        pick_translation({"en": "Paddy"}, "en", "FB")
        == "Paddy"
    )
