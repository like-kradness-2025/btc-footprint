#!/usr/bin/env python3
"""Generate BTC footprint + orderbook chart from btc-receiver data.

Data sources (from btc-receiver/data/live/):
  - live_trades_compact.jsonl : buy/sell volume per price bucket per minute
  - live_book_bucketed.jsonl  : orderbook depth snapshots
  - live_features_1s.jsonl    : mid price for candle OHLC

Output: single PNG with bid=green / ask=red heatmap + orderbook depth panel.

Layout:
  +----------------------------+------------------+
  | Candles + Footprint heatmap | Orderbook Depth  |
  | (bid=green / ask=red per   | (bid left, ask   |
  |  price bin, alpha = depth) |  right from mid) |
  +----------------------------+------------------+
  | Volume bars                |                  |
  +----------------------------+------------------+

Style: navy dark theme, bid=green / ask=red per price bin, alpha = depth.
Max depth bin → fully opaque (alpha 1.0).
Index-based X axis, footprint bars right-of-candle.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter
from matplotlib.colors import LinearSegmentedColormap, ListedColormap

# ── Color palette (navy dark) ──────────────────────────────────────────────
NAVY = "#07111f"
BG = "#0b1628"
GRID = "#2d4a6a"
TEXT = "#ecf3fe"
MUTED = "#96a8bf"
BID_GREEN = "#22c55e"  # solid green for bid volume
ASK_RED = "#ef4444"    # solid red for ask volume
DELTA_BID = "#38bdf8"  # light blue for delta-bid
DELTA_ASK = "#f97316"  # orange for delta-ask
UP = "#4ade80"
DOWN = "#f43f5e"
SPINE = "#3a5a7a"

# ── Defaults ────────────────────────────────────────────────────────────────
DEFAULT_PRICE_BIN = 10        # USD
DEFAULT_HOURS = 3             # data window
DEFAULT_TARGET_INTERVAL = 15  # minutes
DEFAULT_CANDLES = 12          # display count
CANDLE_WIDTH = 0.19
CANDLE_X_OFFSET = -0.3       # left-align candle within heatmap cell
GAP = 0.05
FP_WIDTH = 1.0                # max footprint bar width in index units
MIN_ALPHA = 0.08
MAX_ALPHA = 1.0

JST = None
try:
    import pytz
    JST = pytz.timezone("Asia/Tokyo")
except ImportError:
    pass


def _to_ts(obj) -> pd.Timestamp:
    """任意の時刻表現を UTC pd.Timestamp に統一する。"""
    if isinstance(obj, pd.Timestamp):
        if obj.tz is None:
            return obj.tz_localize("UTC")
        return obj.tz_convert("UTC")
    if isinstance(obj, np.datetime64):
        return pd.Timestamp(obj).tz_localize("UTC")
    if isinstance(obj, str):
        return pd.to_datetime(obj, utc=True)
    ts = pd.Timestamp(obj)
    if ts.tz is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _to_dt64(obj) -> np.datetime64:
    """任意の時刻表現を UTC np.datetime64[us] に統一する。"""
    return np.datetime64(_to_ts(obj).to_pydatetime().replace(tzinfo=None), "us")


# ── Data loading ────────────────────────────────────────────────────────────

def _load_jsonl_tail(
    path: Path,
    hours: float,
    byte_count: int = 100 * 1024 * 1024,
    cutoff: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Load only the last N hours from a JSONL file (read from end efficiently).

    Args:
        path: Path to JSONL file.
        hours: How many hours of data to keep (time filter).
        byte_count: Bytes to read from end of file (default 100MB). Reduce for
                    large raw files to speed up loading.
    """
    path = Path(path)
    import subprocess
    result = subprocess.run(
        ["tail", "-c", str(byte_count), str(path)],
        capture_output=True, timeout=120
    )
    if result.returncode != 0:
        print(f"WARNING: tail command failed (rc={result.returncode}): {path.name}", file=sys.stderr)
        return pd.DataFrame()
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    # First line may be truncated; skip it if we're not at file start
    file_size = path.stat().st_size
    if file_size > byte_count:
        lines = lines[1:]  # skip truncated first line
    if cutoff is None:
        cutoff = _to_ts("now") - pd.Timedelta(hours=hours)
    else:
        cutoff = _to_ts(cutoff)
    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts_str = d.get("ts") or d.get("timestamp")
        if ts_str is None:
            continue
        d["ts"] = ts_str  # keep as string, vectorize later
        rows.append(d)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df[df["ts"] >= cutoff].reset_index(drop=True)
    return df


def load_trades(
    path: Path,
    hours: int = DEFAULT_HOURS,
    byte_count: int = 100 * 1024 * 1024,
    cutoff: pd.Timestamp | None = None,
) -> pd.DataFrame:
    df = _load_jsonl_tail(path, hours, byte_count, cutoff=cutoff)
    if df.empty:
        return df
    required = {"ts", "price", "qty", "side"}
    alt_required = {"ts", "price_bucket", "buy", "sell"}
    compact_required = {"ts", "price_bucket", "qty_sum", "side"}
    if required.issubset(df.columns) or alt_required.issubset(df.columns) or compact_required.issubset(df.columns):
        return df
    print(f"WARNING: trades data missing expected columns (cols={list(df.columns)}), returning empty", file=sys.stderr)
    return pd.DataFrame()


def load_book_bucketed(
    path: Path,
    hours: int = DEFAULT_HOURS,
    byte_count: int = 100 * 1024 * 1024,
    cutoff: pd.Timestamp | None = None,
) -> pd.DataFrame:
    df = _load_jsonl_tail(path, hours, byte_count, cutoff=cutoff)
    if df.empty:
        return df
    if "mid" not in df.columns:
        print(f"WARNING: book data missing 'mid' column (cols={list(df.columns)})", file=sys.stderr)
        return pd.DataFrame()
    # Skip rows where bids_bucketed or asks_bucketed contain lists (corrupted)
    for col in ["bids_bucketed", "asks_bucketed"]:
        if col in df.columns and df[col].dtype == object:
            mask = df[col].apply(lambda x: isinstance(x, list))
            if mask.any():
                df = df[~mask]
    return df


