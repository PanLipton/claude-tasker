# -*- coding: utf-8 -*-
"""Tests for the time formatting / parsing helpers."""
from datetime import datetime, timezone


# -- fmt_countdown ----------------------------------------------------------
def test_fmt_countdown_none(ct_mod):
    assert ct_mod.fmt_countdown(None) == "--"
    assert ct_mod.fmt_countdown(0) == "--"


def test_fmt_countdown_past_is_now(ct_mod, clock):
    assert ct_mod.fmt_countdown(clock["t"] - 5) == "now"
    assert ct_mod.fmt_countdown(clock["t"]) == "now"


def test_fmt_countdown_minutes(ct_mod, clock):
    assert ct_mod.fmt_countdown(clock["t"] + 2 * 60) == "2m"
    assert ct_mod.fmt_countdown(clock["t"] + 59 * 60) == "59m"


def test_fmt_countdown_hours(ct_mod, clock):
    out = ct_mod.fmt_countdown(clock["t"] + 3 * 3600 + 30 * 60)
    assert out == "3h 30m"


def test_fmt_countdown_days(ct_mod, clock):
    out = ct_mod.fmt_countdown(clock["t"] + 2 * 86400 + 5 * 3600)
    assert out == "2d 05h"


# -- fmt_clock --------------------------------------------------------------
def test_fmt_clock_none(ct_mod):
    assert ct_mod.fmt_clock(None) == "--"


def test_fmt_clock_formats_local(ct_mod):
    # build an epoch from a known local time and read it back through strftime
    dt = datetime(2026, 6, 5, 14, 30)
    epoch = dt.timestamp()
    assert ct_mod.fmt_clock(epoch) == dt.strftime("%a %H:%M")


# -- parse_at ---------------------------------------------------------------
def test_parse_at_valid(ct_mod):
    epoch = ct_mod.parse_at("2026-06-05", "03:00")
    assert epoch == datetime(2026, 6, 5, 3, 0).timestamp()


def test_parse_at_trims_whitespace(ct_mod):
    assert ct_mod.parse_at("  2026-06-05 ", " 03:00 ") == \
        datetime(2026, 6, 5, 3, 0).timestamp()


def test_parse_at_invalid(ct_mod):
    assert ct_mod.parse_at("not-a-date", "03:00") is None
    assert ct_mod.parse_at("2026-06-05", "25:99") is None
    assert ct_mod.parse_at("", "") is None


# -- parse_reset ------------------------------------------------------------
def test_parse_reset_with_tz(ct_mod):
    iso = "2026-06-05T10:00:00+00:00"
    expected = datetime(2026, 6, 5, 10, 0, tzinfo=timezone.utc).timestamp()
    assert ct_mod.parse_reset(iso) == expected


def test_parse_reset_naive_treated_as_utc(ct_mod):
    iso = "2026-06-05T10:00:00"
    expected = datetime(2026, 6, 5, 10, 0, tzinfo=timezone.utc).timestamp()
    assert ct_mod.parse_reset(iso) == expected


def test_parse_reset_empty_and_invalid(ct_mod):
    assert ct_mod.parse_reset(None) is None
    assert ct_mod.parse_reset("") is None
    assert ct_mod.parse_reset("garbage") is None
