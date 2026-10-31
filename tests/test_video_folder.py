"""Video folder playlist — a folder's videos, one after another, in the App.

Feature: video folder playlist.  ``SetVideoFolder`` saves the folder and
starts its first video once through; ``VideoFolderDriver`` (a ``SendTask`` in
the App, so no UI has to stay open) starts the next when it ends, in name
order or shuffled, and steps aside when another source takes the panel.

The picker is pure (``VideoFolderService``) and tested on its own with an
injected RNG; the rest runs on ``SyncSendScheduler`` so the cadence is a
``tick()``, with ``MediaService.load_video`` stubbed (no ffmpeg).
"""
from __future__ import annotations

import json
import random
from itertools import pairwise
from pathlib import Path

import pytest

from trcc.adapters.infra.send_scheduler import SyncSendScheduler
from trcc.app import App
from trcc.core.commands import (
    ConfigureSlideshow,
    ConnectDevice,
    SetBackground,
    SetSlideshow,
    SetVideoFolder,
)
from trcc.core.events import VideoFolderChanged
from trcc.services.media import MediaService, Playback
from trcc.services.settings import CONFIG_FILE, Settings
from trcc.services.video_folder import VideoFolderService
from trcc.services.video_folder_driver import task_key

from .conftest import FakePlatform, _CliRenderer, loopback_client, show_a_theme

_KEY = "0402:3922"
_MP4 = b"\x00\x00\x00\x18ftypmp42"   # mp4 magic; the stubbed decode ignores it


# ── The picker: pure, RNG injected ──────────────────────────────────────


def _paths(*names: str) -> list[Path]:
    return [Path("/v") / n for n in names]


def test_in_order_plays_by_name_and_wraps() -> None:
    svc = VideoFolderService()
    videos = _paths("a.mp4", "b.mp4", "c.mp4")
    picks = [svc.next(_KEY, videos, shuffle=False) for _ in range(4)]
    assert [p.name for p in picks if p] == ["a.mp4", "b.mp4", "c.mp4", "a.mp4"]


def test_in_order_continues_after_a_deleted_video() -> None:
    """The video that just played was deleted: the next NAME after it plays,
    not the first again."""
    svc = VideoFolderService()
    svc.next(_KEY, _paths("a.mp4", "b.mp4", "c.mp4"), shuffle=False)
    svc.next(_KEY, _paths("a.mp4", "b.mp4", "c.mp4"), shuffle=False)   # b
    pick = svc.next(_KEY, _paths("a.mp4", "c.mp4"), shuffle=False)
    assert pick is not None and pick.name == "c.mp4"


def test_in_order_picks_up_an_added_video() -> None:
    svc = VideoFolderService()
    svc.next(_KEY, _paths("a.mp4", "c.mp4"), shuffle=False)            # a
    pick = svc.next(_KEY, _paths("a.mp4", "b.mp4", "c.mp4"), shuffle=False)
    assert pick is not None and pick.name == "b.mp4"


@pytest.mark.parametrize("seed", range(8))
def test_shuffle_plays_every_video_once_per_round(seed: int) -> None:
    svc = VideoFolderService(random.Random(seed))
    videos = _paths("a.mp4", "b.mp4", "c.mp4", "d.mp4")
    picks = [svc.next(_KEY, videos, shuffle=True) for _ in range(12)]
    for start in range(0, 12, 4):
        assert set(picks[start:start + 4]) == set(videos), picks


@pytest.mark.parametrize("seed", range(20))
def test_shuffle_never_plays_one_video_twice_running(seed: int) -> None:
    """Across the round boundary too: a new round never starts with the
    video that ended the last one."""
    svc = VideoFolderService(random.Random(seed))
    videos = _paths("a.mp4", "b.mp4", "c.mp4")
    picks = [svc.next(_KEY, videos, shuffle=True) for _ in range(30)]
    assert all(x != y for x, y in pairwise(picks)), picks


def test_shuffle_round_takes_in_an_added_video() -> None:
    svc = VideoFolderService(random.Random(3))
    first = svc.next(_KEY, _paths("a.mp4", "b.mp4", "c.mp4"), shuffle=True)
    grown = _paths("a.mp4", "b.mp4", "c.mp4", "d.mp4")
    rest = [svc.next(_KEY, grown, shuffle=True) for _ in range(3)]
    assert {first, *rest} == set(grown)


