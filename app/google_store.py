import base64
import json
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings


TODO_HEADERS_V2 = [
    "id",
    "title",
    "details",
    "status",
    "priority",
    "due_date",
    "tags_json",
    "created_at",
    "updated_at",
    "completed_at",
    "version",
]
TODO_HEADERS_V3 = TODO_HEADERS_V2 + ["due_at"]
TODO_HEADERS = TODO_HEADERS_V3 + [
    "merged_into_id",
    "merge_id",
    "merged_from_status",
    "merged_at",
]
META_HEADERS = ["key", "value"]
ACTIVITY_HEADERS = [
    "event_id",
    "task_id",
    "action",
    "task_version",
    "database_version",
    "changed_at",
    "snapshot_json",
]
MERGE_LOG_HEADERS = [
    "merge_id",
    "target_id",
    "target_before_json",
    "source_ids_json",
    "post_versions_json",
    "created_at",
    "undone_at",
]
STATUSES = ["inbox", "planned", "in_progress", "blocked", "done", "removed"]
PRIORITIES = ["P1", "P2", "P3", "P4"]
BANGKOK = ZoneInfo("Asia/Bangkok")


class StoreError(Exception):
    pass


class GoogleStore:
    def __init__(self, settings: Settings):
        configuration_error = settings.google_error()
        if configuration_error:
            raise StoreError(configuration_error)

        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if settings.google_service_account_info:
            credentials = service_account.Credentials.from_service_account_info(
                settings.google_service_account_info, scopes=scopes
            )
        else:
            credentials = service_account.Credentials.from_service_account_file(
                settings.google_service_account_file, scopes=scopes
            )
        self.sheets = build(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )
        self.spreadsheet_id = settings.spreadsheet_id
        self.activity_log_enabled = settings.activity_log_enabled
        self._lock = threading.RLock()

    def dispatch(self, action: str, payload: dict[str, Any]) -> Any:
        handlers = {
            "setup": lambda _: self.setup(),
            "meta": lambda _: self.get_meta(),
            "summary": lambda _: self.get_summary(),
            "list": self.list_tasks,
            "create": self.create_task,
            "update": self.update_task,
            "remove": self.remove_task,
            "merge": self.merge_tasks,
            "undoMerge": self.undo_merge,
            "bulkUpdateStatus": self.bulk_update_status,
        }
        handler = handlers.get(action)
        if not handler:
            raise StoreError(f"Unknown action: {action}")
        return handler(payload or {})

    def setup(self) -> dict[str, Any]:
        with self._lock:
            metadata = (
                self.sheets.spreadsheets()
                .get(
                    spreadsheetId=self.spreadsheet_id,
                    fields="properties.title,sheets.properties",
                )
                .execute()
            )
            existing = {
                sheet["properties"]["title"] for sheet in metadata.get("sheets", [])
            }
            required = {
                "todos": TODO_HEADERS,
                "meta": META_HEADERS,
                "activity_log": ACTIVITY_HEADERS,
                "merge_log": MERGE_LOG_HEADERS,
            }
            requests = [
                {"addSheet": {"properties": {"title": name}}}
                for name in required
                if name not in existing
            ]
            if requests:
                self.sheets.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id, body={"requests": requests}
                ).execute()
            for name, headers in required.items():
                rows = self._values(name)
                if not rows:
                    self._update_values(name, 1, headers)
                else:
                    actual_headers = [str(value) for value in rows[0]]
                    if name == "todos" and tuple(actual_headers) in {
                        tuple(TODO_HEADERS_V2),
                        tuple(TODO_HEADERS_V3),
                    }:
                        self._update_values("todos", 1, TODO_HEADERS)
                    elif actual_headers != headers:
                        raise StoreError(f"Unexpected schema in sheet: {name}")

            meta = self._read_meta()
            if "schema_version" not in meta:
                self._append("meta", ["schema_version", 4])
            else:
                self._set_meta("schema_version", 4)
            if "database_version" not in meta:
                self._append("meta", ["database_version", 0])
            if "last_updated_at" not in meta:
                self._append("meta", ["last_updated_at", _now_iso()])
            return {
                "spreadsheetId": self.spreadsheet_id,
                "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}",
                "schemaVersion": 4,
                "databaseVersion": int(self._read_meta().get("database_version", 0)),
            }

    def get_meta(self) -> dict[str, Any]:
        meta = self._read_meta()
        return {
            "schemaVersion": int(meta.get("schema_version", 0)),
            "databaseVersion": int(meta.get("database_version", 0)),
            "lastUpdatedAt": meta.get("last_updated_at") or None,
            "serverTime": _now_iso(),
        }

    def list_tasks(self, options: dict[str, Any]) -> dict[str, Any]:
        tasks = self._read_tasks()
        requested_status = str(options.get("status") or "all").lower()
        limit = max(1, min(_int(options.get("limit"), 50), 100))
        offset = _decode_cursor(options.get("cursor"))
        filtered = [
            task
            for task in tasks
            if (requested_status == "all" and task["status"] != "removed")
            or task["status"] == requested_status
        ]
        filtered.sort(key=_focus_sort_key)
        page = filtered[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "items": [self._task_for_client(task) for task in page],
            "nextCursor": (
                _encode_cursor(next_offset) if next_offset < len(filtered) else None
            ),
            "databaseVersion": self.get_meta()["databaseVersion"],
        }

    def get_summary(self) -> dict[str, Any]:
        tasks = [task for task in self._read_tasks() if task["status"] != "removed"]
        today = datetime.now(BANGKOK).date()
        monday = today - timedelta(days=today.weekday())
        next_monday = monday + timedelta(days=7)
        active = [task for task in tasks if task["status"] != "done"]
        done_this_week = sum(
            1
            for task in tasks
            if task.get("completed_at")
            and monday <= _as_bangkok_date(task["completed_at"]) < next_monday
        )
        unfinished_due = sum(
            1
            for task in active
            if task.get("due_date")
            and monday <= date.fromisoformat(task["due_date"]) < next_monday
        )
        denominator = done_this_week + unfinished_due
        today_text = today.isoformat()
        now = datetime.now(BANGKOK)
        return {
            "dueToday": sum(task.get("due_date") == today_text for task in active),
            "overdue": sum(_is_overdue(task, now) for task in active),
            "inProgress": sum(task["status"] == "in_progress" for task in tasks),
            "doneThisWeek": done_this_week,
            "completionPercent": (
                round(done_this_week / denominator * 100) if denominator else 0
            ),
            "today": today_text,
            "weekStartsAt": monday.isoformat(),
        }

    def create_task(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            _validate_task_input(data, partial=False)
            now = _now_iso()
            status = _normalize_status(data.get("status") or "inbox")
            due_at = _normalize_due_at(data.get("dueAt"))
            task = {
                "id": str(uuid.uuid4()),
                "title": str(data["title"]).strip(),
                "details": str(data.get("details") or "").strip(),
                "status": status,
                "priority": _normalize_priority(data.get("priority") or "P3"),
                "due_date": (
                    due_at[:10] if due_at else _normalize_date(data.get("dueDate"))
                ),
                "tags_json": json.dumps(_normalize_tags(data.get("tags"))),
                "created_at": now,
                "updated_at": now,
                "completed_at": now if status == "done" else "",
                "version": 1,
                "due_at": due_at,
                "merged_into_id": "",
                "merge_id": "",
                "merged_from_status": "",
                "merged_at": "",
            }
            self._append("todos", [task[header] for header in TODO_HEADERS])
            database_version = self._bump_database_version(now)
            self._log_activity("create", task, database_version)
            return {
                "item": self._task_for_client(task),
                "databaseVersion": database_version,
            }

    def update_task(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            row_number, current = self._find_task(data.get("id"))
            _assert_version(current, data.get("version"))
            _validate_task_input(data, partial=True)
            task = dict(current)
            mappings = {
                "title": ("title", lambda value: str(value).strip()),
                "details": ("details", lambda value: str(value or "").strip()),
                "status": ("status", _normalize_status),
                "priority": ("priority", _normalize_priority),
                "tags": ("tags_json", lambda value: json.dumps(_normalize_tags(value))),
            }
            for input_key, (task_key, transform) in mappings.items():
                if input_key in data:
                    task[task_key] = transform(data[input_key])
            if "dueAt" in data:
                task["due_at"] = _normalize_due_at(data["dueAt"])
                task["due_date"] = task["due_at"][:10] if task["due_at"] else ""
            elif "dueDate" in data:
                task["due_date"] = _normalize_date(data["dueDate"])
                task["due_at"] = ""
            task["updated_at"] = _now_iso()
            task["completed_at"] = (
                task.get("completed_at") or task["updated_at"]
                if task["status"] == "done"
                else ""
            )
            task["version"] = int(current["version"]) + 1
            self._update_values(
                "todos", row_number, [task[header] for header in TODO_HEADERS]
            )
            database_version = self._bump_database_version(task["updated_at"])
            self._log_activity("update", task, database_version)
            return {
                "item": self._task_for_client(task),
                "databaseVersion": database_version,
            }

    def remove_task(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            row_number, current = self._find_task(data.get("id"))
            _assert_version(current, data.get("version"))
            if current["status"] == "removed":
                raise StoreError("Task is already removed.")
            task = dict(current)
            task.update(
                status="removed",
                updated_at=_now_iso(),
                completed_at="",
                version=int(current["version"]) + 1,
            )
            self._update_values(
                "todos", row_number, [task[header] for header in TODO_HEADERS]
            )
            database_version = self._bump_database_version(task["updated_at"])
            self._log_activity("remove", task, database_version)
            return {
                "item": self._task_for_client(task),
                "databaseVersion": database_version,
            }

    def merge_tasks(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            source_specs = data.get("sources")
            if not isinstance(source_specs, list) or not 1 <= len(source_specs) <= 20:
                raise StoreError("Merge requires between 1 and 20 source tasks.")

            target_id = str(data.get("targetId") or "").strip()
            source_ids = [str(spec.get("id") or "").strip() for spec in source_specs]
            if not target_id or any(not source_id for source_id in source_ids):
                raise StoreError("Target and source task ids are required.")
            if target_id in source_ids or len(set(source_ids)) != len(source_ids):
                raise StoreError("Merge target and sources must be unique.")

            records = self._task_record_map()
            if target_id not in records or any(
                source_id not in records for source_id in source_ids
            ):
                raise StoreError("One or more merge tasks were not found.")
            target_row, current_target = records[target_id]
            _assert_version(current_target, data.get("targetVersion"))
            if current_target["status"] == "removed":
                raise StoreError("A removed task cannot be a merge target.")

            sources = []
            for spec, source_id in zip(source_specs, source_ids):
                row_number, source = records[source_id]
                _assert_version(source, spec.get("version"))
                if source["status"] == "removed":
                    raise StoreError("Removed tasks cannot be merged again.")
                sources.append((row_number, source))

            now = _now_iso()
            merge_id = str(uuid.uuid4())
            target_before = dict(current_target)
            target = dict(current_target)
            merged_priority = min(
                [target] + [source for _, source in sources],
                key=lambda task: PRIORITIES.index(task["priority"]),
            )["priority"]
            merged_due_date, merged_due_at = _earliest_due(
                [target] + [source for _, source in sources]
            )
            merged_tags = _merged_tags([target] + [source for _, source in sources])
            target.update(
                details=_merged_details(target, [source for _, source in sources], now),
                priority=merged_priority,
                due_date=merged_due_date,
                due_at=merged_due_at,
                tags_json=json.dumps(merged_tags),
                updated_at=now,
                version=int(target["version"]) + 1,
            )

            updates = [
                ("todos", target_row, [target[header] for header in TODO_HEADERS])
            ]
            post_versions = {target_id: target["version"]}
            removed_ids = []
            for row_number, source in sources:
                merged_source = dict(source)
                merged_source.update(
                    status="removed",
                    completed_at="",
                    updated_at=now,
                    version=int(source["version"]) + 1,
                    merged_into_id=target_id,
                    merge_id=merge_id,
                    merged_from_status=source["status"],
                    merged_at=now,
                )
                updates.append(
                    (
                        "todos",
                        row_number,
                        [merged_source[header] for header in TODO_HEADERS],
                    )
                )
                post_versions[source["id"]] = merged_source["version"]
                removed_ids.append(source["id"])

            merge_log_row = len(self._values("merge_log")) + 1
            log_values = [
                merge_id,
                target_id,
                json.dumps(target_before, separators=(",", ":")),
                json.dumps(source_ids),
                json.dumps(post_versions),
                now,
                "",
            ]
            updates.append(("merge_log", merge_log_row, log_values))
            database_version = self._batch_write(updates, now)
            return {
                "item": self._task_for_client(target),
                "removedIds": removed_ids,
                "mergeId": merge_id,
                "databaseVersion": database_version,
                "undoUntil": (datetime.now(timezone.utc) + timedelta(seconds=15))
                .isoformat()
                .replace("+00:00", "Z"),
            }

    def undo_merge(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merge_id = str(data.get("mergeId") or "").strip()
            if not merge_id:
                raise StoreError("Merge id is required.")
            log_row, merge_log = self._find_merge_log(merge_id)
            if merge_log.get("undone_at"):
                raise StoreError("This merge was already undone.")

            try:
                target_before = json.loads(str(merge_log["target_before_json"]))
                source_ids = json.loads(str(merge_log["source_ids_json"]))
                post_versions = json.loads(str(merge_log["post_versions_json"]))
            except (TypeError, ValueError) as error:
                raise StoreError("Merge history is invalid.") from error

            target_id = str(merge_log["target_id"])
            task_ids = [target_id] + [str(source_id) for source_id in source_ids]
            records = self._task_record_map()
            if any(task_id not in records for task_id in task_ids):
                raise StoreError("A merged task no longer exists.")
            for task_id in task_ids:
                _, task = records[task_id]
                if int(task["version"]) != int(post_versions.get(task_id, -1)):
                    raise StoreError(
                        "VERSION_CONFLICT: A merged task changed after the merge."
                    )

            now = _now_iso()
            target_row, current_target = records[target_id]
            restored_target = {
                header: target_before.get(header, "") for header in TODO_HEADERS
            }
            restored_target.update(
                updated_at=now,
                version=int(current_target["version"]) + 1,
            )
            updates = [
                (
                    "todos",
                    target_row,
                    [restored_target[header] for header in TODO_HEADERS],
                )
            ]
            restored_items = [self._task_for_client(restored_target)]

            for source_id in source_ids:
                row_number, current_source = records[str(source_id)]
                if current_source.get("merge_id") != merge_id:
                    raise StoreError("A source task is no longer part of this merge.")
                restored_source = dict(current_source)
                restored_source.update(
                    status=current_source.get("merged_from_status") or "inbox",
                    updated_at=now,
                    version=int(current_source["version"]) + 1,
                    merged_into_id="",
                    merge_id="",
                    merged_from_status="",
                    merged_at="",
                )
                updates.append(
                    (
                        "todos",
                        row_number,
                        [restored_source[header] for header in TODO_HEADERS],
                    )
                )
                restored_items.append(self._task_for_client(restored_source))

            merge_log["undone_at"] = now
            updates.append(
                (
                    "merge_log",
                    log_row,
                    [merge_log.get(header, "") for header in MERGE_LOG_HEADERS],
                )
            )
            database_version = self._batch_write(updates, now)
            return {
                "items": restored_items,
                "mergeId": merge_id,
                "databaseVersion": database_version,
            }

    def bulk_update_status(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            specs = data.get("tasks")
            if not isinstance(specs, list) or not 1 <= len(specs) <= 20:
                raise StoreError("Bulk move requires between 1 and 20 tasks.")
            status = _normalize_status(data.get("status"))
            if status == "removed":
                raise StoreError("Use the remove action to remove tasks.")

            task_ids = [str(spec.get("id") or "").strip() for spec in specs]
            if any(not task_id for task_id in task_ids) or len(set(task_ids)) != len(
                task_ids
            ):
                raise StoreError("Bulk move task ids must be present and unique.")
            records = self._task_record_map()
            if any(task_id not in records for task_id in task_ids):
                raise StoreError("One or more bulk move tasks were not found.")

            now = _now_iso()
            updates = []
            items = []
            for spec, task_id in zip(specs, task_ids):
                row_number, current = records[task_id]
                _assert_version(current, spec.get("version"))
                if current["status"] == "removed":
                    raise StoreError("Removed tasks cannot be moved.")
                task = dict(current)
                if task["status"] != status:
                    task.update(
                        status=status,
                        updated_at=now,
                        completed_at=(
                            task.get("completed_at") or now if status == "done" else ""
                        ),
                        version=int(task["version"]) + 1,
                    )
                    updates.append(
                        ("todos", row_number, [task[header] for header in TODO_HEADERS])
                    )
                items.append(self._task_for_client(task))

            if updates:
                database_version = self._batch_write(updates, now)
            else:
                database_version = self.get_meta()["databaseVersion"]
            return {"items": items, "databaseVersion": database_version}

    def _read_tasks(self) -> list[dict[str, Any]]:
        tasks = self._read_objects("todos", TODO_HEADERS)
        for task in tasks:
            task["id"] = str(task.get("id", ""))
            task["status"] = str(task.get("status") or "inbox")
            task["priority"] = str(task.get("priority") or "P3")
            task["due_date"] = str(task.get("due_date") or "")[:10]
            task["due_at"] = str(task.get("due_at") or "")
            task["merged_into_id"] = str(task.get("merged_into_id") or "")
            task["merge_id"] = str(task.get("merge_id") or "")
            task["merged_from_status"] = str(task.get("merged_from_status") or "")
            task["merged_at"] = str(task.get("merged_at") or "")
            task["version"] = _int(task.get("version"), 1)
        return tasks

    def _find_task(self, task_id: Any) -> tuple[int, dict[str, Any]]:
        task_id = str(task_id or "").strip()
        if not task_id:
            raise StoreError("Task id is required.")
        for index, task in enumerate(self._read_tasks(), start=2):
            if task["id"] == task_id:
                return index, task
        raise StoreError("Task not found.")

    def _task_record_map(self) -> dict[str, tuple[int, dict[str, Any]]]:
        return {
            task["id"]: (row_number, task)
            for row_number, task in enumerate(self._read_tasks(), start=2)
        }

    def _find_merge_log(self, merge_id: str) -> tuple[int, dict[str, Any]]:
        for row_number, record in enumerate(
            self._read_objects("merge_log", MERGE_LOG_HEADERS), start=2
        ):
            if str(record.get("merge_id")) == merge_id:
                return row_number, record
        raise StoreError("Merge history was not found.")

    def _task_for_client(self, task: dict[str, Any]) -> dict[str, Any]:
        try:
            tags = _normalize_tags(json.loads(str(task.get("tags_json") or "[]")))
        except (ValueError, TypeError):
            tags = []
        return {
            "id": str(task["id"]),
            "title": str(task["title"]),
            "details": str(task.get("details") or ""),
            "status": str(task.get("status") or "inbox"),
            "priority": str(task.get("priority") or "P3"),
            "dueDate": str(task.get("due_date") or "")[:10],
            "dueAt": str(task.get("due_at") or "") or None,
            "tags": tags,
            "createdAt": _to_iso(task.get("created_at")),
            "updatedAt": _to_iso(task.get("updated_at")),
            "completedAt": _to_iso(task.get("completed_at")) or None,
            "version": _int(task.get("version"), 1),
        }

    def _values(self, sheet: str) -> list[list[Any]]:
        return (
            self.sheets.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=f"'{sheet}'")
            .execute()
            .get("values", [])
        )

    def _read_objects(self, sheet: str, headers: list[str]) -> list[dict[str, Any]]:
        rows = self._values(sheet)
        return [
            dict(zip(headers, row + [""] * (len(headers) - len(row))))
            for row in rows[1:]
        ]

    def _append(self, sheet: str, row: list[Any]) -> None:
        self.sheets.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet}'!A:A",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()

    def _update_values(self, sheet: str, row: int, values: list[Any]) -> None:
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{sheet}'!A{row}",
            valueInputOption="RAW",
            body={"values": [values]},
        ).execute()

    def _batch_write(
        self, updates: list[tuple[str, int, list[Any]]], updated_at: str
    ) -> int:
        meta_rows = self._values("meta")
        meta_indexes = {
            str(row[0]): row_number
            for row_number, row in enumerate(meta_rows[1:], start=2)
            if row
        }
        if (
            "database_version" not in meta_indexes
            or "last_updated_at" not in meta_indexes
        ):
            raise StoreError("Meta sheet is incomplete. Run setup first.")
        meta = {str(row[0]): row[1] for row in meta_rows[1:] if len(row) >= 2}
        database_version = int(meta.get("database_version", 0)) + 1
        all_updates = updates + [
            (
                "meta",
                meta_indexes["database_version"],
                ["database_version", database_version],
            ),
            (
                "meta",
                meta_indexes["last_updated_at"],
                ["last_updated_at", updated_at],
            ),
        ]
        self.sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=self.spreadsheet_id,
            body={
                "valueInputOption": "RAW",
                "data": [
                    {"range": f"'{sheet}'!A{row}", "values": [values]}
                    for sheet, row, values in all_updates
                ],
            },
        ).execute()
        return database_version

    def _read_meta(self) -> dict[str, Any]:
        return {
            str(row[0]): row[1] for row in self._values("meta")[1:] if len(row) >= 2
        }

    def _set_meta(self, key: str, value: Any) -> None:
        rows = self._values("meta")
        for index, row in enumerate(rows[1:], start=2):
            if row and str(row[0]) == key:
                self._update_values("meta", index, [key, value])
                return
        self._append("meta", [key, value])

    def _bump_database_version(self, updated_at: str) -> int:
        next_version = int(self._read_meta().get("database_version", 0)) + 1
        self._set_meta("database_version", next_version)
        self._set_meta("last_updated_at", updated_at)
        return next_version

    def _log_activity(
        self,
        action: str,
        task: dict[str, Any],
        database_version: int,
    ) -> None:
        if not self.activity_log_enabled:
            return
        self._append(
            "activity_log",
            [
                str(uuid.uuid4()),
                task["id"],
                action,
                task["version"],
                database_version,
                task["updated_at"],
                json.dumps(self._task_for_client(task)),
            ],
        )


def _validate_task_input(data: dict[str, Any], partial: bool) -> None:
    if not partial or "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            raise StoreError("Title is required.")
        if len(title) > 200:
            raise StoreError("Title must be 200 characters or fewer.")
    if "details" in data and len(str(data.get("details") or "")) > 20000:
        raise StoreError("Details must be 20,000 characters or fewer.")
    if "status" in data:
        _normalize_status(data["status"])
    if "priority" in data:
        _normalize_priority(data["priority"])
    if "dueDate" in data:
        _normalize_date(data["dueDate"])
    if "dueAt" in data:
        _normalize_due_at(data["dueAt"])
    if "tags" in data:
        _normalize_tags(data["tags"])


def _task_tags(task: dict[str, Any]) -> list[str]:
    try:
        return _normalize_tags(json.loads(str(task.get("tags_json") or "[]")))
    except (TypeError, ValueError):
        return []


def _merged_tags(tasks: list[dict[str, Any]]) -> list[str]:
    result = []
    for task in tasks:
        for tag in _task_tags(task):
            if tag not in result and len(result) < 8:
                result.append(tag)
    return result


def _earliest_due(tasks: list[dict[str, Any]]) -> tuple[str, str]:
    candidates = []
    for task in tasks:
        due_at = str(task.get("due_at") or "")
        due_date = str(task.get("due_date") or "")[:10]
        try:
            if due_at:
                candidates.append(
                    (_parse_due_at(due_at), due_date or due_at[:10], due_at)
                )
            elif due_date:
                candidates.append(
                    (
                        datetime.fromisoformat(f"{due_date}T23:59:00+07:00"),
                        due_date,
                        "",
                    )
                )
        except ValueError:
            continue
    if not candidates:
        return "", ""
    _, due_date, due_at = min(candidates, key=lambda candidate: candidate[0])
    return due_date, due_at


def _merged_details(
    target: dict[str, Any], sources: list[dict[str, Any]], merged_at: str
) -> str:
    date_label = _parse_due_at(merged_at).astimezone(BANGKOK).strftime("%d %b %Y %H:%M")
    lines = [f"— Merged {len(sources)} ticket(s) · {date_label} BKK —"]
    for source in sources:
        title = " ".join(str(source.get("title") or "Untitled").split())[:200]
        details = str(source.get("details") or "").strip()
        summary = " ".join(details.split())[:500]
        line = f"• [{source.get('priority') or 'P3'}] {title}"
        if summary:
            line += f" — {summary}"
        lines.append(line)
    existing = str(target.get("details") or "").rstrip()
    combined = "\n\n".join(part for part in [existing, "\n".join(lines)] if part)
    if len(combined) <= 20000:
        return combined
    suffix = "\n… Additional merged content remains in the removed source tickets."
    return combined[: 20000 - len(suffix)].rstrip() + suffix


def _normalize_status(value: Any) -> str:
    normalized = str(value or "").lower()
    if normalized not in STATUSES:
        raise StoreError("Invalid task status.")
    return normalized


def _normalize_priority(value: Any) -> str:
    normalized = str(value or "").upper()
    if normalized not in PRIORITIES:
        raise StoreError("Invalid priority.")
    return normalized


def _normalize_date(value: Any) -> str:
    if not value:
        return ""
    normalized = str(value)[:10]
    try:
        date.fromisoformat(normalized)
    except ValueError as error:
        raise StoreError("Due date must use YYYY-MM-DD.") from error
    return normalized


def _normalize_due_at(value: Any) -> str:
    if not value:
        return ""
    try:
        parsed = _parse_due_at(value)
    except ValueError as error:
        raise StoreError("Due time must use YYYY-MM-DDTHH:MM.") from error
    return parsed.astimezone(BANGKOK).isoformat(timespec="minutes")


def _parse_due_at(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=BANGKOK)


def _normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise StoreError("Tags must be an array.")
    result = []
    for tag in value:
        normalized = str(tag or "").strip().lower()
        if normalized and normalized not in result and len(result) < 8:
            result.append(normalized)
    return result


def _assert_version(task: dict[str, Any], expected: Any) -> None:
    try:
        expected_version = int(expected)
    except (ValueError, TypeError):
        raise StoreError("An integer task version is required.")
    if int(task["version"]) != expected_version:
        raise StoreError(
            "VERSION_CONFLICT: This task changed elsewhere. Refresh and try again."
        )


def _focus_sort_key(task: dict[str, Any]) -> tuple[Any, ...]:
    now = datetime.now(BANGKOK)
    today = now.date().isoformat()
    score = (
        0
        if _is_overdue(task, now)
        else 1 if task["status"] != "done" and task.get("due_date") == today else 2
    )
    return (
        score,
        PRIORITIES.index(task["priority"]),
        task.get("due_at") or task.get("due_date") or "9999-99-99",
        _Reverse(str(task.get("updated_at") or "")),
    )


def _is_overdue(task: dict[str, Any], now: datetime) -> bool:
    if task.get("status") == "done":
        return False
    if task.get("due_at"):
        try:
            return _parse_due_at(task["due_at"]) < now
        except ValueError:
            return False
    return bool(task.get("due_date") and task["due_date"] < now.date().isoformat())


class _Reverse(str):
    def __lt__(self, other: object) -> bool:
        return str.__gt__(self, other)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _to_iso(value: Any) -> str:
    return str(value or "")


def _as_bangkok_date(value: Any) -> date:
    return (
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        .astimezone(BANGKOK)
        .date()
    )


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(cursor: Any) -> int:
    if not cursor:
        return 0
    try:
        value = int(
            base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4))
        )
        return max(0, value)
    except (ValueError, TypeError):
        return 0
