#!/usr/bin/env python3
"""Build complete Pine Script strategy: Trend Bounce Pro + XGBoost"""

import json

def load_xgb_arrays(json_path):
    with open(json_path) as f:
        data = json.load(f)

    gb = data['learner']['gradient_booster']['model']
    trees = gb['trees']
    max_nodes = max(len(t['left_children']) for t in trees)
    n_trees = len(trees)

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
    return {
        'max_nodes': max_nodes,
        'n_trees': n_trees,
        'left_arr': left_arr,
        'right_arr': right_arr,
        'split_idx_arr': split_idx_arr,
        'split_cond_arr': split_cond_arr,
        'weight_arr': weight_arr,
    }


def fmt_array_chunks(arr, float_prec=10, chunk_size=200):
    """Split array into list of comma-joined chunks (each fit for array.from())."""
    items = []
    for v in arr:
        if isinstance(v, (int, float)) and v == int(v):
            items.append(str(int(v)))
        else:
            items.append(f"{v:.{float_prec}f}")

    return [", ".join(items[i:i + chunk_size])
            for i in range(0, len(items), chunk_size)]


def build_strategy(xgb):
    indent = "    "
    lines = []
    def L(s=""):
        lines.append(s)

    L("//@version=6")
    L("strategy(")
    L('  title="Trend Bounce Pro w/ XGBoost",')
    L('  shorttitle="TB Pro XGB",')
    L('  overlay=true,')
    L('  initial_capital=1000,')
    L('  default_qty_type=strategy.percent_of_equity,')
    L('  default_qty_value=95,')
    L('  commission_type=strategy.commission.percent,')
    L('  commission_value=0.05,')
    L("  pyramiding=0)")
    L("")

    L("// ─── Parameters ───")
    L("ema_trend = input.int(200, 'EMA Trend')")
    L("swing_window = input.int(40, 'Swing Window')")
    L("rsi_exit = input.int(80, 'RSI Exit')")
    L("xgb_threshold = input.float(0.5, 'XGBoost Threshold')")
    L("use_xgb = input.bool(true, 'Use XGBoost Gate')")
    L("")

    L("// ─── Features & XGBoost arrays ───")
    L(f"MAX_NODES = {xgb['max_nodes']}")
    L(f"NUM_TREES = {xgb['n_trees']}")
    L("")
    def emit_array(var_name, arr, is_float=False):
        ctor = "array.new_float" if is_float else "array.new_int"
        L(f"// Build {var_name}")
        L(f"{var_name} = {ctor}()")
        chunks = fmt_array_chunks(arr)
        for ch in chunks:
            L(f"array.concat({var_name}, array.from({ch}))")
    emit_array("LEFT_CHILDREN", xgb['left_arr'])
    emit_array("RIGHT_CHILDREN", xgb['right_arr'])
    emit_array("XGB_SPLIT_IDX", xgb['split_idx_arr'])
    emit_array("XGB_SPLIT_COND", xgb['split_cond_arr'], is_float=True)
    emit_array("XGB_WEIGHTS", xgb['weight_arr'], is_float=True)
    L("")

    L("// ─── XGBoost inference ───")
    L("xgb_predict(f) =>")
    L(f'{indent}score = 0.0')
    L(f'{indent}for t = 0 to NUM_TREES - 1')
    L(f'{indent}    node = t * MAX_NODES')
    L(f'{indent}    left = array.get(LEFT_CHILDREN, node)')
    L(f'{indent}    while left != -1')
    L(f'{indent}        feat_idx = array.get(XGB_SPLIT_IDX, node)')
    L(f'{indent}        cond = array.get(XGB_SPLIT_COND, node)')
    L(f'{indent}        if array.get(f, feat_idx) <= cond')
    L(f'{indent}            node := array.get(LEFT_CHILDREN, node)')
    L(f'{indent}        else')
    L(f'{indent}            node := array.get(RIGHT_CHILDREN, node)')
    L(f'{indent}        left := array.get(LEFT_CHILDREN, node)')
    L(f'{indent}    score += array.get(XGB_WEIGHTS, node)')
    L(f'{indent}1.0 / (1.0 + math.exp(-score))')
    L("")

    L("// ─── Indicator calculations ───")
    L("close_val = close")
    L("high_val = high")
    L("low_val = low")
    L("vol_val = volume")
    L("")

    L("// EMA trend (param ema_trend, default 200)")
    L("ema_trend_line = ta.ema(close_val, ema_trend)")
    L("")

    L("// Swing highs/lows")
    L("sh = ta.pivothigh(high_val, swing_window, swing_window)")
    L("sl = ta.pivotlow(low_val, swing_window, swing_window)")
    L("csh = 0.0")
    L("csl = 0.0")
    L("csh := nz(sh, csh[1])")
    L("csl := nz(sl, csl[1])")
    L("")

    L("// RSI(14) — SMA-based for strategy exit logic (match generate_signals)")
    L("// Pine ta.rsi uses RMA; we need SMA to match Python training")
    L("change_val = ta.change(close_val)")
    L("gain = math.max(change_val, 0)")
    L("loss = -math.min(change_val, 0)")
    L("avg_g_sma = ta.sma(gain, 14)")
    L("avg_l_sma = ta.sma(loss, 14)")
    L("rsi_val = 100.0 - 100.0 / (1.0 + avg_g_sma / math.max(avg_l_sma, 0.0001))")
    L("")

    L("// RSI(14) — EMA-based for XGBoost feature (match _compute_entry_features)")
    L("avg_g_ema = ta.ema(gain, 14)")
    L("avg_l_ema = ta.ema(loss, 14)")
    L("rsi_ema = 100.0 - 100.0 / (1.0 + avg_g_ema / math.max(avg_l_ema, 0.0001))")
    L("")

    L("// ATR(14) ratio (match Python: atr_pct = atr / close)")
    L("atr14 = ta.atr(14)")
    L("atr_ratio = atr14 / close_val")
    L("")

    L("// MACD histogram")
    L("[macd_line, _, macd_hist] = ta.macd(close_val, 12, 26, 9)")
    L("")

    L("// Bollinger Width (match Python: 4*std/sma)")
    L("bb_basis = ta.sma(close_val, 20)")
    L("bb_std = ta.stdev(close_val, 20)")
    L("bb_width = 4 * bb_std / bb_basis")
    L("")

    L("// Volume ratio")
    L("vol_sma20 = ta.sma(vol_val, 20)")
    L("vol_ratio = vol_val / vol_sma20")
    L("")

    L("// Distance from EMAs (match Python: ratio, not %)")
    L("ema50 = ta.ema(close_val, 50)")
    L("dist_ema200 = close_val / ema_trend_line - 1")
    L("dist_ema50 = close_val / ema50 - 1")
    L("")

    L("// Swing range & dist (match Python: raw ratios)")
    L("swing_range = (csh - csl) / close_val")
    L("dist_to_swing = math.min(math.abs(close_val - csh), math.abs(close_val - csl)) / (atr14 + 0.0001)")
    L("")

    L("// ─── Feature vector for XGBoost ───")
    L("f_arr = array.from(rsi_ema, atr_ratio, macd_hist, bb_width, vol_ratio, dist_ema200, dist_ema50, swing_range, dist_to_swing)")
    L("")

    L("// ─── Signal logic ───")
    L("uptrend = close_val > ema_trend_line")
    L("downtrend = close_val < ema_trend_line")
    L("")

    L("// Entry conditions (same as Python)")
    L("dropped = close_val < csh * 0.995")
    L("near_sl = close_val <= csl * 1.005")
    L("bounce = low_val > low_val[1]")
    L("long_entry = uptrend and dropped and near_sl and bounce")
    L("")

    L("climbed = close_val > csl * 1.005")
    L("near_sh = close_val >= csh * 0.995")
    L("reject = high_val < high_val[1]")
    L("short_entry = downtrend and climbed and near_sh and reject")
    L("")

    L("// Exit conditions")
    L("long_exit = close_val < csl or rsi_val > rsi_exit")
    L("short_exit = close_val > csh or rsi_val < (100 - rsi_exit)")
    L("")

    L("// XGBoost gate")
    L("xgb_prob = xgb_predict(f_arr)")
    L("xgb_pass = xgb_prob >= xgb_threshold")
    L("")

    L("// Final signals (with optional XGBoost)")
    L("buy_signal = long_entry and (not use_xgb or xgb_pass)")
    L("sell_signal = short_entry and (not use_xgb or xgb_pass)")
    L("")

    L("// ─── Strategy execution ───")
    L("if buy_signal")
    L(f'{indent}strategy.entry("Long", strategy.long)')
    L("")
    L("if sell_signal")
    L(f'{indent}strategy.entry("Short", strategy.short)')
    L("")
    L("if strategy.position_size > 0 and long_exit")
    L(f'{indent}strategy.close("Long")')
    L("")
    L("if strategy.position_size < 0 and short_exit")
    L(f'{indent}strategy.close("Short")')
    L("")

    L("// ─── Plots ───")
    L("plot(ema_trend_line, 'EMA Trend', color=color.blue, linewidth=1)")
    L("plotshape(buy_signal, style=shape.triangleup, location=location.belowbar, color=color.green, size=size.small)")
    L("plotshape(sell_signal, style=shape.triangledown, location=location.abovebar, color=color.red, size=size.small)")
    L("plot(csh, 'Swing High', color=color.gray, linewidth=1, style=plot.style_circles)")
    L("plot(csl, 'Swing Low', color=color.gray, linewidth=1, style=plot.style_circles)")
    L("")

    L("// XGB debug plot in a separate pane")
    L("// @description XGBoost probability (in separate indicator pane)")
    L("hline(0.5, 'XGB Threshold', color=color.gray, linestyle=hline.style_dashed)")
    L("plot(xgb_prob, 'XGBoost Prob', color=color.purple, linewidth=2)")

    return "\n".join(lines)


if __name__ == "__main__":
    xgb = load_xgb_arrays("models/xgb_gate.json")
    script = build_strategy(xgb)
    out_path = "scripts/trend_bounce_pro_xgb.pine"
    with open(out_path, "w") as f:
        f.write(script)
    print(f"Generated {out_path}")
    print(f"Lines: {script.count(chr(10)) + 1}, Size: {len(script)} bytes")
