"""Bulk SEC Form 4 ingest for the insider-following event study.

For each company in the universe (S&P 500 by default), query EDGAR's full-text
search for its Form 4 filings over the window, fetch + parse each filing's
ownership XML, and collect open-market common-stock transactions into a parquet
the event-study reads. Resumable: skips accessions already in the output.

PIT-correct: only `filing_date` is recorded as the disclosure anchor; the
event-study joins on it, never on transaction_date.

Run on deployment-host (EDGAR + the company CIKs are there):
  python3 /tmp/form4_fetch.py --years 3 --out /tmp/form4_trades.parquet

Free, ~10 req/s. A 100-name subset over 3 years is ~10-20 min; the full 500
is ~1-2 hours. Paginate EFTS by `from` (100/page); `size` is ignored.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.etree.ElementTree import ParseError

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from form4 import parse_ownership_xml

UA = "omni-analyst mail2@tylersinbox.com"
EFTS = "https://efts.sec.gov/LATEST/search-index"


def _database_url() -> str:
    try:
        from omni.config import settings
        if settings.database_url:
            return settings.database_url
    except ImportError:
        pass
    return "postgresql://postgres:postgres@localhost:5434/omni_v2"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for attempt in range(3):
        try:
            resp = urllib.request.urlopen(req, timeout=25)
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"429 persisted for {url}")


def list_form4_for_cik(cik: str, start: str, end: str) -> list[dict]:
    """Page EFTS for one issuer's Form 4s. Returns [{accession, file_date, primary, path_cik}]."""
    out, frm = [], 0
    while True:
        url = f"{EFTS}?q=&forms=4&startdt={start}&enddt={end}&ciks={cik.zfill(10)}&from={frm}"
        data = json.loads(_get(url))
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break
        for h in hits:
            src = h["_source"]
            accession = src.get("adsh") or h["_id"].split(":")[0]
            primary = h["_id"].split(":", 1)[1] if ":" in h["_id"] else ""
            out.append({
                "accession": accession,
                "file_date": src.get("file_date", ""),
                "primary": primary,
                "path_cik": accession.split("-")[0].lstrip("0") or "0",
            })
        if len(hits) < 100:
            break
        frm += 100
    return out


def fetch_and_parse(item: dict) -> list[dict]:
    acc_nodash = item["accession"].replace("-", "")
    xml_url = f"https://www.sec.gov/Archives/edgar/data/{item['path_cik']}/{acc_nodash}/{item['primary']}"
    try:
        xml = _get(xml_url)
    except (urllib.error.URLError, OSError, ValueError, ParseError):
        return []
    trades = parse_ownership_xml(xml, filing_date=item["file_date"])
    return [t.__dict__ for t in trades]


async def load_universe(limit: int | None) -> dict[str, str]:
    """ticker -> cik, from the prod DB's seeded S&P 500."""
    import asyncpg

    conn = await asyncpg.connect(_database_url())
    try:
        rows = await conn.fetch(
            "SELECT symbol, identifiers->>'cik' AS cik FROM entity "
            "WHERE kind='company' AND identifiers ? 'cik' ORDER BY symbol"
            + (f" LIMIT {int(limit)}" if limit else "")
        )
        return {r["symbol"]: r["cik"] for r in rows}
    finally:
        await conn.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--out", default="/tmp/form4_trades.parquet")
    p.add_argument("--limit", type=int, default=None, help="cap universe size (for a fast first run)")
    args = p.parse_args()

    import asyncio
    from datetime import UTC, datetime, timedelta

    end = datetime.now(UTC).strftime("%Y-%m-%d")
    start = (datetime.now(UTC) - timedelta(days=365 * args.years)).strftime("%Y-%m-%d")
    universe = asyncio.run(load_universe(args.limit))
    print(f"universe: {len(universe)} companies, window {start}..{end}", flush=True)

    out_path = Path(args.out)
    jsonl_path = out_path.with_suffix(".jsonl")
    done_accessions: set[str] = set()
    if jsonl_path.exists():
        done_accessions = set(pd.read_json(jsonl_path, lines=True)["accession"].unique())
        print(f"resuming: {len(done_accessions)} accessions already fetched", flush=True)

    all_rows: list[dict] = []
    for i, (ticker, cik) in enumerate(universe.items(), 1):
        try:
            items = list_form4_for_cik(cik, start, end)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"  [{i}/{len(universe)}] {ticker}: EFTS error {exc}", flush=True)
            continue
        new = [it for it in items if it["accession"] not in done_accessions]
        if not new:
            continue
        for it in new:
            for row in fetch_and_parse(it):
                row["accession"] = it["accession"]
                all_rows.append(row)
            done_accessions.add(it["accession"])
            time.sleep(0.08)  # ~12 req/s ceiling
        print(f"  [{i}/{len(universe)}] {ticker}: {len(new)} filings, {len(all_rows)} trades total", flush=True)
        # checkpoint every 25 companies so a kill loses little
        if i % 25 == 0 and all_rows:
            _save(all_rows, out_path)

    _save(all_rows, out_path)
    print(f"done. {len(all_rows)} trades -> {out_path}", flush=True)
    return 0


def _save(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    df = pd.DataFrame(rows)
    # JSONL, not parquet: the runtime container has pandas but no pyarrow, and
    # JSONL needs no extra engine. One trade per line, easy to read back.
    df.to_json(path.with_suffix(".jsonl"), orient="records", lines=True)


if __name__ == "__main__":
    raise SystemExit(main())
