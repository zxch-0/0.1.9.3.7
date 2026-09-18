"""In-memory state with a small JSON snapshot: admins, submissions, cooldowns.

Discord-free on purpose, so it is easy to test. On Render's free tier the disk is
ephemeral (wiped on every deploy) — that is fine: the env var ``ADMIN_USER_IDS``
is always the source of truth and this file only carries runtime tweaks and the
"this form is already handled" lock between restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("formbot.store")

# Ambiguous glyphs (0/O, 1/I/l) are excluded so codes survive copy-paste.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SCHEMA_VERSION = 1


def make_code(length: int = 4) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


@dataclass(slots=True)
class Submission:
    """One filled-out form, from the modal to the last admin note."""

    code: str
    submitter_id: int
    submitter_tag: str = ""
    result_user_id: int = 0
    result_source: str = "self"  # self | provided | unverified
    infos: str = ""
    note: str = ""  # what the bot could not verify about the typed ID
    page: int = 0
    extra: dict[str, str] = field(default_factory=dict)  # answers from pages 2+
    guild_id: int | None = None
    guild_name: str = "DM"
    channel_id: int | None = None
    channel_name: str = ""
    is_admin_origin: bool = False
    all_admin_ids: list[int] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    status: str = "open"  # open | handled
    handled_by: int | None = None
    handled_at: float | None = None
    admin_message_ids: dict[str, int] = field(default_factory=dict)  # admin id -> DM message id
    delivery: dict[str, str] = field(default_factory=dict)  # admin id -> delivered|forbidden|missing|error
    notes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_handled(self) -> bool:
        return self.status == "handled"

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created_at)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = round(self.created_at, 3)
        if self.handled_at:
            data["handled_at"] = round(self.handled_at, 3)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Submission:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        clean = {k: v for k, v in data.items() if k in known}
        clean.setdefault("code", "????")
        clean.setdefault("submitter_id", 0)
        return cls(**clean)


class StateStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        admins: Iterable[int] = (),
        persist: bool = True,
        max_stored: int = 200,
    ) -> None:
        self.path = Path(path) if path else None
        self.persist = bool(persist and self.path is not None)
        self.max_stored = max(10, int(max_stored))
        self._lock = asyncio.Lock()
        self._admins: set[int] = {int(a) for a in admins if a}
        self._submissions: dict[str, Submission] = {}
        self._cooldowns: dict[int, float] = {}
        self._pings: int = 0
        self._loaded = False

    # ------------------------------------------------------------------ admins

    @property
    def admin_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._admins))

    def is_admin(self, user_id: int) -> bool:
        return int(user_id) in self._admins

    def add_admin(self, user_id: int) -> bool:
        """Returns True when the set actually changed."""
        user_id = int(user_id)
        if user_id in self._admins:
            return False
        self._admins.add(user_id)
        return True

    def remove_admin(self, user_id: int) -> bool:
        user_id = int(user_id)
        if user_id not in self._admins:
            return False
        self._admins.discard(user_id)
        return True

    def reset_admins(self, user_ids: Iterable[int]) -> None:
        """Env var wins on every boot; runtime additions are merged on top."""
        self._admins = {int(u) for u in user_ids if u}

    # -------------------------------------------------------------- submissions

    def get(self, code: str) -> Submission | None:
        """Look a form up by its code, tolerating "#AB12" / "F-AB12" / "fab12".

        Careful: this must not be ``lstrip("#F")`` — that silently eats codes that
        happen to start with the letter F, and the whole form then looks lost.
        """
        key = (code or "").strip().upper().lstrip("#")
        if key.startswith("F-"):
            key = key[2:]
        return self._submissions.get(key)

    @property
    def submissions(self) -> tuple[Submission, ...]:
        return tuple(self._submissions.values())

    @property
    def open_count(self) -> int:
        return sum(1 for sub in self._submissions.values() if not sub.is_handled)

    async def create(self, **kwargs: Any) -> Submission:
        async with self._lock:
            code = self._unique_code()
            sub = Submission(code=code, **kwargs)
            self._submissions[code] = sub
            self._trim()
            self._save_nowait()
            return sub

    async def claim(self, code: str, admin_id: int) -> tuple[Submission | None, bool]:
        """Anti-double-work lock: exactly one admin wins the claim."""
        sub = self.get(code)
        if sub is None:
            return None, False
        async with self._lock:
            if sub.is_handled:
                return sub, False
            sub.status = "handled"
            sub.handled_by = int(admin_id)
            sub.handled_at = time.time()
            self._save_nowait()
        return sub, True

    async def record_note(self, code: str, *, admin_id: int, text: str, delivered: bool) -> None:
        sub = self.get(code)
        if sub is None:
            return
        async with self._lock:
            sub.notes.append(
                {"admin_id": int(admin_id), "text": text[:1800], "delivered": bool(delivered), "at": round(time.time(), 3)}
            )
            self._save_nowait()

    async def record_delivery(self, code: str, *, admin_id: int, status: str) -> None:
        sub = self.get(code)
        if sub is None:
            return
        async with self._lock:
            sub.delivery[str(int(admin_id))] = status
            self._save_nowait()

    async def record_admin_message(self, code: str, *, admin_id: int, message_id: int) -> None:
        sub = self.get(code)
        if sub is None:
            return
        async with self._lock:
            sub.admin_message_ids[str(int(admin_id))] = int(message_id)
            self._save_nowait()

    def _unique_code(self) -> str:
        for _ in range(50):
            code = make_code()
            if code not in self._submissions:
                return code
        code = make_code(6)
        while code in self._submissions:  # pragma: no cover
            code = make_code(6)
        return code

    def _trim(self) -> None:
        if len(self._submissions) <= self.max_stored:
            return
        ordered = sorted(self._submissions.items(), key=lambda kv: kv[1].created_at, reverse=True)
        self._submissions = dict(ordered[: self.max_stored])

    # -------------------------------------------------------------- anti-spam

    def cooldown_remaining(self, user_id: int, seconds: int) -> int:
        """Seconds left before this member may submit again (0 = allowed)."""
        if seconds <= 0:
            return 0
        last = self._cooldowns.get(int(user_id))
        if last is None:
            return 0
        left = seconds - (time.time() - last)
        return max(0, int(left + 0.999))

    def touch(self, user_id: int) -> None:
        self._cooldowns[int(user_id)] = time.time()

    def prune_cooldowns(self, window: int = 3600) -> int:
        cutoff = time.time() - max(60, window)
        stale = [uid for uid, when in self._cooldowns.items() if when < cutoff]
        for uid in stale:
            del self._cooldowns[uid]
        return len(stale)

    # ----------------------------------------------------------- keep-alive

    def ping(self) -> int:
        self._pings += 1
        return self._pings

    @property
    def pings(self) -> int:
        return self._pings

    # ------------------------------------------------------------- persistence

    async def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.persist or self.path is None or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # corrupt file must never stop the bot
            log.warning("could not read %s (%s) — starting fresh", self.path, exc)
            return
        admins = raw.get("admins") or []
        # merge, never replace: env admins are already loaded in _admins
        for entry in admins:
            try:
                self._admins.add(int(entry))
            except (TypeError, ValueError):
                continue
        for entry in raw.get("submissions") or []:
            try:
                sub = Submission.from_json(entry)
            except Exception:
                continue
            self._submissions[sub.code] = sub
        log.info("restored %d form(s) and %d admin(s) from disk", len(self._submissions), len(self._admins))

    def _save_nowait(self) -> None:
        if not self.persist or self.path is None:
            return
        try:
            self._write()
        except Exception as exc:  # pragma: no cover
            log.warning("could not write %s (%s)", self.path, exc)

    def _write(self) -> None:
        assert self.path is not None
        payload = {
            "version": SCHEMA_VERSION,
            "saved_at": round(time.time(), 3),
            "admins": sorted(self._admins),
            "submissions": [
                sub.to_json() for sub in sorted(self._submissions.values(), key=lambda s: s.created_at, reverse=True)
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -------------------------------------------------------------- reporting

    def snapshot(self) -> dict[str, Any]:
        return {
            "admins": len(self._admins),
            "forms_total": len(self._submissions),
            "forms_open": self.open_count,
            "forms_handled": len(self._submissions) - self.open_count,
            "pings": self._pings,
        }
