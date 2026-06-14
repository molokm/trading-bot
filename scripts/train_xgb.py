#!/usr/bin/env python3
"""Тренировка XGBoost-фильтра для отсеивания убыточных сигналов стратегий.

Использование:
  1. Запустить бэктест стратегии (возвращает xgb_dataset)
  2. Сохранить датасет в JSON
  3. Запустить скрипт: python scripts/train_xgb.py <dataset.json> <output_dir>/

Пример:
  python scripts/train_xgb.py xgb_dataset_levx.json ../backend/models/
"""

import json
import sys
import os
import numpy as np

os.environ.setdefault("LOKY_MAX_CPU_COUNT", "4")


def train(dataset_path: str, output_dir: str):
    import pandas as pd
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
    import xgboost as xgb

    with open(dataset_path) as f:
        data = json.load(f)

    rows = [r for r in data if r.get("outcome") is not None and r.get("features")]
    if not rows:
        print("Нет записей с outcome. Сначала запусти бэктест.")
        return

    print(f"Записей: {len(data)}")
    print(f"С outcome: {len(rows)}")
    wins = sum(1 for r in rows if r["outcome"] == 1)
    losses = sum(1 for r in rows if r["outcome"] == 0)
    print(f"Прибыльных: {wins}  Убыточных: {losses}  WinRate: {wins/max(1,len(rows))*100:.1f}%")

    FEATURE_NAMES = [
        "rsi",
        "atr_pct",
        "macd_hist",
        "bb_width",
        "vol_ratio",
        "dist_ema200_pct",
        "dist_ema50_pct",
        "swing_range",
        "dist_to_swing",
    ]

    X = np.array([r["features"] for r in rows], dtype=np.float32)
    y = np.array([r["outcome"] for r in rows], dtype=np.int32)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42, stratify=y
    )

    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=3,
        scale_pos_weight=losses / max(wins, 1),
        eval_metric="logloss",
        random_state=42,
        n_jobs=2,
    )
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        verbose=False,
    )

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]

    print("\n=== Результаты ===")
    print(f"Accuracy:  {accuracy_score(y_test, y_pred):.3f}")
    print(f"Precision: {precision_score(y_test, y_pred):.3f}")
    print(f"Recall:    {recall_score(y_test, y_pred):.3f}")
    print(f"F1:        {f1_score(y_test, y_pred):.3f}")

    # Feature importance
    imp = sorted(zip(FEATURE_NAMES, model.feature_importances_),
                 key=lambda x: -x[1])
    print("\n=== Важность признаков ===")
    for name, val in imp:
        print(f"  {name:<20} {val:.4f}")

    os.makedirs(output_dir, exist_ok=True)
    model_path = os.path.join(output_dir, "xgb_gate.json")
    model.save_model(model_path)

    # Сохраняем мета-информацию
    meta = {
        "feature_names": FEATURE_NAMES,
        "n_samples": len(rows),
        "win_rate": wins / max(1, len(rows)),
        "test_accuracy": round(float(accuracy_score(y_test, y_pred)), 3),
        "test_f1": round(float(f1_score(y_test, y_pred)), 3),
        "threshold": 0.5,
    }
    meta_path = os.path.join(output_dir, "xgb_meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\nМодель: {model_path}")
    print(f"Мета:   {meta_path}")
    print(f"Порог:  {meta['threshold']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/train_xgb.py <dataset.json> [output_dir]")
        sys.exit(1)
    dataset = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(__file__), "..", "backend", "models"
    )
    train(dataset, out)
