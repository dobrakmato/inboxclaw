import asyncio
import logging
import httpx
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config import AsanaSourceConfig
from src.schemas import NewEvent
from src.services import AppServices

logger = logging.getLogger(__name__)

# Built-in fields to request via opt_fields (Asana has no field discovery endpoint for these)
BUILTIN_TASK_FIELDS = [
    "name", "assignee", "assignee.name", "assignee.gid",
    "due_on", "due_at", "start_on", "start_at",
    "completed", "completed_at",
    "notes", "tags", "tags.name",
    "memberships.section.name", "memberships.project.name",
    "parent", "parent.name",
    "followers", "followers.name",
    "custom_fields", "custom_fields.name", "custom_fields.display_value",
    "custom_fields.type", "custom_fields.enum_value", "custom_fields.number_value",
    "custom_fields.text_value",
    "modified_at", "created_at",
    "liked", "num_likes", "num_subtasks",
]

# Fields used for the compact project task listing (to detect changes cheaply)
LIST_FIELDS = ["name", "assignee", "assignee.name", "modified_at", "completed"]


class AsanaSource:
    def __init__(self, name: str, config: AsanaSourceConfig, services: AppServices, source_id: int):
        self.name = name
        self.config = config
        self.services = services
        self.source_id = source_id
        self.client = httpx.AsyncClient(
            base_url="https://app.asana.com/api/1.0",
            headers={
                "Authorization": f"Bearer {self.config.access_token}",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
        self.custom_field_map: Dict[str, str] = {}  # gid -> name

    async def run(self):
        logger.info(f"Starting AsanaSource '{self.name}'")

        # Initial custom field discovery
        await self._discover_custom_fields()

        # Start periodic field discovery
        self.services.add_task(self._periodic_field_discovery())

        while True:
            try:
                await self.fetch_and_publish()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in AsanaSource '{self.name}': {e}", exc_info=True)

            await asyncio.sleep(self.config.poll_interval)

    async def _periodic_field_discovery(self):
        while True:
            await asyncio.sleep(self.config.field_discovery_interval)
            try:
                await self._discover_custom_fields()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error discovering custom fields in AsanaSource '{self.name}': {e}")

    async def _discover_custom_fields(self):
        """Discover custom fields for all configured projects."""
        logger.info(f"Discovering custom fields for AsanaSource '{self.name}'")
        total = 0
        for project_gid in self.config.project_gids:
            try:
                response = await self.client.get(
                    f"/projects/{project_gid}/custom_field_settings",
                    params={"opt_fields": "custom_field.name,custom_field.type,custom_field.enum_options"},
                )
                response.raise_for_status()
                data = response.json().get("data", [])
                for setting in data:
                    cf = setting.get("custom_field", {})
                    if cf.get("gid") and cf.get("name"):
                        self.custom_field_map[cf["gid"]] = cf["name"]
                        total += 1
            except Exception as e:
                logger.error(f"Failed to discover custom fields for project {project_gid}: {e}")
        logger.info(f"Discovered {total} custom fields for AsanaSource '{self.name}'")

    async def fetch_and_publish(self):
        logger.debug(f"Polling Asana tasks for '{self.name}'")

        for project_gid in self.config.project_gids:
            await self._poll_project(project_gid)

    async def _poll_project(self, project_gid: str):
        # 1. List tasks in project
        tasks = await self._list_project_tasks(project_gid)
        current_gids = {t["gid"] for t in tasks}

        # 2. Get previous state
        prefix = f"project:{project_gid}:task:"
        previous_keys = set(self.services.kv.list_keys_with_prefix(self.source_id, prefix))
        previous_gids = {k.split("task:", 1)[1] for k in previous_keys}

        # 3. Detect changes
        new_gids = current_gids - previous_gids
        removed_gids = previous_gids - current_gids
        existing_gids = current_gids & previous_gids

        task_by_gid = {t["gid"]: t for t in tasks}

        for gid in new_gids:
            try:
                await self._handle_new_task(project_gid, task_by_gid[gid])
            except Exception as e:
                logger.error(f"Error processing new Asana task {gid}: {e}", exc_info=True)

        for gid in existing_gids:
            try:
                await self._handle_existing_task(project_gid, task_by_gid[gid])
            except Exception as e:
                logger.error(f"Error processing existing Asana task {gid}: {e}", exc_info=True)

        for gid in removed_gids:
            try:
                await self._handle_removed_task(project_gid, gid)
            except Exception as e:
                logger.error(f"Error processing removed Asana task {gid}: {e}", exc_info=True)

    async def _list_project_tasks(self, project_gid: str) -> List[Dict[str, Any]]:
        all_tasks: List[Dict[str, Any]] = []
        offset: Optional[str] = None

        while True:
            params: Dict[str, Any] = {
                "opt_fields": ",".join(LIST_FIELDS),
                "limit": 100,
            }
            if offset:
                params["offset"] = offset

            response = await self.client.get(f"/projects/{project_gid}/tasks", params=params)
            response.raise_for_status()
            body = response.json()
            all_tasks.extend(body.get("data", []))

            next_page = body.get("next_page")
            if next_page and next_page.get("offset"):
                offset = next_page["offset"]
            else:
                break

        return all_tasks

    async def _fetch_task_detail(self, task_gid: str) -> Dict[str, Any]:
        params = {"opt_fields": ",".join(BUILTIN_TASK_FIELDS)}
        response = await self.client.get(f"/tasks/{task_gid}", params=params)
        response.raise_for_status()
        return response.json().get("data", {})

    async def _fetch_task_stories(self, task_gid: str) -> List[Dict[str, Any]]:
        """Fetch comment stories for a task."""
        params = {"opt_fields": "type,created_at,text,created_by.name"}
        response = await self.client.get(f"/tasks/{task_gid}/stories", params=params)
        response.raise_for_status()
        stories = response.json().get("data", [])
        # Filter to only comments
        return [s for s in stories if s.get("type") == "comment"]

    def _kv_key(self, project_gid: str, task_gid: str) -> str:
        return f"project:{project_gid}:task:{task_gid}"

    def _comments_kv_key(self, project_gid: str, task_gid: str) -> str:
        return f"project:{project_gid}:comments:{task_gid}"

    async def _handle_new_task(self, project_gid: str, task_summary: Dict[str, Any]):
        task_gid = task_summary["gid"]
        logger.info(f"New Asana task detected: {task_gid}")

        full_task = await self._fetch_task_detail(task_gid)

        # Save to KV
        self.services.kv.set(self.source_id, self._kv_key(project_gid, task_gid), full_task)

        # Emit created event
        self.services.writer.write_events(self.source_id, [NewEvent(
            event_id=f"asana-{task_gid}-created-{full_task.get('created_at', '')}",
            event_type="asana.task_created",
            entity_id=task_gid,
            data={
                "task_gid": task_gid,
                "name": full_task.get("name"),
                "assignee": self._extract_assignee_name(full_task),
                "completed": full_task.get("completed", False),
                "project_gid": project_gid,
                "full_task": full_task,
            },
            occurred_at=self._parse_asana_date(full_task.get("created_at")),
        )])

        # If assignee is set, also emit assigned
        if full_task.get("assignee"):
            self.services.writer.write_events(self.source_id, [NewEvent(
                event_id=f"asana-{task_gid}-assigned-{full_task.get('modified_at', '')}",
                event_type="asana.task_assigned",
                entity_id=task_gid,
                data={
                    "task_gid": task_gid,
                    "name": full_task.get("name"),
                    "assignee": self._extract_assignee_name(full_task),
                    "project_gid": project_gid,
                },
                occurred_at=self._parse_asana_date(full_task.get("modified_at")),
            )])

        # Track comments baseline
        if self.config.track_comments:
            comments = await self._fetch_task_stories(task_gid)
            comment_gids = [c["gid"] for c in comments]
            self.services.kv.set(self.source_id, self._comments_kv_key(project_gid, task_gid), comment_gids)

    async def _handle_existing_task(self, project_gid: str, task_summary: Dict[str, Any]):
        task_gid = task_summary["gid"]
        cached_task = self.services.kv.get(self.source_id, self._kv_key(project_gid, task_gid))

        if not cached_task:
            await self._handle_new_task(project_gid, task_summary)
            return

        # Check if modified_at changed
        cached_modified = cached_task.get("modified_at")
        current_modified = task_summary.get("modified_at")

        if cached_modified != current_modified:
            logger.info(f"Asana task updated: {task_gid}")
            full_task = await self._fetch_task_detail(task_gid)
            diff = self._compute_diff(cached_task, full_task)

            if diff:
                # Emit generic update
                self.services.writer.write_events(self.source_id, [NewEvent(
                    event_id=f"asana-{task_gid}-updated-{full_task.get('modified_at', '')}",
                    event_type="asana.task_updated",
                    entity_id=task_gid,
                    data={
                        "task_gid": task_gid,
                        "name": full_task.get("name"),
                        "diff": diff,
                        "full_task": full_task,
                    },
                    occurred_at=self._parse_asana_date(full_task.get("modified_at")),
                )])

                # Detect assignee changes
                self._emit_assignee_events(task_gid, project_gid, cached_task, full_task)

                # Detect completion
                if not cached_task.get("completed") and full_task.get("completed"):
                    self.services.writer.write_events(self.source_id, [NewEvent(
                        event_id=f"asana-{task_gid}-completed-{full_task.get('modified_at', '')}",
                        event_type="asana.task_completed",
                        entity_id=task_gid,
                        data={
                            "task_gid": task_gid,
                            "name": full_task.get("name"),
                            "project_gid": project_gid,
                        },
                        occurred_at=self._parse_asana_date(full_task.get("modified_at")),
                    )])

            # Update cache
            self.services.kv.set(self.source_id, self._kv_key(project_gid, task_gid), full_task)

        # Check for new comments
        if self.config.track_comments:
            await self._check_new_comments(project_gid, task_gid)

    async def _handle_removed_task(self, project_gid: str, task_gid: str):
        logger.info(f"Asana task removed from project: {task_gid}")
        cached_task = self.services.kv.get(self.source_id, self._kv_key(project_gid, task_gid))

        data: Dict[str, Any] = {
            "task_gid": task_gid,
            "project_gid": project_gid,
            "last_known_state": cached_task,
        }
        if cached_task:
            data["name"] = cached_task.get("name")
            data["assignee"] = self._extract_assignee_name(cached_task)

        self.services.writer.write_events(self.source_id, [NewEvent(
            event_id=f"asana-{task_gid}-removed-{datetime.now(timezone.utc).isoformat()}",
            event_type="asana.task_removed",
            entity_id=task_gid,
            data=data,
            occurred_at=datetime.now(timezone.utc),
        )])

        self.services.kv.delete(self.source_id, self._kv_key(project_gid, task_gid))
        self.services.kv.delete(self.source_id, self._comments_kv_key(project_gid, task_gid))

    def _emit_assignee_events(self, task_gid: str, project_gid: str,
                               old_task: Dict[str, Any], new_task: Dict[str, Any]):
        old_assignee = (old_task.get("assignee") or {}).get("gid")
        new_assignee = (new_task.get("assignee") or {}).get("gid")

        if old_assignee == new_assignee:
            return

        if old_assignee and not new_assignee:
            # Unassigned
            self.services.writer.write_events(self.source_id, [NewEvent(
                event_id=f"asana-{task_gid}-unassigned-{new_task.get('modified_at', '')}",
                event_type="asana.task_unassigned",
                entity_id=task_gid,
                data={
                    "task_gid": task_gid,
                    "name": new_task.get("name"),
                    "previous_assignee": self._extract_assignee_name(old_task),
                    "project_gid": project_gid,
                },
                occurred_at=self._parse_asana_date(new_task.get("modified_at")),
            )])
        elif new_assignee:
            # Assigned (either from null or changed)
            self.services.writer.write_events(self.source_id, [NewEvent(
                event_id=f"asana-{task_gid}-assigned-{new_task.get('modified_at', '')}",
                event_type="asana.task_assigned",
                entity_id=task_gid,
                data={
                    "task_gid": task_gid,
                    "name": new_task.get("name"),
                    "assignee": self._extract_assignee_name(new_task),
                    "previous_assignee": self._extract_assignee_name(old_task),
                    "project_gid": project_gid,
                },
                occurred_at=self._parse_asana_date(new_task.get("modified_at")),
            )])

    async def _check_new_comments(self, project_gid: str, task_gid: str):
        comments = await self._fetch_task_stories(task_gid)
        current_comment_gids = [c["gid"] for c in comments]

        cached_comment_gids = self.services.kv.get(
            self.source_id, self._comments_kv_key(project_gid, task_gid)
        ) or []

        cached_set = set(cached_comment_gids)
        new_comments = [c for c in comments if c["gid"] not in cached_set]

        for comment in new_comments:
            self.services.writer.write_events(self.source_id, [NewEvent(
                event_id=f"asana-{task_gid}-comment-{comment['gid']}",
                event_type="asana.task_commented",
                entity_id=task_gid,
                data={
                    "task_gid": task_gid,
                    "comment_gid": comment["gid"],
                    "text": comment.get("text", ""),
                    "author": (comment.get("created_by") or {}).get("name"),
                    "project_gid": project_gid,
                },
                occurred_at=self._parse_asana_date(comment.get("created_at")),
            )])

        if new_comments:
            self.services.kv.set(
                self.source_id,
                self._comments_kv_key(project_gid, task_gid),
                current_comment_gids,
            )

    def _compute_diff(self, old_task: Dict[str, Any], new_task: Dict[str, Any]) -> Dict[str, Any]:
        diff = {}
        # Compare top-level scalar/simple fields
        compare_keys = {"name", "assignee", "due_on", "due_at", "start_on", "start_at",
                        "completed", "completed_at", "notes", "liked", "num_likes",
                        "num_subtasks"}

        for key in compare_keys:
            if key in self.config.ignored_fields:
                continue
            old_val = old_task.get(key)
            new_val = new_task.get(key)
            if old_val != new_val:
                diff[key] = {
                    "before": self._simplify_value(old_val),
                    "after": self._simplify_value(new_val),
                }

        # Compare custom fields
        old_cf = {cf["gid"]: cf for cf in (old_task.get("custom_fields") or [])}
        new_cf = {cf["gid"]: cf for cf in (new_task.get("custom_fields") or [])}
        all_cf_gids = set(old_cf.keys()) | set(new_cf.keys())

        for cf_gid in all_cf_gids:
            old_display = (old_cf.get(cf_gid) or {}).get("display_value")
            new_display = (new_cf.get(cf_gid) or {}).get("display_value")
            if old_display != new_display:
                cf_name = self.custom_field_map.get(cf_gid,
                    (new_cf.get(cf_gid) or old_cf.get(cf_gid) or {}).get("name", cf_gid))
                diff[cf_name] = {
                    "field_gid": cf_gid,
                    "before": old_display,
                    "after": new_display,
                }

        return diff

    @staticmethod
    def _simplify_value(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            if "name" in value:
                return value["name"]
            if "gid" in value:
                return value["gid"]
        if isinstance(value, list):
            return [AsanaSource._simplify_value(v) for v in value]
        return value

    @staticmethod
    def _extract_assignee_name(task: Dict[str, Any]) -> Optional[str]:
        assignee = task.get("assignee")
        if assignee and isinstance(assignee, dict):
            return assignee.get("name")
        return None

    @staticmethod
    def _parse_asana_date(date_str: Optional[str]) -> Optional[datetime]:
        if not date_str:
            return None
        try:
            # Asana uses ISO 8601: "2024-03-22T14:55:00.000Z"
            cleaned = date_str.replace("Z", "+00:00")
            return datetime.fromisoformat(cleaned)
        except (ValueError, TypeError):
            return None
