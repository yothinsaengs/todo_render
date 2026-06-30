import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv('/Users/yothinsaengsakoon/Documents/todo_render/.env.example')


@dataclass(frozen=True)
class Settings:
    username: str
    password_hash: str
    session_secret: str
    spreadsheet_id: str
    google_service_account_info: dict | None
    google_service_account_file: str | None
    cookie_secure: bool
    session_hours: int
    activity_log_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        raw_credentials = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        credentials_info = json.loads(raw_credentials) if raw_credentials else None
        return cls(
            username=os.getenv("APP_USERNAME", "").strip(),
            password_hash=os.getenv("APP_PASSWORD_HASH", "").strip(),
            session_secret=os.getenv("SESSION_SECRET", "").strip(),
            spreadsheet_id=os.getenv("SPREADSHEET_ID", "").strip(),
            google_service_account_info=credentials_info,
            google_service_account_file=os.getenv(
                "GOOGLE_SERVICE_ACCOUNT_FILE", ""
            ).strip()
            or None,
            cookie_secure=os.getenv("COOKIE_SECURE", "true").lower()
            not in {"0", "false", "no"},
            session_hours=max(1, int(os.getenv("SESSION_HOURS", "168"))),
            activity_log_enabled=os.getenv("ACTIVITY_LOG_ENABLED", "false").lower()
            in {"1", "true", "yes"},
        )

    def auth_error(self) -> str | None:
        missing = [
            name
            for name, value in (
                ("APP_USERNAME", self.username),
                ("APP_PASSWORD_HASH", self.password_hash),
                ("SESSION_SECRET", self.session_secret),
            )
            if not value
        ]
        return (
            f"Missing environment variables: {', '.join(missing)}" if missing else None
        )

    def google_error(self) -> str | None:
        missing = [] if self.spreadsheet_id else ["SPREADSHEET_ID"]
        if (
            not self.google_service_account_info
            and not self.google_service_account_file
        ):
            missing.append("GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_FILE")
        if (
            self.google_service_account_file
            and not Path(self.google_service_account_file).is_file()
        ):
            return "GOOGLE_SERVICE_ACCOUNT_FILE does not exist"
        return (
            f"Missing environment variables: {', '.join(missing)}" if missing else None
        )
