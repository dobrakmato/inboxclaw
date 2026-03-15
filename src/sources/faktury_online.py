import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Dict, List
import httpx

from src.config import FakturyOnlineSourceConfig
from src.schemas import NewEvent
from src.services import AppServices
from src.utils.diff import DictDiff

logger = logging.getLogger(__name__)

class FakturyOnlineSource:
    """
    Source for monitoring invoices from Faktury-online.com.
    """
    BASE_URL = "https://www.faktury-online.com/api"

    def __init__(self, name: str, config: FakturyOnlineSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self._client = httpx.AsyncClient(timeout=10.0)
        self._cookies: Dict[str, str] = {}

    async def _init_session(self) -> bool:
        """Initialize session to get cookies."""
        data = {
            "key": self.config.api_key,
            "email": self.config.email
        }
        if not data["key"] or not data["email"]:
            logger.error(f"[{self.name}] API key or email missing in config/env.")
            return False
        try:
            url = f"{self.BASE_URL}/init"
            params = {"data": json.dumps(data)}
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            result = response.json()
            if result.get("status") == 1:
                self._cookies = dict(response.cookies)
                logger.info(f"[{self.name}] Session initialized.")
                return True
            else:
                logger.error(f"[{self.name}] Session init failed: {result.get('status')}")
                return False
        except Exception as e:
            logger.error(f"[{self.name}] Error initializing session: {e}")
            return False

    async def _fetch_invoices(self) -> List[Dict[str, Any]]:
        """Fetch invoices for the configured period."""
        if not self._cookies:
            if not await self._init_session():
                return []

        # We'll use /api/list/created to find recently created/updated invoices
        now = datetime.now(timezone.utc)
        date_from = (now - timedelta(days=self.config.max_days_back)).strftime("%Y-%m-%d")
        
        data = {
            "key": self.config.api_key,
            "email": self.config.email,
            "from": date_from
        }
        
        try:
            url = f"{self.BASE_URL}/list/created"
            params = {"data": json.dumps(data)}
            response = await self._client.get(url, params=params, cookies=self._cookies)
            response.raise_for_status()
            result = response.json()
            
            if result.get("status") == 1:
                return result.get("invoices", [])
            elif result.get("status") in [506, 507]: # Session errors
                logger.info(f"[{self.name}] Session expired, re-initializing...")
                if await self._init_session():
                    return await self._fetch_invoices()
            else:
                logger.error(f"[{self.name}] Failed to fetch invoices: {result.get('status')}")
                
        except Exception as e:
            logger.error(f"[{self.name}] Error fetching invoices: {e}")
            
        return []

    async def _get_invoice_detail(self, code: str) -> Optional[Dict[str, Any]]:
        """Fetch full details of an invoice."""
        data = {
            "key": self.config.api_key,
            "email": self.config.email,
            "code": code
        }
        try:
            url = f"{self.BASE_URL}/status"
            params = {"data": json.dumps(data)}
            response = await self._client.get(url, params=params, cookies=self._cookies)
            response.raise_for_status()
            result = response.json()
            if result.get("status") == 1:
                return result
            return None
        except Exception as e:
            logger.error(f"[{self.name}] Error fetching invoice detail {code}: {e}")
            return None

    def _compute_diff(self, old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
        """Compute what changed between two snapshots."""
        return DictDiff.compute(old, new, exclude={"status"})

    def _write_event(self, event: NewEvent) -> None:
        """Persist a single event using the batch writer API."""
        self.services.writer.write_events(self.source_id, [event])

    async def poll(self):
        """Poll for changes and emit events."""
        logger.info(f"[{self.name}] Polling for invoices...")
        invoices = await self._fetch_invoices()
        
        current_codes = {inv["code"] for inv in invoices}
        
        # Get previous state of codes from KV to detect deletions
        # (Though we only fetch last N days, so 'deletion' might just be 'getting old')
        previous_codes_raw = self.services.kv.get(self.source_id, "active_codes") or []
        previous_codes = set(previous_codes_raw)

        for inv in invoices:
            code = inv["code"]
            # Fetch full detail for better change detection
            current_detail = await self._get_invoice_detail(code)
            if not current_detail:
                continue

            # Check cache
            cached_snapshot = self.services.kv.get(self.source_id, f"invoice:{code}")
            
            if not cached_snapshot:
                # Created
                logger.info(f"[{self.name}] New invoice detected: {code}")
                self._write_event(NewEvent(
                    event_id=f"faktury:{code}:created:{datetime.now(timezone.utc).timestamp()}",
                    event_type="faktury.invoice.created",
                    entity_id=code,
                    data={"invoice": current_detail},
                    occurred_at=datetime.now(timezone.utc)
                ))
            else:
                # Check for updates
                diff = self._compute_diff(cached_snapshot, current_detail)
                if diff:
                    logger.info(f"[{self.name}] Invoice updated: {code}")
                    self._write_event(NewEvent(
                        event_id=f"faktury:{code}:updated:{datetime.now(timezone.utc).timestamp()}",
                        event_type="faktury.invoice.updated",
                        entity_id=code,
                        data={
                            "changes": diff,
                            "snapshot": current_detail
                        },
                        occurred_at=datetime.now(timezone.utc)
                    ))
            
            # Update cache
            self.services.kv.set(self.source_id, f"invoice:{code}", current_detail)

        # Detect deletions (items that were in previous poll but not in current)
        # Note: this has the caveat that if max_days_back changes or 
        # an invoice just falls out of the 'created' window, it might look like a deletion.
        # But usually invoices aren't deleted that often.
        deleted_codes = previous_codes - current_codes
        for code in deleted_codes:
            logger.info(f"[{self.name}] Invoice deleted (or missing): {code}")
            last_known = self.services.kv.get(self.source_id, f"invoice:{code}")
            self._write_event(NewEvent(
                event_id=f"faktury:{code}:deleted:{datetime.now(timezone.utc).timestamp()}",
                event_type="faktury.invoice.deleted",
                entity_id=code,
                data={"last_known_state": last_known},
                occurred_at=datetime.now(timezone.utc)
            ))
            self.services.kv.delete(self.source_id, f"invoice:{code}")

        self.services.kv.set(self.source_id, "active_codes", list(current_codes))

    async def run(self):
        """Main loop for the source."""
        while True:
            try:
                await self.poll()
            except Exception as e:
                logger.error(f"[{self.name}] Error in main loop: {e}", exc_info=True)
            
            await asyncio.sleep(self.config.poll_interval)
