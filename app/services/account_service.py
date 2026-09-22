"""Account lifecycle: creation, lookup, listing, and SYSTEM provisioning.

Business logic lives here rather than in the route module, so the routes
stay to validate / call / shape (DESIGN.md).
"""

from __future__ import annotations

from typing import Any

from bson import ObjectId
from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError

from app.core import db
from app.core.errors import AccountNotFoundError
from app.core.logging_config import get_logger
from app.core.security import AuthenticatedUser
from app.models.account import (
    AccountDocument,
    AccountStatus,
    AccountType,
)
from app.models.common import to_object_id

logger = get_logger(__name__)

#: How SYSTEM boundary accounts are labelled, one per supported currency.
SYSTEM_ACCOUNT_LABEL_TEMPLATE = "Ledger boundary ({currency})"

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


async def create_account(
    *,
    owner_id: ObjectId,
    currency: str,
    label: str,
) -> dict[str, Any]:
    """Open a new USER account.

    `account_type` is not a parameter: it is always USER. SYSTEM accounts
    skip the overdraft check, so letting a caller ask for one would be
    letting them mint money.
    """
    account = AccountDocument(
        owner_id=owner_id,
        currency=currency,
        label=label,
        account_type=AccountType.USER,
        status=AccountStatus.ACTIVE,
    )
    result = await db.accounts_collection().insert_one(account.to_bson())

    document = account.to_bson()
    document["_id"] = result.inserted_id

    logger.info(
        "account_created",
        extra={
            "account_id": str(result.inserted_id),
            "owner_id": str(owner_id),
            "currency": currency,
        },
    )
    return document


async def get_account(account_id: ObjectId) -> dict[str, Any] | None:
    """Fetch an account document, or None."""
    return await db.accounts_collection().find_one({"_id": account_id})


async def get_account_or_raise(account_id_raw: str) -> dict[str, Any]:
    """Fetch an account by its id string, raising if absent.

    The id is converted to an ObjectId here. Route models have already
    constrained it to a valid 24-hex string, so no unvalidated client value
    reaches the query document (RULES.md).
    """
    document = await get_account(to_object_id(account_id_raw))
    if document is None:
        raise AccountNotFoundError(context={"account_id": account_id_raw})
    return document


async def get_owned_account_or_raise(
    account_id_raw: str, user: AuthenticatedUser
) -> dict[str, Any]:
    """Fetch an account and confirm the caller owns it.

    The ownership filter is part of the query, so a caller asking for
    someone else's account gets the same ACCOUNT_NOT_FOUND they would get
    for an id that does not exist. That avoids turning this route into a
    probe for which account ids are real. SYSTEM accounts have a null
    owner and so are never returned here.
    """
    document = await db.accounts_collection().find_one(
        {"_id": to_object_id(account_id_raw), "owner_id": user.id}
    )
    if document is None:
        raise AccountNotFoundError(
            context={"account_id": account_id_raw, "user_id": str(user.id)}
        )
    return document


async def list_accounts_for_owner(
    owner_id: ObjectId,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List a user's accounts, oldest first. Always bounded (DESIGN.md)."""
    bounded_limit = max(1, min(limit, MAX_PAGE_SIZE))
    cursor = (
        db.accounts_collection()
        .find({"owner_id": owner_id})
        .sort("created_at", ASCENDING)
        .skip(max(0, offset))
        .limit(bounded_limit)
    )
    return await cursor.to_list(length=bounded_limit)


async def get_system_account(currency: str) -> dict[str, Any] | None:
    """Fetch the SYSTEM boundary account for a currency."""
    return await db.accounts_collection().find_one(
        {"account_type": AccountType.SYSTEM.value, "currency": currency}
    )


async def ensure_system_accounts(currencies: list[str]) -> dict[str, ObjectId]:
    """Provision exactly one SYSTEM boundary account per currency.

    Called at startup. Idempotent, and safe to run concurrently from
    several replicas: the unique partial index
    `uq_system_account_per_currency` means a race produces a
    DuplicateKeyError for the loser rather than a second boundary account,
    and a second boundary account would be a second untracked source of
    money entering the ledger.
    """
    system_account_ids: dict[str, ObjectId] = {}

    for currency in currencies:
        existing = await get_system_account(currency)
        if existing is not None:
            system_account_ids[currency] = existing["_id"]
            continue

        account = AccountDocument(
            owner_id=None,  # nobody owns the ledger boundary
            currency=currency,
            label=SYSTEM_ACCOUNT_LABEL_TEMPLATE.format(currency=currency),
            account_type=AccountType.SYSTEM,
            status=AccountStatus.ACTIVE,
        )
        try:
            result = await db.accounts_collection().insert_one(account.to_bson())
            system_account_ids[currency] = result.inserted_id
            logger.info(
                "system_account_provisioned",
                extra={"currency": currency, "account_id": str(result.inserted_id)},
            )
        except DuplicateKeyError:
            # Lost the race to another worker; adopt theirs.
            winner = await get_system_account(currency)
            if winner is None:  # pragma: no cover - would mean the index lied
                raise
            system_account_ids[currency] = winner["_id"]

    return system_account_ids