def test_shuffle_never_picks_a_removed_video() -> None:
    svc = VideoFolderService(random.Random(5))
    svc.next(_KEY, _paths("a.mp4", "b.mp4", "c.mp4"), shuffle=True)
    shrunk = _paths("a.mp4", "b.mp4")
    picks = {svc.next(_KEY, shrunk, shuffle=True) for _ in range(6)}
    assert picks <= set(shrunk)


def test_a_single_video_repeats_and_no_videos_is_none() -> None:
    svc = VideoFolderService(random.Random(1))
    only = _paths("a.mp4")
    assert [svc.next(_KEY, only, shuffle=True) for _ in range(3)] == only * 3
    assert svc.next(_KEY, [], shuffle=False) is None


def test_reset_starts_the_folder_from_its_first_video() -> None:
    svc = VideoFolderService()
    videos = _paths("a.mp4", "b.mp4")
    svc.next(_KEY, videos, shuffle=False)
    svc.reset(_KEY)
    pick = svc.next(_KEY, videos, shuffle=False)
    assert pick is not None and pick.name == "a.mp4"


# ── The App: Command, driver, persistence ───────────────────────────────


@pytest.fixture
def scheduler() -> SyncSendScheduler:
    return SyncSendScheduler()


@pytest.fixture
def loads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every video decoded, in order -- a 3-frame stub playback each."""
    loaded: list[Path] = []

    def fake_load(self, device_key, path, size, **kwargs):
        loaded.append(path)
        playback = Playback(frames=[b"f0", b"f1", b"f2"],
                            fps=kwargs.get("fps", 15))
        self._playbacks[device_key] = playback
        return playback

    monkeypatch.setattr(MediaService, "load_video", fake_load)
    return loaded


@pytest.fixture
def app(tmp_home: Path, scheduler: SyncSendScheduler, loads: list[Path]) -> App:
    platform = FakePlatform(tmp_home)
    a = App(platform=platform, send_scheduler=scheduler, renderer=_CliRenderer())
    resp = bytearray(0xE100)
    resp[0] = 100                      # FBL=100 → 320x320
    platform.scsi.read_script.append(bytes(resp))
    assert a.dispatch(ConnectDevice(key=_KEY)).ok
    show_a_theme(a, _KEY)
    a.video_folder = VideoFolderService(random.Random(11))
    return a


@pytest.fixture
def folder(app: App) -> Path:
    """Three videos, a non-video, and a video in a SUBfolder (not played).
    Under user content, so the API's path confinement accepts it too."""
    d = app.platform.paths().user_content_dir() / "playlist"
    (d / "sub").mkdir(parents=True)
    for name in ("b.mp4", "a.mp4", "C.mov"):
        (d / name).write_bytes(_MP4)
    (d / "notes.txt").write_text("not a video", encoding="utf-8")
    (d / "sub" / "z.mp4").write_bytes(_MP4)
    return d


def _end_current(app: App) -> None:
    playback = app.media.playback(_KEY)
    assert playback is not None
    playback.cursor = len(playback.frames) - 1


def test_the_store_lists_the_folders_own_videos_by_name(app: App,
                                                       folder: Path) -> None:
    assert app.themes.list_videos(folder) == [
        folder / "a.mp4", folder / "b.mp4", folder / "C.mov"]
    assert app.themes.list_videos(folder / "missing") == []
    assert app.themes.list_videos(folder / "a.mp4") == []   # not a folder


def test_switching_on_plays_the_first_video_once_through(
    app: App, scheduler: SyncSendScheduler, folder: Path, loads: list[Path],
) -> None:
    result = app.dispatch(SetVideoFolder(key=_KEY, folder=folder))

    assert result.ok, result.message
    assert (result.enabled, result.video_count) == (True, 3)
    assert loads == [folder / "a.mp4"]
    playback = app.media.playback(_KEY)
    assert playback is not None and playback.loop is False
    s = app.settings.for_device(_KEY)
    assert s.video_folder_enabled and s.video_folder_path == str(folder)
    assert s.background_path == str(folder / "a.mp4")
    assert task_key(_KEY) in scheduler._tasks


