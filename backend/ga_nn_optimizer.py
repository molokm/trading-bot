"""
Genetic Algorithm + Neural Network Strategy Optimizer
=====================================================
GA: evolves strategy parameters (SL, TP, trailing, cooldown, risk%)
NN: learns entry/exit signals from technical indicators

Uses pre-computed indicators from scalping_strategy.py for speed.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

import numpy as np
import asyncio
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.metrics import accuracy_score, classification_report
from scalping_strategy import (
    downsample_5m_to_15m, downsample_5m_to_1h,
    compute_all_indicators, ENTRY_MODES,
    mode_f_long, mode_f_short,
    run_scalp_backtest, analyze_results,
)

np.random.seed(42)

# ═══════════════════════════════════════════════════════════════
# PART 1: GENETIC ALGORITHM OPTIMIZER
# ═══════════════════════════════════════════════════════════════

class GeneticOptimizer:
    """
    GA for optimizing scalping strategy parameters.
    Each chromosome = [risk%, SL, TP, trail_act, trail_dist, cooldown, max_hold, partial_pct, be_atr]
    """
    # Parameter bounds: (min, max)
    PARAM_BOUNDS = {
        'risk':     (0.003, 0.03),    # 0.3% - 3%
        'sl':       (0.5, 3.0),       # ATR multiples
        'tp':       (1.0, 5.0),
        'trail_a':  (0.2, 1.5),
        'trail_d':  (0.2, 1.5),
        'cooldown': (3, 30),          # bars
        'max_hold': (10, 60),
        'partial':  (0.0, 0.5),       # fraction
        'be':       (0.0, 1.0),       # ATR
    }
    PARAM_NAMES = list(PARAM_BOUNDS.keys())

    def __init__(self, close, high, low, vol, ts, long_fn, short_fn,
                 pop_size=60, generations=40, elite_pct=0.15,
                 mutation_rate=0.3, crossover_rate=0.7, fee=0.0005):
        self.close = close
        self.high = high
        self.low = low
        self.vol = vol
        self.ts = ts
        self.long_fn = long_fn
        self.short_fn = short_fn
        self.pop_size = pop_size
        self.generations = generations
        self.elite_pct = elite_pct
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.fee = fee
        self.n_params = len(self.PARAM_NAMES)
        self.bounds = np.array([self.PARAM_BOUNDS[n] for n in self.PARAM_NAMES])

        # Pre-compute indicators once
        self.ind = compute_all_indicators(close, high, low, vol)
        self.best_history = []

    def _random_chromosome(self):
        """Random individual within bounds."""
        return np.random.uniform(self.bounds[:, 0], self.bounds[:, 1])

    def _init_population(self, seed_chromosomes=None):
        """Initialize population, optionally seeding with known good params."""
        pop = []
        if seed_chromosomes:
            for sc in seed_chromosomes:
                pop.append(np.array(sc))
        while len(pop) < self.pop_size:
            pop.append(self._random_chromosome())
        return np.array(pop[:self.pop_size])

    def _decode(self, chrom):
        """Decode chromosome to parameter dict."""
        return {
            'risk_pct': chrom[0],
            'sl_atr': chrom[1],
            'tp_atr': chrom[2],
            'trail_activate': chrom[3],
            'trail_atr': chrom[4],
            'cooldown': int(chrom[5]),
            'max_hold': int(chrom[6]),
            'partial_tp_pct': chrom[7],
            'be_atr': chrom[8],
        }

    def _fitness(self, chrom):
        """
        Fitness = return / drawdown ratio (higher = better).
        Penalizes: too few trades, negative return, excessive DD.
        """
        params = self._decode(chrom)
        try:
            bal, trades, equity = run_scalp_backtest(
                self.close, self.high, self.low, self.vol, self.ts,
                cap=10000, fee=self.fee,
                long_fn=self.long_fn, short_fn=self.short_fn,
                precomputed_ind=self.ind,
                **params,
            )
        except Exception:
            return -100.0

        n_trades = len(trades)
        if n_trades < 10:
            return -10.0

        ret = (bal / 10000 - 1) * 100
        eq_arr = np.array(equity)
        dd = ((np.maximum.accumulate(eq_arr) - eq_arr) / np.maximum.accumulate(eq_arr) * 100).max()
        dd = max(dd, 0.01)

        wins = [t for t in trades if t["pnl"] > 0]
        wr = len(wins) / n_trades
        gp = sum(t["pnl"] for t in wins)
        gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0)) or 0.001
        pf = gp / gl

        # Multi-objective fitness
        ret_dd = ret / dd if dd > 0 else 0
        fitness = ret_dd * (1 + wr * 0.5) * min(pf, 5) / 5

        # Penalties
        if ret < 0:
            fitness *= 0.3
        if n_trades < 20:
            fitness *= 0.7
        if dd > 15:
            fitness *= 0.5

        return fitness

    def _crossover(self, p1, p2):
        """Blend crossover (BLX-alpha)."""
        alpha = 0.3
        lo = np.minimum(p1, p2)
        hi = np.maximum(p1, p2)
        span = hi - lo
        child = np.random.uniform(lo - alpha * span, hi + alpha * span)
        return np.clip(child, self.bounds[:, 0], self.bounds[:, 1])

    def _mutate(self, chrom, gen):
        """Gaussian mutation with adaptive sigma."""
        sigma = 0.2 * (1 - gen / self.generations)  # decays over time
        mask = np.random.random(self.n_params) < self.mutation_rate
        mutation = np.random.normal(0, sigma, self.n_params) * (self.bounds[:, 1] - self.bounds[:, 0])
        chrom[mask] += mutation[mask]
        return np.clip(chrom, self.bounds[:, 0], self.bounds[:, 1])

    def evolve(self, verbose=True):
        """Run the genetic algorithm."""
        # Seed with known good params from previous tests
        seeds = [
            [0.01, 1.0, 1.5, 0.5, 0.5, 12, 30, 0.0, 0.0],   # Mode F baseline
            [0.015, 1.0, 1.5, 0.5, 0.5, 12, 30, 0.3, 0.5],   # + partial + BE
            [0.01, 0.8, 2.0, 0.3, 0.3, 8, 30, 0.0, 0.0],     # tighter
            [0.02, 1.5, 3.0, 0.5, 0.5, 15, 30, 0.3, 0.5],    # wider
        ]
        pop = self._init_population(seeds)

        best_fitness = -999
        best_chrom = None
        stagnation = 0

        t0 = time.time()
        for gen in range(self.generations):
            # Evaluate fitness
            fitness = np.array([self._fitness(c) for c in pop])

            # Track best
            gen_best_idx = np.argmax(fitness)
            gen_best_fit = fitness[gen_best_idx]
            gen_best_chrom = pop[gen_best_idx].copy()

            if gen_best_fit > best_fitness:
                best_fitness = gen_best_fit
                best_chrom = gen_best_chrom.copy()
                stagnation = 0
            else:
                stagnation += 1

            self.best_history.append(best_fitness)

            if verbose:
                elapsed = time.time() - t0
                params = self._decode(gen_best_chrom)
                bal, tr, _ = run_scalp_backtest(
                    self.close, self.high, self.low, self.vol, self.ts,
                    cap=10000, fee=self.fee,
                    long_fn=self.long_fn, short_fn=self.short_fn,
                    precomputed_ind=self.ind, **params)
                ret = (bal / 10000 - 1) * 100
                print(f"  Gen {gen:>3}/{self.generations} | fit={gen_best_fit:>7.3f} | "
                      f"ret={ret:>+6.1f}% | trades={len(tr):>4} | "
                      f"SL={params['sl_atr']:.1f} TP={params['tp_atr']:.1f} "
                      f"CD={params['cooldown']} risk={params['risk_pct']*100:.1f}% | "
                      f"{elapsed:.0f}s")

            # Selection: tournament of 3
            def tournament():
                idxs = np.random.choice(self.pop_size, 3, replace=False)
                best = idxs[np.argmax(fitness[idxs])]
                return pop[best]

            # Elitism
            n_elite = max(2, int(self.pop_size * self.elite_pct))
            elite_idx = np.argsort(fitness)[-n_elite:]
            new_pop = [pop[i].copy() for i in elite_idx]

            # Fill rest with crossover + mutation
            while len(new_pop) < self.pop_size:
                if np.random.random() < self.crossover_rate:
                    p1 = tournament()
                    p2 = tournament()
                    child = self._crossover(p1, p2)
                else:
                    child = tournament().copy()
                child = self._mutate(child, gen)
                new_pop.append(child)

            pop = np.array(new_pop[:self.pop_size])

            # Adaptive mutation boost on stagnation
            if stagnation > 5:
                self.mutation_rate = min(0.6, self.mutation_rate + 0.05)
            else:
                self.mutation_rate = 0.3

        # Final results
        if verbose:
            params = self._decode(best_chrom)
            print(f"\n{'='*70}")
            print(f" GA OPTIMIZATION COMPLETE ({self.generations} generations)")
            print(f"{'='*70}")
            bal, tr, eq = run_scalp_backtest(
                self.close, self.high, self.low, self.vol, self.ts,
                cap=10000, fee=self.fee,
                long_fn=self.long_fn, short_fn=self.short_fn,
                precomputed_ind=self.ind, **params)
            analyze_results(10000, bal, tr, eq)
            print(f"\n Best params: {params}")
            print(f" Time: {time.time()-t0:.1f}s")

        return best_chrom, best_fitness


# ═══════════════════════════════════════════════════════════════
# PART 2: NEURAL NETWORK SIGNAL PREDICTOR
# ═══════════════════════════════════════════════════════════════

class NeuralSignalPredictor:
    """
    MLP neural network that learns to predict trade direction from indicators.
    Features: 30+ technical indicators per bar
    Labels: +1 (long), -1 (short), 0 (no trade)
    """
    def __init__(self, close, high, low, vol, ts, lookahead=12, threshold_pct=0.003):
        self.close = close
        self.high = high
        self.low = low
        self.vol = vol
        self.ts = ts
        self.lookahead = lookahead   # bars to look ahead for label
        self.threshold_pct = threshold_pct  # min move to count as signal
        self.scaler = StandardScaler()
        self.model = None
        self.feature_names = []

    def _build_features(self):
        """Build feature matrix from indicators."""
        ind = compute_all_indicators(self.close, self.high, self.low, self.vol)
        n = len(self.close)

        features = {}
        # Core indicators from compute_all_indicators
        features['rsi14'] = ind['rsi14']
        features['macd_hist'] = ind['macd_hist']
        features['macd_line'] = ind['macd_line']
        features['bb_mid'] = ind['bb_mid']
        features['bb_upper'] = ind['bb_upper']
        features['bb_lower'] = ind['bb_lower']
        features['atr14'] = ind['atr14']
        features['atr_pctile'] = ind['atr_pctile']
        features['adx'] = ind['adx'] if 'adx' in ind else np.zeros(n)
        features['obv'] = ind['obv']
        features['obv_ema21'] = ind['obv_ema21']
        features['vwap'] = ind['vwap']
        features['vwap_dev'] = ind['vwap_dev']
        features['vol_delta'] = ind['vol_delta']
        features['vol_delta_sma'] = ind['vol_delta_sma']
        features['vol_sma20'] = ind['vol_sma20']
        features['poc'] = ind['poc']
        features['ema9'] = ind['ema9']
        features['ema21'] = ind['ema21']
        features['ema50'] = ind['ema50']
        features['ema200'] = ind['ema200']
        features['sma20'] = ind['sma20']
        features['highest12'] = ind['highest12']
        features['lowest12'] = ind['lowest12']
        features['rsi_os'] = ind['rsi_os']
        features['rsi_ob'] = ind['rsi_ob']
        features['sl_mult'] = ind['sl_mult']
        features['tp_mult'] = ind['tp_mult']
        features['vol_thresh'] = ind['vol_thresh']

        # Derived features
        features['price_vs_ema50'] = (self.close - ind['ema50']) / np.where(ind['ema50'] > 0, ind['ema50'], 1)
        features['price_vs_ema200'] = (self.close - ind['ema200']) / np.where(ind['ema200'] > 0, ind['ema200'], 1)
        features['rsi_momentum'] = np.diff(ind['rsi14'], prepend=ind['rsi14'][0])
        features['vol_surge'] = self.vol / np.where(ind['vol_sma20'] > 0, ind['vol_sma20'], 1)
        features['obv_signal'] = np.where(ind['obv'] > ind['obv_ema21'], 1, -1).astype(float)

        # Price action
        ret_1 = np.zeros(n)
        ret_1[1:] = np.diff(self.close) / self.close[:-1]
        features['ret_1'] = ret_1
        features['ret_5'] = np.zeros(n)
        features['ret_5'][5:] = (self.close[5:] - self.close[:-5]) / self.close[:-5]
        features['ret_12'] = np.zeros(n)
        features['ret_12'][12:] = (self.close[12:] - self.close[:-12]) / self.close[:-12]

        # Volatility features
        features['high_low_range'] = (self.high - self.low) / np.where(self.close > 0, self.close, 1)
        features['close_position'] = (self.close - self.low) / np.where((self.high - self.low) > 0, self.high - self.low, 1)

        # BB position
        bb_range = ind['bb_upper'] - ind['bb_lower']
        features['bb_pct'] = np.where(bb_range > 0, (self.close - ind['bb_lower']) / bb_range, 0.5)
        features['bb_width'] = bb_range / np.where(ind['bb_mid'] > 0, ind['bb_mid'], 1)

        # EMA cross signals
        features['ema_cross_9_21'] = np.where(ind['ema9'] > ind['ema21'], 1, -1).astype(float)
        features['ema_cross_50_200'] = np.where(ind['ema50'] > ind['ema200'], 1, -1).astype(float)

        self.feature_names = list(features.keys())
        X = np.column_stack([features[k] for k in self.feature_names])

        return X, ind

    def _build_labels(self, close):
        """Build labels: +1 long, -1 short, 0 no trade."""
        n = len(close)
        labels = np.zeros(n)
        for i in range(n - self.lookahead):
            future_return = (close[i + self.lookahead] - close[i]) / close[i]
            if future_return > self.threshold_pct:
                labels[i] = 1    # long
            elif future_return < -self.threshold_pct:
                labels[i] = -1   # short
        return labels

    def train(self, verbose=True):
        """Train the neural network with walk-forward validation."""
        t0 = time.time()
        X, ind = self._build_features()
        y = self._build_labels(self.close)
        n = len(self.close)

        if verbose:
            print(f"\n{'='*70}")
            print(f" NEURAL NETWORK SIGNAL PREDICTOR")
            print(f"{'='*70}")
            print(f" Features: {X.shape[1]} indicators")
            print(f" Samples: {n} bars")
            print(f" Labels: +1={np.sum(y==1)} (-1={np.sum(y==-1)} 0={np.sum(y==0)})")

        # Replace inf/nan
        X = np.nan_to_num(X, nan=0.0, posinf=10.0, neginf=-10.0)

        # Walk-forward: train on first 60%, validate on last 40%
        split = int(n * 0.6)
        X_train, X_val = X[:split], X[split:]
        y_train, y_val = y[:split], y[split:]

        # Scale
        X_train_s = self.scaler.fit_transform(X_train)
        X_val_s = self.scaler.transform(X_val)

        # Train MLP
        self.model = MLPClassifier(
            hidden_layer_sizes=(64, 32, 16),
            activation='relu',
            max_iter=500,
            learning_rate_init=0.001,
            early_stopping=True,
            validation_fraction=0.15,
            random_state=42,
            verbose=False,
        )
        self.model.fit(X_train_s, y_train)

        # Evaluate
        y_pred_train = self.model.predict(X_train_s)
        y_pred_val = self.model.predict(X_val_s)

        train_acc = accuracy_score(y_train, y_pred_train)
        val_acc = accuracy_score(y_val, y_pred_val)

        if verbose:
            print(f"\n Train accuracy: {train_acc:.1%}")
            print(f" Validation accuracy: {val_acc:.1%}")
            print(f" Training time: {time.time()-t0:.1f}s")

        # Now backtest: convert NN signals to trades
        y_all_pred = self.model.predict(self.scaler.transform(X))

        if verbose:
            print(f"\n Backtesting NN signals on full data...")
            print(f" {'SL':>4} {'TP':>4} {'Ret%':>7} {'#':>4} {'WR%':>5} {'PF':>5} {'DD%':>5}")
            print(f" {'-'*40}")

        best_score = -999
        best_params = None
        best_result = None

        for sl in [0.8, 1.0, 1.5, 2.0]:
            for tp in [1.5, 2.0, 3.0]:
                for tr_a in [0.3, 0.5, 0.8]:
                    for tr_d in [0.3, 0.5]:
                        for cd in [5, 8, 12]:
                            for risk in [0.005, 0.01, 0.015]:
                                # Create custom entry functions from NN predictions
                                def make_nn_entry(preds, idx):
                                    return preds[idx]

                                # Build long/short functions from predictions
                                nn_preds = y_all_pred
                                def nn_long(i, ind2, close2, vol2):
                                    return nn_preds[i] == 1
                                def nn_short(i, ind2, close2, vol2):
                                    return nn_preds[i] == -1

                                bal, tr, eq = run_scalp_backtest(
                                    self.close, self.high, self.low, self.vol, self.ts,
                                    cap=10000, risk_pct=risk,
                                    sl_atr=sl, tp_atr=tp,
                                    trail_activate=tr_a, trail_atr=tr_d,
                                    cooldown=cd, max_hold=30, fee=0.0005,
                                    long_fn=nn_long, short_fn=nn_short,
                                    precomputed_ind=ind)

                                if len(tr) < 8:
                                    continue
                                ret = (bal / 10000 - 1) * 100
                                wins = [t for t in tr if t["pnl"] > 0]
                                wr = len(wins) / len(tr) * 100
                                gp = sum(t["pnl"] for t in wins) if wins else 0
                                gl = abs(sum(t["pnl"] for t in tr if t["pnl"] <= 0)) or 0.001
                                pf = gp / gl
                                eq_a = np.array(eq)
                                dd = ((np.maximum.accumulate(eq_a) - eq_a) / np.maximum.accumulate(eq_a) * 100).max()
                                rdd = ret / dd if dd > 0 else 0

                                if ret > 0 and pf > 1.0 and rdd > best_score:
                                    best_score = rdd
                                    best_params = (risk, sl, tp, tr_a, tr_d, cd)
                                    best_result = (bal, tr, eq)

                                if pf > 1.2 and ret > 3:
                                    print(f" {sl:>4.1f} {tp:>4.1f} {ret:>+6.1f}% {len(tr):>4} {wr:>5.1f}% {pf:>5.2f} {dd:>5.1f}%")

        if best_params and best_result:
            risk, sl, tp, tr_a, tr_d, cd = best_params
            print(f"\n >>> BEST NN: risk={risk*100:.1f}% SL={sl} TP={tp} Trail={tr_a}/{tr_d} CD={cd}")
            analyze_results(10000, *best_result)
        else:
            print(f"\n No profitable NN combo found.")

        return self.model, best_params, best_result

    def predict(self, close, high, low, vol):
        """Predict signals for new data."""
        X, _ = self._build_features()
        X = np.nan_to_num(X, nan=0.0, posinf=10.0, neginf=-10.0)
        return self.model.predict(self.scaler.transform(X))


# ═══════════════════════════════════════════════════════════════
# PART 3: COMBINED GA + NN — NEURON-EVOLVED STRATEGY
# ═══════════════════════════════════════════════════════════════

class NeuroEvolvedStrategy:
    """
    GA optimizes entry mode weights + exit parameters simultaneously.
    Uses NN as one of the entry signals alongside traditional modes.
    """
    def __init__(self, close, high, low, vol, ts, fee=0.0005):
        self.close = close
        self.high = high
        self.low = low
        self.vol = vol
        self.ts = ts
        self.fee = fee

    def run(self, verbose=True):
        """Run combined GA + NN optimization."""
        t0 = time.time()
        ind = compute_all_indicators(self.close, self.high, self.low, self.vol)

        if verbose:
            print(f"\n{'='*70}")
            print(f" NEURO-EVOLVED STRATEGY OPTIMIZER")
            print(f"{'='*70}")

        # Step 1: Train NN
        if verbose:
            print(f"\n--- Step 1: Training Neural Network ---")
        nn = NeuralSignalPredictor(self.close, self.high, self.low, self.vol, self.ts)
        nn_model, nn_best_params, nn_best_result = nn.train(verbose=verbose)

        # Step 2: GA on traditional Mode F
        if verbose:
            print(f"\n--- Step 2: GA Optimizing Mode F ---")
        ga = GeneticOptimizer(
            self.close, self.high, self.low, self.vol, self.ts,
            mode_f_long, mode_f_short,
            pop_size=40, generations=25,
        )
        ga_best_chrom, ga_best_fit = ga.evolve(verbose=verbose)

        # Step 3: Build combined entry from GA-optimized Mode F + NN
        if verbose:
            print(f"\n--- Step 3: Combining GA + NN Signals ---")
        nn_preds = nn.predict(self.close, self.high, self.low, self.vol)
        ga_params = ga._decode(ga_best_chrom)

        def combined_long(i, ind2, close2, vol2):
            # GA-optimized Mode F signal OR NN signal (union)
            f_long = mode_f_long(i, ind2, close2, vol2)
            nn_long = nn_preds[i] == 1
            return f_long or nn_long

        def combined_short(i, ind2, close2, vol2):
            f_short = mode_f_short(i, ind2, close2, vol2)
            nn_short = nn_preds[i] == -1
            return f_short or nn_short

        # Step 4: Optimize combined strategy
        if verbose:
            print(f"\n--- Step 4: Optimizing Combined Strategy ---")
        ga_combo = GeneticOptimizer(
            self.close, self.high, self.low, self.vol, self.ts,
            combined_long, combined_short,
            pop_size=40, generations=25,
        )
        combo_best_chrom, combo_best_fit = ga_combo.evolve(verbose=verbose)

        if verbose:
            elapsed = time.time() - t0
            print(f"\n{'='*70}")
            print(f" NEURO-EVOLVED OPTIMIZATION COMPLETE")
            print(f" Total time: {elapsed:.1f}s")
            print(f"{'='*70}")

        return {
            'nn_model': nn_model,
            'nn_best_params': nn_best_params,
            'ga_best_chrom': ga_best_chrom,
            'combo_best_chrom': combo_best_chrom,
            'ga_best_fit': ga_best_fit,
            'combo_best_fit': combo_best_fit,
        }


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    from app.services.data_cache import _load_cache

    cache = _load_cache("BTC-USDT", "5m")
    if not cache:
        print("No 5m cache found")
        return

    arr = np.array(cache, dtype=object)
    close_5m = arr[:, 4].astype(float)
    high_5m = arr[:, 2].astype(float)
    low_5m = arr[:, 3].astype(float)
    vol_5m = arr[:, 5].astype(float)
    ts_5m = arr[:, 0]

    # 1H data
    data_1h = downsample_5m_to_1h(cache)
    arr_1h = np.array(data_1h, dtype=object)
    close_1h = arr_1h[:, 4].astype(float)
    high_1h = arr_1h[:, 2].astype(float)
    low_1h = arr_1h[:, 3].astype(float)
    vol_1h = arr_1h[:, 5].astype(float)
    ts_1h = arr_1h[:, 0]

    print(f"{'#'*70}")
    print(f" STRATEGY OPTIMIZATION: GA + NEURAL NETWORK")
    print(f" Data: {len(cache)} bars 5m | {len(data_1h)} bars 1H")
    print(f"{'#'*70}")

    # ═══════════════════════════════════════════════════════════════
    # Run on 1H (best timeframe from previous work)
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print(f" 1H TIMEFRAME OPTIMIZATION")
    print(f"{'='*70}")

    optimizer = NeuroEvolvedStrategy(close_1h, high_1h, low_1h, vol_1h, ts_1h)
    results = optimizer.run(verbose=True)

    # Also run standalone GA for comparison
    print(f"\n\n{'='*70}")
    print(f" STANDALONE GA: Mode F on 1H (comparison)")
    print(f"{'='*70}")
    ga_standalone = GeneticOptimizer(
        close_1h, high_1h, low_1h, vol_1h, ts_1h,
        mode_f_long, mode_f_short,
        pop_size=40, generations=25,
    )
    ga_standalone.evolve(verbose=True)

    # Also test on 15m
    data_15m = downsample_5m_to_15m(cache)
    arr_15m = np.array(data_15m, dtype=object)
    close_15m = arr_15m[:, 4].astype(float)
    high_15m = arr_15m[:, 2].astype(float)
    low_15m = arr_15m[:, 3].astype(float)
    vol_15m = arr_15m[:, 5].astype(float)
    ts_15m = arr_15m[:, 0]

    print(f"\n\n{'='*70}")
    print(f" 15m TIMEFRAME OPTIMIZATION (GA only)")
    print(f"{'='*70}")
    ga_15m = GeneticOptimizer(
        close_15m, high_15m, low_15m, vol_15m, ts_15m,
        mode_f_long, mode_f_short,
        pop_size=40, generations=25,
    )
    ga_15m.evolve(verbose=True)


if __name__ == "__main__":
    asyncio.run(main())
