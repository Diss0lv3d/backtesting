import json

import pandas as pd
import pytest

from perp_lab import runs
from perp_lab.data import columns
from perp_lab.data.columns import ColumnGroup
from tests.conftest import market_config

PRE_CHANGE_DATA_ID = "47eef1a72d124acd"


def fake_group(rule=1, name="fake"):
    return ColumnGroup(
        name=name, columns=("f_x",), prefix="f_", rule=rule, synthetic_id=9, exam_required="if_read",
        helpers=(), paper_ready=False, load_source=None, attach=lambda b, s, tf, src: b,
        synthetic=lambda b, seed: b, sources=lambda cfg, symbols, tf: [("BTCUSDT", tf)])


@pytest.fixture(autouse=True)
def fake_group_alone(monkeypatch):
    """These tests exercise the registry with a fake group alone; the reference group's own
    registration is pinned in tests/test_reference.py."""
    monkeypatch.setattr(columns, "GROUPS", ())


@pytest.fixture
def cfg(tmp_path):
    return market_config(tmp_path)


def manifest(cfg, entries, symbol="BTCUSDT", dataset="4h"):
    d = cfg.data_dir / symbol / dataset
    d.mkdir(parents=True, exist_ok=True)
    (d / "_manifest.json").write_text(json.dumps(entries))


def months(cfg):
    return [str(m) for m in pd.period_range("2025-01", "2026-06", freq="M")]


def good(cfg):
    return {m: {"complete": True, "rows": 1, "source": "monthly"} for m in months(cfg)}


def with_gaps(cfg, gaps, exclusions=()):
    raw = dict(cfg.raw)
    raw["column_groups"] = {"fake": {"known_gaps": gaps, "exclusions": list(exclusions)}}
    cfg.raw = raw
    return cfg


def test_complete_months_give_no_problem(cfg):
    manifest(cfg, good(cfg))
    with columns.registered(fake_group()):
        assert columns.column_group_problems(cfg, ["ETHUSDT"], "4h") == []


def test_incomplete_month_is_one_problem(cfg):
    m = good(cfg)
    m["2025-05"]["complete"] = False
    manifest(cfg, m)
    with columns.registered(fake_group()):
        p = columns.column_group_problems(cfg, ["ETHUSDT"], "4h")
    assert len(p) == 1 and all(w in p[0] for w in ("fake", "BTCUSDT", "4h", "2025-05"))


def test_missing_manifest_is_a_problem(cfg):
    with columns.registered(fake_group()):
        p = columns.column_group_problems(cfg, ["ETHUSDT"], "1h")
    assert len(p) == len(months(cfg)) + 1 and "2025-01" in p[0]


def test_absent_before_first_real_month_is_fine(cfg):
    m = good(cfg)
    for k in ("2025-01", "2025-02"):
        m[k] = {"complete": True, "rows": 0, "source": "absent"}
    manifest(cfg, m)
    with columns.registered(fake_group()):
        assert columns.column_group_problems(cfg, [], "4h") == []


def test_absent_after_first_real_month_is_a_problem(cfg):
    m = good(cfg)
    m["2025-08"] = {"complete": True, "rows": 0, "source": "absent"}
    manifest(cfg, m)
    with columns.registered(fake_group()):
        p = columns.column_group_problems(cfg, [], "4h")
    assert len(p) == 1 and "2025-08" in p[0]


def test_known_gap_clears_its_month(cfg):
    m = good(cfg)
    m["2025-05"]["complete"] = False
    manifest(cfg, m)
    with_gaps(cfg, [{"symbol": "BTCUSDT", "dataset": "4h", "month": "2025-05", "why": "exchange outage"}])
    with columns.registered(fake_group()):
        assert columns.column_group_problems(cfg, [], "4h") == []


@pytest.mark.parametrize("name", ["BTCUSDT", "BTC"])
def test_excluded_symbol_is_skipped(cfg, name):
    with_gaps(cfg, [], exclusions=[name])
    with columns.registered(fake_group()):
        assert columns.column_group_problems(cfg, [], "4h") == []


def test_all_absent_source_is_one_problem_and_exclusion_clears_it(cfg):
    manifest(cfg, {m: {"complete": True, "rows": 0, "source": "absent"} for m in months(cfg)})
    with columns.registered(fake_group()):
        p = columns.column_group_problems(cfg, [], "4h")
        assert len(p) == 1 and "no real month" in p[0] and "BTCUSDT/4h" in p[0]
        with_gaps(cfg, [], exclusions=["BTC"])
        assert columns.column_group_problems(cfg, [], "4h") == []


def test_holdout_months_are_never_read(cfg):
    m = good(cfg)
    m["2026-07"] = {"complete": False, "rows": 0, "source": "daily"}
    manifest(cfg, m)
    with columns.registered(fake_group()):
        assert columns.column_group_problems(cfg, [], "4h") == []


def test_registry_is_restored(cfg):
    with columns.registered(fake_group()):
        assert len(columns.GROUPS) == 1
    assert columns.GROUPS == ()


def test_identity_block_empty_at_rule_1():
    with columns.registered(fake_group(rule=1)):
        assert columns.identity_block() == {}


def test_data_id_unchanged_today(cfg):
    assert runs.data_id(runs.data_identity(cfg, cfg.symbols)) == PRE_CHANGE_DATA_ID


def test_rule_2_changes_data_id(cfg):
    with columns.registered(fake_group(rule=2)):
        assert columns.identity_block() == {"columns": {"fake": 2}}
        assert runs.data_id(runs.data_identity(cfg, cfg.symbols)) != PRE_CHANGE_DATA_ID
