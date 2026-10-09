"""Illuminate roles from Cognito groups: Admin, Author, Developer, or Viewer when in none of them."""

from typing import Literal

Role = Literal["admin", "author", "developer", "viewer"]

# Highest first: a user in several groups takes the first role listed here.
GROUP_ROLES: dict[str, Role] = {
    "illuminate-admins": "admin",
    "illuminate-authors": "author",
    "illuminate-developers": "developer",
}


def role_of(user: dict) -> Role:
    groups = set(user.get("cognito:groups") or [])
    return next((role for group, role in GROUP_ROLES.items() if group in groups), "viewer")


def role_allows_identity(user: dict) -> bool:
    """Viewers see counts only; every other role may select personally identifiable dimensions."""
    return role_of(user) != "viewer"
