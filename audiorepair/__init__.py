"""audiorepair: make a video play everywhere by re-encoding only the audio track."""
from .core import (AudioRepairError, repair_audio, probe_codec, probe_duration,
                   needs_audio_repair)

__all__ = ["AudioRepairError", "repair_audio", "probe_codec", "probe_duration",
           "needs_audio_repair"]
__version__ = "0.1.0"
