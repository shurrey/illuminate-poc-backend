"""
DynamoDB-backed conversation memory.

Stores message history per context_id with automatic TTL expiry.
Replaces AgentCore STM memory at ~$0.25/month instead of AgentCore pricing.
"""
import json
import os
import time
import logging
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

logger = logging.getLogger("API-PROXY")

_TABLE_NAME = os.environ.get("CONVERSATION_TABLE", "illuminate-conversations-dev")
_TTL_SECONDS = int(os.environ.get("CONVERSATION_TTL", str(30 * 24 * 3600)))  # 30 days
_MAX_MESSAGES = int(os.environ.get("CONVERSATION_MAX_MESSAGES", "50"))

_table = None


def _get_table():
    """Lazy-init DynamoDB Table resource."""
    global _table
    if _table is None:
        dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        _table = dynamodb.Table(_TABLE_NAME)
    return _table


def _item(context_id: str) -> Optional[dict]:
    return _get_table().get_item(Key={"context_id": context_id}).get("Item")


def owns(context_id: str, owner: str) -> bool:
    """True when the conversation exists and was created by owner (a Cognito sub)."""
    try:
        item = _item(context_id)
    except Exception as e:
        logger.warning(f"Failed to read conversation owner: {e}")
        return False
    return bool(item) and item.get("owner_sub") == owner


def load_history(context_id: str, owner: str) -> list[dict]:
    """Messages for context_id, oldest first; empty if missing or owned by someone else.

    Returns list of {"role": "user"|"assistant", "content": "..."} dicts.
    """
    if not context_id:
        return []
    try:
        item = _item(context_id)
        if not item or item.get("owner_sub") != owner:
            return []
        messages = json.loads(item.get("messages", "[]"))
        return messages[-_MAX_MESSAGES:]
    except Exception as e:
        logger.warning(f"Failed to load conversation history: {e}")
        return []


def save_turn(context_id: str, owner: str, user_message: str, assistant_message: str):
    """Append a turn; never writes over a conversation that belongs to someone else.

    Items without an owner predate ownership tracking; the first writer claims them.
    """
    if not context_id:
        return
    try:
        history = load_history(context_id, owner)
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": assistant_message})
        history = history[-_MAX_MESSAGES:]

        _get_table().put_item(
            Item={
                "context_id": context_id,
                "owner_sub": owner,
                "messages": json.dumps(history),
                "updated_at": int(time.time()),
                "ttl": int(time.time()) + _TTL_SECONDS,
            },
            ConditionExpression=Attr("owner_sub").not_exists() | Attr("owner_sub").eq(owner),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning("Refused to save turn: context %s belongs to another user", context_id)
        else:
            logger.warning(f"Failed to save conversation history: {e}")
    except Exception as e:
        logger.warning(f"Failed to save conversation history: {e}")


def clear_history(context_id: str, owner: str) -> bool:
    """Delete the conversation if owner owns it; False when it is missing or someone else's."""
    if not context_id:
        return False
    try:
        _get_table().delete_item(
            Key={"context_id": context_id},
            ConditionExpression=Attr("owner_sub").eq(owner),
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning(f"Failed to clear conversation history: {e}")
        return False
