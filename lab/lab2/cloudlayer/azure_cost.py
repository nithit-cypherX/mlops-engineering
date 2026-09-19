"""Read-only Azure billing access for Lab 2; no resource changes or retries."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlsplit

import requests
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

from src import config

START = date(2026, 9, 17)  # Workspace created at 06:10 UTC; count the full UTC day.
API_VERSION = "2025-03-01"


def resource_scope(cfg: config.Config) -> str:
    if cfg.provider != "azure" or not cfg.azure_subscription_id or cfg.project_id == "unset":
        raise ValueError("Configure Azure subscription and resource group in cloud.env")
    return f"/subscriptions/{cfg.azure_subscription_id}/resourceGroups/{cfg.project_id}"


def fetch_pages(cfg: config.Config, end: date, checked_at: datetime) -> list[dict]:
    endpoint = (f"https://management.azure.com{resource_scope(cfg)}"
                "/providers/Microsoft.CostManagement/query")
    body = {
        "type": "ActualCost", "timeframe": "Custom",
        "timePeriod": {
            "from": f"{START}T00:00:00Z",
            "to": (checked_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                   if end == checked_at.date() else f"{end}T23:59:59Z"),
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ResourceId"}],
        },
    }
    try:
        with AzureCliCredential(process_timeout=30) as credential:
            token = credential.get_token("https://management.azure.com/.default").token
    except AzureError:
        raise RuntimeError("Azure CLI authentication failed; check az login") from None
    url = f"{endpoint}?api-version={API_VERSION}"
    pages, visited = [], set()
    while url:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "management.azure.com"
                or parsed.path.lower() != urlsplit(endpoint).path.lower() or url in visited):
            raise ValueError("Invalid or repeated Cost Management pagination link")
        visited.add(url)
        response = requests.post(
            url, headers={"Authorization": f"Bearer {token}"}, json=body,
            timeout=45, allow_redirects=False,
        )
        if response.status_code == 429:
            raise RuntimeError("Azure Cost Management HTTP 429 (rate limited); try again later")
        if response.status_code == 204:
            raise ValueError("Azure returned no cost data; this is not evidence of zero spend")
        if response.status_code != 200:
            raise RuntimeError(f"Azure Cost Management HTTP {response.status_code}")
        payload = response.json(parse_float=Decimal)
        if not isinstance(payload, dict) or not isinstance(payload.get("properties"), dict):
            raise ValueError("Azure returned an invalid cost response")
        page = payload["properties"]
        pages.append(page)
        url = page.get("nextLink")
        if url is not None and not isinstance(url, str):
            raise ValueError("Azure returned an invalid pagination link")
    return pages
