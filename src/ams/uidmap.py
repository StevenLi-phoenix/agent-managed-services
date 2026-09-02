"""Sub-uid/gid block allocation for services.

The harness user owns one contiguous range in ``/etc/subuid`` and
``/etc/subgid`` (e.g. ``harness:100000:65536``). Each service gets a fixed-size
block carved out of it; assignments are persisted so a service keeps its block
(and therefore file ownership) across harness restarts.

Only ``UidBlock`` is defined here for now; the allocator is implemented in the
allocator task (see .claude/state/PROGRESS.md).
"""

from __future__ import annotations

import logging
import pwd
import re
from dataclasses import dataclass
from pathlib import Path

from ams.spawn import INNER_GID, INNER_UID
from ams.state import StateCorrupt, read_json_checked, write_json_atomic

log = logging.getLogger("ams.uidmap")

# Width of the block mapped into every service's user namespace.
BLOCK_SIZE = 1024


@dataclass(frozen=True)
class UidBlock:
    """Host-side uid/gid block for one service.

    ``uid_start``/``gid_start`` are host ids. Inside the namespace the block
    appears as ``INNER_UID .. INNER_UID + size - 1`` (and the same for gids).
    """

    uid_start: int
    gid_start: int
    size: int = BLOCK_SIZE

    def newuidmap_args(self) -> list[str]:
        """``newuidmap <pid> <args>``: inner start, host start, count."""
        return [str(INNER_UID), str(self.uid_start), str(self.size)]

    def newgidmap_args(self) -> list[str]:
        return [str(INNER_GID), str(self.gid_start), str(self.size)]


class UidExhausted(RuntimeError):
    """No free ``block_size``-wide block left in the configured subid ranges."""


@dataclass(frozen=True)
class SubidRange:
    """One ``name:start:count`` line from ``/etc/subuid``/``/etc/subgid``."""

    start: int
    count: int

    @property
    def end(self) -> int:
        """Exclusive upper bound: the range covers ``[start, end)``."""
        return self.start + self.count


def parse_subid_file(text: str, user: str) -> list[tuple[int, int]]:
    """Parse ``/etc/subuid``-style text, returning ``(start, count)`` ranges for ``user``.

    Lines are ``name:start:count``; ``#`` starts a comment; blank lines are
    skipped. ``name`` may be the username or (as some tools write it) the
    numeric uid as a string -- both are matched literally against ``user``,
    so callers wanting both should call this twice (once with the username,
    once with the resolved uid string) and combine the results. Malformed
    lines are skipped with a logged warning rather than raising, so one bad
    line does not take down the whole file.
    """
    ranges: list[tuple[int, int]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) != 3:
            log.warning("subid line %d: expected name:start:count, got %r", lineno, raw)
            continue
        name, start_s, count_s = parts
        if name != user:
            continue
        if not re.fullmatch(r"\d+", start_s) or not re.fullmatch(r"\d+", count_s):
            log.warning("subid line %d: non-integer start/count: %r", lineno, raw)
            continue
        ranges.append((int(start_s), int(count_s)))
    return ranges


