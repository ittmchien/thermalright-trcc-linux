"""The cadence behind a video folder playlist — what starts the next video.

Feature: video folder playlist.  ``SetVideoFolder`` saves the folder and starts
its first video once through (``loop=False``, so it ends at its last frame);
this driver notices the end and starts the next one.  It runs in the App, not
in a UI, so the playlist keeps playing when every window is closed — and every
UI (CLI, API, gui) gets it by dispatching the one Command.

**A ``SendTask``**, for the reasons ``slideshow_driver`` records: the contract
is ``key``/``wait``/``wake``/``run_once`` and ``SyncSendScheduler`` makes the
cadence testable by ticking a clock.

**The key is namespaced** (``videofolder:<device>``):
``ThreadSendScheduler.add`` STOPS any task under the same key, so the bare
device key would kill the device's own ``DeviceSender``.  ``App.stop_sources``
removes this key beside the slideshow's.

**One source at a time.**  Like the slideshow driver, it remembers what it put
on the panel (:func:`shown_on_panel`); when the panel shows something else — a
theme, a background, a cast or the media player picked from any UI — it
switches the playlist off and leaves the user's pick alone.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ..core.logs import per_frame
from ..core.models import MEDIA, VIDEO_FOLDER_POLL_S, MediaKind
from ._send_task import BaseSendTask, shown_on_panel

if TYPE_CHECKING:
    from ..app import App
    from .media import Playback

log = logging.getLogger(__name__)
frame_log = per_frame(__name__)


class VideoFolderDriver(BaseSendTask):
    """Starts the playlist's next video when the one playing has ended.

    Namespace + cadence + ``_run``; the wait/wake/key machinery is
    :class:`~trcc.services._send_task.BaseSendTask`.
    """

    KEY_PREFIX = "videofolder:"
    DEFAULT_INTERVAL_S = VIDEO_FOLDER_POLL_S
    #: The wait after a video failed to start: the next pick is tried then,
    #: so a folder of broken files costs a warning every few seconds, not four
    #: a second.
    RETRY_S: ClassVar[float] = 5.0

    def __init__(self, app: App, device_key: str,
                 interval_s: float | None = None) -> None:
        super().__init__(app, device_key, interval_s)
        log.info("VideoFolderDriver: %s — nothing started by it yet", device_key)
        # What THIS driver last put on the panel; None until it plays or
        # adopts a video.
        self._shown: tuple[object, ...] | None = None
        # The video it is playing (``background_path``), None until then.
        self._current: str | None = None

    def _run(self, now: float) -> float:
        """Start the next video if the current one has ended; return the
        seconds to wait before looking again."""
        from ..core.commands import LoopVideo, SetVideoFolder
        from ..core.commands._helpers import play_next_folder_video

        app, key = self._app, self._device_key
        if self._shown is not None and shown_on_panel(app, key) != self._shown:
            log.info("VideoFolderDriver: %s — the panel shows something the "
                     "playlist did not put there; switching it off", key)
            app.dispatch(SetVideoFolder(key=key, enabled=False))
            return self._interval
        if key in app.held:
            frame_log.debug("VideoFolderDriver: %s held by a pushed frame", key)
            return self._interval
        if self._shown is None:
            self._adopt()
        playback = app.media.playback(key)
        if (self._current is not None and playback is not None
                and app.settings.for_device(key).background_path == self._current):
            if playback.loop:
                # Re-decoded by someone else (a restore, a rotation) with the
                # default loop=True: it would never end.
                log.info("VideoFolderDriver: %s — %s plays looped; making it "
                         "play once", key, Path(self._current).name)
                app.dispatch(LoopVideo(key=key, loop=False))
            if not _has_ended(playback):
                frame_log.debug("VideoFolderDriver: %s — %s still playing (%d/%d)",
                                key, Path(self._current).name, playback.cursor,
                                len(playback.frames))
                return self._interval

        played = play_next_folder_video(app, key)
        panel = shown_on_panel(app, key)
        if played is None or not played.ok:
            self._current, self._shown = None, panel
            log.warning("VideoFolderDriver: %s — the next video did not start "
                        "(%s); trying another in %.0fs", key,
                        played.message if played else "no videos", self.RETRY_S)
            return self.RETRY_S
        # The background from THIS play's result, the rest read live: a pick
        # from another UI landing during the play must still read as foreign
        # -- the same split the slideshow driver makes for its theme.
        self._current = played.path
        self._shown = (panel[0], played.path, *panel[2:])
        log.info("VideoFolderDriver: %s → %s", key, Path(played.path).name)
        return self._interval

    def _adopt(self) -> None:
        """First turn: take over a folder video that is already playing.

        ``SetVideoFolder`` starts the first video itself, and a restore or a
        rotation re-registers this driver over a video it was playing -- in
        both, the playing video is the playlist's own and runs to its end.
        """
        app, key = self._app, self._device_key
        s = app.settings.for_device(key)
        current = s.background_path
        owned = (current is not None and s.video_folder_path is not None
                 and app.media.playback(key) is not None
                 and Path(current).parent == Path(s.video_folder_path)
                 and MEDIA.kind_of(Path(current)) is MediaKind.ANIMATED)
        log.info("VideoFolderDriver._adopt: %s background=%s owned=%s",
                 key, current, owned)
        if owned:
            self._current = current
            self._shown = shown_on_panel(app, key)


def _has_ended(playback: Playback) -> bool:
    """Whether a once-through playback sits at its last frame for good.

    Not while it is still decoding (``complete`` False: the end of what is
    decoded is not the end of the video), and not while paused -- a paused
    video is the user's, and waits for them.
    """
    ended = (playback.complete and not playback.paused
             and playback.cursor >= len(playback.frames) - 1)
    frame_log.debug("_has_ended: cursor=%d frames=%d complete=%s paused=%s -> %s",
                    playback.cursor, len(playback.frames), playback.complete,
                    playback.paused, ended)
    return ended


def task_key(device_key: str) -> str:
    """The scheduler key for *device_key*'s video folder driver.

    For callers holding the device key but no driver (``App.stop_sources``,
    the Commands registering/removing it).  Single source: the class attribute.
    """
    key = f"{VideoFolderDriver.KEY_PREFIX}{device_key}"
    log.debug("task_key: %s → %s", device_key, key)
    return key