def load_features(path: Path, hours: int = DEFAULT_HOURS, cutoff: pd.Timestamp | None = None) -> pd.DataFrame:
    df = _load_jsonl_tail(path, hours, cutoff=cutoff)
    if df.empty:
        return df
    if "mid" not in df.columns:
        print(f"WARNING: features data missing 'mid' column (cols={list(df.columns)})", file=sys.stderr)
        return pd.DataFrame()
    df["mid"] = pd.to_numeric(df["mid"], errors="coerce")
    return df.dropna(subset=["mid"])


def load_oi(path: Path, hours: int = DEFAULT_HOURS, cutoff: pd.Timestamp | None = None) -> pd.DataFrame:
    """Load OI data from live_oi.jsonl."""
    df = _load_jsonl_tail(path, hours, cutoff=cutoff)
    if df.empty:
        return df
    if "oi" not in df.columns:
        print(f"WARNING: OI data missing 'oi' column (cols={list(df.columns)})", file=sys.stderr)
        return pd.DataFrame()
    df["oi"] = pd.to_numeric(df["oi"], errors="coerce")
    return df.dropna(subset=["oi"])


# ── Processing ──────────────────────────────────────────────────────────────

def build_candles(features: pd.DataFrame, interval_minutes: int) -> pd.DataFrame:
    freq = f"{interval_minutes}min"
    features["bucket"] = features["ts"].dt.floor(freq)
    candles = (
        features.groupby("bucket")["mid"]
        .agg(open="first", high="max", low="min", close="last")
        .reset_index()
    )
    candles.rename(columns={"bucket": "ts"}, inplace=True)
    return candles


