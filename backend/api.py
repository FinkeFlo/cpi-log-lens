"""CPI API client — OAuth2 + Log file fetching."""
import re
import httpx
from typing import Optional


def epoch_ms(date_str: str) -> int:
    """Extract epoch ms from OData /Date(1786543619000)/ format."""
    m = re.search(r"\d+", date_str or "")
    return int(m.group()) if m else 0


async def get_token(oauth_url: str, client_id: str, client_secret: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            oauth_url,
            params={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


async def list_remote_files(api_url: str, token: str, log_type: str) -> list[dict]:
    filter_str = f"LogFileType eq '{log_type}' and NodeScope eq 'worker'"
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.get(
            f"{api_url}/api/v1/LogFiles",
            params={"$filter": filter_str},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        return resp.json().get("d", {}).get("results", [])


async def download_file(
    api_url: str, token: str, name: str, application: str
) -> bytes:
    from urllib.parse import quote
    url = f"{api_url}/api/v1/LogFiles(Name='{quote(name)}',Application='{quote(application)}')/$value"
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.content
