from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from monitor.appointedd import AppointeddClient
from monitor.config import Settings, load_settings
from monitor.notifier import format_alert_message, send_telegram_message
from monitor.state import build_updated_active_slots, diff_slots, load_state, save_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("monitor.main")


def append_run_log(log_path: Path, line: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line.rstrip() + "\n")


async def sync_state_to_git(state_path: Path, log_path: Path, branch: str = "state") -> None:
    """
    Atomically commit and push state & run logs to an orphan `state` branch
    using an isolated temporary git index (`GIT_INDEX_FILE`), without touching `main`.
    """
    files = [str(p) for p in (state_path, log_path) if p.exists()]
    if not files:
        return

    def _push() -> None:
        idx_path = "/tmp/lego_workshop_state_git_index"
        if os.path.exists(idx_path):
            try:
                os.remove(idx_path)
            except OSError:
                pass
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = idx_path
        env.setdefault("GIT_AUTHOR_NAME", "github-actions[bot]")
        env.setdefault("GIT_AUTHOR_EMAIL", "github-actions[bot]@users.noreply.github.com")
        env.setdefault("GIT_COMMITTER_NAME", "github-actions[bot]")
        env.setdefault("GIT_COMMITTER_EMAIL", "github-actions[bot]@users.noreply.github.com")

        subprocess.run(["git", "add", "-f", *files], env=env, check=True, capture_output=True)
        tree_sha = subprocess.check_output(["git", "write-tree"], env=env, text=True).strip()
        commit_sha = subprocess.check_output(
            ["git", "commit-tree", tree_sha, "-m", "Update workshop slots state and run logs"],
            env=env,
            text=True,
        ).strip()
        subprocess.run(
            ["git", "push", "-f", "origin", f"{commit_sha}:refs/heads/{branch}"],
            env=env,
            check=True,
            capture_output=True,
        )

    try:
        await asyncio.to_thread(_push)
        logger.info("Synced %s to origin/%s.", ", ".join(files), branch)
    except Exception as exc:
        logger.warning("Git state sync skipped/failed: %s", exc)


async def run_single_check(
    settings: Settings,
    client: AppointeddClient,
) -> tuple[bool, int, int, bool]:
    """
    Run one availability check.
    Returns: (ok, total_open_slots, alerts_sent, state_changed)
    """
    state = load_state(settings.state_path)
    prev_active = state.get("active_slots") or {}

    try:
        result = await client.check_availability(
            date_from=settings.date_from,
            date_to=settings.date_to,
            tz_name=settings.timezone,
            min_spaces=settings.min_spaces,
        )
    except Exception as exc:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.exception("Appointedd availability check failed: %s", exc)
        append_run_log(settings.run_log_path, f"{now_utc} | ERROR | {exc}")
        return False, 0, 0, False

    diff = diff_slots(prev_active, result.slots)
    new_active = build_updated_active_slots(result.slots)
    state_changed = new_active != prev_active

    alerts_sent = 0
    if diff.has_alerts:
        msg = format_alert_message(
            widget_name=result.widget_name,
            booking_url=settings.booking_url,
            diff=diff,
            all_current_slots=result.slots,
        )
        alerts_sent = await send_telegram_message(
            bot_token=settings.telegram_bot_token,
            chat_ids=settings.telegram_chat_ids,
            text=msg,
            booking_url=settings.booking_url,
        )
        logger.info(
            "ALERT! %d new slot(s), %d increased slot(s) -> notified %d Telegram chat(s).",
            len(diff.new_slots),
            len(diff.increased_space_slots),
            alerts_sent,
        )
    else:
        logger.info(
            "Checked %s (%s..%s): %d open slot(s), 0 new.",
            result.widget_name,
            settings.date_from.isoformat(),
            settings.date_to.isoformat(),
            len(result.slots),
        )

    state["active_slots"] = new_active
    state["last_checked_utc"] = result.checked_at_utc
    state["total_checks"] = int(state.get("total_checks", 0)) + 1
    state["total_alerts_sent"] = int(state.get("total_alerts_sent", 0)) + alerts_sent
    save_state(settings.state_path, state)

    append_run_log(
        settings.run_log_path,
        (
            f"{result.checked_at_utc} | OK | services={result.services_checked} "
            f"| open={len(result.slots)} | new={len(diff.new_slots)} "
            f"| increased={len(diff.increased_space_slots)} | alerts={alerts_sent}"
        ),
    )
    return True, len(result.slots), alerts_sent, state_changed


