"""Read-only billing query adapted from Lab 3, scoped to reviewed Lab 4 resources."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

import requests

from cloudlayer.lab4_scope import START, access_token, resource_scope, reviewed_resources
from src import config

API_VERSION = "2025-03-01"
# Re-export the same reviewed map for the provider-neutral report.
__all__ = ["API_VERSION", "START", "fetch_pages", "resource_scope", "reviewed_resources", "window_end"]


def window_end(end: date, checked_at: datetime) -> str:
    if checked_at.utcoffset() != timedelta(0):
        raise ValueError("Cost snapshot time must be UTC")
    if not START <= end <= checked_at.date():
        raise ValueError("End date must be on/after the Lab 4 start and not in the future")
    return (checked_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if end == checked_at.date() else f"{end}T23:59:59Z")


def fetch_pages(cfg: config.Config, end: date, checked_at: datetime) -> list[dict]:
    endpoint = (f"https://management.azure.com{resource_scope(cfg)}"
                "/providers/Microsoft.CostManagement/query")
    body = {
        "type": "ActualCost", "timeframe": "Custom",
        "timePeriod": {"from": f"{START}T00:00:00Z", "to": window_end(end, checked_at)},
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceId"}],
        },
    }
    token = access_token()
    url = f"{endpoint}?api-version={API_VERSION}"
    pages, visited = [], set()
    while url:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "management.azure.com"
                or parsed.path.lower() != urlsplit(endpoint).path.lower()
                or parsed.fragment or url in visited or len(visited) >= 100):
            raise ValueError("Invalid or repeated Cost Management pagination link")
        visited.add(url)
        # The Cost Management query API uses POST to read, not create resources.
        response = requests.post(
            url, headers={"Authorization": f"Bearer {token}"}, json=body,
            timeout=(10, 35), allow_redirects=False,
        )
        if response.status_code == 429:
            raise RuntimeError("Cost query rate limited; try again later, no automatic retry")
        if response.status_code == 204:
            raise ValueError("No cost data; this is not evidence of zero spend")
        if response.status_code != 200:
            raise RuntimeError(f"Cost query HTTP {response.status_code}")
        payload = response.json(parse_float=Decimal)
        if not isinstance(payload, dict) or not isinstance(payload.get("properties"), dict):
            raise ValueError("Invalid cost response")
        page = payload["properties"]
        pages.append(page)
        url = page.get("nextLink")
        if url is not None and (not isinstance(url, str) or not url):
            raise ValueError("Invalid cost pagination link")
    return pages
