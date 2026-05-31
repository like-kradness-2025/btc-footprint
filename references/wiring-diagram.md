# gen_footprint.py 配線図

> 生成日: 2026-05-31 (修正版)
> 対象ファイル: `/home/weed420/btc-footprint/gen_footprint.py`
> レビュー結果: **98/100 PASS**

---

## 1. データフロー図

```mermaid
flowchart TD
    %% CLI Args
    CLI["CLI args<br/>--data-dir, --out, --hours, --target-minutes<br/>--price-bin, --ob-price-bin, --candles<br/>--symbol, --title, --no-ob-heatmap"]

    %% Data Sources
    subgraph Sources["Data Sources (JSONL)"]
        TC["live_trades_compact.jsonl<br/>ts, price_bucket, qty_sum, side"]
        RT["live_trades.jsonl (raw)<br/>ts, price, qty, side"]
        BB["live_book_bucketed.jsonl<br/>ts, mid, bids_bucketed, asks_bucketed"]
        FE["live_features_1s.jsonl<br/>ts, mid"]
        OI["live_oi.jsonl<br/>ts, oi"]
    end

    %% Loading Phase (cutoff同期)
    subgraph Load["Loading Phase (cutoff同期)"]
        direction TB
        L1["_load_jsonl_tail(path, hours, byte_count, cutoff=None)<br/>tail -c → JSON parse<br/>→ pd.to_datetime(utc=True) vectorized<br/>→ cutoff or now-hours でフィルタ"]
        L2["load_trades()<br/>→ 3形式バリデーション<br/>(A: price/qty/side, B: melt buy/sell, C: price_bucket/qty_sum/side)"]
        L3["load_book_bucketed(cutoff=)<br/>→ mid check → list行除去"]
        L4["load_features(cutoff=)<br/>→ mid → to_numeric + dropna"]
        L5["load_oi(cutoff=)<br/>→ oi → to_numeric + dropna"]
    end

    %% Processing Phase
    subgraph Process["Processing Phase"]
        direction TB
        P1["build_footprint()<br/>→ _rebucket_trades()<br/>→ floor interval<br/>→ pivot buy/sell/total/delta<br/>※ 形式Bはmelt後に同一ロジック"]
        P2["build_candles()<br/>→ floor freq → groupBy agg OHLC"]
        P3["build_orderbook_depth()<br/>→ sort_values(ts).iloc[-1]<br/>→ sorted bids/asks"]
        P4["build_ob_heatmap()<br/>→ dt.floor(freq) + candle_ts_map 辞書参照<br/>→ sum depth per candle bin<br/>→ mask high-low range"]
        P5["resample_oi_to_candles()<br/>→ floor interval → groupBy last<br/>→ merge + ffill"]
    end

    %% Render Phase
    subgraph Render["Rendering Phase"]
        direction TB
        R1["render_footprint_chart()"]
        R1 --> R2["GridSpec 2×1 + 右余白 add_axes layout"]
        R2 --> R3["OB heatmap pcolormesh<br/>X,Y = meshgrid → bid_norm.T / ask_norm.T"]
        R2 --> R4["Candles vlines + bar"]
        R2 --> R5["Footprint bars<br/>candle_ts_to_idx 辞書参照 O(1)"]
        R2 --> R6["OB side panel barh<br/>右余白・左側"]
        R2 --> R7["Vol Profile barh<br/>右余白・右側・Yラベル非表示"]
        R2 --> R9["Volume bars + OI/CVD twinx<br/>右側3軸"]
        R3 --> R8
        R4 --> R8
        R5 --> R8
        R6 --> R8
        R7 --> R8
        R9 --> R8
        R8["savefig → PNG"]
    end

    %% Main flow: cutoff同期
    CLI -->|args| M["main()"]
    M -->|trades load| L1
    L1 --> L2
    M -->|"end_time → start_time"| L3
    M -->|"cutoff=start_time"| L4
    M -->|"cutoff=start_time"| L5

    TC --> L2
    RT -->|"price_bin != 10"| L2
    BB --> L3
    FE --> L4
    OI --> L5

    L2 --> P1
    L4 --> P2
    L3 --> P3
    L3 --> P4
    L5 --> P5

    P1 --> R5
    P2 --> R4
    P3 --> R6
    P4 --> R3
    P1 --> R7
    P5 --> R9

    R8 --> PNG["footprint_chart.png"]
```

## 2. 関数入出力テーブル

| 関数 | 入力（主要カラム） | 出力 | キー結合カラム |
|------|-------------------|------|--------------|
| `_load_jsonl_tail` | JSONL path, hours, byte_count, cutoff | DataFrame (生JSON列 + ts変換済) | `ts` (pd.Timestamp UTC) |
| `load_trades` | JSONL path, hours, byte_count | DataFrame validated (3形式対応) | `ts` |
| `load_book_bucketed` | JSONL path, hours, byte_count, cutoff | DataFrame (list行除去済) | `ts` |
| `load_features` | JSONL path, hours, cutoff | DataFrame (numeric mid, dropna) | `ts` |
| `load_oi` | JSONL path, hours, cutoff | DataFrame (numeric oi, dropna) | `ts` |
| `build_candles` | features: `ts, mid` | candles: `ts, open, high, low, close` | `ts` (floor freq) |
| `_rebucket_trades` | trades: `ts, price, side` or `price_bucket` | trades: `+price_bucket` | — |
| `build_footprint` | trades: `ts, price_bucket, side, qty/qty_sum` | pivot: `interval, price_bucket, buy, sell, total, delta` | `interval` (= ts floor) |
| `build_orderbook_depth` | book_df: `ts, mid, bids_bucketed, asks_bucketed` | dict: `{mid, bids[(p,q)], asks[(p,q)], ts}` | sort_values(ts).iloc[-1] |
| `build_ob_heatmap` | book_df, candles, price_lo/hi, price_bin | `(x_positions, price_bins, bid_hm, ask_hm)` | dt.floor(freq) → candle_ts_map 辞書参照 |
| `resample_oi_to_candles` | oi_df, candles, interval_minutes | Series (oi per candle 0..n-1) | `ts` merge left |
| `render_footprint_chart` | candles, footprint, ob_data, book_heatmap, ... | (Figure, Axes) → PNG | candle_ts_to_idx 辞書 |

