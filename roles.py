"""Illuminate access and roles from Cognito groups.

A user needs ACCESS_GROUP to use Illuminate at all; within it the role is Admin, Author, Developer,
or Viewer when in none of the role groups. Platform role groups count as the Illuminate group they map to.
"""

from typing import Literal

Role = Literal["admin", "author", "developer", "viewer"]

ACCESS_GROUP = "illuminate-users"

# Highest first: a user in several groups takes the first role listed here.
GROUP_ROLES: dict[str, Role] = {
    "illuminate-admins": "admin",
    "illuminate-authors": "author",
    "illuminate-developers": "developer",
}

# Written only by the platform identity service; each grants the Illuminate role group it names.
PLATFORM_GROUPS: dict[str, str] = {
    "platform-system-admins": "illuminate-admins",
    "platform-program-admins": "illuminate-authors",
}


def groups_of(user: dict) -> set[str]:
    """The user's Cognito groups plus the Illuminate group each platform role group stands for."""
    groups = set(user.get("cognito:groups") or [])
    return groups | {PLATFORM_GROUPS[g] for g in groups & PLATFORM_GROUPS.keys()}


def has_access(user: dict) -> bool:
    return ACCESS_GROUP in groups_of(user)


def role_of(user: dict) -> Role:
    groups = groups_of(user)
    return next((role for group, role in GROUP_ROLES.items() if group in groups), "viewer")


def role_allows_identity(user: dict) -> bool:
    """Viewers see counts only; every other role may select personally identifiable dimensions."""
    return role_of(user) != "viewer"