async def run_daemon(
    settings: Settings,
    *,
    duration_minutes: Optional[int],
    interval_minutes: int,
    sync_git: bool,
) -> int:
    client = AppointeddClient(widget_id=settings.widget_id)
    loop = asyncio.get_running_loop()
    start_mono = loop.time()
    deadline_mono = start_mono + duration_minutes * 60 if duration_minutes else None

    checks_total = 0
    checks_ok = 0
    checks_failed = 0
    total_alerts = 0
    max_open_seen = 0

    logger.info(
        "Starting Appointedd monitor: widget=%s, dates=%s..%s (%s), interval=%dm, window=%s",
        settings.widget_id,
        settings.date_from.isoformat(),
        settings.date_to.isoformat(),
        settings.timezone,
        interval_minutes,
        f"{duration_minutes}m" if duration_minutes else "infinite",
    )

    try:
        while True:
            cycle_start = loop.time()
            checks_total += 1
            ok, open_count, alerts_sent, state_changed = await run_single_check(settings, client)
            if ok:
                checks_ok += 1
                max_open_seen = max(max_open_seen, open_count)
                total_alerts += alerts_sent
            else:
                checks_failed += 1

            if sync_git and (state_changed or alerts_sent > 0):
                await sync_state_to_git(settings.state_path, settings.run_log_path)

            if deadline_mono is not None:
                remaining = deadline_mono - loop.time()
                # Stop cleanly if not enough time for another sleep + check
                if remaining <= 30:
                    logger.info("Reached end of %dm daemon window (%.0fs remaining). Exiting cleanly.", duration_minutes, max(0.0, remaining))
                    break
                elapsed = loop.time() - cycle_start
                sleep_sec = max(5.0, interval_minutes * 60 - elapsed)
                if sleep_sec > remaining - 20:
                    logger.info(
                        "Next scheduled check in %.0fs would exceed window (%.0fs left). Exiting cleanly.",
                        sleep_sec,
                        remaining,
                    )
                    break
                logger.info("Sleeping %.0fs until next check...", sleep_sec)
                await asyncio.sleep(sleep_sec)
            else:
                elapsed = loop.time() - cycle_start
                await asyncio.sleep(max(5.0, interval_minutes * 60 - elapsed))
    finally:
        if sync_git:
            await sync_state_to_git(settings.state_path, settings.run_log_path)
        logger.info(
            "Session summary: checks=%d (ok=%d, failed=%d) | max_open_slots=%d | alerts_sent=%d",
            checks_total,
            checks_ok,
            checks_failed,
            max_open_seen,
            total_alerts,
        )

    # Only fail the workflow if EVERY check failed (transient single-check hiccups shouldn't kill the 3h run)
    return 1 if (checks_total > 0 and checks_ok == 0) else 0


async def send_test_notification(settings: Settings) -> int:
    text = (
        "🧪 <b>Test Notification — Świąteczna Pracownia LEGO® Monitor</b>\n\n"
        f"Monitoring dates: <b>{settings.date_from.isoformat()}</b> to <b>{settings.date_to.isoformat()}</b> "
        f"({settings.timezone})\n"
        f"Widget ID: <code>{settings.widget_id}</code>\n"
        f"Minimum spaces: <b>{settings.min_spaces}</b>\n\n"
        "You will receive an instant alert here as soon as any slot opens!"
    )
    sent = await send_telegram_message(
        bot_token=settings.telegram_bot_token,
        chat_ids=settings.telegram_chat_ids,
        text=text,
        booking_url=settings.booking_url,
    )
    logger.info("Test notification sent to %d chat(s).", sent)
    return 0 if sent > 0 else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Appointedd appointment slot monitor")
    parser.add_argument("--once", action="store_true", help="Run a single availability check and exit.")
    parser.add_argument(
        "--daemon-minutes",
        type=int,
        default=None,
        help="Run check loop for N minutes (e.g. 172 for a 3-hour GitHub Actions window).",
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="Minutes between checks in daemon mode (default: CHECK_INTERVAL_MINUTES or 1).",
    )
    parser.add_argument(
        "--sync-git-state",
        action="store_true",
        help="Push state & run logs to the orphan `state` branch during/after execution.",
    )
    parser.add_argument(
        "--test-notify",
        action="store_true",
        help="Send a test Telegram notification and exit.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    settings = load_settings()

    if args.test_notify:
        sys.exit(asyncio.run(send_test_notification(settings)))

    if args.once:
        async def _once() -> int:
            client = AppointeddClient(widget_id=settings.widget_id)
            ok, _, _, _ = await run_single_check(settings, client)
            if args.sync_git_state:
                await sync_state_to_git(settings.state_path, settings.run_log_path)
            return 0 if ok else 1

        sys.exit(asyncio.run(_once()))

    interval = args.interval_minutes or settings.check_interval_minutes
    exit_code = asyncio.run(
        run_daemon(
            settings,
            duration_minutes=args.daemon_minutes,
            interval_minutes=interval,
            sync_git=args.sync_git_state,
        )
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
