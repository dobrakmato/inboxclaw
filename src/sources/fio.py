import asyncio
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Any, Dict, List, Optional

import httpx
from src.config import FioSourceConfig
from src.schemas import NewEvent
from src.services import AppServices

logger = logging.getLogger(__name__)

class FioSource:
    """
    Source for Fio Banka transactions.
    Polls the Fio API for new transactions and reports them as events.
    """

    BASE_URL = "https://fioapi.fio.cz/v1/rest"
    MIN_POLL_INTERVAL_SECONDS = 30.0

    def __init__(self, name: str, config: FioSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.cursor_manager = services.cursor
        self.writer = services.writer
        self.last_poll_time: Optional[datetime] = None

        if not self.config.token:
            logger.warning(f"Fio source '{self.name}' has no token configured. It will not be able to fetch data.")

    async def run(self):
        """Main loop for the Fio source."""
        logger.info(f"Starting Fio source '{self.name}' with poll interval {self.config.poll_interval}s")
        while True:
            try:
                await self.poll()
            except Exception:
                logger.exception(f"Error in Fio source '{self.name}' poll loop")
            
            await asyncio.sleep(self.config.poll_interval)

    async def poll(self):
        """Perform a single poll of the Fio API."""
        now = datetime.now(timezone.utc)
        
        # Enforce internal rate limit
        if self.last_poll_time:
            elapsed = (now - self.last_poll_time).total_seconds()
            if elapsed < self.MIN_POLL_INTERVAL_SECONDS:
                wait_time = self.MIN_POLL_INTERVAL_SECONDS - elapsed
                logger.debug(f"Rate limiting Fio source '{self.name}': waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time)
                now = datetime.now(timezone.utc)

        self.last_poll_time = now

        last_sync_str = self.cursor_manager.get_last_cursor(self.source_id)
        
        if last_sync_str:
            try:
                start_date = datetime.fromisoformat(last_sync_str).date()
            except ValueError:
                logger.error(f"Invalid cursor format for Fio source '{self.name}': {last_sync_str}")
                start_date = (now - timedelta(days=self.config.max_days_back)).date()
        else:
            start_date = (now - timedelta(days=self.config.max_days_back)).date()

        end_date = (now + timedelta(days=self.config.look_ahead_days)).date()

        # Format for URL: rrrr-mm-dd
        date_from = start_date.isoformat()
        date_to = end_date.isoformat()

        url = f"{self.BASE_URL}/periods/{self.config.token}/{date_from}/{date_to}/transactions.json"
        
        logger.info(f"Polling Fio source '{self.name}' for periods {date_from} to {date_to}")
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            
            if response.status_code == 409:
                logger.warning(f"Fio API rate limit hit (409 Conflict) for source '{self.name}'")
                return
            
            response.raise_for_status()
            data = response.json()

        transactions = self._parse_response(data)
        
        if transactions:
            new_events = [self._map_to_event(tx) for tx in transactions]
            self.writer.write_events(self.source_id, new_events)
            
            logger.info(f"Fio source '{self.name}' processed {len(transactions)} transactions")
            
            # Update cursor to today's date (or the latest transaction date)
            # Fio API 'periods' query is inclusive. 
            # To avoid refetching everything next time, we could use the 'last' API, 
            # but the requirement said "cursor with date".
            # We'll set the cursor to the date of the latest transaction we found, 
            # or today if no transactions were found in the future.
            latest_tx_date = max(t.get("date", start_date) for t in transactions)
            if isinstance(latest_tx_date, str):
                latest_tx_date = date.fromisoformat(latest_tx_date)
            cursor_date = max(latest_tx_date, now.date())
            self.cursor_manager.set_cursor(self.source_id, cursor_date.isoformat())
        else:
            logger.debug(f"Fio source '{self.name}' found no new transactions")
            # Even if no transactions, update cursor to today to move the window forward
            self.cursor_manager.set_cursor(self.source_id, now.date().isoformat())

    def _parse_response(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Parses the Fio JSON response into a flat list of transaction dictionaries."""
        try:
            statement = data.get("accountStatement", {})
            info = statement.get("info", {})
            tx_list = statement.get("transactionList", {}).get("transaction", [])
            
            if tx_list is None:
                tx_list = []

            parsed_txs = []
            for tx in tx_list:
                item = {}
                # The Fio JSON structure is a bit weird: columnXX: {value: ..., name: ..., id: ...}
                for key, val in tx.items():
                    if isinstance(val, dict) and "value" in val:
                        # Map internal column IDs to friendly names if needed, 
                        # but we can also just use the 'name' from the API or fixed mapping.
                        # Column22 is ID pohybu
                        # Column0 is Datum
                        # Column1 is Objem
                        col_id = val.get("id")
                        value = val.get("value")
                        
                        if col_id == 22: item["id"] = str(value)
                        elif col_id == 0: 
                            # Value is timestamp in ms (with timezone sometimes?)
                            # The docs say: rrrr-mm-dd+GMT but the example shows milliseconds 1340661600000
                            if isinstance(value, int):
                                item["date"] = datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
                            else:
                                # Fallback if it's already a string
                                try:
                                    item["date"] = datetime.fromisoformat(str(value).split('+')[0]).date().isoformat()
                                except:
                                    logger.warning(f"Could not parse Fio date: {value}")
                        elif col_id == 1: item["amount"] = value
                        elif col_id == 14: item["currency"] = value
                        elif col_id == 2: item["counterpart_account"] = value
                        elif col_id == 10: item["counterpart_name"] = value
                        elif col_id == 3: item["counterpart_bank_code"] = value
                        elif col_id == 12: item["counterpart_bank_name"] = value
                        elif col_id == 4: item["constant_symbol"] = value
                        elif col_id == 5: item["variable_symbol"] = value
                        elif col_id == 6: item["specific_symbol"] = value
                        elif col_id == 7: item["user_identification"] = value
                        elif col_id == 16: item["recipient_message"] = value
                        elif col_id == 8: item["type"] = value
                        elif col_id == 9: item["authorized_by"] = value
                        elif col_id == 18: item["specification"] = value
                        elif col_id == 25: item["comment"] = value
                        elif col_id == 26: item["counterpart_bic"] = value
                        elif col_id == 17: item["instruction_id"] = str(value)
                        elif col_id == 27: item["payer_reference"] = value
                
                # Add account info to each transaction
                item["account_id"] = info.get("accountId")
                item["bank_id"] = info.get("bankId")
                item["account_currency"] = info.get("currency")
                item["closing_balance"] = info.get("closingBalance") # Reporting account value with every transaction
                
                parsed_txs.append(item)
            return parsed_txs
        except Exception as e:
            logger.error(f"Error parsing Fio response: {e}")
            return []

    def _map_to_event(self, tx: Dict[str, Any]) -> NewEvent:
        """Maps a transaction dictionary to a NewEvent."""
        tx_id = tx.get("id", "unknown")
        amount = tx.get("amount", 0)
        currency = tx.get("currency", "")
        tx_type = tx.get("type", "Transaction")
        
        # Use IDpohybu as unique event ID
        event_id = f"fio_{tx_id}"
        
        # Determine event type based on amount (Inflow/Outflow)
        event_type = "fio.transaction"
        if amount > 0:
            event_type = "fio.transaction.income"
        elif amount < 0:
            event_type = "fio.transaction.expense"

        occurred_at = None
        if "date" in tx:
            # Transactions in Fio are dates, not datetimes. 
            # We'll set it to midnight UTC.
            tx_date = tx["date"]
            if isinstance(tx_date, str):
                tx_date = date.fromisoformat(tx_date)
            occurred_at = datetime.combine(tx_date, datetime.min.time(), tzinfo=timezone.utc)

        return NewEvent(
            event_id=event_id,
            event_type=event_type,
            entity_id=tx_id,
            occurred_at=occurred_at,
            data=tx
        )
