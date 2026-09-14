# FULL-HISTORY ALPHA MINING / V1

## Purpose

This branch changes the research objective from hand-designed "true/false underdog" rules to a data-driven expected-value model. The current historical candidate dataset is treated as the complete training corpus; the first real forward period after model freeze is the final test.

This is deliberately different from a conventional train/validation/test split. It is a **live-discovery experiment**. Any live period used to tune features, model hyperparameters, thresholds, or execution rules ceases to be a clean test period.

## Current repository facts

- Latest master commit is `6ff81195` (2026-09-14), whose b10 work reports BTC 184,391 and ETH 139,490 candidate rows, AUC around 0.55 across coins, and no robust entry-side filter; the remaining practical direction is maker exit / execution rather than another hand-tuned entry veto.
- Existing feature infrastructure already has a leakage guard and a backtest-only `labels.py`; the candidate table is explicitly one row per basic-strategy entry decision and contains the historical feature snapshot plus settlement label.
- Existing research dependencies already declare pandas/scikit-learn/lightgbm/shap under the `research` dependency group.

## V1 model set

1. LightGBM binary classifier: `settlement_win`.
2. LightGBM regressors: `future_return_30/60/120` (run one horizon at a time).
3. Live score is converted to expected settlement PnL using entry ask and the project's current fee approximation.
4. A frozen model is scored in shadow mode before any order-routing integration.

## Feature contract

Only columns known at the candidate timestamp are allowed. Identifiers and future-derived fields are excluded. Future fields include `future_*`, `mfe_*`, `mae_*`, `win_label`, `ev_breakeven`, and `final_outcome`.

The exact fitted feature list is saved in `runtime/alpha_models/manifest.json` and is the contract for future inference.

## Commands

```bash
uv sync --group research
uv run python scripts/make_candidate_orders.py --symbols btc,eth
uv run python scripts/full_history_alpha.py --symbols btc,eth --horizon 60
uv run python scripts/alpha_live_shadow.py --input runtime/features/btc_candidate_orders_hf_20260324_20260518.parquet
```

The first two commands require the feature parquet files to exist. If they do not, run the existing `pm-bt5m --capture-features --spot` pipeline first.

## Interpretation

The training metrics printed by `full_history_alpha.py` are in-sample diagnostics only. They are useful for discovering whether the feature set contains exploitable structure and for ranking candidates, but they are not evidence that the strategy will profit.

The first locked model must be evaluated with:

- shadow signals;
- realistic paper fills;
- actual fee/slippage assumptions;
- then very small live size.

During the locked forward test, do not tune the threshold after seeing individual outcomes. A new model version starts a new forward-test period.

## Success gate

A V1 model is considered promising only if forward data shows positive realized EV after fees/slippage and the positive EV is concentrated in high-score candidates rather than appearing only after arbitrary threshold selection.
