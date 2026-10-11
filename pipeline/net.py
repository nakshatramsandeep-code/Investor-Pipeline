from __future__ import annotations

import time

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    pass


def post_json(url: str, headers: dict, body: dict, *, retries: int = 3, timeout: int = 60) -> dict:
    """POST JSON with exponential backoff on 429/5xx and network errors."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=timeout)
            if r.status_code in RETRY_STATUS:
                last = HttpError(f"HTTP {r.status_code}")
            elif r.ok:
                return r.json()
            else:
                # Never echo the response body into logs verbatim beyond a short slice;
                # it can include request details.
                raise HttpError(f"HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            last = e
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise HttpError(f"gave up after {retries + 1} attempts: {last}")
