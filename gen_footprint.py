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

import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter
from matplotlib.colors import LinearSegmentedColormap

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
DEFAULT_CANDLES = 13          # display count (13 = 3h15m for 15min candles)
CANDLE_WIDTH = 0.19
GAP = 0.05
FP_WIDTH = 0.75               # max footprint bar width in index units
MIN_ALPHA = 0.08
MAX_ALPHA = 1.0

JST = None
try:
    import pytz
    JST = pytz.timezone("Asia/Tokyo")
except ImportError:
    pass


# ── Data loading ────────────────────────────────────────────────────────────

def load_trades_compact(path: Path) -> pd.DataFrame:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def load_book_bucketed(path: Path) -> pd.DataFrame:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if isinstance(d, list):
                continue
            rows.append(d)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def load_features(path: Path) -> pd.DataFrame:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rows.append({"ts": d["ts"], "mid": d.get("mid")})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["mid"] = pd.to_numeric(df["mid"], errors="coerce")
    return df.dropna(subset=["mid"])


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


def build_footprint(
    trades: pd.DataFrame,
    interval_minutes: int,
    price_bin: int,
) -> tuple[pd.DataFrame, dict]:
    if trades.empty:
        return pd.DataFrame(), {}

    freq = f"{interval_minutes}min"
    trades["interval"] = trades["ts"].dt.floor(freq)

    agg = (
        trades.groupby(["interval", "price_bucket", "side"])["qty_sum"]
        .sum()
        .reset_index()
    )

    pivot = agg.pivot_table(
        index=["interval", "price_bucket"],
        columns="side",
        values="qty_sum",
        aggfunc="sum",
    ).fillna(0)

    for col in ["buy", "sell"]:
        if col not in pivot.columns:
            pivot[col] = 0.0

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
        subset = book_df.tail(1)

    latest = subset.sort_values("ts").iloc[-1]
    mid = float(latest.get("mid", 0))
    bids_raw = latest.get("bids_bucketed", {})
    asks_raw = latest.get("asks_bucketed", {})

    bids = sorted(
        [(float(k), float(v)) for k, v in bids_raw.items() if float(v) > 0],
        key=lambda x: x[0], reverse=True,
    )[:max_levels]
    asks = sorted(
        [(float(k), float(v)) for k, v in asks_raw.items() if float(v) > 0],
        key=lambda x: x[0],
    )[:max_levels]

    return {"mid": mid, "bids": bids, "asks": asks, "ts": latest["ts"]}