def test_the_driver_starts_the_next_video_when_one_ends(
    app: App, scheduler: SyncSendScheduler, folder: Path, loads: list[Path],
) -> None:
    """MUTATION CHECK: drop the ``_has_ended`` wait and every tick restarts."""
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder))
    for now in range(3):
        scheduler.tick(float(now))
    assert loads == [folder / "a.mp4"], "a video still playing was replaced"

    for now, expected in ((10.0, "b.mp4"), (20.0, "C.mov"), (30.0, "a.mp4")):
        _end_current(app)
        scheduler.tick(now)
        assert loads[-1] == folder / expected
        playback = app.media.playback(_KEY)
        assert playback is not None and playback.loop is False
    assert len(loads) == 4


def test_a_paused_video_waits_for_the_user(
    app: App, scheduler: SyncSendScheduler, folder: Path, loads: list[Path],
) -> None:
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder))
    playback = app.media.playback(_KEY)
    assert playback is not None
    playback.cursor, playback.paused = len(playback.frames) - 1, True
    scheduler.tick(1.0)
    assert loads == [folder / "a.mp4"]


def test_a_redecoded_video_is_made_to_play_once_again(
    app: App, scheduler: SyncSendScheduler, folder: Path,
) -> None:
    """A restore or rotation re-decodes with loop=True; it would never end."""
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder))
    playback = app.media.playback(_KEY)
    assert playback is not None
    playback.set_loop(True)
    scheduler.tick(1.0)
    assert playback.loop is False


def test_shuffled_plays_the_whole_folder_each_round(
    app: App, scheduler: SyncSendScheduler, folder: Path, loads: list[Path],
) -> None:
    assert app.dispatch(SetVideoFolder(key=_KEY, folder=folder, shuffle=True)).ok
    for now in range(1, 6):
        _end_current(app)
        scheduler.tick(float(now))
    videos = {folder / "a.mp4", folder / "b.mp4", folder / "C.mov"}
    assert set(loads[:3]) == videos and set(loads[3:6]) == videos
    assert all(x != y for x, y in pairwise(loads)), loads


def test_another_source_ends_the_playlist_and_keeps_the_users_pick(
    app: App, scheduler: SyncSendScheduler, folder: Path, loads: list[Path],
    tmp_home: Path,
) -> None:
    """One source at a time: a background picked from any UI wins.

    MUTATION CHECK: drop the panel check from ``VideoFolderDriver._run``.
    """
    heard: list[bool] = []
    app.events.subscribe(VideoFolderChanged, lambda e: heard.append(e.enabled))
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder))
    scheduler.tick(0.0)
    other = tmp_home / "other.mp4"
    other.write_bytes(_MP4)
    assert app.dispatch(SetBackground(key=_KEY, path=other)).ok

    _end_current(app)
    scheduler.tick(1.0)

    s = app.settings.for_device(_KEY)
    assert not s.video_folder_enabled
    assert task_key(_KEY) not in scheduler._tasks
    assert s.background_path == str(other)
    assert loads == [folder / "a.mp4", other]
    assert heard == [True, False]


def test_switching_off_keeps_the_video_showing_looped(
    app: App, scheduler: SyncSendScheduler, folder: Path,
) -> None:
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder))
    result = app.dispatch(SetVideoFolder(key=_KEY, enabled=False))

    assert result.ok and not result.enabled
    assert task_key(_KEY) not in scheduler._tasks
    playback = app.media.playback(_KEY)
    assert playback is not None and playback.loop is True
    assert app.settings.for_device(_KEY).background_path == str(folder / "a.mp4")
    assert app.dispatch(SetVideoFolder(key=_KEY, enabled=False)).ok, "idempotent"


def test_switching_on_again_resumes_the_saved_folder(
    app: App, folder: Path, loads: list[Path],
) -> None:
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder, shuffle=True))
    app.dispatch(SetVideoFolder(key=_KEY, enabled=False))
    result = app.dispatch(SetVideoFolder(key=_KEY))
    assert result.ok and result.folder == str(folder) and result.shuffle is True
    assert len(loads) == 2