def _rebucket_trades(
    trades: pd.DataFrame,
    price_bin: int,
) -> pd.DataFrame:
    """Rebucket raw trades to a given price bin size.

    Raw trades (live_trades.jsonl) have 'price' (float).
    Compact trades (live_trades_compact.jsonl) have 'price_bucket' (int, pre-bucketed).
    This function creates a new 'price_bucket' at the desired resolution.
    """
    if "price" in trades.columns:
        # Raw trades — bucket from exact price
        trades = trades.copy()
        trades["price_bucket"] = (trades["price"] // price_bin) * price_bin
        trades["price_bucket"] = trades["price_bucket"].astype(int)
    # else: already has price_bucket, leave as-is (user's data bin)
    return trades


def build_footprint(
    trades: pd.DataFrame,
    interval_minutes: int,
    price_bin: int,
) -> tuple[pd.DataFrame, dict]:
    if trades.empty:
        return pd.DataFrame(), {}

    freq = f"{interval_minutes}min"
    trades = _rebucket_trades(trades, price_bin)

    # 形式B (price_bucket, buy, sell) は side 列がないため、
    # 先に melt して形式C相当 (price_bucket, qty_sum, side) に正規化する。
    # これにより既存の groupby / pivot ロジックをそのまま再利用できる。
    if "side" not in trades.columns and {"buy", "sell"}.issubset(trades.columns):
        trades = trades.melt(
            id_vars=[c for c in trades.columns if c not in {"buy", "sell"}],
            value_vars=["buy", "sell"],
            var_name="side",
            value_name="qty_sum",
        )
        trades["qty_sum"] = pd.to_numeric(trades["qty_sum"], errors="coerce")
        trades = trades.dropna(subset=["qty_sum"]).copy()

    trades["interval"] = trades["ts"].dt.floor(freq)

    qty_col = "qty" if "qty" in trades.columns else "qty_sum"
    if qty_col not in trades.columns:
        raise KeyError(f"Neither 'qty' nor 'qty_sum' column found in trades data (columns: {list(trades.columns)})")

    agg = (
        trades.groupby(["interval", "price_bucket", "side"])[qty_col]
        .sum()
        .reset_index(name=qty_col)
    )

    pivot = agg.pivot_table(
        index=["interval", "price_bucket"],
        columns="side",
        values=qty_col,
        aggfunc="sum",
    ).fillna(0)

    for col in ["buy", "sell"]:
        if col not in pivot.columns:
            pivot[col] = 0.0
    pivot[["buy", "sell"]] = pivot[["buy", "sell"]].apply(pd.to_numeric, errors="coerce").fillna(0.0)

    pivot["total"] = pivot["buy"] + pivot["sell"]
    pivot["delta"] = pivot["buy"] - pivot["sell"]
    pivot = pivot.reset_index()
    pivot.sort_values(["interval", "price_bucket"], inplace=True)

    return pivot, {
        "ts_min": trades["ts"].min(),
        "ts_max": trades["ts"].max(),
    }


def build_orderbook_depth(
    book_df: pd.DataFrame,
    ts_min: pd.Timestamp | None,
    ts_max: pd.Timestamp | None,
    max_levels: int = 40,
) -> dict | None:
    if book_df.empty:
        return None

    subset = book_df
    if ts_min is not None:
        subset = subset[subset["ts"] >= ts_min]
    if ts_max is not None:
        subset = subset[subset["ts"] <= ts_max]
    if subset.empty:
        # Fallback: use the latest row by timestamp from the full dataset
        latest_row = book_df.sort_values("ts").iloc[-1]
    else:
        latest_row = subset.sort_values("ts").iloc[-1]
    mid = float(latest_row.get("mid", 0))
    bids_raw = latest_row.get("bids_bucketed", {})
    asks_raw = latest_row.get("asks_bucketed", {})

    bids = sorted(
        [(float(k), float(v)) for k, v in bids_raw.items() if float(v) > 0],
        key=lambda x: x[0], reverse=True,
    )[:max_levels]
    asks = sorted(
        [(float(k), float(v)) for k, v in asks_raw.items() if float(v) > 0],
        key=lambda x: x[0],
    )[:max_levels]

    return {"mid": mid, "bids": bids, "asks": asks, "ts": latest_row["ts"]}


def build_ob_heatmap(
    book_df: pd.DataFrame,
    candles: pd.DataFrame,
    price_lo: float,
    price_hi: float,
    price_bin: float,
    interval_minutes: int = 5,
) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    """Build orderbook heatmap arrays for pcolormesh at candle interval resolution.

    Each bucket = one candle interval. Book depth data within each interval is summed.

    Returns (x_positions, price_bins, bid_heatmap, ask_heatmap) where:
      - x_positions: 1D array of x-edge positions (n_buckets + 1) for pcolormesh
      - price_bins: 1D array of price bin centers
      - bid_heatmap: (n_buckets, n_bins) array of normalized bid depth [0..1]
      - ask_heatmap: (n_buckets, n_bins) array of normalized ask depth [0..1]
    Returns all None on failure.
    """
    if book_df.empty or candles.empty:
        return None, None, None, None

    n_candles = len(candles)
    if n_candles < 1:
        return None, None, None, None

    # Align buckets directly to candle timestamps using floor-based assignment.
    freq = f"{interval_minutes}min"
    candle_ts_map = {ts: i for i, ts in enumerate(candles["ts"])}
    book_times = pd.to_datetime(book_df["ts"], utc=True)
    book_buckets = book_times.dt.floor(freq)
    n_buckets = n_candles

    # Fractional x position — one cell per candle, centered at integer indices
    x_positions = np.linspace(-0.5, n_candles - 0.5, n_candles + 1)

    price_bins = np.arange(
        (price_lo // price_bin) * price_bin,
        (price_hi // price_bin) * price_bin + price_bin,
        price_bin
    )
    if len(price_bins) == 0:
        return None, None, None, None

    bid_hm = np.full((n_buckets, len(price_bins)), np.nan)
    ask_hm = np.full((n_buckets, len(price_bins)), np.nan)

    # Precompute candle bucket masks once
    low_high_masks: list[np.ndarray] = []
    for low, high in candles[["low", "high"]].itertuples(index=False, name=None):
        low_high_masks.append((price_bins >= low) & (price_bins <= high))

    # Map each book timestamp to its floor candle bucket.
    book_rows = book_df[["ts", "bids_bucketed", "asks_bucketed"]].itertuples(index=False)
    for row_idx, brow in enumerate(book_rows):
        bucket_ts = book_buckets.iloc[row_idx]
        nearest = candle_ts_map.get(bucket_ts)
        if nearest is None:
            continue

        bids_bucketed = cast(dict, brow.bids_bucketed) if brow.bids_bucketed is not None else {}
        asks_bucketed = cast(dict, brow.asks_bucketed) if brow.asks_bucketed is not None else {}

        for p_str, qty in bids_bucketed.items():
            p = float(p_str)
            pi = int((p - price_bins[0]) / price_bin)
            if 0 <= pi < len(price_bins):
                if np.isnan(bid_hm[nearest, pi]):
                    bid_hm[nearest, pi] = 0
                bid_hm[nearest, pi] += float(qty)

        for p_str, qty in asks_bucketed.items():
            p = float(p_str)
            pi = int((p - price_bins[0]) / price_bin)
            if 0 <= pi < len(price_bins):
                if np.isnan(ask_hm[nearest, pi]):
                    ask_hm[nearest, pi] = 0
                ask_hm[nearest, pi] += float(qty)

    # Mask out price bins within each candle's high-low range
    for bi, mask in enumerate(low_high_masks):
        if mask.any():
            bid_hm[bi, mask] = np.nan
            ask_hm[bi, mask] = np.nan

    return x_positions, price_bins, bid_hm, ask_hm


def resample_oi_to_candles(oi_df: pd.DataFrame, candles: pd.DataFrame, interval_minutes: int) -> pd.Series | None:
    """Resample OI to match candle timestamps. Returns Series indexed by candle index (0..n-1)."""
    if oi_df.empty or candles.empty:
        return None
    freq = f"{interval_minutes}min"
    oi = oi_df.copy()
    oi["bucket"] = oi["ts"].dt.floor(freq)
    last_oi = oi.groupby("bucket")["oi"].last().reset_index()
    last_oi.rename(columns={"bucket": "ts"}, inplace=True)
    merged = candles[["ts"]].merge(last_oi, on="ts", how="left")
    if merged["oi"].isna().all():
        print("WARNING: OI data could not be aligned to candle timestamps", file=sys.stderr)
        return None
    merged["oi"] = merged["oi"].ffill()
    return merged["oi"]


# ── Rendering ───────────────────────────────────────────────────────────────

def _fmt(x):
    """Compact label for Volume axis: always in k units."""
    return f"{x / 1e3:.1f}k"


def _fmt_oi(x):
    """Compact label for OI axis: k-value without suffix (104,400 → 104.4)."""
    return f"{x / 1e3:.1f}"


def render_footprint_chart(
    candles: pd.DataFrame,
    footprint: pd.DataFrame,
    ob_data: dict | None,
    book_heatmap: tuple | None = None,
    symbol: str = "BTCUSDT",
    title: str = "BTC Footprint + Orderbook",
    out_path: str | Path | None = None,
    interval_label: str = "15m",
    display_candles: int = DEFAULT_CANDLES,
    show_ob_heatmap: bool = True,
    price_bin: int = DEFAULT_PRICE_BIN,
    ob_price_bin: int = DEFAULT_PRICE_BIN,
    oi_data: pd.Series | None = None,
) -> tuple[plt.Figure, plt.Axes]:
    """Render footprint chart with bid=green / ask=red heatmap.

    Index-based X axis. Footprint bars right-of-candle.
    Each price bin shows ONE colour: green if bid-dominant, red if ask-dominant.
    Alpha = volume intensity. Max volume → fully opaque.
    """
    if candles.empty:
        fig, ax = plt.subplots(figsize=(16, 10))
        ax.text(0.5, 0.5, "No data", ha="center", va="center", color="white")
        return fig, ax

    n = min(len(candles), display_candles)
    candles = candles.tail(n).reset_index(drop=True)
    x_idx = np.arange(n)  # 0, 1, 2, ..., n-1

    # Price range
    price_min = candles["low"].min()
    price_max = candles["high"].max()
    pad = max(50, (price_max - price_min) * 0.12)
    price_lo = price_min - pad
    price_hi = price_max + pad

    # X-axis extents (heatmap cells span -0.5 to n-0.5)
    cell_left = -0.5
    cell_right = (n - 1) + 0.5
    margin = 0.5
    xlim_l = cell_left - margin
    fp_right = (n - 1) + CANDLE_X_OFFSET + CANDLE_WIDTH / 2 + GAP + FP_WIDTH
    xlim_r = max(cell_right + margin, fp_right + 0.15)

    # ── Layout ──
    fig = plt.figure(figsize=(12, 11), dpi=180)
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(
        2, 1, height_ratios=[4, 1],
        hspace=0.05,
        left=0.03, right=0.82, bottom=0.07, top=0.95,
    )
    ax_main = fig.add_subplot(gs[0, 0])
    ax_vol = fig.add_subplot(gs[1, 0])

    # Place Orderbook / Vol Profile as independent axes in the right margin,
    # above the lower Vol/OI/CVD label area.
    ob_y0, ob_y1 = 0.26, 0.95
    ob_h = ob_y1 - ob_y0
    ax_ob = fig.add_axes((0.826, ob_y0, 0.085, ob_h))
    ax_vp = fig.add_axes((0.916, ob_y0, 0.079, ob_h))

    # ── Main chart axes ──
    ax_main.set_facecolor(NAVY)
    ax_main.set_ylim(price_lo, price_hi)
    ax_main.set_xlim(xlim_l, xlim_r)
    ax_main.set_xticks([])
    ax_main.yaxis.tick_right()
    ax_main.yaxis.set_label_position("right")
    ax_main.tick_params(axis="y", colors=TEXT, labelsize=10)
    ax_main.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:,.0f}"))
    ax_main.grid(True, axis="both", linestyle=":", alpha=0.10, color=GRID)
    for s in ax_main.spines.values():
        s.set_color(GRID)
        s.set_alpha(0.3)

    # ├─ Orderbook heatmap background (per-candle) ──
    if book_heatmap is not None and show_ob_heatmap:
        x_pos_hm, price_bins_hm, bid_hm, ask_hm = book_heatmap
        if bid_hm is not None and ask_hm is not None:
            hm_bid_max = max(np.nanmax(bid_hm), 1.0)
            hm_ask_max = max(np.nanmax(ask_hm), 1.0)
            bid_norm = bid_hm.T / hm_bid_max
            ask_norm = ask_hm.T / hm_ask_max

            # Custom colormaps: transparent at 0 → vivid color at max
            # Cubic alpha curve: low depth nearly transparent, thick depth pops
            n_c = 256
            bid_clr = np.zeros((n_c, 4))
            ask_clr = np.zeros((n_c, 4))
            bid_clr[:, 0] = np.linspace(0, 0.10, n_c)  # R
            bid_clr[:, 1] = np.linspace(0, 0.75, n_c)  # G
            bid_clr[:, 2] = np.linspace(0, 0.20, n_c)  # B
            bid_clr[:, 3] = np.linspace(0, 1, n_c) ** 3  # A: cubic
            ask_clr[:, 0] = np.linspace(0, 0.80, n_c)  # R
            ask_clr[:, 1] = np.linspace(0, 0.15, n_c)  # G
            ask_clr[:, 2] = np.linspace(0, 0.15, n_c)  # B
            ask_clr[:, 3] = np.linspace(0, 1, n_c) ** 3  # A: cubic
            cmap_bid = ListedColormap(bid_clr, 'bid_hm')
            cmap_ask = ListedColormap(ask_clr, 'ask_hm')
            cmap_bid.set_bad(alpha=0)
            cmap_ask.set_bad(alpha=0)

            # Y edge positions for pcolormesh (n_bins + 1 edges)
            # Calculate y-edges from actual bin centers (use min gap for robustness)
            if len(price_bins_hm) > 1:
                half_step = max(np.diff(price_bins_hm).min(), 1.0) / 2.0
            else:
                half_step = DEFAULT_PRICE_BIN / 2.0
            y_edges = np.append(
                price_bins_hm - half_step,
                price_bins_hm[-1] + half_step
            )
            X, Y = np.meshgrid(x_pos_hm, y_edges)
            ax_main.pcolormesh(X, Y, bid_norm, cmap=cmap_bid,
                               alpha=1.0, shading='auto', zorder=0,
                               linewidth=0, edgecolor='none')
            ax_main.pcolormesh(X, Y, ask_norm, cmap=cmap_ask,
                               alpha=1.0, shading='auto', zorder=0,
                               linewidth=0, edgecolor='none')

    # ── Candles ──
    for i in range(n):
        row = candles.iloc[i]
        o, h, l, c = row["open"], row["high"], row["low"], row["close"]
        color = UP if c >= o else DOWN
        xi = i + CANDLE_X_OFFSET
        ax_main.vlines(xi, l, h, color=color, linewidth=1.0, alpha=0.85, zorder=5)
        ax_main.bar(xi, abs(c - o), CANDLE_WIDTH, bottom=min(o, c),
                    color=color, alpha=0.85, linewidth=0.4, edgecolor=color, zorder=5)

    # ── Footprint heatmap (bid=green / ask=red) ──
    candle_ts_to_idx = {ts: idx for idx, ts in enumerate(candles["ts"])}
    fp_plot = pd.DataFrame()
    if not footprint.empty:
        # Match footprint intervals to candles
        fp_plot = footprint.copy()
        fp_plot["_ts_match"] = fp_plot["interval"]
        fp_plot = fp_plot[fp_plot["_ts_match"].isin(candle_ts_to_idx)]

        if not fp_plot.empty:
            # Per-side max for alpha normalisation
            max_buy = max(fp_plot["buy"].max(), 1.0)
            max_sell = max(fp_plot["sell"].max(), 1.0)
            global_max = max(fp_plot["total"].max(), 1.0)

            for _, row in fp_plot.iterrows():
                intv = row["interval"]
                pb = row["price_bucket"]
                buy_v = row["buy"]
                sell_v = row["sell"]
                total_v = row["total"]
                if total_v <= 0:
                    continue

                # Find candle index
                ci = candle_ts_to_idx.get(intv)
                if ci is None:
                    continue

                # Bar geometry
                right_origin = ci + CANDLE_X_OFFSET + CANDLE_WIDTH / 2 + GAP
                usable_w = FP_WIDTH * (total_v / global_max)

                if usable_w <= 1e-8:
                    continue

                y0 = pb - price_bin / 2
                y1 = pb + price_bin / 2

                eq_v = min(buy_v, sell_v)
                delta_v = abs(buy_v - sell_v)
                eq_w = usable_w * (eq_v / total_v) if total_v > 0 else 0
                delta_w = usable_w * (delta_v / total_v) if total_v > 0 else 0
                dc = DELTA_BID if buy_v >= sell_v else DELTA_ASK

                # Equilibrium portion (neutral)
                if eq_w > 1e-8:
                    ax_main.fill_betweenx(
                        [y0, y1],
                        [right_origin, right_origin],
                        [right_origin + eq_w, right_origin + eq_w],
                        color=MUTED, alpha=0.45, linewidth=0, edgecolor="none",
                    )
                # Delta portion (bid=green / ask=red)
                if delta_w > 1e-8:
                    ax_main.fill_betweenx(
                        [y0, y1],
                        [right_origin + eq_w, right_origin + eq_w],
                        [right_origin + eq_w + delta_w, right_origin + eq_w + delta_w],
                        color=dc, alpha=0.75, linewidth=0, edgecolor="none",
                    )

    # ── Time labels above candles ──
    for i in range(n):
        t = candles.iloc[i]["ts"]
        if JST and hasattr(t, "tz_convert"):
            t = t.tz_convert(JST)
        ax_main.text(i + CANDLE_X_OFFSET, price_hi + (price_hi - price_lo) * 0.02,
                     t.strftime("%H:%M"), color=MUTED, fontsize=9,
                     ha="center", va="bottom", alpha=0.85)

    # ── Latest price badge ──
    last = candles.iloc[-1]
    lp = last["close"]
    lc = UP if last["close"] >= last["open"] else DOWN
    ax_main.text(0.02, 0.97, f"{lp:,.0f}  {interval_label}", transform=ax_main.transAxes,
                 fontsize=24, fontweight="bold", color=lc, va="top", ha="left",
                 bbox=dict(boxstyle="round,pad=0.2", fc="black", ec=lc, lw=0.8, alpha=0.75),
                 zorder=6)

    # ── Current price horizontal line ──
    ax_main.axhline(y=lp, color=lc, linewidth=1.0, linestyle="--", alpha=0.7, zorder=4)

    # ── Legend ──
    from matplotlib.patches import Patch
    leg = ax_main.legend(
        handles=[
            Patch(color=BID_GREEN, alpha=0.8, label="Bid"),
            Patch(color=ASK_RED, alpha=0.8, label="Ask"),
        ],
        fontsize=10, loc="upper right", framealpha=0.6, labelcolor=TEXT,
        bbox_to_anchor=(0.99, 0.99),
    )
    leg.get_frame().set_facecolor("black")

    # ── Orderbook panel ──
    ax_ob.set_facecolor(NAVY)
    for s in ax_ob.spines.values():
        s.set_color(GRID)
        s.set_alpha(0.3)
    ax_ob.tick_params(colors=MUTED, labelsize=9)
    ax_ob.set_xticks([])
    ax_ob.set_title("Orderbook", color=TEXT, fontsize=11)
    ax_ob.set_ylim(price_lo, price_hi)

    if ob_data and ob_data.get("bids"):
        bids = [(p, q) for p, q in ob_data["bids"] if price_lo <= p <= price_hi]
        asks = [(p, q) for p, q in ob_data["asks"] if price_lo <= p <= price_hi]
        all_q = [q for _, q in bids] + [q for _, q in asks]
        max_q = max(all_q) if all_q else 1.0

        if bids:
            prices, qties = zip(*bids)
            ax_ob.barh(prices, [-4.0 * q / max_q for q in qties],
                       height=ob_price_bin * 0.85,
                       color=BID_GREEN, alpha=0.55, align="center", linewidth=0)
        if asks:
            prices, qties = zip(*asks)
            ax_ob.barh(prices, [4.0 * q / max_q for q in qties],
                       height=ob_price_bin * 0.85,
                       color=ASK_RED, alpha=0.55, align="center", linewidth=0)
        ax_ob.axvline(0, color=TEXT, linewidth=0.4, alpha=0.3)
        ax_ob.set_xlim(-4.5, 4.5)

        # Depth label
        ax_ob.text(0.5, 0.02, f"depth: {_fmt(max_q)}",
                   transform=ax_ob.transAxes, color=MUTED, fontsize=10,
                   ha="center", va="bottom")
    else:
        ax_ob.set_xlim(-4.5, 4.5)

    # ── Volume Profile panel (rightmost) ──
    ax_vp.set_facecolor(NAVY)
    for s in ax_vp.spines.values():
        s.set_color(GRID)
        s.set_alpha(0.3)
    ax_vp.set_title("Vol Profile", color=TEXT, fontsize=9)
    ax_vp.set_ylim(price_lo, price_hi)
    ax_vp.tick_params(axis="y", left=False, labelleft=False, right=False, labelright=False, colors=MUTED, labelsize=8)
    ax_vp.yaxis.set_ticks_position("none")
    ax_vp.yaxis.set_label_position("right")
    ax_vp.set_xticks([])

    if not footprint.empty:
        fp_vp = footprint[footprint["interval"].isin(candle_ts_to_idx)]
        if not fp_vp.empty:
            agg = fp_vp.groupby("price_bucket")[["buy", "sell"]].sum().reset_index()
            agg = agg[(agg["price_bucket"] >= price_lo) & (agg["price_bucket"] <= price_hi)]
            if not agg.empty:
                max_total = max(agg["buy"].max(), agg["sell"].max(), 1.0)
                for _, row in agg.iterrows():
                    pb = row["price_bucket"]
                    eq = min(row["buy"], row["sell"])
                    delta = abs(row["buy"] - row["sell"])
                    dc = BID_GREEN if row["buy"] >= row["sell"] else ASK_RED
                    # equilibrium part (always present if both sides traded)
                    if eq > 0:
                        ax_vp.barh(pb, 4.0 * eq / max_total,
                                   height=ob_price_bin * 0.85,
                                   color=MUTED, alpha=0.40, align="center", linewidth=0)
                    # delta part (stacked on top)
                    if delta > 0:
                        ax_vp.barh(pb, 4.0 * delta / max_total,
                                   height=ob_price_bin * 0.85,
                                   color=dc, alpha=0.55, align="center", linewidth=0)
                ax_vp.axvline(0, color=TEXT, linewidth=0.4, alpha=0.3)
                ax_vp.set_xlim(-4.5, 4.5)
                ax_vp.text(0.5, 0.02, f"max: {_fmt(max_total)}",
                           transform=ax_vp.transAxes, color=MUTED, fontsize=8,
                           ha="center", va="bottom")
                vp_has_data = True
    if not locals().get("vp_has_data"):
        ax_vp.set_xlim(-4.5, 4.5)

    # ── Volume ──
    ax_vol.set_facecolor(NAVY)
    for s in ax_vol.spines.values():
        s.set_color(GRID)
        s.set_alpha(0.3)
    # Right-side primary Y axis for volume.
    # Keep this on the panel edge; the OI twin axis is offset further right.
    ax_vol.tick_params(
        axis="y", left=False, labelleft=False, right=True, labelright=True,
        colors=MUTED, labelsize=9, pad=3,
    )
    ax_vol.yaxis.set_ticks_position("right")
    ax_vol.yaxis.set_label_position("right")
    ax_vol.spines["left"].set_visible(False)
    ax_vol.spines["right"].set_visible(True)
    ax_vol.spines["right"].set_position(("outward", 0))
    ax_vol.spines["right"].set_color(MUTED)
    ax_vol.spines["right"].set_alpha(0.45)
    ax_vol.set_ylabel("Vol", color=MUTED, fontsize=9, labelpad=6)
    ax_vol.yaxis.set_major_formatter(FuncFormatter(lambda y, _: _fmt(y)))
    ax_vol.grid(True, axis="y", linestyle=":", alpha=0.10, color=GRID)
    ax_vol.set_xlim(xlim_l, xlim_r)
    ax_vol.set_xticks([])

    if not footprint.empty:
        vol_agg = footprint[footprint["interval"].isin(candle_ts_to_idx)].copy()
        vol_agg = vol_agg.groupby("interval")[["buy", "sell"]].sum().reset_index()
        for _, row in vol_agg.iterrows():
            ci = candle_ts_to_idx.get(row["interval"])
            if ci is None:
                continue
            bx = ci + CANDLE_X_OFFSET
            buy = row["buy"]
            sell = row["sell"]
            eq = min(buy, sell)
            delta = abs(buy - sell)
            dc = BID_GREEN if buy >= sell else ASK_RED
            ax_vol.bar(bx, eq, width=CANDLE_WIDTH * 2.0,
                       color=MUTED, alpha=0.50, linewidth=0)
            ax_vol.bar(bx, delta, bottom=eq, width=CANDLE_WIDTH * 2.0,
                       color=dc, alpha=0.50, linewidth=0)

    # ── OI overlay on volume panel ──
    if oi_data is not None and not oi_data.isna().all():
        ax_oi = ax_vol.twinx()
        ax_oi.patch.set_visible(False)
        oi_color = "#fbbf24"  # amber/yellow
        oi_vals = np.asarray(oi_data)
        oi_min, oi_max = oi_vals.min(), oi_vals.max()
        oi_pad = (oi_max - oi_min) * 0.05 if oi_max > oi_min else oi_max * 0.05
        x_pos = np.arange(len(oi_data)) + CANDLE_X_OFFSET
        ax_oi.plot(x_pos, oi_vals, color=oi_color, linewidth=1.2, alpha=0.8, zorder=5)
        ax_oi.set_ylim(oi_min - oi_pad, oi_max + oi_pad)
        ax_oi.tick_params(
            axis="y", left=False, labelleft=False, right=True, labelright=True,
            colors=oi_color, labelsize=8, pad=3,
        )
        ax_oi.yaxis.set_ticks_position("right")
        ax_oi.yaxis.set_label_position("right")
        ax_oi.yaxis.set_major_formatter(FuncFormatter(lambda y, _: _fmt_oi(y)))
        ax_oi.set_ylabel("OI", color=oi_color, fontsize=9, labelpad=8)
        ax_oi.spines["left"].set_visible(False)
        ax_oi.spines["right"].set_visible(True)
        ax_oi.spines["right"].set_position(("outward", 62))
        ax_oi.spines["right"].set_color(oi_color)
        ax_oi.spines["right"].set_alpha(0.45)

    # ── CVD overlay on volume panel ──
    cvd_color = "#a78bfa"  # violet/purple
    cvd_vals = None
    if not footprint.empty:
        cvd_agg = footprint[footprint["interval"].isin(candle_ts_to_idx)].copy()
        cvd_agg = cvd_agg.groupby("interval")[["buy", "sell"]].sum()
        cvd_agg["delta"] = cvd_agg["buy"] - cvd_agg["sell"]
        # Align deltas to candle order, fill missing with 0, then cumsum
        delta_by_ts = cvd_agg["delta"]
        candle_ts_list = list(candles["ts"])
        cvd_series = np.array(
            [delta_by_ts.get(ts, 0.0) for ts in candle_ts_list],  # type: ignore[arg-type]
            dtype=float,
        )
        cvd_vals = np.cumsum(cvd_series)
    if cvd_vals is not None and cvd_vals.max() != cvd_vals.min():
        ax_cvd = ax_vol.twinx()
        ax_cvd.patch.set_visible(False)
        x_pos = np.arange(n) + CANDLE_X_OFFSET
        ax_cvd.plot(x_pos, cvd_vals, color=cvd_color, linewidth=1.4, alpha=0.85, zorder=6)
        cvd_min, cvd_max = cvd_vals.min(), cvd_vals.max()
        cvd_pad = (cvd_max - cvd_min) * 0.05
        ax_cvd.set_ylim(cvd_min - cvd_pad, cvd_max + cvd_pad)
        ax_cvd.tick_params(
            axis="y", left=False, labelleft=False, right=True, labelright=True,
            colors=cvd_color, labelsize=8, pad=3,
        )
        ax_cvd.yaxis.set_ticks_position("right")
        ax_cvd.yaxis.set_label_position("right")
        ax_cvd.yaxis.set_major_formatter(FuncFormatter(lambda y, _: _fmt(y)))
        ax_cvd.set_ylabel("CVD", color=cvd_color, fontsize=9, labelpad=8)
        ax_cvd.spines["left"].set_visible(False)
        ax_cvd.spines["right"].set_visible(True)
        ax_cvd.spines["right"].set_position(("outward", 110))
        ax_cvd.spines["right"].set_color(cvd_color)
        ax_cvd.spines["right"].set_alpha(0.45)

    # twinx() moves the original axis back to the left in some matplotlib
    # versions. Re-apply the Volume axis placement after the OI twin exists.
    ax_vol.yaxis.tick_right()
    ax_vol.yaxis.set_ticks_position("right")
    ax_vol.yaxis.set_label_position("right")
    ax_vol.tick_params(
        axis="y", left=False, labelleft=False, right=True, labelright=True,
        colors=MUTED, labelsize=9, pad=3,
    )
    ax_vol.spines["left"].set_visible(False)
    ax_vol.spines["right"].set_visible(True)
    ax_vol.spines["right"].set_position(("outward", 0))

    # X-axis time labels. Use ax_vol directly instead of twiny() so no overlay
    # axis can hide or confuse the Volume/OI right-side Y axes.
    step = max(1, n // 6)
    tick_positions = []
    tick_labels = []
    for i in range(0, n, step):
        t = candles.iloc[i]["ts"]
        if JST and hasattr(t, "tz_convert"):
            t = t.tz_convert(JST)
        tick_positions.append(i + CANDLE_X_OFFSET)
        tick_labels.append(t.strftime("%H:%M"))
    if (n - 1) not in tick_positions:
        t = candles.iloc[-1]["ts"]
        if JST and hasattr(t, "tz_convert"):
            t = t.tz_convert(JST)
        tick_positions.append((n - 1) + CANDLE_X_OFFSET)
        tick_labels.append(t.strftime("%H:%M"))

    ax_vol.set_xticks(tick_positions)
    ax_vol.set_xticklabels(tick_labels, color=TEXT, fontsize=9)
    ax_vol.tick_params(axis="x", length=2, pad=2, colors=TEXT)

    # ── Title ──
    start_t = candles.iloc[0]["ts"]
    end_t = candles.iloc[-1]["ts"]
    if JST:
        start_t = start_t.tz_convert(JST)
        end_t = end_t.tz_convert(JST)
    fig.suptitle(
        f"{title}  ({n} candles / ${price_bin} bins)  "
        f"{start_t.strftime('%H:%M')} – {end_t.strftime('%H:%M')} JST  •  bid=green  ask=red",
        color=TEXT, fontsize=16, y=0.97,
    )

    # ── Save ──
    if out_path:
        fig.savefig(str(out_path), dpi=200, facecolor=fig.get_facecolor(), bbox_inches="tight")
        print(f"rendered={out_path} candles={n} fp_rows={len(fp_plot) if not footprint.empty else 0}")
    plt.close(fig)

    return fig, ax_main


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate BTC footprint heatmap from btc-receiver data",
    )
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS,
                        help="Data window in hours (default: 3)")
    parser.add_argument("--target-minutes", type=int, default=DEFAULT_TARGET_INTERVAL,
                        help="Candle interval in minutes (default: 15)")
    parser.add_argument("--price-bin", type=int, default=DEFAULT_PRICE_BIN,
                        help="Price bucket size for footprint bars in USD (default: 10)")
    parser.add_argument("--ob-price-bin", type=int, default=DEFAULT_PRICE_BIN,
                        help="Price bucket size for orderbook heatmap in USD (default: 10)")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--title", default="BTC Footprint + Orderbook")
    parser.add_argument("--candles", type=int, default=DEFAULT_CANDLES,
                        help="Number of candles to display (default: 12)")
    parser.add_argument("--no-ob-heatmap", action="store_true",
                        help="Disable orderbook depth heatmap background")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"ERROR: data-dir not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    trades_path = data_dir / "live_trades_compact.jsonl"
    book_path = data_dir / "live_book_bucketed.jsonl"
    features_path = data_dir / "live_features_1s.jsonl"
    raw_trades_path = data_dir / "live_trades.jsonl"

    # Use raw trades for non-default price bin (rebucket at desired resolution)
    use_raw = args.price_bin != DEFAULT_PRICE_BIN and raw_trades_path.exists()
    if use_raw:
        trades_path = raw_trades_path

    if not trades_path.exists():
        print(f"ERROR: trades file not found: {trades_path}", file=sys.stderr)
        sys.exit(1)

    # Reduce tail size for raw trades (large file → faster loading)
    raw_tail_bytes = 50 * 1024 * 1024 if use_raw else 100 * 1024 * 1024

    print(f"Loading trades from {trades_path}...{' (raw, rebucketing to $' + str(args.price_bin) + ')' if use_raw else ''}")
    trades = load_trades(trades_path, args.hours, raw_tail_bytes)
    if trades.empty:
        print("ERROR: no trade data loaded", file=sys.stderr)
        sys.exit(1)
    print(f"  loaded {len(trades)} rows, range: {trades['ts'].min()} → {trades['ts'].max()}")

    end_time = trades["ts"].max()
    start_time = end_time - pd.Timedelta(hours=args.hours)
    trades = trades[trades["ts"] >= start_time]
    print(f"  filtered to {len(trades)} rows ({args.hours}h window)")

    print("Building footprint matrix...")
    footprint, meta = build_footprint(trades, args.target_minutes, args.price_bin)
    print(f"  {len(footprint)} footprint rows, {footprint['interval'].nunique()} intervals")

    print("Building candles...")
    has_features = features_path.exists()
    candles = pd.DataFrame()
    if has_features:
        features = load_features(features_path, args.hours, cutoff=start_time)
        features = features[features["ts"] >= start_time]
        if not features.empty:
            candles = build_candles(features, args.target_minutes)
            print(f"  {len(candles)} candles from features")
    else:
        print("  features file not found, skipping candles")

    if candles.empty and not footprint.empty:
        fp_agg = footprint.groupby("interval").agg(
            low=("price_bucket", "min"), high=("price_bucket", "max"),
        ).reset_index()
        candles = pd.DataFrame()
        candles["ts"] = fp_agg["interval"]
        candles["open"] = fp_agg["low"]
        candles["high"] = fp_agg["high"]
        candles["low"] = fp_agg["low"]
        candles["close"] = fp_agg["high"]
        print(f"  {len(candles)} candles from trade fallback")

    # Limit to display count BEFORE building OB heatmap
    candles = candles.tail(args.candles).reset_index(drop=True)
    print(f"  limited to {len(candles)} candles for display")

    print("Loading orderbook...")
    ob_data = None
    book_heatmap = None
    if book_path.exists():
        books = load_book_bucketed(book_path, args.hours, cutoff=start_time)
        if not books.empty:
            ob_data = build_orderbook_depth(books, start_time, end_time)
            if ob_data:
                print(f"  OB snapshot at {ob_data['ts']}, {len(ob_data['bids'])} bids, {len(ob_data['asks'])} asks")
            else:
                print("  no OB snapshot in window")
            # Build per-candle heatmap from DISPLAY candles only
            if not candles.empty:
                price_lo = candles["low"].min()
                price_hi = candles["high"].max()
                pad = max(50, (price_hi - price_lo) * 0.12)  # match render y-axis padding
                book_heatmap = build_ob_heatmap(
                    books, candles,
                    price_lo - pad, price_hi + pad,
                    args.ob_price_bin,
                    interval_minutes=args.target_minutes,
                )
                if book_heatmap is not None and book_heatmap[1] is not None:
                    pb, n_buckets = book_heatmap[1].shape[0], book_heatmap[2].shape[0]
                    print(f"  OB heatmap: {pb} price bins x {n_buckets} buckets ({args.target_minutes}min interval)")
    else:
        print("  book file not found")

    print("Loading OI data...")
    oi_path = data_dir / "live_oi.jsonl"
    oi_data = None
    if oi_path.exists():
        oi_df = load_oi(oi_path, args.hours, cutoff=start_time)
        if not oi_df.empty:
            oi_df = oi_df[oi_df["ts"] >= start_time]
            oi_data = resample_oi_to_candles(oi_df, candles, args.target_minutes)
            if oi_data is not None:
                print(f"  OI points: {len(oi_df)}, aligned to {len(oi_data)} candles")
            else:
                print("  OI aligned to 0 candles")
    else:
        print("  OI file not found")

    print("Rendering chart...")
    render_footprint_chart(
        candles, footprint, ob_data,
        book_heatmap=book_heatmap,
        symbol=args.symbol,
        title=args.title,
        out_path=args.out,
        interval_label=f"{args.target_minutes}m",
        display_candles=args.candles,
        show_ob_heatmap=not args.no_ob_heatmap,
        price_bin=args.price_bin,
        ob_price_bin=args.ob_price_bin,
        oi_data=oi_data,
    )
    print("Done.")


if __name__ == "__main__":
    main()
