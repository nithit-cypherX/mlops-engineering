"""Read Lab 2's Azure billing rows; never create, update, or delete cloud resources.

The handout asks for make cost-report. This is separate from the instructor's
Lab 5 serving-cost scaffold and from the training estimates in src/costs.py.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from cloudlayer.azure_cost import API_VERSION, START, fetch_pages, resource_scope
from src import config

# Matched Linux F2s v2 primary meter in malaysiawest; checked 2026-09-19.
# Retail meter ID: 6f6004d7-c68d-5437-931c-5046eecae885. Not an invoice FX rate.
THB_PER_USD = Decimal("2.900677") / Decimal("0.0882")


def reviewed_resources(cfg: config.Config) -> dict[str, tuple[str, str]]:
    """Keep the inspected inventory after teardown; stop if new billed resources appear."""
    # Inventory and workspace references checked on 2026-09-19. Shared resources
    # still have lab=1 tags, so filtering only lab=2 would miss their costs.
    entries = [
        (f"Microsoft.MachineLearningServices/workspaces/{cfg.azure_ml_workspace}",
         "Azure ML workspace", "Direct"),
        (f"Microsoft.MachineLearningServices/workspaces/{cfg.azure_ml_workspace}"
         f"/computes/{cfg.azure_ml_compute}", "Azure ML compute", "Direct"),
        ("Microsoft.KeyVault/vaults/mlwitcs3keyvault8263948e", "Key Vault", "Direct"),
        ("Microsoft.OperationalInsights/workspaces/mlwitcs3logalytif5574ef1",
         "Log Analytics", "Direct"),
        ("Microsoft.Insights/components/mlwitcs3insights48346e2c",
         "Application Insights", "Direct"),
        ("Microsoft.ContainerRegistry/registries/itcs355u6688124", "ACR", "Shared"),
        ("Microsoft.Storage/storageAccounts/itcs355u6688124", "Storage", "Shared"),
    ]
    return {f"{resource_scope(cfg)}/providers/{suffix}".lower(): (label, group)
            for suffix, label, group in entries}


def render_report(cfg: config.Config, pages: list[dict], end: date, checked_at: datetime) -> str:
    resources = reviewed_resources(cfg)
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
            # Negative billing adjustments are retained, not silently discarded.
            totals[resource_id] = totals.get(resource_id, Decimal(0)) + amount
    if not totals:
        raise ValueError("No cost rows reported; this is not evidence of zero spend")

    groups = {group: sum((amount for rid, amount in totals.items()
                         if resources[rid][1] == group), Decimal(0))
              for group in ("Direct", "Shared")}
    total = sum(groups.values(), Decimal(0))
    lines = [
        "# Lab 2 - Cost report", "",
        f"Retrieved: {checked_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"Usage window: {START} to {end} (UTC; current day can be incomplete).",
        f"Scope: `{resource_scope(cfg)}`.",
        f"Source: Azure Cost Management `{API_VERSION}`, `ActualCost`, `PreTaxCost`,",
        f"daily rows grouped by `ResourceId`; all {len(pages)} response page(s) read.", "",
        "| Resource | Allocation | Reported USD | THB reference |",
        "|---|---|---:|---:|",
    ]
    for rid, (label, group) in resources.items():
        if rid in totals:
            amount = totals[rid]
            lines.append(f"| {label} | {group} | {amount:.8f} | {amount * THB_PER_USD:.4f} |")
        else:
            lines.append(f"| {label} | {group} | No cost rows reported | Not assumed zero |")
    lines += ["", f"Direct reported subtotal: **USD {groups['Direct']:.8f}**.",
              f"Shared-resource allocation: **USD {groups['Shared']:.8f}**.",
              f"Combined reported amount: **USD {total:.8f} / about THB {total * THB_PER_USD:.2f}**.",
              "Lab budget: **THB 150**. This snapshot is not a final budget pass.", "",
              "## What these numbers mean", "",
              "- The window starts on the workspace creation day, 2026-09-17. We include",
              "  the full day's costs, including hours before its 06:10 UTC creation.",
              "- ACR and Storage also serve Lab 1. We allocate all their reported costs",
              "  in this window to Lab 2 conservatively, not as measured Lab 2-only usage.",
              "- Direct resources use the inventory checked on 2026-09-19. New billed",
              "  resource IDs stop the report for review rather than being silently excluded.",
              "- Costs include failed jobs and idle time where Azure charges for them.",
              "  These rows do not separate those amounts. Trial estimates are not added again.",
              "- USD is the reported pre-tax cost, not the remaining student credit.",
              "  Missing rows do not prove a service is free. Billing data can arrive late;",
              "  refresh after teardown using the same end date. Retained services can",
              "  continue costing money after that cutoff.", "",
              "## THB reference", "",
              f"1 USD = {THB_PER_USD:.8f} THB for budget comparison only, not invoice FX.",
              "Derived from Microsoft's matched Linux Standard_F2s_v2 on-demand prices in",
              "`malaysiawest`: USD 0.0882/hour and THB 2.900677/hour, checked 2026-09-19.",
              "Meter: `6f6004d7-c68d-5437-931c-5046eecae885`; effective date 2025-10-01.",
              "Source: [Azure Retail Prices API](https://prices.azure.com/api/retail/prices).",
              "Microsoft describes non-USD prices as budget references in its",
              "[pricing documentation](https://learn.microsoft.com/en-us/rest/api/"
              "cost-management/retail-prices/azure-retail-prices).", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", default="", help="Last UTC usage date; default today")
    args = parser.parse_args()
    checked_at = datetime.now(timezone.utc)
    try:
        end = date.fromisoformat(args.end) if args.end else checked_at.date()
        if not START <= end <= checked_at.date():
            raise ValueError(f"End date must be between {START} and today (UTC)")
        cfg = config.load(strict=False)
        pages = fetch_pages(cfg, end, checked_at)
        content = render_report(cfg, pages, end, checked_at)
        output = cfg.reports_dir / "lab2-cost.md"
        output.parent.mkdir(parents=True, exist_ok=True)
        # Do not damage the previous report if retrieval, validation, or writing fails.
        with TemporaryDirectory(prefix=".cost-report-", dir=output.parent) as temporary:
            staged = Path(temporary) / output.name
            staged.write_text(content, encoding="utf-8")
            staged.replace(output)
        print(f"Wrote {output}; reported costs only, not a final budget pass.")
        return 0
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        print(f"Cost report failed: {exc}. Previous report left unchanged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
