"""Create a Lab 3 cost snapshot; adapted from lab/lab2/scripts/cost_report.py.

Reads billing only. Does not deploy, tear down, or change Task 5 estimates.
The default end date stays on the teardown day, even when refreshed later.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

from cloudlayer.azure_cost import (
    API_VERSION, DEFAULT_END, START, fetch_pages, resource_scope, reviewed_resources, window_end,
)
from src import config


def render_report(cfg: config.Config, pages: list[dict], end: date, checked_at: datetime) -> str:
    cutoff = window_end(end, checked_at)
    resources = reviewed_resources(cfg)
    if (not isinstance(pages, list) or not pages
            or not all(isinstance(page, dict) for page in pages)
            or pages[-1].get("nextLink")):
        raise ValueError("Missing or incomplete cost pages")
    totals: dict[str, Decimal] = {}
    seen = set()
    for page in pages:
        columns, rows = page.get("columns"), page.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValueError("Missing cost columns or rows")
        names = [column.get("name") if isinstance(column, dict) else None for column in columns]
        required = {"PreTaxCost", "UsageDate", "ResourceId", "Currency"}
        if (not all(isinstance(name, str) for name in names) or len(names) != len(set(names))
                or not required.issubset(names)):
            raise ValueError("Missing or duplicate cost columns")
        for row in rows:
            if not isinstance(row, list) or len(row) != len(names):
                raise ValueError("Invalid cost row length")
            record = dict(zip(names, row))
            if record["Currency"] != "USD":
                raise ValueError("Expected USD cost rows; do not mix or guess currencies")
            usage_date = datetime.strptime(str(record["UsageDate"]), "%Y%m%d").date()
            if not START <= usage_date <= end:
                raise ValueError("Cost row is outside the requested date range")
            resource_id = str(record["ResourceId"]).lower()
            if resource_id not in resources:
                raise ValueError(f"Unreviewed billed resource; check attribution: {resource_id}")
            key = (usage_date, resource_id)
            if key in seen:
                raise ValueError("Duplicate daily resource row; refusing to double-count")
            seen.add(key)
            try:
                amount = Decimal(str(record["PreTaxCost"]))
            except InvalidOperation:
                raise ValueError("Invalid cost amount") from None
            if not amount.is_finite():
                raise ValueError("Non-finite cost amount")
            totals[resource_id] = totals.get(resource_id, Decimal(0)) + amount
    if not totals:
        raise ValueError("No cost rows reported; this is not evidence of zero spend")

    lines = [
        "# Lab 3 - Cost report", "",
        f"Retrieved: {checked_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Usage window: {START} to {end} (UTC).",
        f"Query cutoff: {cutoff}.",
        f"Scope: `{resource_scope(cfg)}`.",
        f"Source: Azure Cost Management `{API_VERSION}`, `ActualCost`, `PreTaxCost`;",
        f"daily rows grouped by `ResourceId`; all {len(pages)} response page(s) read.", "",
        "| Resource | Scope | Reported USD |", "|---|---|---:|",
    ]
    for rid, (label, group) in resources.items():
        amount = (format(totals[rid].normalize(), "f") if rid in totals
                  else "No cost rows reported")
        lines.append(f"| {label} | {group} | {amount} |")
    lines.append("")
    for group, label in (("Lab 3", "Lab 3 resources"), ("Shared", "Shared resources")):
        amounts = [amount for rid, amount in totals.items() if resources[rid][1] == group]
        subtotal = (f"USD {sum(amounts, Decimal(0)).normalize():f}" if amounts
                    else "No cost rows reported; not assumed zero")
        lines.append(f"{label} reported subtotal: **{subtotal}**.")
    total = sum(totals.values(), Decimal(0))
    lines += [
        f"All reported rows in this resource group: **USD {total.normalize():f}**.", "",
        "## Limits", "",
        "- Shared-resource costs are shown separately, not fully assigned to Lab 3.",
        "  The combined amount is not a measured Lab 3-only cost.",
        "- No cost rows reported means unknown, not zero. A reported zero only describes",
        "  the returned rows; it does not prove that a service will always be free.",
        "- These are reported pre-tax costs, not remaining student credit or a final bill.",
        "  Billing rows can arrive late. The current UTC day can also be incomplete.",
        "- We query whole UTC dates, including time before Lab 3 resources were created",
        "  and shared-service usage after teardown. Refresh using the same end date.",
        "  Retained shared services can continue to cost money outside this window.",
        "- Task 5 retail estimates are not added to these amounts. There is no THB conversion.",
        "- Unknown resource IDs stop the report for review; they are not silently excluded.", "",
        "Source: [Azure cost-data scope and timing](https://learn.microsoft.com/en-us/azure/"
        "cost-management-billing/costs/understand-cost-mgt-data).", "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", default="", help=f"Last UTC usage date; default {DEFAULT_END}")
    args = parser.parse_args()
    checked_at = datetime.now(timezone.utc)
    try:
        end = date.fromisoformat(args.end) if args.end else DEFAULT_END
        window_end(end, checked_at)
        cfg = config.load(strict=False)
        pages = fetch_pages(cfg, end, checked_at)
        content = render_report(cfg, pages, end, checked_at)
        output = cfg.reports_dir / "lab3-cost.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".cost-report-", dir=output.parent) as temporary:
            staged = Path(temporary) / output.name
            staged.write_text(content, encoding="utf-8")
            staged.replace(output)
        print(f"Wrote {output}; reported rows only, not a final Lab 3 bill.")
        return 0
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        print(f"Cost report failed: {exc}. Previous report left unchanged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