## 3. 座標系

| 要素 | X軸 | Y軸 | 備考 |
|------|-----|-----|------|
| キャンドル | `ci + CANDLE_X_OFFSET(-0.3)` | 価格 (BTC/USD) | ci = 0..n-1 |
| OB heatmap | `np.linspace(-0.5, n-0.5, n+1)` | 価格bins y_edges | pcolormesh cell-centered |
| Footprint bar | `right_origin = ci + (-0.3) + 0.19/2 + GAP` | `pb ± price_bin/2` | 辞書参照O(1) |
| OB panel | 正規化depth [-4.0, 4.0] | 価格 (mainと同一) | 右余白 `add_axes((0.865, 0.26, 0.055, 0.69))` |
| Vol Profile panel | 正規化出来高 [0, 4.0] | 価格 (mainと同一) | 右余白 `add_axes((0.925, 0.26, 0.055, 0.69))`、Yラベル非表示 |
| Volume | `ci + CANDLE_X_OFFSET` | 出来高 | 辞書参照O(1) |
| OI | `np.arange(n) + CANDLE_X_OFFSET` | OI値 (動的range) | twinx |
| CVD | `np.arange(n) + CANDLE_X_OFFSET` | 累積 `buy-sell` | twinx (右側第3軸) |

## 4. 時間範囲の揃え方

```
main() フロー (修正後):
  1. tradesをload → end_time = trades["ts"].max(), start_time = end_time - hours
  2. tradesを [start_time, end_time] でフィルタ
  3. features, book, OI を _load_jsonl_tail(cutoff=start_time) でロード
     → 全データソースが同じ時間窓 [start_time, ∞) で揃う
  4. 各データソースでさらに [start_time, end_time] でpost-filter
  5. candles = candles.tail(args.candles).reset_index(drop=True)
  6. OB heatmap → dt.floor(freq) + candle_ts_map 辞書参照
  7. footprint マッチング → candle_ts_to_idx 辞書参照 (O(1))
```

## 5. 描画レイヤー次元

| レイヤー | X | Y | C | 関数 | zorder |
|---------|---|---|---|------|--------|
| OB heatmap bid | `(n_bins+1, n_buckets+1)` meshgrid | 同上 | `bid_norm: (n_bins, n_buckets)` T転置 | pcolormesh | 0 |
| OB heatmap ask | 同上 | 同上 | `ask_norm: (n_bins, n_buckets)` T転置 | pcolormesh | 0 |
| キャンドル wicks | 1D: `xi * n` | `[l, h] * n` | — | vlines | 5 |
| キャンドル body | 1D: `xi * n` | `abs(c-o) * n` | — | bar | 5 |
| Footprint eq | 1D: start/end | 1D: `[y0, y1]` | — | fill_betweenx | — |
| Footprint delta | 1D: start/end | 1D: `[y0, y1]` | — | fill_betweenx | — |
| OB panel | 正規化depth | 価格 | — | barh | — |
| Vol Profile eq/delta | 正規化出来高 | 価格 | `min(buy,sell)` + `abs(buy-sell)` | barh | — |
| Volume/OI/CVD | candle index | 各軸値 | — | bar/plot | — |

## 6. 型の一貫性

| データ | 型 | tz | 備考 |
|--------|-----|------|------|
| `_load_jsonl_tail` df["ts"] | `pd.Timestamp` | UTC (tz-aware) | pd.to_datetime(utc=True) |
| `_to_ts()` | `pd.Timestamp` | UTC (tz-aware) | 統一変換 |
| `candles["ts"]` | `pd.Timestamp` | UTC (tz-aware) | dt.floor継承 |
| `footprint["interval"]` | `pd.Timestamp` | UTC (tz-aware) | dt.floor継承 |
| `candle_ts_map` | key: `pd.Timestamp` UTC → value: `int` | — | floor ベースの完全一致 |
| `candle_ts_to_idx` | key: `pd.Timestamp` UTC → value: `int` | — | O(1) 参照 |

## 7. 修正履歴

| ID | 優先度 | 内容 | 状態 |
|----|--------|------|------|
| P0-1 | 致命的 | 形式B (price_bucket/buy/sell) melt変換ロジック追加 | ✅ |
| P0-2 | 致命的 | _load_jsonl_tail cutoff引数追加、main()で時間窓同期 | ✅ |
| P1-1 | 重要 | OB heatmap nearest→floor+辞書参照に変更 | ✅ |
| P1-2 | 重要 | tail(1)→sort_values("ts").iloc[-1] に修正 | ✅ |
| P2 | 軽微 | footprint/volume candle lookup O(n²)→O(1) 辞書参照 | ✅ |
| L1 | 表示 | OB/VPを右余白上部に左右独立配置、VP縦軸ラベル非表示 | ✅ |

## 8. 残留懸念

- **build_ob_heatmap マッチング**: dt.floor(freq)の完全一致依存。candle側とbook側が同じfreqでfloorされていれば問題なし。
- **形式Bの将来拡張**: qty_sum付き混在形式はテストされていない。実データでは現れなかった形式。