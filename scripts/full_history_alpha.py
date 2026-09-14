"""FULL-HISTORY ALPHA MINING v1.

This is intentionally a research/live-discovery pipeline, not an OOS validator.
It consumes the already-built candidate_orders parquet produced by
``make_candidate_orders.py`` and trains on *all available historical rows*.
The future/live period is the real forward test.

Targets:
- settlement_win: binary settlement outcome (classification)
- future_return_30/60/120: short-horizon token return (regression)

The script never creates features from future columns. Columns whose names imply
future information are excluded from X. The fitted model is saved as a LightGBM
text model plus a JSON feature schema so the live inference side can use the
same feature set.

Typical workflow:
    uv sync --group research
    uv run python scripts/make_candidate_orders.py --symbols btc,eth
    uv run python scripts/full_history_alpha.py --symbols btc,eth --horizon 60

Important interpretation rule:
    The training metrics printed here are IN-SAMPLE diagnostics. They are not
    evidence of profitability. Profitability is tested only after the model is
    frozen and exposed to future shadow/paper/live data.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import (
    average_precision_score,
    mean_absolute_error,
    mean_squared_error,
    roc_auc_score,
)

TAG = "hf_20260324_20260518"
FUTURE_PREFIXES = ("future_", "mfe_", "mae_", "win_label", "ev_breakeven", "final_outcome")
NON_FEATURES = {
    "candidate_id",
    "condition_id",
    "window_start",
    "t",
    "timestamp",
    "slug",
    "cand",
    "symbol",
    "entered",
    "outcome",
}


def load_candidates(symbols: list[str], features_dir: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for symbol in symbols:
        path = Path(features_dir) / f"{symbol}_candidate_orders_{TAG}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"missing candidate dataset: {path}")
        df = pd.read_parquet(path)
        df["symbol_flag"] = 1 if symbol.lower() == "btc" else 0
        frames.append(df)
    if not frames:
        raise ValueError("no symbols supplied")
    return pd.concat(frames, ignore_index=True)


def feature_columns(df: pd.DataFrame, target: str) -> list[str]:
    cols: list[str] = []
    for col in df.columns:
        if col == target or col in NON_FEATURES:
            continue
        if any(col.startswith(prefix) for prefix in FUTURE_PREFIXES):
            continue
        if pd.api.types.is_numeric_dtype(df[col]) or pd.api.types.is_bool_dtype(df[col]):
            cols.append(col)
    if not cols:
        raise ValueError("no usable numeric features")
    return cols


def clean_X(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    X = df[cols].copy()
    for col in X.columns:
        if pd.api.types.is_bool_dtype(X[col]):
            X[col] = X[col].astype(float)
        else:
            X[col] = pd.to_numeric(X[col], errors="coerce")
    return X.replace([np.inf, -np.inf], np.nan)


def train_regression(df: pd.DataFrame, horizon: int, out_dir: Path) -> dict:
    target = f"future_return_{horizon}"
    if target not in df:
        return {"target": target, "status": "missing"}
    mask = df[target].notna()
    work = df.loc[mask].copy()
    cols = feature_columns(work, target)
    X = clean_X(work, cols)
    y = pd.to_numeric(work[target], errors="coerce")
    good = y.notna()
    X, y = X.loc[good], y.loc[good]

    model = lgb.LGBMRegressor(
        objective="regression",
        n_estimators=500,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=80,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.2,
        reg_lambda=1.0,
        random_state=42,
        verbosity=-1,
    )
    model.fit(X, y)
    pred = model.predict(X)
    model_path = out_dir / f"alpha_return_{horizon}s.txt"
    model.booster_.save_model(str(model_path))
    report = {
        "target": target,
        "n": int(len(y)),
        "feature_count": len(cols),
        "in_sample_rmse": float(np.sqrt(mean_squared_error(y, pred))),
        "in_sample_mae": float(mean_absolute_error(y, pred)),
        "actual_mean": float(y.mean()),
        "pred_mean": float(pred.mean()),
        "pred_p95": float(np.quantile(pred, 0.95)),
        "model": str(model_path),
        "features": cols,
    }
    return report


def train_classifier(df: pd.DataFrame, out_dir: Path) -> dict:
    target = "win_label"
    if target not in df:
        return {"target": target, "status": "missing"}
    y = df[target].astype("boolean")
    mask = y.notna()
    work = df.loc[mask].copy()
    y = y.loc[mask].astype(int)
    cols = feature_columns(work, target)
    X = clean_X(work, cols)

    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=500,
        learning_rate=0.03,
        num_leaves=31,
        min_child_samples=80,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.2,
        reg_lambda=1.0,
        random_state=42,
        verbosity=-1,
    )
    model.fit(X, y)
    pred = model.predict_proba(X)[:, 1]
    model_path = out_dir / "alpha_settlement_win.txt"
    model.booster_.save_model(str(model_path))
    report = {
        "target": target,
        "n": int(len(y)),
        "feature_count": len(cols),
        "positive_rate": float(y.mean()),
        "in_sample_auc": float(roc_auc_score(y, pred)),
        "in_sample_pr_auc": float(average_precision_score(y, pred)),
        "pred_p95": float(np.quantile(pred, 0.95)),
        "model": str(model_path),
        "features": cols,
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="btc,eth")
    ap.add_argument("--features-dir", default="runtime/features")
    ap.add_argument("--out-dir", default="runtime/alpha_models")
    ap.add_argument("--horizon", type=int, default=60, choices=(30, 60, 120))
    args = ap.parse_args()

    symbols = [s.strip().lower() for s in args.symbols.split(",") if s.strip()]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = load_candidates(symbols, args.features_dir)

    # Freeze the exact training feature schema next to the model.  This is the
    # contract the future live/shadow inference process must obey.
    clf = train_classifier(df, out_dir)
    reg = train_regression(df, args.horizon, out_dir)
    schema = {
        "tag": TAG,
        "symbols": symbols,
        "rows": int(len(df)),
        "training_mode": "full_history",
        "warning": "in-sample metrics are exploratory only; live/shadow is the forward test",
        "classifier": clf,
        "regression": reg,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(schema, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
