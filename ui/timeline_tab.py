"""Timeline push and scheduling. Pushes timelines to the in-game plugin
and runs the in-combat sequence, a thin layer over timeline_engine.py.
Mixin for MainWindow, all state rides on self.
"""

import os
import re
import sys
import threading
import time
import urllib.request

from tts import speak
from sequential import SequentialRunner
import timeline_parser

import app_common as ac
from app_common import (
    FIGHT_TO_CACTBOT_TXT, _CACTBOT_DATA_RAW, _CACTBOT_TIMELINE_TTL_S, _TIMELINE_MAX_BYTES, _bare_fight_tag, _fsync_file,
)


class TimelineTabMixin:
    def _on_timeline_tts(self, text: str) -> None:
        # A cactbot sourced schedule only drives the bars. The reader already
        # speaks cactbot's callouts, so speaking the txt entries too doubles
        # up. Your own timeline files still talk.
        if getattr(self, "_timeline_from_cactbot", False):
            return
        # Your own timeline talks only while the local engine and the global
        # switch are on. The kill switch leaves the feed gated, but a callout
        # already waiting on its timer would still fire without this check.
        if not (getattr(self, "_local_enabled", True)
                and getattr(self, "_global_local_on_flag", True)):
            return
        # Your own timeline callouts are guests. Own triggers win.
        self._emit_guest_callout(text, "info")

    def _on_seq_complete(self, runner: SequentialRunner, captured: dict) -> None:
        trigger = runner.trigger
        self._drop_seq_runner(runner)
        # Suppress if the world moved on during the sequence. Local off,
        # trigger disabled, edited, the list holds a different object, or
        # deleted. Identity, not equality, mirroring the _on_status_timer
        # guard.
        if (not self._local_enabled or not trigger.enabled
                or not any(x is trigger for x in self._triggers)):
            return
        self._fire(trigger, captured)

    def _on_seq_expire(self, runner: SequentialRunner) -> None:
        self._drop_seq_runner(runner)

    def _drop_seq_runner(self, runner: SequentialRunner) -> None:
        """Stop a sequential runner, drop our reference, deleteLater its
        QObject. The runner is parented to the window, so without
        deleteLater it stays a live child all session and accumulates.
        deleteLater is event-loop-safe even from the runner's own
        callback."""
        runner.cancel()
        if runner in self._seq_runners:
            self._seq_runners.remove(runner)
        runner.deleteLater()

    def _clear_seq_runners(self) -> None:
        # Same teardown as status timers. A sequential runner left armed
        # across a zone change or wipe can advance on a new-fight line
        # inside its timeout window and speak a stale callout.
        for r in list(self._seq_runners):
            self._drop_seq_runner(r)

    def _timeline_fight_tag(self, zone: str) -> str:
        """The tag whose timeline the zone shows. The zone-id index tag
        when it maps this zone, else the name-regex fight tag, blanked the
        same way the loader blanks it."""
        cb = self._cactbot_zone_entry()
        if cb:
            return cb[0]
        return _bare_fight_tag(self._fight_tag_for_zone(zone)[0]) if zone else ""

    def _push_timeline_to_plugin(self) -> None:
        """Push the current schedule to the plugin, or clear it when your
        callouts are switched off. The bar belongs to the local engine: the
        master Triggers switch and the global kill switch both take it down.
        Cactbot mode always keeps its own bars."""
        if (getattr(self, "_cactbot_mode", False)
                or (getattr(self, "_local_enabled", True)
                    and getattr(self, "_global_local_on_flag", True))):
            self._plugin_link.send_timeline(self._timeline.upcoming())
        else:
            self._plugin_link.send_clear()

    def _load_timeline_for_zone(self, zone: str) -> None:
        self._timeline.reset()
        fight = ""
        from_cactbot = False
        try:
            local_fight, _unused = self._fight_tag_for_zone(zone) if zone else ("", "")
            # The tag names a timeline file below, so it must be a bare name.
            # Imported triggers carry whatever the file said, and separators
            # or .. would walk the read out of TIMELINES_DIR.
            local_fight = _bare_fight_tag(local_fight)
            # Primary source is the zone-id index, every cactbot timeline.
            # The hand-written converter map stays as fallback for anything
            # the index does not map, stale id, or a zone only the name
            # resolved.
            cb = self._cactbot_zone_entry()
            if (not cb and self._cactbot_mode
                    and local_fight in FIGHT_TO_CACTBOT_TXT):
                cb = (local_fight, FIGHT_TO_CACTBOT_TXT[local_fight])
            fight = cb[0] if cb else local_fight
            path = None
            if cb:
                ctag, rel = cb
                # Prefer a cactbot .txt cached under a distinct name, never
                # clobbers a user's own <FightTag>.txt. Fetch on demand.
                cb_path = ac.TIMELINES_DIR / f"{ctag}.cactbot.txt"
                if cb_path.exists():
                    # Past the TTL the cached copy still serves, but kick a
                    # background re-fetch so an upstream correction reaches
                    # users who fetched once long ago.
                    try:
                        if time.time() - cb_path.stat().st_mtime > _CACTBOT_TIMELINE_TTL_S:
                            self._fetch_cactbot_timeline(ctag, rel)
                    except OSError:
                        pass
                    path = cb_path
                    from_cactbot = True
                else:
                    self._fetch_cactbot_timeline(ctag, rel)   # async. Reloads on done
                    if local_fight:
                        path = ac.TIMELINES_DIR / f"{local_fight}.txt"  # fall back meanwhile
            elif local_fight:
                path = ac.TIMELINES_DIR / f"{local_fight}.txt"
            if path and path.exists():
                # utf-8-sig, not plain utf-8. A BOM survives strip and eats the
                # first line, the anchored entry regex and hideall both miss it.
                text = path.read_text(encoding="utf-8-sig")
                entries = timeline_parser.parse(text)
                self._timeline.load(entries)
                self._timeline_reset_on_combat_end = "# reset-on-combat-end" in text
            else:
                self._timeline.clear()
                self._timeline_reset_on_combat_end = False
                from_cactbot = False
        except Exception as exc:  # noqa: BLE001
            # Corrupt or unreadable file, a parser blowup, anything. Every
            # other failure path around here leaves a trace, and the retry
            # below would otherwise fail silently every 30 s forever.
            ac.log_drop("timeline", f"{zone!r} load failed: {exc!r}")
            self._timeline.clear()
            self._timeline_reset_on_combat_end = False
            from_cactbot = False
            # Do not stamp the failed fight below. Leaving fight empty lets
            # the 30 s re-detect tick retry the load instead of treating the
            # cleared timeline as the current state.
            fight = ""
        # Record what the loaded schedule belongs to, empty string when
        # cleared. The 30 s re-detect tick compares against this and only
        # reloads on a real change, so a steady state sends no plugin
        # traffic.
        self._timeline_fight = fight
        self._timeline_from_cactbot = from_cactbot
        # Push the schedule to the plugin on every load, reload and clear.
        # An empty one is a valid replace. A zone with no timeline shows
        # nothing. The helper holds the bar back while your callouts are
        # switched off.
        self._push_timeline_to_plugin()

    def _fetch_cactbot_timeline(self, tag: str, rel: str) -> None:
        """Download cactbot's .txt timeline `rel`, a path under
        ui/raidboss/data/, into the cache as <tag>.cactbot.txt, on a
        daemon thread. Re-loads the timeline via a signal when done.
        No-ops on any error or if a fetch for this tag is already in
        flight."""
        if not rel:
            return
        # One atomic membership test plus add under the lock. The worker
        # discards from its own thread, so the in-flight dedup must not
        # rely on GIL luck.
        with self._cactbot_tl_lock:
            if tag in self._cactbot_tl_fetching:
                return
            self._cactbot_tl_fetching.add(tag)

        def _worker() -> None:
            try:
                url = f"{_CACTBOT_DATA_RAW}/{rel}"
                req = urllib.request.Request(url, headers={"User-Agent": "NyaaTriggers"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read(_TIMELINE_MAX_BYTES + 1)
                if len(data) > _TIMELINE_MAX_BYTES:
                    raise ValueError("timeline response too large")
                ac.TIMELINES_DIR.mkdir(parents=True, exist_ok=True)
                dest = ac.TIMELINES_DIR / f"{tag}.cactbot.txt"
                tmp = dest.with_suffix(dest.suffix + ".tmp")
                tmp.write_bytes(data)
                _fsync_file(tmp)
                os.replace(tmp, dest)   # never leave a half-written timeline
                self._cactbot_tl_signal.emit(tag)
            except Exception:  # noqa: BLE001 - timeline just stays empty
                # Log to stderr so a corrupted download or a disk-full
                # mid-write is diagnosable. The timeline stays empty and
                # the TTL re-fetch self-heals, exactly as before.
                print(f"cactbot timeline fetch failed for {tag}", file=sys.stderr)
            finally:
                with self._cactbot_tl_lock:
                    self._cactbot_tl_fetching.discard(tag)

        try:
            threading.Thread(target=_worker, daemon=True).start()
        except Exception:  # noqa: BLE001 - a failed start must not strand the tag
            with self._cactbot_tl_lock:
                self._cactbot_tl_fetching.discard(tag)

    def _on_cactbot_timeline_ready(self, fight: str) -> None:
        """A cactbot timeline finished downloading. Reload if still relevant."""
        if not self._cactbot_mode:
            return
        if self._timeline_fight_tag(self._match_zone) == fight:
            self._load_timeline_for_zone(self._match_zone)
