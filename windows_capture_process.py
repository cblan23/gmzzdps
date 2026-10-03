"""Built-in Windows packet source with the existing passive decoder pipeline."""
from npcap_capture_process import (
    CAPTURE_SOURCE_WINDOWS_RAW,
    CaptureProcessClient as PassiveCaptureProcessClient,
)


class CaptureProcessClient(PassiveCaptureProcessClient):
    capture_source = CAPTURE_SOURCE_WINDOWS_RAW

    def __init__(self, *args, **kwargs):
        # Native IPv6 and Raw Socket compatibility are handled by the bundled
        # receive-only WinDivert source. The default build never needs Npcap.
        kwargs.setdefault("allow_npcap_fallback", False)
        super().__init__(*args, **kwargs)
