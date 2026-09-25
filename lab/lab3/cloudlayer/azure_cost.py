"""Read-only Lab 3 billing query, adapted from lab/lab2/cloudlayer/azure_cost.py.

No resource mutations, retries or dependency on the Lab 2 directory at runtime.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

import requests
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

from src import config

START = date(2026, 9, 21)  # First Lab 3 resource creation day (UTC).
DEFAULT_END = date(2026, 9, 24)  # Teardown day; refresh the same window later.
API_VERSION = "2025-03-01"
SUBSCRIPTION = "d9385e82-8bee-4612-8a76-967c241112c8"
GROUP = "itcs355-u6688124"


def resource_scope(cfg: config.Config) -> str:
    if (cfg.provider != "azure" or cfg.azure_subscription_id.lower() != SUBSCRIPTION
            or cfg.project_id.lower() != GROUP):
        raise ValueError("Configuration differs from the reviewed Lab 3 billing scope")
    return f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}"


def window_end(end: date, checked_at: datetime) -> str:
    if checked_at.utcoffset() != timedelta(0):
        raise ValueError("Cost snapshot time must be UTC")
    if not START <= end <= min(DEFAULT_END, checked_at.date()):
        raise ValueError(f"End date must be within {START} to {DEFAULT_END} and not in the future")
    return (checked_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            if end == checked_at.date() else f"{end}T23:59:59Z")


def reviewed_resources(cfg: config.Config) -> dict[str, tuple[str, str]]:
    # Preserve the reviewed IDs after deletion. Lab 1/2 services are shared,
    # not automatically allocated to Lab 3 just because they appear in this window.
    entries = [
        ("Microsoft.App/containerApps/ca-itcs355-u6688124-lab3", "Container App", "Lab 3"),
        ("Microsoft.App/managedEnvironments/cae-itcs355-u6688124-lab3", "Environment", "Lab 3"),
        ("Microsoft.ManagedIdentity/userAssignedIdentities/id-itcs355-u6688124-lab3",
         "Managed identity", "Lab 3"),
        ("Microsoft.ContainerRegistry/registries/itcs355u6688124", "ACR", "Shared"),
        ("Microsoft.Storage/storageAccounts/itcs355u6688124", "Storage", "Shared"),
        ("Microsoft.OperationalInsights/workspaces/mlwitcs3logalytif5574ef1",
         "Log Analytics", "Shared"),
        ("Microsoft.MachineLearningServices/workspaces/mlw-itcs355-u6688124",
         "Azure ML workspace", "Shared"),
        ("Microsoft.KeyVault/vaults/mlwitcs3keyvault8263948e", "Key Vault", "Shared"),
        ("Microsoft.Insights/components/mlwitcs3insights48346e2c", "Application Insights", "Shared"),
        ("microsoft.insights/actiongroups/Application Insights Smart Detection",
         "Action group", "Shared"),
    ]
    return {f"{resource_scope(cfg)}/providers/{suffix}".lower(): (label, group)
            for suffix, label, group in entries}


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
                or parsed.path.lower() != urlsplit(endpoint).path.lower()
                or parsed.fragment or url in visited):
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
        if url is not None and (not isinstance(url, str) or not url):
            raise ValueError("Azure returned an invalid pagination link")
    return pages
