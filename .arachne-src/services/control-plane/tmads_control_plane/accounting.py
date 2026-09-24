from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def _iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


@dataclass(frozen=True)
class Posting:
    account_type: str
    debit_minor: int = 0
    credit_minor: int = 0


def account_id(organization_id: str, account_type: str, currency: str) -> str:
    return f"acct_{organization_id}_{account_type}_{currency.lower()}"


def ensure_account(
    connection: sqlite3.Connection,
    organization_id: str,
    account_type: str,
    currency: str,
) -> str:
    existing = connection.execute(
        """SELECT id FROM financial_accounts
           WHERE organization_id=? AND account_type=? AND currency=?""",
        (organization_id, account_type, currency),
    ).fetchone()
    if existing:
        return str(existing["id"])
    identifier = account_id(organization_id, account_type, currency)
    connection.execute(
        """INSERT OR IGNORE INTO financial_accounts(id,organization_id,account_type,currency)
           VALUES(?,?,?,?)""",
        (identifier, organization_id, account_type, currency),
    )
    return identifier


def allocate_grant_sequence(connection: sqlite3.Connection, device_id: str) -> int:
    row = connection.execute(
        """INSERT INTO spend_grant_sequences(device_id,last_sequence) VALUES(?,0)
           ON CONFLICT(device_id) DO UPDATE SET last_sequence=last_sequence+1
           RETURNING last_sequence""",
        (device_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to allocate spend grant sequence")
    return int(row[0])


def balance(
    connection: sqlite3.Connection,
    organization_id: str,
    account_type: str,
    currency: str = "USD",
) -> int:
    row = connection.execute(
        """SELECT COALESCE(SUM(e.credit_minor-e.debit_minor),0)
           FROM financial_entries e JOIN financial_accounts a ON a.id=e.account_id
           WHERE a.organization_id=? AND a.account_type=? AND a.currency=?""",
        (organization_id, account_type, currency),
    ).fetchone()
    return int(row[0])


def post_transaction(
    connection: sqlite3.Connection,
    *,
    organization_id: str,
    idempotency_key: str,
    kind: str,
    description: str,
    reference_type: str,
    reference_id: str,
    postings: tuple[Posting, ...],
    currency: str = "USD",
) -> str:
    debit = sum(item.debit_minor for item in postings)
    credit = sum(item.credit_minor for item in postings)
    if not postings or debit <= 0 or debit != credit:
        raise ValueError("journal transaction must contain balanced positive postings")
    if any(
        item.debit_minor < 0
        or item.credit_minor < 0
        or ((item.debit_minor == 0) == (item.credit_minor == 0))
        for item in postings
    ):
        raise ValueError("each journal posting must have exactly one positive side")
    existing = connection.execute(
        """SELECT id FROM financial_transactions
           WHERE organization_id=? AND idempotency_key=?""",
        (organization_id, idempotency_key),
    ).fetchone()
    if existing:
        return str(existing["id"])
    transaction_id = _id("txn")
    created_at = _iso()
    connection.execute(
        """INSERT INTO financial_transactions
           (id,organization_id,idempotency_key,kind,description,created_at)
           VALUES(?,?,?,?,?,?)""",
        (transaction_id, organization_id, idempotency_key, kind, description, created_at),
    )
    for item in postings:
        connection.execute(
            """INSERT INTO financial_entries
               (id,transaction_id,account_id,debit_minor,credit_minor,
                reference_type,reference_id,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                _id("entry"),
                transaction_id,
                ensure_account(connection, organization_id, item.account_type, currency),
                item.debit_minor,
                item.credit_minor,
                reference_type,
                reference_id,
                created_at,
            ),
        )
    assert_transaction_balanced(connection, transaction_id)
    return transaction_id


def assert_transaction_balanced(connection: sqlite3.Connection, transaction_id: str) -> None:
    row = connection.execute(
        """SELECT COALESCE(SUM(debit_minor),0),COALESCE(SUM(credit_minor),0)
           FROM financial_entries WHERE transaction_id=?""",
        (transaction_id,),
    ).fetchone()
    if row[0] <= 0 or row[0] != row[1]:
        raise ValueError(f"unbalanced financial transaction: {transaction_id}")


def assert_organization_invariants(
    connection: sqlite3.Connection, organization_id: str, currency: str = "USD"
) -> dict[str, int]:
    values = {
        name: balance(connection, organization_id, name, currency)
        for name in ("org_available", "org_reserved", "org_spent")
    }
    if any(value < 0 for value in values.values()):
        raise ValueError("organization wallet account cannot be negative")
    commitments = connection.execute(
        """SELECT COALESCE(SUM(amount_minor-spent_minor-released_minor),0)
           FROM grant_commitments
           WHERE organization_id=? AND currency=?
           AND status IN ('RESERVED','REGISTERED','DISTRIBUTING','PARTIALLY_SPENT')""",
        (organization_id, currency),
    ).fetchone()[0]
    if int(commitments) != values["org_reserved"]:
        raise ValueError("reserved journal balance does not match open grant commitments")
    return values


def reserve_grant(
    connection: sqlite3.Connection,
    *,
    grant_id: str,
    organization_id: str,
    campaign_id: str,
    contract_hash: str,
    device_id: str,
    amount_minor: int,
    currency: str,
    campaign_cap_minor: int,
    contract_cap_minor: int,
    payload_json: str,
) -> str:
    if amount_minor <= 0:
        raise ValueError("grant amount must be positive")
    existing = connection.execute(
        "SELECT * FROM grant_commitments WHERE grant_id=?", (grant_id,)
    ).fetchone()
    if existing:
        if (
            existing["organization_id"] != organization_id
            or existing["contract_hash"] != contract_hash
            or existing["device_id"] != device_id
            or existing["amount_minor"] != amount_minor
            or existing["payload_json"] != payload_json
        ):
            raise ValueError("grant ID already has another commitment")
        return str(existing["reservation_transaction_id"])
    available = balance(connection, organization_id, "org_available", currency)
    if available < amount_minor:
        raise ValueError("insufficient available funds")
    campaign_committed = connection.execute(
        """SELECT COALESCE(SUM(amount_minor-released_minor),0) FROM grant_commitments
           WHERE organization_id=? AND campaign_id=? AND status!='RELEASED'""",
        (organization_id, campaign_id),
    ).fetchone()[0]
    if campaign_committed + amount_minor > campaign_cap_minor:
        raise ValueError("campaign funding cap exceeded")
    contract_committed = connection.execute(
        """SELECT COALESCE(SUM(amount_minor-released_minor),0) FROM grant_commitments
           WHERE contract_hash=? AND status!='RELEASED'""",
        (contract_hash,),
    ).fetchone()[0]
    if contract_committed + amount_minor > contract_cap_minor:
        raise ValueError("contract funding cap exceeded")
    transaction_id = post_transaction(
        connection,
        organization_id=organization_id,
        idempotency_key=f"grant-reserve:{grant_id}",
        kind="grant_reservation",
        description=f"Reserved funds for spend grant {grant_id}",
        reference_type="spend_grant",
        reference_id=grant_id,
        postings=(
            Posting("org_available", debit_minor=amount_minor),
            Posting("org_reserved", credit_minor=amount_minor),
        ),
        currency=currency,
    )
    connection.execute(
        """INSERT INTO grant_commitments(
           grant_id,organization_id,campaign_id,contract_hash,device_id,currency,
           amount_minor,spent_minor,released_minor,status,payload_json,
           reservation_transaction_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,0,0,'RESERVED',?,?,?,?)""",
        (
            grant_id,
            organization_id,
            campaign_id,
            contract_hash,
            device_id,
            currency,
            amount_minor,
            payload_json,
            transaction_id,
            _iso(),
            _iso(),
        ),
    )
    assert_organization_invariants(connection, organization_id, currency)
    return transaction_id


def apply_settlement(
    connection: sqlite3.Connection,
    event: Any,
    *,
    raw_json: str,
    signature: str,
) -> tuple[str, bool]:
    prior = connection.execute(
        """SELECT transaction_id FROM processed_settlement_events
           WHERE event_id=? OR idempotency_key=? OR receipt_id=?""",
        (event.event_id, event.idempotency_key, event.receipt_id),
    ).fetchone()
    if prior:
        return str(prior["transaction_id"]), True
    commitment = connection.execute(
        "SELECT * FROM grant_commitments WHERE grant_id=?", (event.grant_id,)
    ).fetchone()
    if commitment is None:
        raise ValueError("unknown spend grant")
    if (
        commitment["organization_id"] != event.advertiser_organization_id
        or commitment["contract_hash"] != event.contract_hash
        or commitment["currency"] != event.currency
    ):
        raise ValueError("settlement event does not match grant scope")
    remaining = (
        commitment["amount_minor"] - commitment["spent_minor"] - commitment["released_minor"]
    )
    if event.amount_minor > remaining:
        raise ValueError("settlement exceeds reserved grant amount")
    transaction_id = post_transaction(
        connection,
        organization_id=event.advertiser_organization_id,
        idempotency_key=f"settlement:{event.idempotency_key}",
        kind="settlement_posted",
        description=f"Settlement posted for receipt {event.receipt_id}",
        reference_type="settlement_event",
        reference_id=event.event_id,
        postings=(
            Posting("org_reserved", debit_minor=event.amount_minor),
            Posting("org_spent", credit_minor=event.amount_minor),
        ),
        currency=event.currency,
    )
    spent = commitment["spent_minor"] + event.amount_minor
    open_amount = commitment["amount_minor"] - spent - commitment["released_minor"]
    status = "SPENT" if open_amount == 0 else "PARTIALLY_SPENT"
    connection.execute(
        """UPDATE grant_commitments SET spent_minor=?,status=?,updated_at=?
           WHERE grant_id=?""",
        (spent, status, _iso(), event.grant_id),
    )
    connection.execute(
        """INSERT INTO processed_settlement_events(
           event_id,idempotency_key,grant_id,receipt_id,amount_minor,currency,
           payload_json,signature,transaction_id,processed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            event.event_id,
            event.idempotency_key,
            event.grant_id,
            event.receipt_id,
            event.amount_minor,
            event.currency,
            raw_json,
            signature,
            transaction_id,
            _iso(),
        ),
    )
    connection.execute(
        """INSERT INTO advertiser_settlement_receipts(
           receipt_id,event_id,advertiser_organization_id,campaign_id,contract_hash,
           contract_revision,grant_id,device_pseudonym,publisher_organization_id,
           media_hash,execution_at,qfv_count,soft_score,amount_minor,currency,status,
           reason_code,observation_ids_json,processed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event.receipt_id,
            event.event_id,
            event.advertiser_organization_id,
            event.campaign_id or commitment["campaign_id"],
            event.contract_hash,
            event.contract_revision or 1,
            event.grant_id,
            event.device_pseudonym or f"device:{commitment['device_id']}",
            event.publisher_organization_id,
            event.media_hash or "sha256:" + "0" * 64,
            (
                event.execution_at.isoformat().replace("+00:00", "Z")
                if event.execution_at
                else _iso()
            ),
            event.qfv_count or 0,
            event.soft_score or 0,
            event.amount_minor,
            event.currency,
            event.status,
            "OK",
            json.dumps(event.observation_ids, separators=(",", ":")),
            _iso(),
        ),
    )
    connection.execute(
        """INSERT INTO publisher_payables VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event.receipt_id,
            event.publisher_organization_id,
            event.advertiser_organization_id,
            event.campaign_id or commitment["campaign_id"],
            event.amount_minor,
            event.currency,
            "POSTED",
            (
                event.execution_at.isoformat().replace("+00:00", "Z")
                if event.execution_at
                else _iso()
            ),
            _iso(),
        ),
    )
    assert_organization_invariants(connection, event.advertiser_organization_id, event.currency)
    return transaction_id, False


def release_grant(connection: sqlite3.Connection, grant_id: str) -> tuple[str, int]:
    commitment = connection.execute(
        "SELECT * FROM grant_commitments WHERE grant_id=?", (grant_id,)
    ).fetchone()
    if commitment is None:
        raise ValueError("unknown spend grant")
    amount = (
        commitment["amount_minor"] - commitment["spent_minor"] - commitment["released_minor"]
    )
    if amount == 0:
        return str(commitment["release_transaction_id"] or ""), 0
    transaction_id = post_transaction(
        connection,
        organization_id=commitment["organization_id"],
        idempotency_key=f"grant-release:{grant_id}",
        kind="grant_release",
        description=f"Released unused spend grant funds {grant_id}",
        reference_type="spend_grant",
        reference_id=grant_id,
        postings=(
            Posting("org_reserved", debit_minor=amount),
            Posting("org_available", credit_minor=amount),
        ),
        currency=commitment["currency"],
    )
    connection.execute(
        """UPDATE grant_commitments SET released_minor=released_minor+?,
           status='RELEASED',release_transaction_id=?,updated_at=? WHERE grant_id=?""",
        (amount, transaction_id, _iso(), grant_id),
    )
    assert_organization_invariants(
        connection, commitment["organization_id"], commitment["currency"]
    )
    return transaction_id, amount
