"""Phase 1 Rev B (DESTRUCTIVE — review before running)

  * TRUNCATEs attendance_days  (deliberate; see comment in upgrade())
  * DROPs office_networks, random_checks, wfh_requests, schedule_requests
  * DROPs superseded attendance_days columns
  * Converts audit_logs.detail from TEXT to JSONB

`downgrade()` restores the SCHEMA (tables/columns return with their original DDL and
constraints). It does NOT restore data: the attendance_days rows and the four dropped
tables are gone permanently. Run this against a Neon branch first.

The one exception: audit_logs.detail IS losslessly restored from detail_json->>'legacy'.

Revision ID: 9e8d7c6b5a40
Revises: 7c1d9f0a2b34
Create Date: 2026-07-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9e8d7c6b5a40"
down_revision: Union[str, None] = "7c1d9f0a2b34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ 1. DELIBERATE TRUNCATE
    # attendance_days cannot be faithfully migrated. The old `mode` enum was
    # (office|wfh|flagged|absent|pending); the new `status` is
    # (present|late|flagged|absent|leave|off_day). `mode='wfh'` has NO target status —
    # WFH is removed from the product entirely — and passed_checks/total_checks are
    # meaningless once random checks are gone. Any mapping would be fabricated data.
    # Phase 1 therefore starts this table empty, by explicit decision.
    op.execute("TRUNCATE TABLE attendance_days RESTART IDENTITY")

    # ------------------------------------------------------------------ 2. tighten (table is now empty)
    op.alter_column("attendance_days", "status", existing_type=sa.String(length=20), nullable=False)
    op.drop_constraint("ck_attendance_days_mode", "attendance_days", type_="check")
    op.create_check_constraint(
        "ck_attendance_days_status",
        "attendance_days",
        "status in ('present','late','flagged','absent','leave','off_day')",
    )
    # NULL passes a CHECK, so these permit the nullable method/verification columns.
    op.create_check_constraint(
        "ck_attendance_days_method", "attendance_days", "method in ('wifi','gps','manual')"
    )
    op.create_check_constraint(
        "ck_attendance_days_verification", "attendance_days", "verification in ('verified','gps_pending')"
    )

    # ------------------------------------------------------------------ 3. drop superseded columns
    # source_ip / location move to checkin_attempts, which is their correct home.
    op.drop_column("attendance_days", "mode")
    op.drop_column("attendance_days", "passed_checks")
    op.drop_column("attendance_days", "total_checks")
    op.drop_column("attendance_days", "source_ip")
    op.drop_column("attendance_days", "location")

    # ------------------------------------------------------------------ 4. drop removed features
    op.drop_table("random_checks")
    op.drop_table("wfh_requests")
    op.drop_table("schedule_requests")
    op.drop_table("office_networks")

    # ------------------------------------------------------------------ 5. audit_logs detail -> JSONB
    op.drop_column("audit_logs", "detail")
    op.alter_column("audit_logs", "detail_json", new_column_name="detail")


def downgrade() -> None:
    # 5. audit_logs: restore the TEXT column, losslessly, from the preserved 'legacy' key.
    op.alter_column("audit_logs", "detail", new_column_name="detail_json")
    op.add_column("audit_logs", sa.Column("detail", sa.Text(), nullable=True))
    op.execute(
        "UPDATE audit_logs SET detail = detail_json->>'legacy' WHERE detail_json->>'legacy' IS NOT NULL"
    )

    # 4. recreate the dropped tables (SCHEMA ONLY — rows are not recoverable)
    op.create_table(
        "office_networks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("public_ip", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "random_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.CheckConstraint("result in ('pending','passed','missed')", name="ck_random_checks_result"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "wfh_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('pending','approved','rejected')", name="ck_wfh_requests_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "schedule_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.CheckConstraint("status in ('pending','approved','rejected')", name="ck_schedule_requests_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # 3/2. attendance_days: restore columns and the old CHECK
    op.add_column("attendance_days", sa.Column("location", sa.String(length=120), nullable=True))
    op.add_column("attendance_days", sa.Column("source_ip", sa.String(length=64), nullable=True))
    op.add_column(
        "attendance_days", sa.Column("total_checks", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "attendance_days", sa.Column("passed_checks", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column(
        "attendance_days", sa.Column("mode", sa.String(length=20), server_default="pending", nullable=False)
    )
    op.drop_constraint("ck_attendance_days_verification", "attendance_days", type_="check")
    op.drop_constraint("ck_attendance_days_method", "attendance_days", type_="check")
    op.drop_constraint("ck_attendance_days_status", "attendance_days", type_="check")
    op.create_check_constraint(
        "ck_attendance_days_mode",
        "attendance_days",
        "mode in ('office','wfh','flagged','absent','pending')",
    )
    op.alter_column("attendance_days", "status", existing_type=sa.String(length=20), nullable=True)
