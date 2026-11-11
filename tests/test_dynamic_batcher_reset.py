"""reset_speed_tracker must also reset what the batch scheduler reads."""

from galgame2voice.services import dynamic_batcher
from galgame2voice.services.dynamic_batcher import (
    get_batch_scheduler,
    get_speed_tracker,
    reset_speed_tracker,
)

EIGHT_SENTENCES = "一。二。三。四。五。六。七。八。"


def test_scheduler_reads_samples_recorded_after_reset(monkeypatch):
    monkeypatch.setattr(dynamic_batcher, "_GLOBAL_TRACKER", None)
    monkeypatch.setattr(dynamic_batcher, "_GLOBAL_SCHEDULER", None)
    get_batch_scheduler()  # created on the first tracker, as at app start

    reset_speed_tracker()
    for _ in range(3):
        # 0.2s for 4s of audio and 20 chars: RTF 0.05, 100 chars/s.
        get_speed_tracker().record(char_count=20, elapsed_s=0.2, audio_dur_s=4.0)

    assert get_batch_scheduler().tracker is get_speed_tracker()
    assert get_batch_scheduler().compute_batch_size(EIGHT_SENTENCES, split_method="cut2") == 8
