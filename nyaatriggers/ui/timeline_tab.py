"""Timeline loading, plugin schedules and sequence handling for MainWindow."""

import os
import sys
import threading
import time
import urllib.request

from nyaatriggers.http_fetch import fetch_bytes

from nyaatriggers.sequential import SequentialRunner
from nyaatriggers import timeline_parser

from nyaatriggers import app_common as ac
from nyaatriggers.app_common import (
    FIGHT_TO_CACTBOT_TXT, _CACTBOT_DATA_RAW, _CACTBOT_TIMELINE_TTL_S, _TIMELINE_MAX_BYTES, _bare_fight_tag, _fsync_file,
)


def _local_timeline_path(fight: str):
    """Find a writable local timeline, then its bundled fallback, or return None."""
    p = ac.TIMELINES_DIR / f"{fight}.txt"
    if p.exists():
        return p
    p = ac._BUNDLE_TIMELINES_DIR / f"{fight}.txt"
    return p if p.exists() else None


class TimelineTabMixin:
    def _on_timeline_tts(self, text: str) -> None:
        # Cactbot schedules drive bars only because the reader already supplies speech.
        if getattr(self, "_timeline_from_cactbot", False):
            return
        # Check local switches again when the timer fires.
        if not (getattr(self, "_local_enabled", True)
                and getattr(self, "_global_local_on_flag", True)):
            return
        self._emit_guest_callout(text, "info")

    def _on_seq_complete(self, runner: SequentialRunner, captured: dict) -> None:
        trigger = runner.trigger
        self._drop_seq_runner(runner)
        # Check mode and object identity so disabled, replaced or deleted triggers
        # cannot complete stale sequences.
        if (not self._local_enabled or not trigger.enabled
                or not any(x is trigger for x in self._triggers)):
            return
        self._fire(trigger, captured)

    def _on_seq_expire(self, runner: SequentialRunner) -> None:
        self._drop_seq_runner(runner)

    def _drop_seq_runner(self, runner: SequentialRunner) -> None:
        """Stop and delete a sequence runner so its persistent window parent cannot retain
        it.
        """
        runner.cancel()
        if runner in self._seq_runners:
            self._seq_runners.remove(runner)
        runner.deleteLater()

    def _clear_seq_runners(self) -> None:
        # Cancel sequences at encounter boundaries so later log lines cannot finish
        # them.
        for r in list(self._seq_runners):
            self._drop_seq_runner(r)

    def _timeline_fight_tag(self, zone: str) -> str:
        """Resolve the timeline tag from the zone index or local fight name."""
        cb = self._cactbot_zone_entry()
        if cb:
            return cb[0]
        return _bare_fight_tag(self._fight_tag_for_zone(zone)[0]) if zone else ""

    def _push_timeline_to_plugin(self) -> None:
        """Push the current schedule when its mode is enabled. Preserve cactbot bars while
        local callouts are off.
        """
        if (getattr(self, "_cactbot_mode", False)
                or (getattr(self, "_local_enabled", True)
                    and getattr(self, "_global_local_on_flag", True))):
            self._plugin_link.send_timeline(self._timeline.upcoming())
        else:
            self._plugin_link.send_clear(keep_dps=True)

    def _load_timeline_for_zone(self, zone: str, *, preserve_time: bool = False) -> None:
        if not preserve_time:
            self._timeline.reset()
        fight = ""
        from_cactbot = False
        try:
            local_fight, _unused = self._fight_tag_for_zone(zone) if zone else ("", "")
            # Require a bare fight tag before using it as a timeline filename.
            local_fight = _bare_fight_tag(local_fight)
            # Prefer the zone ID index, falling back to the known fight map.
            cb = self._cactbot_zone_entry()
            if (not cb and self._cactbot_mode
                    and local_fight in FIGHT_TO_CACTBOT_TXT):
                cb = (local_fight, FIGHT_TO_CACTBOT_TXT[local_fight])
            fight = cb[0] if cb else local_fight
            path = None
            if cb:
                ctag, rel = cb
                # Prefer writable downloads over bundled cactbot files. Both use names
                # separate from user timelines.
                cb_path = ac.TIMELINES_DIR / f"{ctag}.cactbot.cache.txt"
                if not cb_path.exists():
                    cb_path = ac._BUNDLE_TIMELINES_DIR / f"{ctag}.cactbot.txt"
                if cb_path.exists():
                    # Serve the existing copy during a background refresh after its TTL
                    # expires.
                    try:
                        if time.time() - cb_path.stat().st_mtime > _CACTBOT_TIMELINE_TTL_S:
                            self._fetch_cactbot_timeline(ctag, rel)
                    except OSError:
                        pass
                    path = cb_path
                    from_cactbot = True
                else:
                    self._fetch_cactbot_timeline(ctag, rel)
                    if local_fight:
                        path = _local_timeline_path(local_fight)
            elif local_fight:
                path = _local_timeline_path(local_fight)
            if path and path.exists():
                # Accept a BOM so the first entry or hideall directive still parses.
                text = path.read_text(encoding="utf-8-sig")
                entries = timeline_parser.parse(text)
                if preserve_time:
                    if not entries:
                        raise ValueError("refreshed timeline has no entries")
                    self._timeline.load(entries, preserve_time=True)
                else:
                    self._timeline.load(entries)
                self._timeline_reset_on_combat_end = "# reset-on-combat-end" in text
            else:
                if preserve_time:
                    raise ValueError("refreshed timeline is missing")
                self._timeline.clear()
                self._timeline_reset_on_combat_end = False
                from_cactbot = False
                if cb:
                    fight = ""
        except Exception as exc:  # noqa: BLE001
            # Log loading errors before the next periodic retry.
            ac.log_drop("timeline", f"{zone!r} load failed: {exc!r}")
            if preserve_time:
                return
            self._timeline.clear()
            self._timeline_reset_on_combat_end = False
            from_cactbot = False
            # Leave the fight unstamped after failure so periodic detection retries.
            fight = ""
        # Record the loaded fight so unchanged zones do not reload.
        self._timeline_fight = fight
        self._timeline_from_cactbot = from_cactbot
        # Replace the plugin schedule on load or clear, respecting the current mode.
        self._push_timeline_to_plugin()

    def _fetch_cactbot_timeline(self, tag: str, rel: str) -> None:
        """Download a cactbot timeline to the writable cache in the background and signal
        for reload. Allow one fetch per tag and leave bundled files untouched.
        """
        if not rel:
            return
        # Protect fetch membership checks and updates because workers remove entries
        # from another thread.
        with self._cactbot_tl_lock:
            if tag in self._cactbot_tl_fetching:
                return
            self._cactbot_tl_fetching.add(tag)

        def _worker() -> None:
            tmp = None
            try:
                url = f"{_CACTBOT_DATA_RAW}/{rel}"
                req = urllib.request.Request(url, headers={"User-Agent": "NyaaTriggers"})
                data = fetch_bytes(req, _TIMELINE_MAX_BYTES)
                if len(data) > _TIMELINE_MAX_BYTES:
                    raise ValueError("timeline response too large")
                ac.TIMELINES_DIR.mkdir(parents=True, exist_ok=True)
                dest = ac.TIMELINES_DIR / f"{tag}.cactbot.cache.txt"
                tmp = dest.with_name(f"{dest.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_bytes(data)
                _fsync_file(tmp)
                os.replace(tmp, dest)
                self._cactbot_tl_signal.emit(tag)
            except Exception:  # noqa: BLE001
                # Log download failures and leave later refreshes able to retry.
                print(f"cactbot timeline fetch failed for {tag}", file=sys.stderr)
            finally:
                if tmp is not None:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                with self._cactbot_tl_lock:
                    self._cactbot_tl_fetching.discard(tag)

        try:
            threading.Thread(target=_worker, daemon=True).start()
        except Exception:  # noqa: BLE001
            with self._cactbot_tl_lock:
                self._cactbot_tl_fetching.discard(tag)

    def _on_cactbot_timeline_ready(self, fight: str) -> None:
        """Reload a downloaded timeline if it still matches the current zone."""
        if not self._cactbot_mode:
            return
        if self._timeline_fight_tag(self._match_zone) == fight:
            self._load_timeline_for_zone(self._match_zone, preserve_time=True)
