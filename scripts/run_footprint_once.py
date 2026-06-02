#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

WORKDIR = Path(os.environ.get("WORKDIR", str(Path.home() / "btc-footprint")))
OUT_PATH = Path(os.environ.get("OUT_PATH", str(Path.home() / "footprint_auto.png")))
WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1487440800047693954/Eqkkm8_3uNgGHJJ2ojQGH3jC64SpS3CpVOeOMwYOHKrex6dRuz1I8AxYfRtGKdJ2aLqR",
)
DEFAULT_DATA_DIR = os.environ.get("DATA_DIR", str(Path.home() / "btc-receiver" / "data" / "live"))


def jst_now_str() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S JST")
    except Exception:
        return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def send_webhook(content: str, image_path: Path | None = None) -> None:
    payload = {"content": content}
    files = None
    data = {"payload_json": json.dumps(payload, ensure_ascii=False)}
    try:
        if image_path is not None:
            files = {"file": (image_path.name, image_path.open("rb"), "image/png")}
        resp = requests.post(WEBHOOK_URL, data=data, files=files, timeout=30)
        if resp.status_code >= 300:
            raise RuntimeError(f"webhook HTTP {resp.status_code}: {resp.text[:500]}")
    finally:
        if files and "file" in files:
            files["file"][1].close()


def main(data_dir: str | None = None) -> int:
    data_dir = data_dir or DEFAULT_DATA_DIR
    gen_cmd = [
        "python3",
        "gen_footprint.py",
        "--data-dir",
        data_dir,
        "--out",
        str(OUT_PATH),
        "--hours",
        "3",
        "--target-minutes",
        "15",
        "--price-bin",
        "10",
        "--candles",
        "12",
    ]

    try:
        print(f"[{jst_now_str()}] start: footprint generate+upload data_dir={data_dir} out={OUT_PATH}")
        proc = subprocess.run(
            gen_cmd,
            cwd=str(WORKDIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "gen_footprint.py failed without stderr").strip()
            try:
                send_webhook(
                    f"⚠️ {jst_now_str()} | 15分足 BTCフットプリント\n生成失敗:\n```\n{err[:1800]}\n```"
                )
            except Exception as webhook_exc:
                print(f"Webhook send failed: {webhook_exc}")
            return 1

        if not OUT_PATH.exists():
            msg = f"{OUT_PATH} was not created"
            try:
                send_webhook(f"⚠️ {jst_now_str()} | 15分足 BTCフットプリント\n生成失敗:\n```\n{msg}\n```")
            except Exception as webhook_exc:
                print(f"Webhook send failed: {webhook_exc}")
            return 1

        try:
            send_webhook(f"🕐 {jst_now_str()} | 15分足 BTCフットプリント", OUT_PATH)
        except Exception as webhook_exc:
            print(f"Webhook send failed: {webhook_exc}")
            return 1
        print(f"[{jst_now_str()}] done: footprint generate+upload out={OUT_PATH}")
        return 0
    except Exception as exc:
        try:
            send_webhook(f"⚠️ {jst_now_str()} | 15分足 BTCフットプリント\n例外:\n```\n{exc}\n```")
        except Exception as webhook_exc:
            print(f"Webhook send failed: {webhook_exc}")
        print(f"Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = parser.parse_args()
    raise SystemExit(main(args.data_dir))
