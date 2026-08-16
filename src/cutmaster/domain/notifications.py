"""In-application notification values."""

from enum import StrEnum


class NotificationStatus(StrEnum):
    UNREAD = "unread"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


__all__ = ["NotificationStatus"]