class UidAllocator:
    """Carves fixed-size uid/gid blocks out of the harness's subuid/subgid ranges.

    Assignments are keyed by service id, persisted as JSON, and idempotent:
    allocating an already-known service id returns its existing block rather
    than carving a new one.
    """

    def __init__(
        self,
        uid_ranges: list[SubidRange],
        gid_ranges: list[SubidRange],
        state_path: Path,
        block_size: int = BLOCK_SIZE,
    ) -> None:
        self._uid_ranges = list(uid_ranges)
        self._gid_ranges = list(gid_ranges)
        self.state_path = state_path
        self.block_size = block_size
        self._blocks: dict[str, UidBlock] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        data = read_json_checked(self.state_path)
        if not isinstance(data, dict):
            raise StateCorrupt(f"{self.state_path}: expected a JSON object at top level")
        stored_block_size = data.get("block_size")
        if stored_block_size != self.block_size:
            raise ValueError(
                f"uidmap state at {self.state_path} was written with "
                f"block_size={stored_block_size}, but this allocator is configured "
                f"with block_size={self.block_size}; refusing to re-carve existing "
                "blocks under a different width"
            )
        blocks = data.get("blocks", {})
        if not isinstance(blocks, dict):
            raise StateCorrupt(f"{self.state_path}: 'blocks' must be an object")
        for service_id, b in blocks.items():
            block = self._parse_block(service_id, b)
            self._validate_block_placement(service_id, block)
            self._blocks[service_id] = block

    def _parse_block(self, service_id: str, b: object) -> UidBlock:
        if not isinstance(b, dict):
            raise StateCorrupt(f"{self.state_path}: block for {service_id!r} is not an object")
        try:
            uid_start, gid_start, size = b["uid_start"], b["gid_start"], b["size"]
        except KeyError as e:
            raise StateCorrupt(
                f"{self.state_path}: block for {service_id!r} is missing field {e}"
            ) from e
        for field_name, value in (
            ("uid_start", uid_start),
            ("gid_start", gid_start),
            ("size", size),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise StateCorrupt(
                    f"{self.state_path}: block for {service_id!r} field {field_name!r} "
                    f"is not an integer: {value!r}"
                )
        if uid_start < 0 or gid_start < 0 or size <= 0:
            raise StateCorrupt(
                f"{self.state_path}: block for {service_id!r} has an out-of-range value "
                f"(uid_start={uid_start}, gid_start={gid_start}, size={size})"
            )
        return UidBlock(uid_start=uid_start, gid_start=gid_start, size=size)

    def _validate_block_placement(self, service_id: str, block: UidBlock) -> None:
        """Reject a stored block that does not fit a currently-configured
        subid range: wrong width, outside the range, or not aligned to
        ``block_size`` within it. Never silently re-carve over such a
        block -- e.g. the host's subuid range start moved since the block
        was assigned -- surface it so an operator can decide."""
        if block.size != self.block_size:
            raise StateCorrupt(
                f"{self.state_path}: block for {service_id!r} has size={block.size}, "
                f"expected {self.block_size}"
            )
        if not self._fits_some_range(block.uid_start, block.size, self._uid_ranges):
            raise StateCorrupt(
                f"{self.state_path}: block for {service_id!r} uid_start={block.uid_start} "
                "does not fit inside any configured subuid range at a block_size-aligned "
                "offset"
            )
        if not self._fits_some_range(block.gid_start, block.size, self._gid_ranges):
            raise StateCorrupt(
                f"{self.state_path}: block for {service_id!r} gid_start={block.gid_start} "
                "does not fit inside any configured subgid range at a block_size-aligned "
                "offset"
            )

    @staticmethod
    def _fits_some_range(start: int, size: int, ranges: list[SubidRange]) -> bool:
        return any(
            r.start <= start and start + size <= r.end and (start - r.start) % size == 0
            for r in ranges
        )

    @staticmethod
    def _overlaps(a_start: int, a_size: int, b_start: int, b_size: int) -> bool:
        return a_start < b_start + b_size and b_start < a_start + a_size

    def _save(self) -> None:
        data = {
            "version": 1,
            "block_size": self.block_size,
            "blocks": {
                sid: {"uid_start": b.uid_start, "gid_start": b.gid_start, "size": b.size}
                for sid, b in self._blocks.items()
            },
        }
        write_json_atomic(self.state_path, data)

    def get(self, service_id: str) -> UidBlock | None:
        return self._blocks.get(service_id)

    def assignments(self) -> dict[str, UidBlock]:
        return dict(self._blocks)

    def _lowest_free_index(self) -> int:
        """Lowest block-grid index (in the first uid/gid range) whose uid
        interval AND gid interval overlap no *currently stored* block in
        either dimension. Deliberately interval-overlap based rather than
        derived from ``uid_start`` alone: an index computed only from the
        uid side can miss a gid-only collision (e.g. a stored block whose
        gid_start does not correspond to the same grid index as its
        uid_start -- tolerated by ``_validate_block_placement`` as long as
        each side independently fits *some* configured range)."""
        if not self._uid_ranges or not self._gid_ranges:
            raise UidExhausted("no subid ranges configured")
        ur, gr = self._uid_ranges[0], self._gid_ranges[0]
        max_blocks = min(ur.count, gr.count) // self.block_size
        for i in range(max_blocks):
            cand_uid = ur.start + i * self.block_size
            cand_gid = gr.start + i * self.block_size
            if any(
                self._overlaps(cand_uid, self.block_size, b.uid_start, b.size)
                or self._overlaps(cand_gid, self.block_size, b.gid_start, b.size)
                for b in self._blocks.values()
            ):
                continue
            return i
        raise UidExhausted(
            f"no free {self.block_size}-wide block in uid range {ur.start}..{ur.end} "
            f"/ gid range {gr.start}..{gr.end} ({max_blocks} blocks, all in use)"
        )

    def allocate(self, service_id: str) -> UidBlock:
        """Idempotent: returns the existing block for a known id, else carves
        the lowest free block (by index) from the first uid range and the
        first gid range. Raises ``UidExhausted`` when none is left."""
        existing = self._blocks.get(service_id)
        if existing is not None:
            return existing
        i = self._lowest_free_index()
        ur, gr = self._uid_ranges[0], self._gid_ranges[0]
        block = UidBlock(
            uid_start=ur.start + i * self.block_size,
            gid_start=gr.start + i * self.block_size,
            size=self.block_size,
        )
        self._blocks[service_id] = block
        self._save()
        return block

    def release(self, service_id: str) -> None:
        """Forget the assignment (idempotent: releasing an unknown id is a no-op).

        This only frees the block for reuse by a future ``allocate()``; any
        host-side files already owned by the block's uid/gid outlive this
        call and must be removed via the admin uid/gid map (see
        ``admin_map_args``) before the block is handed to a different
        service, or that service would inherit the previous owner's files.
        """
        if service_id in self._blocks:
            del self._blocks[service_id]
            self._save()

    @classmethod
    def from_host(
        cls,
        user: str,
        state_path: Path,
        subuid: Path = Path("/etc/subuid"),
        subgid: Path = Path("/etc/subgid"),
    ) -> UidAllocator:
        """Read ranges for ``user`` from ``subuid``/``subgid``, matching both
        the username and (per ``parse_subid_file``'s own caveat) the numeric
        uid/gid some tools write instead -- merging both so a host using
        either convention is picked up."""
        uid_keys = [user]
        gid_keys = [user]
        try:
            pw = pwd.getpwnam(user)
            uid_keys.append(str(pw.pw_uid))
            gid_keys.append(str(pw.pw_gid))
        except KeyError:
            log.warning("no local passwd entry for %r; matching subuid/subgid by name only", user)

        uid_text = subuid.read_text(encoding="utf-8")
        gid_text = subgid.read_text(encoding="utf-8")
        uid_ranges = [
            SubidRange(start, count)
            for key in dict.fromkeys(uid_keys)
            for start, count in parse_subid_file(uid_text, key)
        ]
        gid_ranges = [
            SubidRange(start, count)
            for key in dict.fromkeys(gid_keys)
            for start, count in parse_subid_file(gid_text, key)
        ]
        if not uid_ranges:
            raise RuntimeError(
                f"no subuid range for {user!r} in {subuid}; add a line like "
                f"'{user}:100000:65536' and re-run"
            )
        if not gid_ranges:
            raise RuntimeError(
                f"no subgid range for {user!r} in {subgid}; add a line like "
                f"'{user}:100000:65536' and re-run"
            )
        return cls(uid_ranges, gid_ranges, state_path)


def admin_map_args(
    block: UidBlock, harness_uid: int, harness_gid: int
) -> tuple[list[str], list[str]]:
    """``newuidmap``/``newgidmap`` args for the harness's own admin map.

    Unlike the runtime map used while a service runs (``UidBlock.newuidmap_args``,
    inner 1000 only), the admin map also keeps the harness's own uid mapped as
    inner 0, so the harness can chown/rm files it created inside a service's
    block before releasing it (see DECISIONS.md D4).
    """
    uid_args = ["0", str(harness_uid), "1", str(INNER_UID), str(block.uid_start), str(block.size)]
    gid_args = ["0", str(harness_gid), "1", str(INNER_GID), str(block.gid_start), str(block.size)]
    return uid_args, gid_args
