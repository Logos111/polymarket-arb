"""Shadow inference for FULL-HISTORY ALPHA MINING v1.

Reads a frozen LightGBM model + manifest and scores future candidate rows.
This deliberately does not place orders. It records model scores and, when
future labels are already present in a historical replay, can compare the
scores with realized outcomes. For true live use, feed only features known at
signal time and leave future columns absent.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

FUTURE_PREFIXES = ("future_", "mfe_", "mae_", "win_label", "ev_breakeven", "final_outcome")
NON_FEATURES = {"candidate_id", "condition_id", "window_start", "t", "timestamp", "slug", "cand", "symbol", "entered", "outcome"}


def clean_features(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    X = df[cols].copy()
    for c in cols:
        if pd.api.types.is_bool_dtype(X[c]):
            X[c] = X[c].astype(float)
        else:
            X[c] = pd.to_numeric(X[c], errors="coerce")
    return X.replace([np.inf, -np.inf], np.nan)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="candidate parquet or feature parquet")
    ap.add_argument("--model-dir", default="runtime/alpha_models")
    ap.add_argument("--output", default="runtime/alpha_shadow.parquet")
    args = ap.parse_args()

    root = Path(args.model_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    df = pd.read_parquet(args.input)
    clf_info = manifest["classifier"]
    cols = clf_info["features"]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise SystemExit(f"missing live feature columns: {missing}")

    X = clean_features(df, cols)
    clf = lgb.Booster(model_file=str(root / "alpha_settlement_win.txt"))
    df["p_settlement_win"] = clf.predict(X)
    if "alpha_return_60s.txt" in {p.name for p in root.iterdir()}:
        reg_info = manifest["regression"]
        rcols = reg_info["features"]
        rx = clean_features(df, rcols)
        reg = lgb.Booster(model_file=str(root / "alpha_return_60s.txt"))
        df["pred_return_60"] = reg.predict(rx)

    # Expected value for a binary $1 settlement payoff. Fee follows the
    # project's current documented taker fee formula; this is a research
    # approximation and does not replace execution-level fee accounting.
    if "cand_ask" in df.columns:
        p = df["p_settlement_win"].clip(0, 1)
        ask = pd.to_numeric(df["cand_ask"], errors="coerce")
        fee = 0.07 * ask * (1 - ask)
        df["expected_pnl_per_share"] = p * (1 - ask) - (1 - p) * ask - fee

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(f"wrote {len(df)} shadow rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