def test_a_random_toggle_keeps_the_video_playing(
    app: App, folder: Path, loads: list[Path],
) -> None:
    """Same folder, new order: no restart -- the next pick uses it."""
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder))
    assert app.dispatch(SetVideoFolder(key=_KEY, shuffle=True)).ok
    assert loads == [folder / "a.mp4"]
    assert app.settings.for_device(_KEY).video_folder_shuffle is True


def test_a_folder_without_videos_is_refused(
    app: App, scheduler: SyncSendScheduler, tmp_home: Path,
) -> None:
    empty = tmp_home / "empty"
    empty.mkdir()
    for folder in (empty, tmp_home / "missing"):
        result = app.dispatch(SetVideoFolder(key=_KEY, folder=folder))
        assert not result.ok and "No videos" in result.message
    assert not app.settings.for_device(_KEY).video_folder_enabled
    assert task_key(_KEY) not in scheduler._tasks


def test_switching_on_with_no_folder_saved_is_refused(app: App) -> None:
    result = app.dispatch(SetVideoFolder(key=_KEY))
    assert not result.ok and "No video folder" in result.message


def test_the_slideshow_and_the_playlist_are_never_on_together(
    app: App, folder: Path,
) -> None:
    app.dispatch(ConfigureSlideshow(key=_KEY, themes=("A", "B"), interval_s=5.0))
    app.dispatch(SetSlideshow(key=_KEY, enabled=True))
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder))
    s = app.settings.for_device(_KEY)
    assert s.video_folder_enabled and not s.slideshow_enabled

    app.dispatch(SetSlideshow(key=_KEY, enabled=True))
    assert s.slideshow_enabled and not s.video_folder_enabled


def test_letting_the_panel_go_drops_the_driver(
    app: App, scheduler: SyncSendScheduler, folder: Path,
) -> None:
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder))
    app.detach(_KEY)
    assert task_key(_KEY) not in scheduler._tasks


def test_the_key_is_namespaced_away_from_the_device_sender() -> None:
    """The bare key would evict the device's own sender (see the driver)."""
    assert task_key(_KEY) != _KEY and task_key(_KEY).endswith(_KEY)


def test_every_ui_hears_the_playlist_change(app: App, folder: Path) -> None:
    heard: list[VideoFolderChanged] = []
    app.events.subscribe(VideoFolderChanged, heard.append)
    app.dispatch(SetVideoFolder(key=_KEY, folder=folder, shuffle=True))
    assert heard == [VideoFolderChanged(key=_KEY, enabled=True,
                                        folder=str(folder), shuffle=True)]


def test_a_config_from_before_the_feature_loads_with_it_off(tmp_home: Path) -> None:
    paths = FakePlatform(tmp_home).paths()
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    (paths.config_dir() / CONFIG_FILE).write_text(json.dumps(
        {"devices": {_KEY: {"brightness": 40}}}), encoding="utf-8")
    s = Settings(paths).for_device(_KEY)
    assert s.brightness == 40
    assert (s.video_folder_enabled, s.video_folder_path,
            s.video_folder_shuffle) == (False, None, False)


# ── The faces: CLI and API dispatch the one Command ─────────────────────


def test_the_cli_switches_the_playlist_off(cli_runner, cli_app) -> None:
    from trcc.ui.cli.main import app as cli

    del cli_app
    result = cli_runner.invoke(cli, ["display", "video-folder", _KEY, "--off"])
    assert result.exit_code == 0, result.output
    assert "Video folder off" in result.output


def test_the_api_plays_a_folder_inside_trccs_data(app: App, folder: Path,
                                                  tmp_home: Path) -> None:
    from trcc.ui.api.main import build_app

    with loopback_client(build_app(app)) as client:
        ok = client.post(f"/devices/{_KEY}/display/video-folder",
                         json={"folder": str(folder), "shuffle": False})
        assert ok.status_code == 200, ok.text
        assert ok.json()["enabled"] is True

        outside = tmp_home / "outside"
        outside.mkdir()
        refused = client.post(f"/devices/{_KEY}/display/video-folder",
                              json={"folder": str(outside)})
        assert refused.status_code == 400
