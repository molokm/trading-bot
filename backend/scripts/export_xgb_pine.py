#!/usr/bin/env python3
"""Export XGBoost model to Pine Script arrays for TradingView strategy"""

import json

def export_xgb_to_pine(json_path: str) -> str:
    with open(json_path) as f:
        data = json.load(f)

    gb = data['learner']['gradient_booster']['model']
    trees = gb['trees']
    # model_param has base_score
    bs_raw = data['learner']['learner_model_param'].get('base_score', '0.5')
    base_score = float(bs_raw.strip('[]'))

    max_nodes = max(len(t['left_children']) for t in trees)
    n_trees = len(trees)

    # Build padded arrays
    pad = -1
    left_arr, right_arr, split_idx_arr, split_cond_arr, weight_arr = [], [], [], [], []
    for t in trees:
        lc = t['left_children']
        rc = t['right_children']
        si = t.get('split_indices', [])
        sc = t.get('split_conditions', [])
        bw = t['base_weights']
        n = len(lc)
        left_arr.extend(lc + [pad] * (max_nodes - n))
        right_arr.extend(rc + [pad] * (max_nodes - n))
        split_idx_arr.extend(si + [pad] * (max_nodes - n))
        split_cond_arr.extend(sc + [0.0] * (max_nodes - n))
        weight_arr.extend(bw + [0.0] * (max_nodes - n))

    feature_names = [
        "rsi", "atr_pct", "macd_hist", "bb_width", "vol_ratio",
        "dist_ema200_pct", "dist_ema50_pct", "swing_range", "dist_to_swing"
    ]

    indent = "    "
    lines = []
    def L(s=""):
        lines.append(s)

    L("// @description XGBoost model — 200 trees, 9 features")
    L(f"// base_score={base_score}, trees={n_trees}, max_nodes={max_nodes}")
    L("")

    # Arrays
    L(f"MAX_NODES = {max_nodes}")
    L(f"NUM_TREES = {n_trees}")
    L("")
    L("// Tree structure arrays (flat, one tree after another)")
    L(f"LEFT_CHILDREN = array.from({', '.join(str(x) for x in left_arr)})")
    L(f"RIGHT_CHILDREN = array.from({', '.join(str(x) for x in right_arr)})")
    L(f"SPLIT_INDICES = array.from({', '.join(str(x) for x in split_idx_arr)})")

    # Split conditions: format with high precision
    sc_strs = []
    for v in split_cond_arr:
        if isinstance(v, (int, float)) and v == int(v):
            sc_strs.append(str(int(v)))
        else:
            sc_strs.append(f"{v:.10f}")
    L(f"SPLIT_CONDITIONS = array.from({', '.join(sc_strs)})")

    bw_strs = []
    for v in weight_arr:
        if isinstance(v, (int, float)) and v == int(v):
            bw_strs.append(str(int(v)))
        else:
            bw_strs.append(f"{v:.10f}")
    L(f"BASE_WEIGHTS = array.from({', '.join(bw_strs)})")

    L("")
    L("// Feature names (for reference, not used in inference)")
    L(f"FEATURE_NAMES = array.from({', '.join(repr(n) for n in feature_names)})")
    L("")

    # Prediction function
    L("xgb_predict(f) =>")
    L(f'{indent}// f is an array of 9 feature values:')
    L(f'{indent}//   [0]=rsi, [1]=atr_pct, [2]=macd_hist, [3]=bb_width,')
    L(f'{indent}//   [4]=vol_ratio, [5]=dist_ema200_pct, [6]=dist_ema50_pct,')
    L(f'{indent}//   [7]=swing_range, [8]=dist_to_swing')
    L(f'{indent}score = 0.0')
    L(f'{indent}for t = 0 to NUM_TREES - 1')
    L(f'{indent}    node = t * MAX_NODES')
    L(f'{indent}    left = array.get(LEFT_CHILDREN, node)')
    L(f'{indent}    while left != -1')
    L(f'{indent}        feat_idx = array.get(SPLIT_INDICES, node)')
    L(f'{indent}        cond = array.get(SPLIT_CONDITIONS, node)')
    L(f'{indent}        if array.get(f, feat_idx) <= cond')
    L(f'{indent}            node := array.get(LEFT_CHILDREN, node)')
    L(f'{indent}        else')
    L(f'{indent}            node := array.get(RIGHT_CHILDREN, node)')
    L(f'{indent}        left := array.get(LEFT_CHILDREN, node)')
    L(f'{indent}    score += array.get(BASE_WEIGHTS, node)')
    L(f'{indent}// Sigmoid: P(y=1) = 1 / (1 + e^(-score))')
    L(f'{indent}prob = 1.0 / (1.0 + math.exp(-score))')
    L(f'{indent}prob')
    L("")

    return "\n".join(lines)


if __name__ == "__main__":
    code = export_xgb_to_pine("models/xgb_gate.json")
    out_path = "scripts/xgb_pine_inference.pine"
    with open(out_path, "w") as f:
        f.write(code)
    print(f"Generated {out_path}")
    # Count lines and estimate size
    n_lines = code.count("\n") + 1
    print(f"Lines: {n_lines}, Size: {len(code)} bytes")
