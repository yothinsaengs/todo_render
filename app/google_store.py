import base64
import json
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import Settings


TODO_HEADERS = [
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
    "due_at",
]
LEGACY_TODO_HEADERS = TODO_HEADERS[:-1]
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
STATUSES = ["inbox", "planned", "in_progress", "blocked", "done"]
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
                    if name == "todos" and actual_headers == LEGACY_TODO_HEADERS:
                        self._update_values("todos", 1, TODO_HEADERS)
                    elif actual_headers != headers:
                        raise StoreError(f"Unexpected schema in sheet: {name}")

            meta = self._read_meta()
            if "schema_version" not in meta:
                self._append("meta", ["schema_version", 3])
            else:
                self._set_meta("schema_version", 3)
            if "database_version" not in meta:
                self._append("meta", ["database_version", 0])
            if "last_updated_at" not in meta:
                self._append("meta", ["last_updated_at", _now_iso()])
            return {
                "spreadsheetId": self.spreadsheet_id,
                "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}",
                "schemaVersion": 3,
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
            if requested_status == "all" or task["status"] == requested_status
        ]
        filtered.sort(key=_focus_sort_key)
        page = filtered[offset : offset + limit]
        next_offset = offset + len(page)
        return {
            "items": [self._task_for_client(task) for task in page],
            "nextCursor": _encode_cursor(next_offset)
            if next_offset < len(filtered)
            else None,
            "databaseVersion": self.get_meta()["databaseVersion"],
        }

    def get_summary(self) -> dict[str, Any]:
        tasks = self._read_tasks()
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
            "completionPercent": round(done_this_week / denominator * 100)
            if denominator
            else 0,
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
                "due_date": due_at[:10]
                if due_at
                else _normalize_date(data.get("dueDate")),
                "tags_json": json.dumps(_normalize_tags(data.get("tags"))),
                "created_at": now,
                "updated_at": now,
                "completed_at": now if status == "done" else "",
                "version": 1,
                "due_at": due_at,
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

    def _read_tasks(self) -> list[dict[str, Any]]:
        tasks = self._read_objects("todos", TODO_HEADERS)
        for task in tasks:
            task["id"] = str(task.get("id", ""))
            task["status"] = str(task.get("status") or "inbox")
            task["priority"] = str(task.get("priority") or "P3")
            task["due_date"] = str(task.get("due_date") or "")[:10]
            task["due_at"] = str(task.get("due_at") or "")
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
        if not str(data.get("title") or "").strip():
            raise StoreError("Title is required.")
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
        else 1
        if task["status"] != "done" and task.get("due_date") == today
        else 2
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
