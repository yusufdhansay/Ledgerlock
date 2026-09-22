"""Reconciliation route: the system-wide ledger integrity check.

PRD.md core feature 6. Exposed as an endpoint as well as a test so the
invariant can be checked against a running deployment, which is what makes
it meaningful after the Phase 6 load test rather than only inside the
suite.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from app.core.logging_config import get_logger
from app.core.security import AuthenticatedUser, get_current_user
from app.models.ledger_entry import ReconciliationReport
from app.services import reconciliation as reconciliation_service

logger = get_logger(__name__)

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])


@router.get(
    "",
    response_model=ReconciliationReport,
    status_code=status.HTTP_200_OK,
    summary="Check that the entire ledger nets to zero",
    responses={401: {"description": "UNAUTHENTICATED"}},
)
async def read_reconciliation(
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReconciliationReport:
    """Sum every ledger entry in the system and report whether it is sound.

    `net_signed_minor` must be exactly 0. Because every transaction writes
    a credit of +X and a debit of -X, and amounts are integers, there is no
    rounding tolerance to argue about: any non-zero value means a pair was
    written incomplete, which the atomic write path is built to make
    impossible.

    A net of zero alone is a weaker claim than it appears, so the response
    also reports entries belonging to no transaction, transactions whose
    entries are not exactly one debit and one credit, the net per currency,
    and any USER account with a negative balance. Assert on `healthy`,
    which is true only when all of those are clean.

    Always computed live from the entries. Nothing here is cached, and the
    check never repairs anything: silently correcting a discrepancy would
    destroy the evidence of how it arose.

    **Access:** any authenticated user, which is a deliberate simplification
    for a reference system. In production this reports system-wide totals
    and would be restricted to an operator role.
    """
    report = await reconciliation_service.reconcile()

    if not report.healthy:
        # A failed reconciliation is the most serious event this system can
        # report, so it is logged at error level with the full breakdown
        # rather than merely returned to whoever happened to ask.
        logger.error(
            "reconciliation_failed",
            extra={
                "net_signed_minor": report.net_signed_minor,
                "entries_without_transaction": report.entries_without_transaction,
                "unbalanced_transaction_groups": (report.unbalanced_transaction_groups),
                "negative_user_account_count": len(report.negative_user_accounts),
                "per_currency_net_minor": report.per_currency_net_minor,
            },
        )
    else:
        logger.info(
            "reconciliation_ok",
            extra={
                "total_entries": report.total_entries,
                "total_transactions": report.total_transactions,
            },
        )

    return report
