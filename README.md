# BTC Footprint Chart

Bid=green / ask=red footprint heatmap chart from BTC orderbook + trade data.

## Design

- **Bid-dominant** price bins → green bars (`#22c55e`)
- **Ask-dominant** price bins → red bars (`#ef4444`)
- **Alpha** = volume intensity (max volume → fully opaque)
- Each price bin shows **one colour only** (dominant side)
- Index-based X-axis, footprint bars right-of-candle
- Right panel: orderbook depth (bid=green left / ask=red right from mid)

## Data sources

Requires btc-receiver live data files:
- `data/live/live_trades_compact.jsonl`
- `data/live/live_features_1s.jsonl`
- `data/live/live_book_bucketed.jsonl`

## Usage

```bash
# Generate footprint chart
python3 gen_footprint.py \
  --data-dir data/live \
  --out artifacts/footprint_chart.png \
  --hours 3 \
  --target-minutes 15 \
  --price-bin 10
```

## Arguments

| Argument | Default | Description |
|---|---|---|
| `--data-dir` | required | Path to btc-receiver `data/live/` |
| `--out` | required | Output PNG path |
| `--hours` | 3 | Data window in hours |
| `--target-minutes` | 15 | Candle interval in minutes |
| `--price-bin` | 10 | Price bucket size in USD |
| `--symbol` | BTCUSDT | Trading pair symbol |

## Layout

```
+----------------------------+------------------+
| Candles + Footprint bars   | Orderbook Depth  |
| (bid=green / ask=red per   | (bid left, ask   |
|  price bin, alpha = depth) |  right from mid) |
+----------------------------+------------------+
| Volume bars                |                  |
+----------------------------+------------------+
```

## Colour palette

- Background: `#0b1628` navy
- Text: `#ecf3fe`
- Grid: `#2d4a6a`
- Bid volume: `#22c55e` green
- Ask volume: `#ef4444` red
- Candle up: `#4ade80`
- Candle down: `#f43f5e`

## Integration with btc-orderheatmap

The `run_orderflow_once.sh` script in `btc-orderheatmap` generates:
1. Orderheatmap (left)
2. Footprint chart via `gen_footprint.py` (right)
3. Stitched composite image

When splitting into separate repos, update the pipeline to:
1. Generate orderheatmap in `btc-orderheatmap`
2. Generate footprint via this repo
3. Stitch externally
