"""Resource limits shared by the reference audio importer and desktop client."""

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
REFERENCE_RATE = 22050
PCM_SAMPLE_BYTES = 2
WAV_HEADER_BYTES = 44  # Header written by wave for mono 16-bit PCM.
MAX_REFERENCE_FRAMES = (MAX_UPLOAD_BYTES - WAV_HEADER_BYTES) // PCM_SAMPLE_BYTES
# The request waits for playback to finish. Size this for the largest saved WAV
# with headroom for decoding, device startup, and draining its output buffer.
PREVIEW_READ_TIMEOUT = 45 + MAX_REFERENCE_FRAMES / REFERENCE_RATE
