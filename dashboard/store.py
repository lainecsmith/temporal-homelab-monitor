"""SQLite copy of what Temporal knows: last-known snapshots and update aging.
Temporal stays the source of truth for live state; nothing here is written by workers.

One connection, used only from the event loop thread. The database is tiny and writes happen
once per poll, so blocking calls are fine here."""
import json
import sqlite3
from pathlib import Path
from typing import Optional

from dashboard.normalize import normalize

SCHEMA = Path(__file__).with_name("schema.sql")


def _flag(value) -> Optional[int]:
    return None if value is None else int(bool(value))


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA.read_text())

    # ---------------------------------------------------------------- write

    def save(self, host: str, snapshot: dict, fetched_at: str) -> None:
        """Track update aging, then store the snapshot, in one transaction."""
        n = normalize(snapshot)
        with self.db:
            prev = self.db.execute(
                "SELECT updates_checked_at FROM host_snapshot WHERE host = ?", (host,)).fetchone()
            self._track_updates(host, n, prev["updates_checked_at"] if prev else None)
            s = n["summary"]
            self.db.execute(
                """INSERT INTO host_snapshot (host, fetched_at, status, last_seen, updates_checked_at,
                       os_updates, security_updates, image_updates, reboot_required, snapshot_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(host) DO UPDATE SET
                       fetched_at = excluded.fetched_at, status = excluded.status,
                       last_seen = excluded.last_seen, updates_checked_at = excluded.updates_checked_at,
                       os_updates = excluded.os_updates, security_updates = excluded.security_updates,
                       image_updates = excluded.image_updates, reboot_required = excluded.reboot_required,
                       snapshot_json = excluded.snapshot_json""",
                (host, fetched_at, snapshot.get("status") or "unknown", snapshot.get("last_seen"),
                 snapshot.get("updates_checked_at"), s["os_updates"], s["security_updates"],
                 s["image_updates"], _flag(s["reboot_required"]), json.dumps(snapshot)))

    def _track_updates(self, host: str, n: dict, prev_attempt: Optional[str]) -> None:
        """Rules that keep the aging clock honest:
        - only look at a check once (compare the attempt timestamp), not every 30 s poll
        - a failed or missing check neither creates nor resolves anything
        - an OS package can only be resolved if the stored list was complete (the workflow caps
          it), otherwise "missing from the list" could just mean "past the cap"."""
        u = n["updates"]
        if u["state"] != "ok" or u["last_attempt_at"] == prev_attempt:
            return
        at = u["checked_at"]
        seen, resolvable = {}, set()

        os_ = u["os"]
        if os_["state"] == "ok":
            for p in os_["packages"]:
                seen[("os_package", p["name"])] = (p.get("current"), p.get("available"), _flag(p.get("security")), None)
            if os_["hidden_count"] == 0:
                resolvable.add("os_package")

        images = u["images"]
        if images["state"] == "ok":
            resolvable |= {"image_build", "image_tag"}
            for it in images["items"]:
                if it["new_build"]:
                    seen[("image_build", it["image"])] = (None, None, None, None)
                if it["newest_tag"]:
                    seen[("image_tag", it["image"])] = (
                        None, it["newest_tag_same_major"] or it["newest_tag"], None,
                        json.dumps({"newest_tag": it["newest_tag"], "newest_tag_same_major": it["newest_tag_same_major"]}))

        open_rows = {(r["kind"], r["name"]): r["id"] for r in self.db.execute(
            "SELECT id, kind, name FROM pending_update WHERE host = ? AND resolved_at IS NULL", (host,))}
        for (kind, name), (current, available, security, detail) in seen.items():
            if (kind, name) in open_rows:
                self.db.execute(
                    """UPDATE pending_update SET last_seen_at = ?, current_version = ?, available_version = ?,
                       is_security = ?, detail_json = ? WHERE id = ?""",
                    (at, current, available, security, detail, open_rows[(kind, name)]))
            else:
                self.db.execute(
                    """INSERT INTO pending_update (host, kind, name, first_seen_at, last_seen_at,
                       current_version, available_version, is_security, detail_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (host, kind, name, at, at, current, available, security, detail))
        for key, row_id in open_rows.items():
            if key[0] in resolvable and key not in seen:
                self.db.execute("UPDATE pending_update SET resolved_at = ? WHERE id = ?", (at, row_id))

    # ---------------------------------------------------------------- read

    def rows(self) -> list:
        return [(r["host"], r["fetched_at"], json.loads(r["snapshot_json"]))
                for r in self.db.execute("SELECT host, fetched_at, snapshot_json FROM host_snapshot ORDER BY host")]

    def oldest_ages(self) -> dict:
        return {r["host"]: {"oldest_update_days": r["oldest"], "oldest_security_update_days": r["oldest_sec"]}
                for r in self.db.execute(
                    """SELECT host, MAX(age_days) AS oldest,
                              MAX(CASE WHEN is_security = 1 THEN age_days END) AS oldest_sec
                       FROM open_update_age GROUP BY host""")}

    def pending_updates(self, host: str) -> list:
        return [dict(r) for r in self.db.execute("SELECT * FROM open_update_age WHERE host = ?", (host,))]
