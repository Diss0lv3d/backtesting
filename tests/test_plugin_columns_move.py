"""The generic guard (spec Testing 7): every column a plugin may read moves in the checks' synthetic bars, so a
plugin reading it is tested on data that varies."""

from __future__ import annotations

import pandas as pd
import pytest

from perp_lab.proposer import context
from perp_lab.validate.causality import synthetic_bars, with_moving_columns
from perp_lab.validate.synthetic_market import market


def _moves(frame: pd.DataFrame, name: str) -> bool:
    return name in frame.columns and frame[name].dropna().nunique() > 1


def test_every_plugin_column_moves():
    frames = {"causality": with_moving_columns(synthetic_bars())}
    for tf in ("15m", "1h", "4h"):
        for symbol, frame in market(tf).items():
            frames[f"{tf} {symbol}"] = frame
    still = {where: [n for n in context.PLUGIN_COLUMNS if not _moves(f, n)] for where, f in frames.items()}
    assert {w: bad for w, bad in still.items() if bad} == {}
