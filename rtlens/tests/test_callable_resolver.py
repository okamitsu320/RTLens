"""Tests for callable_resolver utilities."""
from __future__ import annotations

import pytest

from rtlens.model import DesignDB, SourceLoc
from rtlens.callable_resolver import (
    token_variants,
    resolve_callable_key_from_site,
    resolve_callable_key_for_definition_site,
    resolve_callable_key_any_site,
)


# ---------------------------------------------------------------------------
# token_variants
# ---------------------------------------------------------------------------

class TestTokenVariants:
    def test_plain_identifier(self):
        assert token_variants("alu_op") == ["alu_op"]

    def test_strips_semicolon(self):
        result = token_variants("alu_op;")
        assert "alu_op" in result

    def test_scope_resolution(self):
        result = token_variants("pkg::func")
        assert "func" in result
        assert "pkg::func" in result

    def test_arrow_method(self):
        result = token_variants("obj->method")
        assert "method" in result

    def test_dot_member(self):
        result = token_variants("obj.method")
        assert "method" in result

    def test_empty_string(self):
        assert token_variants("") == []

    def test_whitespace_only(self):
        assert token_variants("   ") == []

    def test_no_duplicates(self):
        result = token_variants("foo")
        assert len(result) == len(set(result))


# ---------------------------------------------------------------------------
# Helpers to build a minimal DesignDB with one callable
# ---------------------------------------------------------------------------

def _make_db_with_callable(
    key: str = "function:callable.alu_op",
    kind: str = "function",
    short_name: str = "alu_op",
    def_file: str = "callable.sv",
    def_line: int = 6,
    ref_file: str = "callable.sv",
    ref_line: int = 14,
    ref_token: str = "alu_op",
) -> DesignDB:
    db = DesignDB()
    db.callable_defs[key] = SourceLoc(file=def_file, line=def_line)
    db.callable_kinds[key] = kind
    db.callable_names[key] = short_name
    db.callable_name_index.setdefault(short_name, set()).add(key)
    db.callable_ref_sites[(ref_file, ref_line, ref_token)] = [key]
    db.callable_def_sites[(def_file, def_line, short_name)] = [key]
    return db


# ---------------------------------------------------------------------------
# resolve_callable_key_from_site
# ---------------------------------------------------------------------------

class TestResolveCallableKeyFromSite:
    def test_resolves_via_ref_site(self):
        db = _make_db_with_callable()
        result = resolve_callable_key_from_site(db, "callable.sv", 14, "alu_op")
        assert result == "function:callable.alu_op"

    def test_resolves_via_name_fallback(self):
        # Wrong line — no ref site hit, but name index still finds it
        db = _make_db_with_callable()
        result = resolve_callable_key_from_site(db, "callable.sv", 99, "alu_op")
        assert result == "function:callable.alu_op"

    def test_returns_none_for_unknown_name(self):
        db = _make_db_with_callable()
        result = resolve_callable_key_from_site(db, "callable.sv", 14, "unknown_func")
        assert result is None

    def test_returns_none_for_empty_file(self):
        db = _make_db_with_callable()
        result = resolve_callable_key_from_site(db, "", 14, "alu_op")
        assert result is None

    def test_returns_none_for_empty_word(self):
        db = _make_db_with_callable()
        result = resolve_callable_key_from_site(db, "callable.sv", 14, "")
        assert result is None

    def test_hier_path_does_not_break_resolution(self):
        db = _make_db_with_callable()
        result = resolve_callable_key_from_site(
            db, "callable.sv", 14, "alu_op", current_hier_path="callable"
        )
        assert result == "function:callable.alu_op"

    def test_prefers_function_over_other_kinds(self):
        db = DesignDB()
        func_key = "function:mod.my_func"
        module_key = "module:my_func"
        for key, kind in [(func_key, "function"), (module_key, "module")]:
            db.callable_defs[key] = SourceLoc(file="x.sv", line=10)
            db.callable_kinds[key] = kind
            db.callable_names[key] = "my_func"
            db.callable_name_index.setdefault("my_func", set()).add(key)
        result = resolve_callable_key_from_site(db, "x.sv", 20, "my_func")
        assert result == func_key


# ---------------------------------------------------------------------------
# resolve_callable_key_for_definition_site
# ---------------------------------------------------------------------------

class TestResolveForDefinitionSite:
    def test_resolves_at_definition_line(self):
        db = _make_db_with_callable()
        result = resolve_callable_key_for_definition_site(db, "callable.sv", 6, "alu_op")
        assert result == "function:callable.alu_op"

    def test_falls_back_to_name_when_no_def_site(self):
        db = _make_db_with_callable()
        result = resolve_callable_key_for_definition_site(db, "callable.sv", 99, "alu_op")
        assert result == "function:callable.alu_op"


# ---------------------------------------------------------------------------
# resolve_callable_key_any_site
# ---------------------------------------------------------------------------

class TestResolveCallableKeyAnySite:
    def test_finds_ref_site_on_line(self):
        db = _make_db_with_callable(ref_line=14)
        result = resolve_callable_key_any_site(db, "callable.sv", 14)
        assert result == "function:callable.alu_op"

    def test_finds_def_site_on_line(self):
        db = _make_db_with_callable(def_line=6)
        result = resolve_callable_key_any_site(db, "callable.sv", 6)
        assert result == "function:callable.alu_op"

    def test_returns_none_for_empty_file(self):
        db = _make_db_with_callable()
        result = resolve_callable_key_any_site(db, "", 14)
        assert result is None

    def test_returns_none_for_line_with_no_sites(self):
        db = _make_db_with_callable(ref_line=14)
        result = resolve_callable_key_any_site(db, "callable.sv", 99)
        assert result is None

    def test_prefer_kinds_filters_to_function(self):
        db = DesignDB()
        func_key = "function:mod.fn"
        module_key = "module:mod"
        for key, kind in [(func_key, "function"), (module_key, "module")]:
            db.callable_defs[key] = SourceLoc(file="x.sv", line=5)
            db.callable_kinds[key] = kind
            db.callable_names[key] = key.split(".")[-1]
            db.callable_ref_sites[("x.sv", 5, key.split(".")[-1])] = [key]
        result = resolve_callable_key_any_site(db, "x.sv", 5, prefer_kinds=("function",))
        assert result == func_key
