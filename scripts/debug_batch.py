"""Batch debug runner: run debug_one_question.py for multiple QIDs with concurrency.

Usage:
  PYTHONPATH=. uv run python scripts/debug_batch.py \
    --batch /home/manjaro/tmp/debug_batch_30.json \
    --concurrency 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

DEFAULT_TMP = "/home/manjaro/tmp"
DEFAULT_DATA = "longmemeval_oracle.json"
DEFAULT_DATA_DIR = "/home/manjaro/AI/LongMemEval/data"


async def run_one(qid: str, data_path: str, out_dir: str, sem: asyncio.Semaphore,
                  done: dict, total: int, start_time: float) -> None:
    out_path = str(Path(out_dir) / f"debug_{qid}.json")
    if Path(out_path).exists():
        done[qid] = "skipped_exists"
        elapsed = time.time() - start_time
        completed = len(done)
        print(f"[{completed}/{total}] {qid} SKIPPED (already exists) [{elapsed:.0f}s]",
              file=sys.stderr, flush=True)
        return

    async with sem:
        cmd = [
            sys.executable, "scripts/debug_one_question.py",
            "--qid", qid,
            "--data", data_path,
            "--out", out_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except asyncio.TimeoutError:
            proc.kill()
            done[qid] = "timeout"
            elapsed = time.time() - start_time
            completed = len(done)
            print(f"[{completed}/{total}] {qid} TIMEOUT [{elapsed:.0f}s]",
                  file=sys.stderr, flush=True)
            return

        if proc.returncode == 0:
            done[qid] = "ok"
        else:
            done[qid] = f"error_rc{proc.returncode}"

        elapsed = time.time() - start_time
        completed = len(done)
        status = "OK" if proc.returncode == 0 else f"ERR(rc={proc.returncode})"
        print(f"[{completed}/{total}] {qid} {status} [{elapsed:.0f}s]",
              file=sys.stderr, flush=True)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True, help="JSON file with type->qid list")
    parser.add_argument("--data", default=DEFAULT_DATA)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", default=DEFAULT_TMP)
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args()

    with open(args.batch) as f:
        batch = json.load(f)

    # Resolve data path
    data_path = args.data
    if not Path(data_path).is_absolute():
        candidate = Path(args.data_dir) / data_path
        if candidate.exists():
            data_path = str(candidate)

    all_qids = []
    for qt, qids in batch.items():
        all_qids.extend(qids)

    total = len(all_qids)
    sem = asyncio.Semaphore(args.concurrency)
    done: dict[str, str] = {}
    start_time = time.time()

    print(f"Batch debug: {total} questions, concurrency={args.concurrency}", file=sys.stderr, flush=True)

    tasks = [run_one(qid, data_path, args.out_dir, sem, done, total, start_time)
             for qid in all_qids]
    await asyncio.gather(*tasks)

    ok = sum(1 for v in done.values() if v == "ok")
    skipped = sum(1 for v in done.values() if "skipped" in v)
    errors = total - ok - skipped
    elapsed = time.time() - start_time
    print(f"\nDone: {ok} ok, {skipped} skipped, {errors} errors, {elapsed:.0f}s total",
          file=sys.stderr, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
