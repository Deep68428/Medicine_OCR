from __future__ import annotations

from typing import Any

import httpx

from core.config import get_config


class BackendClient:
    """Async HTTP client for communicating with the backend API server."""

    def __init__(self) -> None:
        config = get_config()
        self.base_url = config.BACKEND_API_URL.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    async def aclose(self) -> None:
        """Close the underlying HTTP client and release its resources."""
        await self._client.aclose()

    async def fetch_machine_config(self, machine_id: int) -> dict[str, Any]:
        """Fetch the machine configuration record for the given machine ID.

        Args:
            machine_id: Unique identifier of the machine.

        Returns:
            Parsed JSON response containing the machine configuration.

        Raises:
            httpx.HTTPStatusError: If the server returns a non-2xx status.
        """
        response = await self._client.get(
            f"{self.base_url}/config/machine-config",
            params={"machine_id": machine_id},
        )
        response.raise_for_status()
        return response.json()

    async def search_picknote(self, picknote: str, machine_id: int) -> dict[str, Any]:
        """Search for a picknote and return its associated product data.

        Args:
            picknote: The picknote identifier string to search for.
            machine_id: ID of the machine issuing the request.

        Returns:
            Parsed JSON response with picknote data and status.

        Raises:
            httpx.HTTPStatusError: If the server returns a non-2xx status.
        """
        response = await self._client.post(
            f"{self.base_url}/picknote/search",
            json={"picknote": picknote, "machine_id": machine_id},
        )
        response.raise_for_status()
        return response.json()

    async def submit_picknote(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Forward a picknote submission to the backend.

        Args:
            payload: Submission body with picknote, machine_id, party_name,
                store_code, and products list.

        Returns:
            Parsed JSON response from the backend.

        Raises:
            httpx.HTTPStatusError: If the server returns a non-2xx status.
        """
        response = await self._client.post(
            f"{self.base_url}/picknote/submit",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def get_product_batch_list(self, picknote: str) -> list[dict[str, Any]]:
        """Fetch all stock lots for every product in the given picknote.

        Used for batch-correction matching: when an OCR-read batch is not in the
        picklist, this list is checked to see if the batch belongs to a picklist product.

        Args:
            picknote: The picknote document name to query.

        Returns:
            List of batch entry dicts (product_name, product_code, batch_number, …).

        Raises:
            httpx.HTTPStatusError: If the server returns a non-2xx status.
        """

        response = await self._client.get(
            f"{self.base_url}/picknote/batches",
            params={"picknote": picknote},
        )
        response.raise_for_status()
        return response.json().get("batches", [])
