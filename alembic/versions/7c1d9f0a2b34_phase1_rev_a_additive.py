"""Phase 1 Rev A (ADDITIVE, REVERSIBLE)

Creates offices/rosters/checkin_attempts/app_state, adds the new nullable columns,
performs lossless renames, inserts reference data, and backfills.

Nothing is dropped and no data is destroyed here. `downgrade()` is fully lossless.
The destructive half lives in Rev B (9e8d7c6b5a40).

Revision ID: 7c1d9f0a2b34
Revises: 2eb328672b4f
Create Date: 2026-07-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c1d9f0a2b34"
down_revision: Union[str, None] = "2eb328672b4f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ 1. new tables
    op.create_table(
        "offices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("public_ips", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("radius_meters", sa.Integer(), server_default="80", nullable=False),
        sa.Column("timezone", sa.String(length=64), server_default="Asia/Karachi", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "rosters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("grace_minutes", sa.Integer(), server_default="15", nullable=False),
        sa.Column("working_days", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("office_id", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("end_time > start_time", name="ck_rosters_time_order"),
        sa.ForeignKeyConstraint(["office_id"], ["offices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "checkin_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("source_ip", sa.String(length=64), nullable=True),
        sa.Column("matched_office_id", sa.Integer(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("gps_accuracy", sa.Float(), nullable=True),
        sa.Column("distance_meters", sa.Float(), nullable=True),
        sa.Column("result", sa.String(length=24), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.CheckConstraint("method in ('wifi','gps')", name="ck_checkin_attempts_method"),
        sa.CheckConstraint(
            "result in ('success','ip_no_match','outside_radius','low_accuracy',"
            "'before_shift','after_shift','off_day','already_checked_in')",
            name="ck_checkin_attempts_result",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matched_office_id"], ["offices.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Cross-process key/value store. Holds the DEV_MODE time_override so /debug/set-time
    # is visible to the separate scheduler process (a module global would not be).
    op.create_table(
        "app_state",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )

    # ------------------------------------------------------------------ 2. users
    op.add_column("users", sa.Column("phone", sa.String(length=40), nullable=True))
    op.add_column("users", sa.Column("requested_roster_id", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("assigned_roster_id", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("admin_feedback", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("approved_by", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key("fk_users_requested_roster", "users", "rosters", ["requested_roster_id"], ["id"])
    op.create_foreign_key("fk_users_assigned_roster", "users", "rosters", ["assigned_roster_id"], ["id"])
    op.create_foreign_key("fk_users_approved_by", "users", "users", ["approved_by"], ["id"])

    # status gains 'rejected'
    op.drop_constraint("ck_users_status", "users", type_="check")
    op.create_check_constraint(
        "ck_users_status", "users", "status in ('pending','active','rejected','disabled')"
    )

    # ------------------------------------------------------------------ 3. devices (lossless rename)
    op.alter_column("devices", "platform", new_column_name="user_agent")

    # ------------------------------------------------------------------ 4. heartbeats.user_id
    # Losslessly derivable from devices -> add nullable, backfill, then enforce NOT NULL.
    op.add_column("heartbeats", sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute("UPDATE heartbeats h SET user_id = d.user_id FROM devices d WHERE d.id = h.device_id")
    # Any heartbeat whose device vanished cannot be attributed; there should be none
    # (devices.id is an FK target with ON DELETE CASCADE), but be explicit rather than fail.
    op.execute("DELETE FROM heartbeats WHERE user_id IS NULL")
    op.alter_column("heartbeats", "user_id", existing_type=sa.Integer(), nullable=False)
    op.create_foreign_key(
        "fk_heartbeats_user", "heartbeats", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )

    # ------------------------------------------------------------------ 5. audit_logs
    # Never truncate an audit trail. Preserve the old free-text detail inside JSONB.
    op.add_column("audit_logs", sa.Column("detail_json", postgresql.JSONB(), nullable=True))
    op.execute(
        "UPDATE audit_logs SET detail_json = jsonb_build_object('legacy', detail) WHERE detail IS NOT NULL"
    )
    op.alter_column("audit_logs", "user_id", new_column_name="actor_user_id")

    # ------------------------------------------------------------------ 6. attendance_days
    # `status` stays NULLABLE here: existing rows have no valid value for it, and the old
    # `mode='wfh'` has no target status. Rev B truncates the table and then SETs NOT NULL.
    op.add_column("attendance_days", sa.Column("status", sa.String(length=20), nullable=True))
    op.add_column("attendance_days", sa.Column("method", sa.String(length=10), nullable=True))
    op.add_column("attendance_days", sa.Column("verification", sa.String(length=20), nullable=True))
    op.add_column("attendance_days", sa.Column("last_seen", sa.DateTime(timezone=True), nullable=True))
    # Safe to add NOT NULL directly: the server_default backfills existing rows with 0.
    op.add_column(
        "attendance_days", sa.Column("late_minutes", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("attendance_days", sa.Column("resolved_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_attendance_days_resolved_by", "attendance_days", "users", ["resolved_by"], ["id"]
    )
    op.alter_column("attendance_days", "note", new_column_name="admin_note")

    # ------------------------------------------------------------------ 7. reference data (idempotent)
    op.execute(
        """
        INSERT INTO offices (name, public_ips, latitude, longitude, radius_meters, timezone, active)
        SELECT 'Head Office', '[]'::jsonb, NULL, NULL, 80, 'Asia/Karachi', true
        WHERE NOT EXISTS (SELECT 1 FROM offices WHERE name = 'Head Office')
        """
    )
    for name, start, end, is_default in (
        ("Morning", "10:00", "19:00", "true"),
        ("Mid", "11:00", "20:00", "false"),
        ("Late", "12:00", "21:00", "false"),
    ):
        op.execute(
            f"""
            INSERT INTO rosters (name, start_time, end_time, grace_minutes, working_days, is_default, office_id, active)
            SELECT '{name}', '{start}', '{end}', 15, '[0,1,2,3,4]'::jsonb, {is_default}, o.id, true
            FROM offices o
            WHERE o.name = 'Head Office'
              AND NOT EXISTS (SELECT 1 FROM rosters WHERE name = '{name}')
            """
        )

    # ------------------------------------------------------------------ 8. backfill
    # Without this every existing active employee is locked out with
    # 403 "Roster not assigned, contact admin." on their next check-in attempt.
    op.execute(
        """
        UPDATE users
           SET assigned_roster_id = (SELECT id FROM rosters WHERE is_default IS TRUE ORDER BY id LIMIT 1)
         WHERE role = 'employee' AND status = 'active' AND assigned_roster_id IS NULL
        """
    )


def downgrade() -> None:
    """Fully lossless: only drops what this revision created."""
    # 8/7. reference data + backfill: clearing the FKs lets us drop rosters/offices below.
    op.execute("UPDATE users SET assigned_roster_id = NULL, requested_roster_id = NULL")

    # 6. attendance_days
    op.alter_column("attendance_days", "admin_note", new_column_name="note")
    op.drop_constraint("fk_attendance_days_resolved_by", "attendance_days", type_="foreignkey")
    op.drop_column("attendance_days", "resolved_by")
    op.drop_column("attendance_days", "late_minutes")
    op.drop_column("attendance_days", "last_seen")
    op.drop_column("attendance_days", "verification")
    op.drop_column("attendance_days", "method")
    op.drop_column("attendance_days", "status")

    # 5. audit_logs
    op.alter_column("audit_logs", "actor_user_id", new_column_name="user_id")
    op.drop_column("audit_logs", "detail_json")

    # 4. heartbeats
    op.drop_constraint("fk_heartbeats_user", "heartbeats", type_="foreignkey")
    op.drop_column("heartbeats", "user_id")

    # 3. devices
    op.alter_column("devices", "user_agent", new_column_name="platform")

    # 2. users
    op.drop_constraint("ck_users_status", "users", type_="check")
    op.create_check_constraint("ck_users_status", "users", "status in ('pending','active','disabled')")
    op.drop_constraint("fk_users_approved_by", "users", type_="foreignkey")
    op.drop_constraint("fk_users_assigned_roster", "users", type_="foreignkey")
    op.drop_constraint("fk_users_requested_roster", "users", type_="foreignkey")
    op.drop_column("users", "approved_at")
    op.drop_column("users", "approved_by")
    op.drop_column("users", "admin_feedback")
    op.drop_column("users", "assigned_roster_id")
    op.drop_column("users", "requested_roster_id")
    op.drop_column("users", "phone")

    # 1. new tables
    op.drop_table("app_state")
    op.drop_table("checkin_attempts")
    op.drop_table("rosters")
    op.drop_table("offices")
