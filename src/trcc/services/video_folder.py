"""VideoFolderService — which video in a folder plays next.

Feature: video folder playlist.  The user picks a folder; the App plays its
videos one after another, by name or shuffled.  This is the pure half: given
the folder's videos as they are NOW, pick the next one.  The folder itself is
read behind the ``ContentStore`` port (``list_videos``) by the caller, on every
pick, so a video added or removed between two videos is seen by the next one.

* **In order** — by name, wrapping round after the last.  When the video that
  just played has been deleted, the next is the first name after it.
* **Shuffled** — a shuffle bag: every video once per round, in random order,
  then a new round.  A round never starts with the video that ended the last
  one (when there is more than one), so no video plays twice running.

State is transient, like ``SlideshowService``: the persisted config
(``DeviceSettings.video_folder_*``) says what to play; this cursor is rebuilt
on the first pick of a process.  The RNG is injected so tests are
deterministic.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from ..core.logs import per_frame

log = logging.getLogger(__name__)
frame_log = per_frame(__name__)


@dataclass
class _Cursor:
    """Transient cursor for one device's playlist."""
    last: Path | None = None
    #: The videos already played in the current shuffle round.
    played: set[Path] = field(default_factory=set)


class VideoFolderService:
    """Per-device cursor over a folder's videos."""

    def __init__(self, rng: random.Random | None = None) -> None:
        log.debug("__init__: injected rng=%s", rng is not None)
        self._rng = rng if rng is not None else random.Random()
        self._cursors: dict[str, _Cursor] = {}

    def reset(self, key: str) -> None:
        """Forget *key*'s cursor -- a new folder starts from its beginning."""
        log.info("reset: key=%s", key)
        self._cursors.pop(key, None)

    def next(self, key: str, videos: list[Path], *, shuffle: bool) -> Path | None:
        """The video to play next from *videos* (sorted by name), or ``None``
        when there are none."""
        if not videos:
            log.warning("next: %s — the folder has no videos to play", key)
            return None
        cursor = self._cursors.setdefault(key, _Cursor())
        pick = (self._shuffled(cursor, videos) if shuffle
                else self._in_order(cursor, videos))
        cursor.last = pick
        log.info("next: %s → %s (%s, %d video(s))", key, pick.name,
                 "shuffled" if shuffle else "in order", len(videos))
        return pick

    def _in_order(self, cursor: _Cursor, videos: list[Path]) -> Path:
        """The name after the last one played, wrapping to the first."""
        last = cursor.last
        cursor.played.clear()   # a later switch to shuffle starts a fresh round
        if last is None:
            log.debug("_in_order: nothing played yet — first video")
            return videos[0]
        after = name_order(last)
        later = [v for v in videos if name_order(v) > after]
        log.debug("_in_order: %d video(s) sort after %s", len(later), last.name)
        return later[0] if later else videos[0]

    def _shuffled(self, cursor: _Cursor, videos: list[Path]) -> Path:
        """A random video not yet played this round; a new round once all
        have played."""
        # Re-derived from today's listing: a deleted video is never picked and
        # an added one joins the current round.
        left = [v for v in videos if v not in cursor.played]
        if not left:
            cursor.played.clear()
            left = [v for v in videos if v != cursor.last] or list(videos)
            log.debug("_shuffled: new round of %d (last was %s)", len(videos),
                      cursor.last.name if cursor.last else None)
        pick = self._rng.choice(left)
        cursor.played.add(pick)
        log.debug("_shuffled: %s of %d left this round", pick.name, len(left))
        return pick


def name_order(path: Path) -> tuple[str, str]:
    """The playlist's "by name": case-insensitive, the exact name only to
    break a tie.  ``ContentStore.list_videos`` sorts with it too, so "the
    name after" here agrees with the listing's order.  Called per comparison,
    so per-frame logging."""
    frame_log.debug("name_order: %s", path.name)
    return path.name.casefold(), path.name
