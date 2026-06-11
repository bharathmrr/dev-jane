"""Remove leads from the database.

Usage:
  python scripts/remove_leads.py                          # interactive list + prompt
  python scripts/remove_leads.py --email foo@bar.com      # remove one lead by email
  python scripts/remove_leads.py --status new             # remove all leads with status
  python scripts/remove_leads.py --all                    # remove ALL leads
  python scripts/remove_leads.py --dry-run --all          # preview without deleting

Statuses: new, sent, replied, booked, opted_out, bounced

Cascades: deleting a lead also deletes its onboarding_record and kyc_submissions
          (via ON DELETE CASCADE on the FK constraints).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timezone

import asyncpg

DB_URL = "postgresql://scheduler:scheduler@localhost:5432/scheduler"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


async def _list_leads(conn: asyncpg.Connection, where: str = "", params: list = []) -> list[asyncpg.Record]:
    sql = """
        SELECT
            lv.id,
            lv.email,
            lv.business_name,
            lv.status,
            lv.sent_at,
            lv.replied_at,
            lv.booked_at,
            lv.created_at,
            ob.id AS onboarding_id,
            ob.kyc_status,
            ob.nda_status,
            ob.agreement_status
        FROM leads_v2 lv
        LEFT JOIN onboarding_records ob ON ob.lead_id = lv.id
    """
    if where:
        sql += f" WHERE {where}"
    sql += " ORDER BY lv.created_at DESC"
    return await conn.fetch(sql, *params)


def _print_table(rows: list[asyncpg.Record]) -> None:
    if not rows:
        print("  (no leads found)")
        return
    print(f"\n  {'#':<4} {'Email':<38} {'Company':<28} {'Status':<10} {'KYC':<12} {'NDA':<12} {'Agr':<12} {'Created'}")
    print("  " + "-" * 135)
    for i, r in enumerate(rows, 1):
        kyc  = r["kyc_status"]  or "—"
        nda  = r["nda_status"]  or "—"
        agr  = r["agreement_status"] or "—"
        print(
            f"  {i:<4} {r['email']:<38} {(r['business_name'] or ''):<28} "
            f"{r['status']:<10} {kyc:<12} {nda:<12} {agr:<12} {_fmt_dt(r['created_at'])}"
        )
    print()


async def _delete_by_ids(conn: asyncpg.Connection, ids: list[str], dry_run: bool) -> int:
    if not ids:
        return 0
    if dry_run:
        return len(ids)
    result = await conn.execute(
        "DELETE FROM leads_v2 WHERE id = ANY($1::uuid[])", ids
    )
    # result is e.g. "DELETE 3"
    return int(result.split()[-1])


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Remove leads from the Jane DB")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--email",  metavar="EMAIL",  help="Remove a specific lead by email")
    group.add_argument("--status", metavar="STATUS", help="Remove all leads with this status (new/sent/replied/booked/opted_out/bounced)")
    group.add_argument("--all",    action="store_true", help="Remove ALL leads")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be deleted without deleting")
    args = parser.parse_args()

    try:
        conn = await asyncpg.connect(DB_URL)
    except Exception as e:
        print(f"[ERROR] Cannot connect to DB: {e}")
        print(f"  Make sure postgres is running: docker-compose up -d postgres")
        sys.exit(1)

    try:
        # ── mode: single email ──────────────────────────────────────────────
        if args.email:
            rows = await _list_leads(conn, "LOWER(lv.email) = LOWER($1)", [args.email])
            if not rows:
                print(f"[WARN] No lead found with email: {args.email}")
                return
            _print_table(rows)
            if not args.dry_run:
                confirm = input(f"  Delete lead '{args.email}'? [y/N] ").strip().lower()
                if confirm != "y":
                    print("  Aborted.")
                    return
            n = await _delete_by_ids(conn, [str(rows[0]["id"])], args.dry_run)
            _report(n, args.dry_run)

        # ── mode: by status ─────────────────────────────────────────────────
        elif args.status:
            rows = await _list_leads(conn, "LOWER(lv.status) = LOWER($1)", [args.status])
            if not rows:
                print(f"[WARN] No leads found with status: {args.status}")
                return
            _print_table(rows)
            if not args.dry_run:
                confirm = input(f"  Delete {len(rows)} lead(s) with status '{args.status}'? [y/N] ").strip().lower()
                if confirm != "y":
                    print("  Aborted.")
                    return
            n = await _delete_by_ids(conn, [str(r["id"]) for r in rows], args.dry_run)
            _report(n, args.dry_run)

        # ── mode: delete all ────────────────────────────────────────────────
        elif args.all:
            rows = await _list_leads(conn)
            if not rows:
                print("  No leads in DB.")
                return
            _print_table(rows)
            if not args.dry_run:
                confirm = input(f"  !! Delete ALL {len(rows)} leads? This cannot be undone. Type 'yes' to confirm: ").strip().lower()
                if confirm != "yes":
                    print("  Aborted.")
                    return
            n = await _delete_by_ids(conn, [str(r["id"]) for r in rows], args.dry_run)
            _report(n, args.dry_run)

        # ── mode: interactive ───────────────────────────────────────────────
        else:
            rows = await _list_leads(conn)
            if not rows:
                print("  No leads in DB.")
                return
            _print_table(rows)
            print("  Options:")
            print("    a          — delete all")
            print("    1,3,5      — delete by row numbers (comma-separated)")
            print("    @email     — delete by email address")
            print("    q          — quit")
            choice = input("\n  Choice: ").strip().lower()

            if choice in ("q", ""):
                print("  Quit.")
                return
            elif choice == "a":
                confirm = input(f"  !! Delete ALL {len(rows)} leads? Type 'yes': ").strip().lower()
                if confirm != "yes":
                    print("  Aborted.")
                    return
                targets = rows
            elif choice.startswith("@"):
                email = choice[1:].strip()
                targets = [r for r in rows if r["email"].lower() == email.lower()]
                if not targets:
                    print(f"  No lead found: {email}")
                    return
            else:
                indices = []
                for part in choice.split(","):
                    part = part.strip()
                    if part.isdigit():
                        idx = int(part) - 1
                        if 0 <= idx < len(rows):
                            indices.append(idx)
                        else:
                            print(f"  [WARN] Row {part} out of range, skipping")
                targets = [rows[i] for i in indices]
                if not targets:
                    print("  No valid rows selected.")
                    return

            print(f"\n  About to delete {len(targets)} lead(s):")
            for r in targets:
                print(f"    • {r['email']}  ({r['business_name']})")
            confirm = input("\n  Confirm? [y/N] ").strip().lower()
            if confirm != "y":
                print("  Aborted.")
                return
            n = await _delete_by_ids(conn, [str(r["id"]) for r in targets], dry_run=False)
            _report(n, dry_run=False)

    finally:
        await conn.close()


def _report(n: int, dry_run: bool) -> None:
    if dry_run:
        print(f"\n  [DRY RUN] Would delete {n} lead(s) (+ their onboarding records and KYC submissions).")
    else:
        print(f"\n  Deleted {n} lead(s) (onboarding records and KYC submissions removed via cascade).")


if __name__ == "__main__":
    asyncio.run(main())
