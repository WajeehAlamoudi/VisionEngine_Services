"""Batch-identified HTTP delivery with bounded response and total deadline."""

import asyncio
from dataclasses import dataclass
import json
import time

import httpx

MAX_RESPONSE_BYTES = 65536


@dataclass
class Result:
    success: bool
    status_code: int | None
    reason: str
    elapsed_ms: float
    ambiguous: bool = False


class Backend:
    def __init__(self, api, *, transport=None):
        self.url = api.url.rstrip("/") + "/branches/ingest"
        self.timeout = api.request.timeout_seconds
        self.client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            transport=transport,
            headers={"X-API-Key": api.key},
        )

    async def push(self, table, records, *, batch_id):
        started = time.monotonic()
        status = None

        def result(success, reason, ambiguous=False):
            return Result(
                success, status, reason, (time.monotonic() - started) * 1000, ambiguous
            )

        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream(
                    "POST",
                    self.url,
                    json={
                        "batch_id": batch_id,
                        "table": table,
                        "rows": [record["row"] for record in records],
                    },
                ) as response:
                    status = response.status_code
                    if status not in (200, 201, 409):
                        return result(False, "http_error", status >= 500)
                    payload = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        payload.extend(chunk)
                        if len(payload) > MAX_RESPONSE_BYTES:
                            return result(False, "oversized_response", True)
                    document = json.loads(payload)
                    if status == 409:
                        error = (
                            document.get("error")
                            if isinstance(document, dict)
                            else None
                        )
                        if (
                            isinstance(error, dict)
                            and document.get("success") is False
                            and error.get("code") == "BATCH_ID_CONFLICT"
                        ):
                            return result(False, "batch_id_conflict")
                        return result(False, "http_error")
                    data = document.get("data") if isinstance(document, dict) else None
                    if (
                        document.get("success") is not True
                        or not isinstance(data, dict)
                        or data.get("batch_id") != batch_id
                        or data.get("table") != table
                        or type(data.get("inserted")) is not int
                        or data["inserted"] != len(records)
                    ):
                        return result(False, "unconfirmed_delivery", True)
                    return result(True, "accepted")
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return result(False, "timeout", True)
        except httpx.RequestError:
            return result(False, "connection_error", True)
        except (ValueError, TypeError, AttributeError):
            return result(
                False,
                "http_error" if status == 409 else "invalid_response",
                status != 409,
            )

    async def close(self):
        await self.client.aclose()
