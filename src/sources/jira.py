import asyncio
import logging
import base64
import httpx
from datetime import datetime, timezone
from typing import Any, Dict, List, Set, Optional

from src.config import JiraSourceConfig
from src.schemas import NewEvent
from src.services import AppServices

logger = logging.getLogger(__name__)

class JiraSource:
    def __init__(self, name: str, config: JiraSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.client = httpx.AsyncClient(
            base_url=self.config.url.rstrip("/"),
            headers={
                "Authorization": f"Basic {self._get_auth_header()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30.0
        )
        self.field_map = {}  # id -> name
        self.task: Optional[asyncio.Task] = None
        self.health = services.health.reporter(name)
        self._poll_had_errors = False
        self._field_discovery_healthy = True

    def _get_auth_header(self) -> str:
        auth_str = f"{self.config.email}:{self.config.api_token}"
        return base64.b64encode(auth_str.encode()).decode()

    async def run(self):
        logger.info(f"Starting JiraSource '{self.name}'")
        
        # Initial field discovery
        await self._discover_fields()
        
        # Start periodic field discovery
        self.services.add_task(self._periodic_field_discovery())
        
        while True:
            self.health.checking()
            try:
                await self.fetch_and_publish()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Error in JiraSource '{self.name}': {e}", exc_info=True)
                self.health.unhealthy_from_exception(e)
            else:
                if self._poll_had_errors or not self._field_discovery_healthy:
                    self.health.unhealthy(
                        "partial_failure",
                        "The source reached Jira, but one or more issues could not be processed.",
                    )
                else:
                    self.health.healthy()
            
            await asyncio.sleep(self.config.poll_interval)

    async def _periodic_field_discovery(self):
        while True:
            await asyncio.sleep(self.config.field_discovery_interval)
            try:
                await self._discover_fields()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error discovering fields in JiraSource '{self.name}': {e}")
            if not self._field_discovery_healthy:
                self.health.unhealthy(
                    "partial_failure",
                    "Jira field discovery did not complete successfully.",
                )

    async def _discover_fields(self):
        logger.info(f"Discovering fields for JiraSource '{self.name}'")
        try:
            response = await self.client.get("/rest/api/3/field")
            response.raise_for_status()
            fields = response.json()
            self.field_map = {f["id"]: f["name"] for f in fields}
            self._field_discovery_healthy = True
            logger.info(f"Discovered {len(self.field_map)} fields for JiraSource '{self.name}'")
        except Exception as e:
            logger.error(f"Failed to discover fields for JiraSource '{self.name}': {e}")
            self._field_discovery_healthy = False

    async def fetch_and_publish(self):
        logger.debug(f"Polling Jira issues for '{self.name}'")
        self._poll_had_errors = False
        
        # 1. Search for issues
        issues = await self._search_issues()
        current_issue_keys = {issue["key"] for issue in issues}
        
        # 2. Get previous state keys
        previous_keys = set(self.services.kv.list_keys_with_prefix(self.source_id, "issue:"))
        previous_issue_keys = {k.split(":", 1)[1] for k in previous_keys}
        
        # 3. Detect changes
        new_keys = current_issue_keys - previous_issue_keys
        removed_keys = previous_issue_keys - current_issue_keys
        existing_keys = current_issue_keys & previous_issue_keys
        
        # Handle new issues
        for issue in issues:
            key = issue["key"]
            try:
                if key in new_keys:
                    await self._handle_new_issue(issue)
                elif key in existing_keys:
                    await self._handle_existing_issue(issue)
            except Exception as e:
                logger.error(f"Error processing Jira issue {key} in source '{self.name}': {e}", exc_info=True)
                self._poll_had_errors = True
        
        # Handle removed issues (no longer assigned)
        for key in removed_keys:
            try:
                await self._handle_removed_issue(key)
            except Exception as e:
                logger.error(f"Error processing removed Jira issue {key} in source '{self.name}': {e}", exc_info=True)
                self._poll_had_errors = True

    async def _search_issues(self) -> List[Dict[str, Any]]:
        all_issues = []
        start_at = 0
        max_results = 100
        
        # Include all requested fields + custom fields identified during discovery
        # Jira search API accepts both field IDs (like customfield_101) and field names (if unique)
        # We prefer IDs for stability.
        base_fields = ["summary", "status", "assignee", "priority", "updated", "issuetype", "project", "parent", "labels", "created", "duedate", "resolution", "resolutiondate", "description", "attachment", "comment"]
        
        # We want as many fields as possible to avoid extra GET /issue/{key} calls if nothing changed
        # but also to have a good snapshot. 
        # However, requesting TOO many fields might hit response size limits.
        # Let's stick to base_fields + custom fields that are NOT ignored.
        requested_fields = list(base_fields)
        for fid in self.field_map:
            if fid.startswith("customfield_") and fid not in self.config.ignored_fields:
                requested_fields.append(fid)

        while True:
            payload = {
                "jql": self.config.jql,
                "fields": requested_fields,
                "maxResults": max_results,
                "startAt": start_at,
            }
            response = await self.client.post("/rest/api/3/search", json=payload)
            response.raise_for_status()
            data = response.json()
            issues = data.get("issues", [])
            all_issues.extend(issues)
            
            if len(all_issues) >= data.get("total", 0) or len(issues) < max_results:
                break
            start_at += max_results
            
        return all_issues

    async def _handle_new_issue(self, issue: Dict[str, Any]):
        key = issue["key"]
        logger.info(f"New Jira issue detected: {key}")
        
        full_issue = await self._fetch_issue_detail(key)
        comments = await self._fetch_comments(key) or []
        
        # Save to KV
        self.services.kv.set(self.source_id, f"issue:{key}", full_issue)
        self.services.kv.set(self.source_id, f"comments:{key}", comments)
        
        # Emit event
        self.services.writer.write_events(self.source_id, [NewEvent(
            event_id=f"jira-{key}-{full_issue['fields']['updated']}",
            event_type="jira.task_assigned",
            entity_id=key,
            data={
                "issue_key": key,
                "summary": full_issue["fields"].get("summary"),
                "status": (full_issue["fields"].get("status") or {}).get("name"),
                "priority": (full_issue["fields"].get("priority") or {}).get("name"),
                "issue_type": (full_issue["fields"].get("issuetype") or {}).get("name"),
                "project": (full_issue["fields"].get("project") or {}).get("name"),
                "full_issue": full_issue
            },
            occurred_at=self._parse_jira_date(full_issue["fields"]["updated"])
        )])

    async def _handle_existing_issue(self, issue: Dict[str, Any]):
        key = issue["key"]
        cached_issue = self.services.kv.get(self.source_id, f"issue:{key}")
        
        if not cached_issue:
            await self._handle_new_issue(issue)
            return

        if cached_issue["fields"]["updated"] != issue["fields"]["updated"]:
            logger.info(f"Jira issue updated: {key}")
            
            full_issue = await self._fetch_issue_detail(key)
            changelog = await self._fetch_issue_changelog(key)
            fetched_comments = await self._fetch_comments(key)
            cached_comments = self.services.kv.get(self.source_id, f"comments:{key}") or []
            # If comment fetch failed, use cached comments to avoid false diffs
            comments = fetched_comments if fetched_comments is not None else cached_comments
            
            diff = self._compute_diff(cached_issue, full_issue)
            # Remove comment container from field diff — comments are handled separately
            diff.pop("comment", None)
            diff.pop("Comment", None)
            
            occurred_at = self._parse_jira_date(full_issue["fields"]["updated"])
            summary = full_issue["fields"].get("summary")
            events: List[NewEvent] = []
            
            # Extract specific field-level events before emitting generic task_updated
            # Pop both possible key variants to avoid leaking into generic task_updated
            status_diff = diff.pop("Status", None) or diff.pop("status", None)
            if "status" in diff:
                diff.pop("status")
            if "Status" in diff:
                diff.pop("Status")
            assignee_diff = diff.pop("Assignee", None) or diff.pop("assignee", None)
            if "assignee" in diff:
                diff.pop("assignee")
            if "Assignee" in diff:
                diff.pop("Assignee")
            
            if status_diff:
                events.append(NewEvent(
                    event_id=f"jira-{key}-status-{full_issue['fields']['updated']}",
                    event_type="jira.task_status_changed",
                    entity_id=key,
                    data={
                        "issue_key": key,
                        "summary": summary,
                        "status_before": status_diff["before"],
                        "status_after": status_diff["after"],
                        "changelog": changelog,
                        "full_issue": full_issue
                    },
                    occurred_at=occurred_at
                ))
            
            if assignee_diff:
                events.append(NewEvent(
                    event_id=f"jira-{key}-reassigned-{full_issue['fields']['updated']}",
                    event_type="jira.task_reassigned",
                    entity_id=key,
                    data={
                        "issue_key": key,
                        "summary": summary,
                        "assignee_before": assignee_diff["before"],
                        "assignee_after": assignee_diff["after"],
                        "changelog": changelog,
                        "full_issue": full_issue
                    },
                    occurred_at=occurred_at
                ))
            
            # Remaining field changes → generic task_updated
            if diff:
                events.append(NewEvent(
                    event_id=f"jira-{key}-{full_issue['fields']['updated']}",
                    event_type="jira.task_updated",
                    entity_id=key,
                    data={
                        "issue_key": key,
                        "summary": summary,
                        "diff": diff,
                        "changelog": changelog,
                        "full_issue": full_issue
                    },
                    occurred_at=occurred_at
                ))
            
            # Detect new comments
            old_comment_ids = {c["id"] for c in cached_comments}
            old_comments_by_id = {c["id"]: c for c in cached_comments}
            for c in comments:
                if c["id"] not in old_comment_ids:
                    events.append(NewEvent(
                        event_id=f"jira-{key}-comment-{c['id']}-created",
                        event_type="jira.comment_added",
                        entity_id=key,
                        data={
                            "issue_key": key,
                            "summary": summary,
                            "comment_id": c["id"],
                            "author": (c.get("author") or {}).get("displayName"),
                            "body": c.get("body"),
                            "created": c.get("created")
                        },
                        occurred_at=self._parse_jira_date(c.get("created", "")) if c.get("created") else occurred_at
                    ))
                elif c.get("updated") != old_comments_by_id[c["id"]].get("updated"):
                    events.append(NewEvent(
                        event_id=f"jira-{key}-comment-{c['id']}-updated-{c.get('updated', '')}",
                        event_type="jira.comment_updated",
                        entity_id=key,
                        data={
                            "issue_key": key,
                            "summary": summary,
                            "comment_id": c["id"],
                            "author": (c.get("author") or {}).get("displayName"),
                            "body_before": old_comments_by_id[c["id"]].get("body"),
                            "body_after": c.get("body"),
                            "updated": c.get("updated")
                        },
                        occurred_at=self._parse_jira_date(c.get("updated", "")) if c.get("updated") else occurred_at
                    ))
            
            # Write all events
            if events:
                self.services.writer.write_events(self.source_id, events)
            
            # Update caches
            self.services.kv.set(self.source_id, f"issue:{key}", full_issue)
            # Only update comment cache if fetch succeeded to avoid false diffs on next poll
            if fetched_comments is not None:
                self.services.kv.set(self.source_id, f"comments:{key}", comments)

    async def _handle_removed_issue(self, key: str):
        logger.info(f"Jira issue no longer assigned: {key}")
        cached_issue = self.services.kv.get(self.source_id, f"issue:{key}")
        
        # Emit event
        data = {
            "issue_key": key,
            "summary": cached_issue["fields"].get("summary") if cached_issue else None,
            "last_known_state": cached_issue
        }
        
        if cached_issue and "fields" in cached_issue:
            f = cached_issue["fields"]
            data.update({
                "status": (f.get("status") or {}).get("name"),
                "priority": (f.get("priority") or {}).get("name"),
                "assignee": (f.get("assignee") or {}).get("displayName"),
                "issuetype": (f.get("issuetype") or {}).get("name"),
                "project": (f.get("project") or {}).get("name"),
            })

        self.services.writer.write_events(self.source_id, [NewEvent(
            event_id=f"jira-{key}-unassigned-{datetime.now(timezone.utc).isoformat()}",
            event_type="jira.task_unassigned",
            entity_id=key,
            data=data,
            occurred_at=datetime.now(timezone.utc)
        )])
        
        # Remove from cache
        self.services.kv.delete(self.source_id, f"issue:{key}")
        self.services.kv.delete(self.source_id, f"comments:{key}")

    async def _fetch_comments(self, key: str) -> Optional[List[Dict[str, Any]]]:
        """Fetch comments for an issue. Returns None on failure to distinguish from empty list."""
        try:
            response = await self.client.get(f"/rest/api/3/issue/{key}/comment")
            response.raise_for_status()
            data = response.json()
            return data.get("comments", [])
        except Exception as e:
            logger.error(f"Failed to fetch comments for {key}: {e}")
            self._poll_had_errors = True
            return None

    async def _fetch_issue_detail(self, key: str) -> Dict[str, Any]:
        response = await self.client.get(f"/rest/api/3/issue/{key}")
        response.raise_for_status()
        return response.json()

    async def _fetch_issue_changelog(self, key: str) -> List[Dict[str, Any]]:
        response = await self.client.get(f"/rest/api/3/issue/{key}/changelog")
        response.raise_for_status()
        data = response.json()
        
        histories = data.get("values", [])
        
        # Filter items in each history entry based on ignored_fields
        filtered_histories = []
        for history in histories:
            filtered_items = [
                item for item in history.get("items", [])
                if item.get("field") not in self.config.ignored_fields
                and item.get("fieldId") not in self.config.ignored_fields
            ]
            if filtered_items:
                # Create a copy and update items
                new_history = dict(history)
                new_history["items"] = []
                for item in filtered_items:
                    # Simplify values in changelog items for easier reading
                    new_item = dict(item)
                    new_item["fromString"] = item.get("fromString")
                    new_item["toString"] = item.get("toString")
                    new_history["items"].append(new_item)
                filtered_histories.append(new_history)
        
        # Return only the most recent histories
        return filtered_histories[-10:]

    def _compute_diff(self, old_issue: Dict[str, Any], new_issue: Dict[str, Any]) -> Dict[str, Any]:
        diff = {}
        old_fields = old_issue.get("fields", {})
        new_fields = new_issue.get("fields", {})
        
        all_field_keys = set(old_fields.keys()) | set(new_fields.keys())
        
        for field_key in all_field_keys:
            if field_key in self.config.ignored_fields:
                continue
                
            old_val = old_fields.get(field_key)
            new_val = new_fields.get(field_key)
            
            if old_val != new_val:
                field_name = self.field_map.get(field_key, field_key)
                diff[field_name] = {
                    "field_id": field_key,
                    "before": self._simplify_field_value(old_val),
                    "after": self._simplify_field_value(new_val)
                }
        
        return diff

    def _simplify_field_value(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            # Prioritize standard Jira object fields
            if "name" in value:
                return value["name"]
            if "displayName" in value:
                return value["displayName"]
            if "value" in value:
                return value["value"]
            if "label" in value:
                return value["label"]
            if "emailAddress" in value:
                return value["emailAddress"]
            # Fallback for other simple objects or return string representation
            if len(value) == 1:
                # Some custom fields wrap values in a single-key dict like {"value": "..."} 
                # or similar. But we should only do this if it's one of the known keys or if we are sure.
                # Actually, many Jira fields are just {"id": "...", "value": "..."} or {"self": "...", "name": "..."}.
                # If we don't recognize the keys above, and it has more than 1 key, we return the dict.
                # If it has only 1 key, it's likely a wrapper.
                pass 
            return value
        if isinstance(value, list):
            return [self._simplify_field_value(v) for v in value]
        return value

    def _parse_jira_date(self, date_str: str) -> datetime:
        # Jira dates are often in format: 2024-03-22T14:55:00.000+0000
        # or ISO format. httpx might have issues with +0000 but modern python handles it well if it's +HHMM
        try:
            # Replace +HHMM with +HH:MM for ISO parser if needed
            # Handle +HHMM or -HHMM (no colon) timezone offsets
            for sep in ("+", "-"):
                if sep in date_str and ":" not in date_str.rsplit(sep, 1)[-1]:
                    prefix, tz_part = date_str.rsplit(sep, 1)
                    if len(tz_part) == 4 and tz_part.isdigit():
                        date_str = prefix + sep + tz_part[:2] + ":" + tz_part[2:]
                        break
            return datetime.fromisoformat(date_str)
        except Exception as e:
            logger.debug(f"Failed to parse Jira date '{date_str}': {e}")
            return datetime.now(timezone.utc)
