from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, phone_e164 TEXT NOT NULL UNIQUE, display_name TEXT,
 status TEXT NOT NULL DEFAULT 'ACTIVE', last_login_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS otp_challenges(
 id TEXT PRIMARY KEY, phone_e164 TEXT NOT NULL, code_hash TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, sent_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 consumed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_otp_phone_sent ON otp_challenges(phone_e164, sent_at);
CREATE TABLE IF NOT EXISTS auth_sessions(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), token_hash TEXT NOT NULL UNIQUE,
 csrf_token TEXT NOT NULL, network_key_hash TEXT, created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS security_notifications(
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL,
 severity TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT);
CREATE TABLE IF NOT EXISTS sms_outbox(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('INVITATION','SECURITY')),
 phone_e164 TEXT NOT NULL, body TEXT NOT NULL, related_id TEXT,
 status TEXT NOT NULL CHECK(status IN ('PENDING','FAILED','SENT','QUARANTINED')),
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL,
 provider_id TEXT, last_error TEXT, created_at TEXT NOT NULL, sent_at TEXT);
CREATE INDEX IF NOT EXISTS idx_sms_outbox_due ON sms_outbox(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS organizations(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'advertiser',
 created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS organization_memberships(
 organization_id TEXT NOT NULL REFERENCES organizations(id), user_id TEXT NOT NULL REFERENCES users(id),
 role TEXT NOT NULL CHECK(role IN ('owner','admin','campaign_manager','billing_manager',
 'device_manager','analyst','viewer')), created_at TEXT NOT NULL,
 PRIMARY KEY(organization_id,user_id));
CREATE TABLE IF NOT EXISTS organization_invitations(
 id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
 phone_e164 TEXT NOT NULL, invitee_name TEXT, role TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 invited_by TEXT NOT NULL REFERENCES users(id), invited_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 accepted_at TEXT, accepted_by TEXT REFERENCES users(id), revoked_at TEXT,
 delivery_status TEXT NOT NULL DEFAULT 'PENDING', delivery_attempts INTEGER NOT NULL DEFAULT 0,
 delivery_provider_id TEXT, delivery_error TEXT, delivered_at TEXT);
CREATE INDEX IF NOT EXISTS idx_invitation_phone ON organization_invitations(phone_e164,expires_at);
CREATE TABLE IF NOT EXISTS otp_ip_windows(
 ip_key_hash TEXT NOT NULL, action TEXT NOT NULL, window_started_at TEXT NOT NULL,
 request_count INTEGER NOT NULL CHECK(request_count >= 0),
 PRIMARY KEY(ip_key_hash,action,window_started_at));
CREATE TABLE IF NOT EXISTS financial_accounts(
 id TEXT PRIMARY KEY, organization_id TEXT REFERENCES organizations(id), account_type TEXT NOT NULL,
 currency TEXT NOT NULL, UNIQUE(organization_id,account_type,currency));
CREATE TABLE IF NOT EXISTS financial_transactions(
 id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id), idempotency_key TEXT,
 kind TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(organization_id,idempotency_key));
CREATE TABLE IF NOT EXISTS financial_entries(
 id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL REFERENCES financial_transactions(id),
 account_id TEXT NOT NULL REFERENCES financial_accounts(id), debit_minor INTEGER NOT NULL DEFAULT 0,
 credit_minor INTEGER NOT NULL DEFAULT 0, reference_type TEXT NOT NULL, reference_id TEXT NOT NULL,
 created_at TEXT NOT NULL, CHECK(debit_minor >= 0 AND credit_minor >= 0),
 CHECK((debit_minor=0) != (credit_minor=0)));
CREATE TRIGGER IF NOT EXISTS financial_entries_no_update BEFORE UPDATE ON financial_entries
BEGIN SELECT RAISE(ABORT,'financial journal entries are immutable'); END;
CREATE TRIGGER IF NOT EXISTS financial_entries_no_delete BEFORE DELETE ON financial_entries
BEGIN SELECT RAISE(ABORT,'financial journal entries are immutable'); END;
CREATE TABLE IF NOT EXISTS pending_deposits(
 id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
 user_id TEXT NOT NULL REFERENCES users(id), provider TEXT NOT NULL,
 amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), currency TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, stripe_session_id TEXT UNIQUE, stripe_checkout_url TEXT,
 status TEXT NOT NULL CHECK(status IN ('pending','paid','failed')),
 transaction_id TEXT UNIQUE REFERENCES financial_transactions(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(organization_id,idempotency_key));
CREATE INDEX IF NOT EXISTS idx_pending_deposits_session
 ON pending_deposits(stripe_session_id);
CREATE TABLE IF NOT EXISTS stripe_events(
 event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
 pending_deposit_id TEXT REFERENCES pending_deposits(id),
 status TEXT NOT NULL CHECK(status IN ('received','processed','ignored','rejected')),
 error TEXT, created_at TEXT NOT NULL, processed_at TEXT);
CREATE TABLE IF NOT EXISTS grant_commitments(
 grant_id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
 campaign_id TEXT NOT NULL REFERENCES campaigns(id), contract_hash TEXT NOT NULL,
 device_id TEXT NOT NULL, currency TEXT NOT NULL,
 amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
 spent_minor INTEGER NOT NULL DEFAULT 0 CHECK(spent_minor >= 0),
 released_minor INTEGER NOT NULL DEFAULT 0 CHECK(released_minor >= 0),
 status TEXT NOT NULL CHECK(status IN
 ('RESERVED','REGISTERED','DISTRIBUTING','PARTIALLY_SPENT','SPENT','RELEASED','FAILED')),
 settlement_attempts INTEGER NOT NULL DEFAULT 0,
 settlement_next_attempt_at TEXT NOT NULL DEFAULT '',
 settlement_last_error TEXT,
 payload_json TEXT NOT NULL,
 reservation_transaction_id TEXT NOT NULL UNIQUE REFERENCES financial_transactions(id),
 release_transaction_id TEXT UNIQUE REFERENCES financial_transactions(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK(spent_minor + released_minor <= amount_minor));
CREATE INDEX IF NOT EXISTS idx_grant_commitments_org
 ON grant_commitments(organization_id,status,created_at);
CREATE INDEX IF NOT EXISTS idx_grant_commitments_contract
 ON grant_commitments(contract_hash,status);
CREATE TABLE IF NOT EXISTS processed_settlement_events(
 event_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
 grant_id TEXT NOT NULL REFERENCES grant_commitments(grant_id),
 receipt_id TEXT NOT NULL UNIQUE, amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
 currency TEXT NOT NULL, payload_json TEXT NOT NULL, signature TEXT NOT NULL,
 transaction_id TEXT NOT NULL UNIQUE REFERENCES financial_transactions(id),
 processed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS advertiser_settlement_receipts(
 receipt_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
 advertiser_organization_id TEXT NOT NULL REFERENCES organizations(id),
 campaign_id TEXT NOT NULL REFERENCES campaigns(id), contract_hash TEXT NOT NULL,
 contract_revision INTEGER NOT NULL, grant_id TEXT NOT NULL,
 device_pseudonym TEXT NOT NULL, publisher_organization_id TEXT NOT NULL,
 media_hash TEXT NOT NULL, execution_at TEXT NOT NULL, qfv_count INTEGER NOT NULL,
 soft_score REAL NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
 status TEXT NOT NULL, reason_code TEXT NOT NULL, observation_ids_json TEXT NOT NULL,
 batch_id TEXT, merkle_root TEXT, leaf_hash TEXT, leaf_index INTEGER,
 proof_json TEXT, chain_id INTEGER, transaction_hash TEXT, block_number INTEGER,
 contract_address TEXT, finality TEXT, anchored_at TEXT, processed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_advertiser_receipts_org
 ON advertiser_settlement_receipts(advertiser_organization_id,campaign_id,execution_at);
CREATE TABLE IF NOT EXISTS publisher_payables(
 receipt_id TEXT PRIMARY KEY, publisher_organization_id TEXT NOT NULL,
 advertiser_organization_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
 amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
 execution_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_publisher_payables_org
 ON publisher_payables(publisher_organization_id,created_at);
CREATE TABLE IF NOT EXISTS spend_grant_sequences(
 device_id TEXT PRIMARY KEY, last_sequence INTEGER NOT NULL CHECK(last_sequence >= 0));
CREATE TABLE IF NOT EXISTS campaigns(
 id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id), name TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'DRAFT',
 budget_cap_minor INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'USD',
 start_at TEXT NOT NULL, end_at TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
CREATE TABLE IF NOT EXISTS media_assets(
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id), organization_id TEXT NOT NULL,
 source_filename TEXT NOT NULL, source_hash TEXT NOT NULL, derivative_hash TEXT, kind TEXT NOT NULL,
 mime TEXT NOT NULL, width INTEGER, height INTEGER, duration_ms INTEGER, bytes INTEGER NOT NULL,
 processing_status TEXT NOT NULL, error TEXT, storage_name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bids(
 id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id), organization_id TEXT NOT NULL,
 current_revision INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'DRAFT',
 draft_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bid_revisions(
 id TEXT PRIMARY KEY, bid_id TEXT NOT NULL REFERENCES bids(id), revision INTEGER NOT NULL,
 contract_json TEXT NOT NULL, canonical_hash TEXT NOT NULL UNIQUE, previous_hash TEXT,
 signature TEXT NOT NULL, signer_key_id TEXT NOT NULL, published_at TEXT NOT NULL,
 lifecycle_state TEXT NOT NULL, UNIQUE(bid_id,revision));
CREATE TABLE IF NOT EXISTS ledger_anchors(
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES bid_revisions(id), adapter TEXT NOT NULL,
 network TEXT NOT NULL, transaction_ref TEXT NOT NULL, object_hash TEXT NOT NULL,
 prototype INTEGER NOT NULL DEFAULT 0, anchored_at TEXT NOT NULL,
 chain_id INTEGER, block_number INTEGER, contract_address TEXT, finality TEXT,
 anchor_kind TEXT NOT NULL DEFAULT 'CONTRACT', lifecycle_event_id TEXT);
CREATE TABLE IF NOT EXISTS audit_events(
 id TEXT PRIMARY KEY, organization_id TEXT, user_id TEXT, action TEXT NOT NULL,
 object_type TEXT NOT NULL, object_id TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS signing_keys(
 key_id TEXT PRIMARY KEY, algorithm TEXT NOT NULL, public_key TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','RETIRED','REVOKED')),
 created_at TEXT NOT NULL, retired_at TEXT, revoked_at TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS lifecycle_events(
 id TEXT PRIMARY KEY, bid_id TEXT NOT NULL REFERENCES bids(id),
 revision_id TEXT NOT NULL REFERENCES bid_revisions(id), organization_id TEXT NOT NULL,
 event_type TEXT NOT NULL CHECK(event_type IN
 ('ACTIVE','PAUSED','CANCELLED','SUPERSEDED','COMPLETED','EXPIRED','EXHAUSTED')),
 active_contract_hash TEXT NOT NULL, predecessor_event_hash TEXT,
 event_json TEXT NOT NULL, canonical_hash TEXT NOT NULL UNIQUE, signature TEXT NOT NULL,
 signer_key_id TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
 anchor_status TEXT NOT NULL DEFAULT 'PENDING_LEDGER',
 distributed_contract_json TEXT, distributed_contract_hash TEXT);
CREATE INDEX IF NOT EXISTS idx_lifecycle_bid ON lifecycle_events(bid_id,created_at);
CREATE TABLE IF NOT EXISTS publication_outbox(
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL UNIQUE REFERENCES bid_revisions(id),
 organization_id TEXT NOT NULL, requested_by TEXT NOT NULL REFERENCES users(id),
 status TEXT NOT NULL CHECK(status IN
 ('PENDING_LEDGER','PENDING_SETTLEMENT','PENDING_SUPERSEDE','READY_FINALIZE','COMPLETE')),
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, next_attempt_at TEXT NOT NULL,
 created_at TEXT NOT NULL, completed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_publication_outbox_due
 ON publication_outbox(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS distributor_outbox(
 id TEXT PRIMARY KEY, organization_id TEXT NOT NULL, object_type TEXT NOT NULL,
 object_key TEXT NOT NULL, payload_json TEXT, content_path TEXT,
 status TEXT NOT NULL CHECK(status IN ('PENDING','PROCESSING','SYNCED','FAILED')),
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, next_attempt_at TEXT NOT NULL,
 created_at TEXT NOT NULL, synced_at TEXT, UNIQUE(object_type,object_key));
CREATE INDEX IF NOT EXISTS idx_outbox_due ON distributor_outbox(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS devices(
 id TEXT PRIMARY KEY, organization_id TEXT NOT NULL REFERENCES organizations(id),
 profile_json TEXT NOT NULL, distributor_status TEXT NOT NULL,
 enrollment_token TEXT, registered_by TEXT NOT NULL REFERENCES users(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device_aliases(
 device_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS campaign_product_upcs(
 campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
 upc TEXT NOT NULL, label TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(campaign_id, upc));
CREATE INDEX IF NOT EXISTS idx_campaign_product_upcs_upc ON campaign_product_upcs(upc);
CREATE TABLE IF NOT EXISTS shelf_deployments(
 id TEXT PRIMARY KEY, device_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
 contract_hash TEXT NOT NULL, upc TEXT NOT NULL, token_hash TEXT NOT NULL,
 compliance_photo_path TEXT, status TEXT NOT NULL,
 message TEXT, deployed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_shelf_deployments_device ON shelf_deployments(device_id, deployed_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executescript(SCHEMA)
                self._migrate_roles(conn)
                self._migrate_member_names(conn)
                self._migrate_identity_and_invitations(conn)
                self._migrate_ledger_publication(conn)
                self._migrate_lifecycle_types(conn)
                grant_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(grant_commitments)")
                }
                for name, declaration in {
                    "settlement_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "settlement_next_attempt_at": "TEXT NOT NULL DEFAULT ''",
                    "settlement_last_error": "TEXT",
                }.items():
                    if name not in grant_columns:
                        conn.execute(
                            f"ALTER TABLE grant_commitments ADD COLUMN {name} {declaration}"
                        )
                conn.execute(
                    """INSERT OR IGNORE INTO spend_grant_sequences(device_id,last_sequence)
                       SELECT json_extract(payload_json,'$.device_id'),
                              MAX(CAST(json_extract(payload_json,'$.sequence') AS INTEGER))
                       FROM grant_commitments
                       WHERE json_valid(payload_json)
                       AND json_extract(payload_json,'$.device_id') IS NOT NULL
                       GROUP BY json_extract(payload_json,'$.device_id')"""
                )
                conn.execute(
                    """UPDATE financial_accounts SET account_type='org_available'
                       WHERE account_type='campaign_funds'
                       AND NOT EXISTS(
                         SELECT 1 FROM financial_accounts newer
                         WHERE newer.organization_id=financial_accounts.organization_id
                         AND newer.account_type='org_available'
                         AND newer.currency=financial_accounts.currency)"""
                )
                conn.execute(
                    """UPDATE financial_accounts SET account_type='external_stripe_clearing'
                       WHERE account_type='stripe_clearing'
                       AND NOT EXISTS(
                         SELECT 1 FROM financial_accounts newer
                         WHERE newer.organization_id IS financial_accounts.organization_id
                         AND newer.account_type='external_stripe_clearing'
                         AND newer.currency=financial_accounts.currency)"""
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(1,datetime('now'))"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(2,datetime('now'))"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(3,datetime('now'))"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(4,datetime('now'))"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(5,datetime('now'))"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(6,datetime('now'))"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(7,datetime('now'))"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(8,datetime('now'))"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(9,datetime('now'))"
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS device_aliases(
                       device_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                       updated_at TEXT NOT NULL)"""
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(10,datetime('now'))"
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS campaign_product_upcs(
                       campaign_id TEXT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
                       upc TEXT NOT NULL, label TEXT, created_at TEXT NOT NULL,
                       PRIMARY KEY(campaign_id, upc))"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_campaign_product_upcs_upc
                       ON campaign_product_upcs(upc)"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS shelf_deployments(
                       id TEXT PRIMARY KEY, device_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
                       contract_hash TEXT NOT NULL, upc TEXT NOT NULL, token_hash TEXT NOT NULL,
                       compliance_photo_path TEXT, status TEXT NOT NULL,
                       message TEXT, deployed_at TEXT NOT NULL)"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_shelf_deployments_device
                       ON shelf_deployments(device_id, deployed_at DESC)"""
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations VALUES(11,datetime('now'))"
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _migrate_roles(conn: sqlite3.Connection) -> None:
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='organization_memberships'"
        ).fetchone()
        if not sql_row or "campaign_manager" in sql_row[0]:
            return
        conn.execute("ALTER TABLE organization_memberships RENAME TO organization_memberships_old")
        conn.execute(
            """CREATE TABLE organization_memberships(
               organization_id TEXT NOT NULL REFERENCES organizations(id),
               user_id TEXT NOT NULL REFERENCES users(id),
               role TEXT NOT NULL CHECK(role IN ('owner','admin','campaign_manager',
               'billing_manager','device_manager','analyst','viewer')),
               created_at TEXT NOT NULL, PRIMARY KEY(organization_id,user_id))"""
        )
        conn.execute(
            """INSERT INTO organization_memberships
               SELECT organization_id,user_id,
               CASE WHEN role='member' THEN 'campaign_manager' ELSE role END,created_at
               FROM organization_memberships_old"""
        )
        conn.execute("DROP TABLE organization_memberships_old")

    @staticmethod
    def _migrate_member_names(conn: sqlite3.Connection) -> None:
        user_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }
        if "display_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
        invitation_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(organization_invitations)"
            ).fetchall()
        }
        if "invitee_name" not in invitation_columns:
            conn.execute(
                "ALTER TABLE organization_invitations ADD COLUMN invitee_name TEXT"
            )

    @staticmethod
    def _migrate_identity_and_invitations(conn: sqlite3.Connection) -> None:
        user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        if "status" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'")
        if "last_login_at" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT")
        session_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(auth_sessions)")
        }
        if "network_key_hash" not in session_columns:
            conn.execute("ALTER TABLE auth_sessions ADD COLUMN network_key_hash TEXT")
        invitation_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(organization_invitations)")
        }
        for name, declaration in {
            "delivery_status": "TEXT NOT NULL DEFAULT 'PENDING'",
            "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
            "delivery_provider_id": "TEXT",
            "delivery_error": "TEXT",
            "delivered_at": "TEXT",
        }.items():
            if name not in invitation_columns:
                conn.execute(
                    f"ALTER TABLE organization_invitations ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _migrate_ledger_publication(conn: sqlite3.Connection) -> None:
        anchor_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(ledger_anchors)").fetchall()
        }
        additions = {
            "chain_id": "INTEGER",
            "block_number": "INTEGER",
            "contract_address": "TEXT",
            "finality": "TEXT",
            "anchor_kind": "TEXT NOT NULL DEFAULT 'CONTRACT'",
            "lifecycle_event_id": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in anchor_columns:
                conn.execute(f"ALTER TABLE ledger_anchors ADD COLUMN {name} {declaration}")
        lifecycle_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(lifecycle_events)").fetchall()
        }
        migrated_legacy_events = "anchor_status" not in lifecycle_columns
        lifecycle_additions = {
            "anchor_status": "TEXT NOT NULL DEFAULT 'PENDING_LEDGER'",
            "distributed_contract_json": "TEXT",
            "distributed_contract_hash": "TEXT",
        }
        for name, declaration in lifecycle_additions.items():
            if name not in lifecycle_columns:
                conn.execute(f"ALTER TABLE lifecycle_events ADD COLUMN {name} {declaration}")
        if migrated_legacy_events:
            conn.execute(
                """UPDATE lifecycle_events SET anchor_status='FINAL'
                   WHERE anchor_status='PENDING_LEDGER'"""
            )

    @staticmethod
    def _migrate_lifecycle_types(conn: sqlite3.Connection) -> None:
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='lifecycle_events'"
        ).fetchone()
        if not sql_row or "'ACTIVE'" in sql_row[0]:
            return
        conn.execute("ALTER TABLE lifecycle_events RENAME TO lifecycle_events_old")
        conn.execute(
            """CREATE TABLE lifecycle_events(
               id TEXT PRIMARY KEY, bid_id TEXT NOT NULL REFERENCES bids(id),
               revision_id TEXT NOT NULL REFERENCES bid_revisions(id),
               organization_id TEXT NOT NULL,
               event_type TEXT NOT NULL CHECK(event_type IN
               ('ACTIVE','PAUSED','CANCELLED','SUPERSEDED','COMPLETED','EXPIRED','EXHAUSTED')),
               active_contract_hash TEXT NOT NULL, predecessor_event_hash TEXT,
               event_json TEXT NOT NULL, canonical_hash TEXT NOT NULL UNIQUE,
               signature TEXT NOT NULL, signer_key_id TEXT NOT NULL,
               created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
               anchor_status TEXT NOT NULL DEFAULT 'PENDING_LEDGER',
               distributed_contract_json TEXT, distributed_contract_hash TEXT)"""
        )
        conn.execute(
            """INSERT INTO lifecycle_events SELECT id,bid_id,revision_id,organization_id,
               event_type,active_contract_hash,predecessor_event_hash,event_json,
               canonical_hash,signature,signer_key_id,created_by,created_at,
               anchor_status,distributed_contract_json,distributed_contract_hash
               FROM lifecycle_events_old"""
        )
        conn.execute("DROP TABLE lifecycle_events_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lifecycle_bid ON lifecycle_events(bid_id,created_at)"
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
