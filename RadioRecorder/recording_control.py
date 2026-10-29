"""Serialize slow recording lifecycle work without calling Tk from workers."""
from __future__ import annotations

from copy import deepcopy
import threading
from typing import Callable


class RecordingControl:
    """Latest requested state wins; a cancelled start is cleaned up in order.

    Recorder.start clears its own stop flag. Calling stop concurrently with a
    slow input-device probe could therefore resurrect capture after Stop or
    window close. One worker owns all start/stop calls instead.
    """

    def __init__(self, recorder, metadata, on_state: Callable[[dict], None],
                 close_monitor: Callable[[], None]):
        self.recorder = recorder
        self.metadata = metadata
        self.on_state = on_state
        self.close_monitor = close_monitor
        self._condition = threading.Condition()
        self._generation = 0
        self._request = None
        self._thread: threading.Thread | None = None
        self._closing = False
        self._active = False
        self._retention: tuple[bool, int] | None = None

    def _submit(self, action: str, config=None, title: str = '') -> int:
        with self._condition:
            if self._closing:
                return self._generation
            self._closing = action == 'close'
            self._generation += 1
            self._request = (self._generation, action, config, title)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='recording-control', daemon=True)
                self._thread.start()
            self._condition.notify()
            return self._generation

    def start(self, config: dict, title: str = '') -> int:
        # Audio and metadata must use the same settings, even if a settings
        # dialog is saved while opening the input device takes several seconds.
        return self._submit('start', deepcopy(config), title)

    def stop(self) -> int:
        return self._submit('stop')

    def close(self) -> int:
        return self._submit('close')

    def update_retention(self, enabled: bool, days: int) -> None:
        with self._condition:
            self._retention = (enabled, days)
            self._apply_retention()

    def _apply_retention(self) -> None:
        # Also called after start returns: Recorder.start may have replaced
        # cfg after a settings save that happened during device validation.
        if self._retention is not None:
            enabled, days = self._retention
            self.recorder.update_retention(enabled, days)

    def _stop_capture(self) -> str | None:
        errors = []
        for stop in (self.metadata.stop, self.recorder.stop):
            try:
                stop()
            except Exception as exc:
                errors.append(str(exc))
        watcher = getattr(self.recorder, 'watch_thread', None)
        output = getattr(self.recorder, '_output_thread', None)
        proc = self.recorder.process
        self._active = bool(self.recorder.running or
                            (watcher is not None and watcher.is_alive()) or
                            (output is not None and output.is_alive()) or
                            (proc is not None and proc.poll() is None))
        return '; '.join(errors) or None

    def _report(self, generation: int, state: str, error=None, metadata_error=None) -> None:
        self.on_state(dict(generation=generation, state=state, error=error,
                           metadata_error=metadata_error))

    def _run(self) -> None:
        handled = 0
        failed_start = None
        last_stop_error = None
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._generation != handled)
                generation, action, config, title = self._request
            handled = generation
            if failed_start is not None and failed_start[0] != generation:
                failed_start = None
            if action != 'start' or self._active or self.recorder.running:
                error = self._stop_capture()
                if self._active:
                    # A watcher may still be finalizing CUE data after the
                    # engine's bounded join. Keep Tk alive and prevent a new
                    # session until all capture state can be released safely.
                    message = error or 'Поток записи ещё завершается'
                    if message != last_stop_error:
                        self._report(generation, 'stopping', message)
                        last_stop_error = message
                    with self._condition:
                        if generation == self._generation:
                            self._condition.wait(.25)
                    handled = 0  # retry this command, or process a newer one
                    continue
                last_stop_error = None
                if action == 'close':
                    try:
                        self.close_monitor()
                    except Exception as exc:
                        error = '; '.join(filter(None, (error, str(exc))))
                    self._report(generation, 'closed', error)
                    return
                if action == 'stop' or error:
                    self._report(generation, 'stopped' if action == 'stop' else 'failed', error)
                    continue
                if failed_start is not None:
                    self._report(generation, 'failed', failed_start[1])
                    failed_start = None
                    continue

            with self._condition:
                if generation != self._generation:
                    continue
            self._active = True  # failed or cancelled starts also require cleanup
            try:
                self.recorder.start(config, title)
            except Exception as exc:
                failed_start = (generation, str(exc))
                handled = 0  # clean up the failed attempt before reporting it
                continue

            metadata_error = None
            with self._condition:
                if generation != self._generation:
                    # The next iteration performs Stop before another Start.
                    # Do not open a metadata connection for cancelled capture.
                    continue
                self._apply_retention()
                try:
                    mode = config.get('metadata_mode', 'none')
                    self.metadata.start(mode, config['metadata_api_url'] if mode == 'api'
                                        else config['metadata_icy_url'],
                                        config.get('metadata_password', ''),
                                        config.get('metadata_username', ''))
                except Exception as exc:
                    # Metadata failure must not stop local audio capture.
                    metadata_error = str(exc)
            self._report(generation, 'started', metadata_error=metadata_error)