def build_ob_heatmap(
    book_df: pd.DataFrame,
    candles: pd.DataFrame,
    price_lo: float,
    price_hi: float,
    price_bin: float,
) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    """Build per-candle orderbook heatmap arrays for pcolormesh rendering.

    Returns (price_bins, bid_heatmap, ask_heatmap) where:
      - price_bins: 1D array of price bin centers
      - bid_heatmap: (n, n_bins) array of normalized bid depth [0..1]
      - ask_heatmap: (n, n_bins) array of normalized ask depth [0..1]
    Returns (None, None, None) on failure.
    """
    if book_df.empty or candles.empty:
        return None, None, None

    n = len(candles)
    price_bins = np.arange(
        (price_lo // price_bin) * price_bin,
        (price_hi // price_bin) * price_bin + price_bin,
        price_bin
    )
    if len(price_bins) == 0:
        return None, None, None

    bid_hm = np.full((n, len(price_bins)), np.nan)
    ask_hm = np.full((n, len(price_bins)), np.nan)

    candle_times = candles["ts"].values.astype("datetime64[us]")

    for _, brow in book_df.iterrows():
        bt = brow["ts"].to_datetime64() if hasattr(brow["ts"], "to_datetime64") else np.datetime64(brow["ts"], "us")
        diffs = np.abs(candle_times - bt)
        ci = diffs.argmin()
        min_diff_sec = diffs.min().astype("timedelta64[s]").astype(int)
        if min_diff_sec > 300:
            continue  # too far from any candle

        for p_str, qty in brow.get("bids_bucketed", {}).items():
            p = float(p_str)
            bi = int((p - price_bins[0]) / price_bin)
            if 0 <= bi < len(price_bins):
                if np.isnan(bid_hm[ci, bi]):
                    bid_hm[ci, bi] = 0
                bid_hm[ci, bi] += float(qty)

        for p_str, qty in brow.get("asks_bucketed", {}).items():
            p = float(p_str)
            bi = int((p - price_bins[0]) / price_bin)
            if 0 <= bi < len(price_bins):
                if np.isnan(ask_hm[ci, bi]):
                    ask_hm[ci, bi] = 0
                ask_hm[ci, bi] += float(qty)

    return price_bins, bid_hm, ask_hm


# ── Rendering ───────────────────────────────────────────────────────────────

def _fmt(x):
    if abs(x) >= 1e6:
        return f"{x / 1e6:.1f}M"
    if abs(x) >= 1e3:
        return f"{x / 1e3:.1f}k"
    return f"{x:.1f}"


def render_footprint_chart(
    candles: pd.DataFrame,
    footprint: pd.DataFrame,
    ob_data: dict | None,
    book_heatmap: tuple | None = None,
    symbol: str = "BTCUSDT",
    title: str = "BTC Footprint + Orderbook",
    out_path: str | Path | None = None,
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

    n = min(len(candles), DEFAULT_CANDLES)
    candles = candles.tail(n).reset_index(drop=True)
    x_idx = np.arange(n)  # 0, 1, 2, ..., n-1

    # Price range
    price_min = candles["low"].min()
    price_max = candles["high"].max()
    pad = max(50, (price_max - price_min) * 0.12)
    price_lo = price_min - pad
    price_hi = price_max + pad

    # X-axis extents
    candle_left = -CANDLE_WIDTH / 2
    candle_right = (n - 1) + CANDLE_WIDTH / 2
    margin = 0.5
    xlim_l = candle_left - margin
    fp_right = (n - 1) + CANDLE_WIDTH / 2 + GAP + FP_WIDTH
    xlim_r = max(candle_right + margin, fp_right + 0.15)

    # ── Layout ──
    fig = plt.figure(figsize=(12, 11), dpi=180)
    fig.patch.set_facecolor(BG)

    gs = fig.add_gridspec(
        2, 2, height_ratios=[4, 1], width_ratios=[8, 1],
        hspace=0.05, wspace=0.02,
        left=0.03, right=0.97, bottom=0.07, top=0.95,
    )
    ax_main = fig.add_subplot(gs[0, 0])
    ax_ob = fig.add_subplot(gs[0, 1])
    ax_vol = fig.add_subplot(gs[1, 0])

    # ── Main chart axes ──
    ax_main.set_facecolor(NAVY)
    ax_main.set_ylim(price_lo, price_hi)
    ax_main.set_xlim(xlim_l, xlim_r)
    ax_main.set_xticks([])
    ax_main.yaxis.tick_right()
    ax_main.yaxis.set_label_position("right")
    ax_main.tick_params(axis="y", colors=TEXT, labelsize=7)
    ax_main.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:,.0f}"))
    ax_main.grid(True, axis="both", linestyle=":", alpha=0.10, color=GRID)
    for s in ax_main.spines.values():
        s.set_color(GRID)
        s.set_alpha(0.3)

    # ├─ Orderbook heatmap background (per-candle) ──
    if book_heatmap is not None:
        price_bins_hm, bid_hm, ask_hm = book_heatmap
        if bid_hm is not None and ask_hm is not None:
            hm_bid_max = max(np.nanmax(bid_hm), 1.0)
            hm_ask_max = max(np.nanmax(ask_hm), 1.0)
            bid_norm = bid_hm.T / hm_bid_max
            ask_norm = ask_hm.T / hm_ask_max

            cmap_bid = LinearSegmentedColormap.from_list(
                'bid_hm', [(0, 0, 0, 0), (0.15, 0.65, 0.15, 1)], N=256)
            cmap_bid.set_bad(alpha=0)
            cmap_ask = LinearSegmentedColormap.from_list(
                'ask_hm', [(0, 0, 0, 0), (0.65, 0.15, 0.15, 1)], N=256)
            cmap_ask.set_bad(alpha=0)

            X, Y = np.meshgrid(np.arange(n) - 0.5, price_bins_hm)
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
        ax_main.vlines(i, l, h, color=color, linewidth=1.0, alpha=0.85, zorder=5)
        ax_main.bar(i, abs(c - o), CANDLE_WIDTH, bottom=min(o, c),
                    color=color, alpha=0.85, linewidth=0.4, edgecolor=color, zorder=5)

    # ── Footprint heatmap (bid=green / ask=red) ──
    candle_ts_set: set = set()
    fp_plot = pd.DataFrame()
    if not footprint.empty:
        # Match footprint intervals to candles (normalize type)
        candle_ts_set = set(candles["ts"].apply(
            lambda t: pd.Timestamp(t).tz_localize("UTC") if t.tz is None else pd.Timestamp(t)
        ))
        fp_plot = footprint.copy()
        fp_plot["_ts_match"] = fp_plot["interval"].apply(lambda t: pd.Timestamp(t))
        fp_plot = fp_plot[fp_plot["_ts_match"].isin(candle_ts_set)]

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
                match = candles[candles["ts"] == intv]
                if match.empty:
                    continue
                ci = match.index[0]

                # Bar geometry
                right_origin = ci + CANDLE_WIDTH / 2 + GAP
                usable_w = FP_WIDTH * (total_v / global_max)

                if usable_w <= 1e-8:
                    continue

                y0 = pb - DEFAULT_PRICE_BIN / 2
                y1 = pb + DEFAULT_PRICE_BIN / 2

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
        ax_main.text(i, price_hi + (price_hi - price_lo) * 0.02,
                     t.strftime("%H:%M"), color=MUTED, fontsize=6.5,
                     ha="center", va="bottom", alpha=0.85)

    # ── Latest price badge ──
    last = candles.iloc[-1]
    lp = last["close"]
    lc = UP if last["close"] >= last["open"] else DOWN
    ax_main.text(0.02, 0.97, f"{lp:,.0f}", transform=ax_main.transAxes,
                 fontsize=18, fontweight="bold", color=lc, va="top", ha="left",
                 bbox=dict(boxstyle="round,pad=0.2", fc="black", ec=lc, lw=0.8, alpha=0.75),
                 zorder=6)

    # ── Legend ──
    from matplotlib.patches import Patch
    leg = ax_main.legend(
        handles=[
            Patch(color=BID_GREEN, alpha=0.8, label="Bid"),
            Patch(color=ASK_RED, alpha=0.8, label="Ask"),
        ],
        fontsize=7, loc="upper right", framealpha=0.6, labelcolor=TEXT,
        bbox_to_anchor=(0.99, 0.99),
    )
    leg.get_frame().set_facecolor("black")

    # ── Orderbook panel ──
    ax_ob.set_facecolor(NAVY)
    for s in ax_ob.spines.values():
        s.set_color(GRID)
        s.set_alpha(0.3)
    ax_ob.tick_params(colors=MUTED, labelsize=6)
    ax_ob.set_xticks([])
    ax_ob.set_title("Orderbook", color=TEXT, fontsize=8)
    ax_ob.set_ylim(price_lo, price_hi)

    if ob_data and ob_data.get("bids"):
        bids = [(p, q) for p, q in ob_data["bids"] if price_lo <= p <= price_hi]
        asks = [(p, q) for p, q in ob_data["asks"] if price_lo <= p <= price_hi]
        all_q = [q for _, q in bids] + [q for _, q in asks]
        max_q = max(all_q) if all_q else 1.0

        if bids:
            prices, qties = zip(*bids)
            ax_ob.barh(prices, [-4.0 * q / max_q for q in qties],
                       height=DEFAULT_PRICE_BIN * 0.85,
                       color=BID_GREEN, alpha=0.55, align="center", linewidth=0)
        if asks:
            prices, qties = zip(*asks)
            ax_ob.barh(prices, [4.0 * q / max_q for q in qties],
                       height=DEFAULT_PRICE_BIN * 0.85,
                       color=ASK_RED, alpha=0.55, align="center", linewidth=0)
        ax_ob.axvline(0, color=TEXT, linewidth=0.4, alpha=0.3)
        ax_ob.set_xlim(-4.5, 4.5)

        # Depth label
        ax_ob.text(0.5, 0.02, f"depth: {_fmt(max_q)}",
                   transform=ax_ob.transAxes, color=MUTED, fontsize=7,
                   ha="center", va="bottom")
    else:
        ax_ob.set_xlim(-4.5, 4.5)

    # ── Volume ──
    ax_vol.set_facecolor(NAVY)
    for s in ax_vol.spines.values():
        s.set_color(GRID)
        s.set_alpha(0.3)
    ax_vol.tick_params(colors=MUTED, labelsize=6)
    ax_vol.yaxis.tick_right()
    ax_vol.yaxis.set_label_position("right")
    ax_vol.yaxis.set_major_formatter(FuncFormatter(lambda y, _: _fmt(y)))
    ax_vol.grid(True, axis="y", linestyle=":", alpha=0.10, color=GRID)
    ax_vol.set_xlim(xlim_l, xlim_r)
    ax_vol.set_xticks([])

    if not footprint.empty:
        vol_agg = footprint[footprint["interval"].isin(candle_ts_set)].copy()
        vol_agg = vol_agg.groupby("interval")[["buy", "sell"]].sum().reset_index()
        for _, row in vol_agg.iterrows():
            match = candles[candles["ts"] == row["interval"]]
            if match.empty:
                continue
            ci = match.index[0]
            buy = row["buy"]
            sell = row["sell"]
            eq = min(buy, sell)
            delta = abs(buy - sell)
            dc = BID_GREEN if buy >= sell else ASK_RED
            ax_vol.bar(ci, eq, width=CANDLE_WIDTH * 2.0,
                       color=MUTED, alpha=0.50, linewidth=0)
            ax_vol.bar(ci, delta, bottom=eq, width=CANDLE_WIDTH * 2.0,
                       color=dc, alpha=0.50, linewidth=0)

    # X-axis time labels (shared via twinx)
    step = max(1, n // 6)
    tick_positions = []
    tick_labels = []
    for i in range(0, n, step):
        t = candles.iloc[i]["ts"]
        if JST and hasattr(t, "tz_convert"):
            t = t.tz_convert(JST)
        tick_positions.append(i)
        tick_labels.append(t.strftime("%H:%M"))
    if (n - 1) not in tick_positions:
        t = candles.iloc[-1]["ts"]
        if JST and hasattr(t, "tz_convert"):
            t = t.tz_convert(JST)
        tick_positions.append(n - 1)
        tick_labels.append(t.strftime("%H:%M"))

    ax_twin = ax_vol.twiny()
    ax_twin.set_xlim(xlim_l, xlim_r)
    ax_twin.set_xticks(tick_positions)
    ax_twin.set_xticklabels(tick_labels, color=TEXT, fontsize=6.5)
    ax_twin.tick_params(length=2, pad=2)
    for s in ax_twin.spines.values():
        s.set_visible(False)

    # ── Title ──
    start_t = candles.iloc[0]["ts"]
    end_t = candles.iloc[-1]["ts"]
    if JST:
        start_t = start_t.tz_convert(JST)
        end_t = end_t.tz_convert(JST)
    fig.suptitle(
        f"BTC {symbol} Footprint ({n} candles / ${DEFAULT_PRICE_BIN} bins)  "
        f"{start_t.strftime('%H:%M')} – {end_t.strftime('%H:%M')} JST  •  bid=green  ask=red",
        color=TEXT, fontsize=12, y=0.97,
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
                        help="Price bucket size in USD (default: 10)")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--title", default="BTC Footprint + Orderbook")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"ERROR: data-dir not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    trades_path = data_dir / "live_trades_compact.jsonl"
    book_path = data_dir / "live_book_bucketed.jsonl"
    features_path = data_dir / "live_features_1s.jsonl"

    if not trades_path.exists():
        print(f"ERROR: trades file not found: {trades_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading trades from {trades_path}...")
    trades = load_trades_compact(trades_path)
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
        features = load_features(features_path)
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
        candles["open"] = candles["ts"]
        candles["high"] = fp_agg["high"]
        candles["low"] = fp_agg["low"]
        candles["close"] = candles["ts"]
        print(f"  {len(candles)} candles from trade fallback")

    print("Loading orderbook...")
    ob_data = None
    book_heatmap = None
    if book_path.exists():
        books = load_book_bucketed(book_path)
        if not books.empty:
            ob_data = build_orderbook_depth(books, start_time, end_time)
            if ob_data:
                print(f"  OB snapshot at {ob_data['ts']}, {len(ob_data['bids'])} bids, {len(ob_data['asks'])} asks")
            else:
                print("  no OB snapshot in window")
            # Build per-candle heatmap
            if not candles.empty:
                price_lo = candles["low"].min()
                price_hi = candles["high"].max()
                pad = (price_hi - price_lo) * 0.05
                book_heatmap = build_ob_heatmap(
                    books, candles,
                    price_lo - pad, price_hi + pad,
                    args.price_bin,
                )
                if book_heatmap[0] is not None:
                    print(f"  OB heatmap: {book_heatmap[0].shape[0]} price bins x {len(candles)} candles")
    else:
        print("  book file not found")

    print("Rendering chart...")
    render_footprint_chart(
        candles, footprint, ob_data,
        book_heatmap=book_heatmap,
        symbol=args.symbol,
        title=args.title,
        out_path=args.out,
    )
    print("Done.")


if __name__ == "__main__":
    main()
