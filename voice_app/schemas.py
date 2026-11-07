from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Language = Literal["ZH", "EN", "JA", "ES"]


class GenerationParameters(BaseModel):
    """Optional overrides; omitted fields fall back to the selected preset."""

    model_config = ConfigDict(extra="forbid")

    do_sample: bool | None = None
    top_p: float | None = Field(default=None, gt=0, le=1)
    top_k: int | None = Field(default=None, ge=0, le=200)
    temperature: float | None = Field(default=None, gt=0, le=2)
    length_penalty: float | None = Field(default=None, ge=-10, le=10)
    num_beams: int | None = Field(default=None, ge=1, le=10)
    repetition_penalty: float | None = Field(default=None, gt=0, le=20)
    max_mel_tokens: int | None = Field(default=None, ge=100, le=4096)
    max_text_tokens_per_segment: int | None = Field(default=None, ge=20, le=240)
    duration_factor: float | None = Field(default=None, ge=0.5, le=2)
    interval_silence: int | None = Field(default=None, ge=0, le=3000)


class SynthesisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=10000)
    preset: str | None = None
    reference_audio: str | None = None
    language: Language = "ZH"

    emotion_mode: int | None = Field(default=None, ge=0, le=3)
    emotion_audio: str | None = None
    emotion_weight: float | None = Field(default=None, ge=0, le=1)
    emotion_vector: list[float] | None = None
    emotion_text: str | None = None
    emotion_random: bool | None = None

    output_device: int | None = None
    monitor_enabled: bool = False
    monitor_device: int | None = None
    save_output: bool = False
    generation: GenerationParameters = Field(default_factory=GenerationParameters)

    @model_validator(mode="after")
    def validate_source_and_emotion(self):
        if not self.preset and not self.reference_audio:
            raise ValueError("preset or reference_audio is required")
        if self.emotion_vector is not None and len(self.emotion_vector) != 8:
            raise ValueError("emotion_vector must contain exactly 8 values")
        return self


class DesktopSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    save_output: bool = False
    output_limit_mb: int = Field(default=500, ge=10, le=10240)

    output_device: int | None = None
    microphone_enabled: bool = False
    microphone_device: int | None = None
    microphone_gain: float = Field(default=1.0, ge=0, le=3)
    tts_gain: float = Field(default=1.0, ge=0, le=3)
    monitor_enabled: bool = True
    monitor_device: int | None = None
    last_preset: str = ""
    last_reference_audio: str = ""
    language: Language = "ZH"
    top_p: float = Field(default=0.8, gt=0, le=1)
    top_k: int = Field(default=30, ge=0, le=200)
    temperature: float = Field(default=0.8, gt=0, le=2)
    max_text_tokens_per_segment: int = Field(default=120, ge=20, le=240)
    duration_factor: float = Field(default=1.0, ge=0.5, le=2)


class PresetSummary(BaseModel):
    name: str
    reference_audio: str
    emotion_mode: int = 0
    reference_exists: bool = True
    language: Language = "ZH"
    duration_seconds: float | None = None


class PresetTrashRequest(BaseModel):
    confirm: Literal[True]


class AudioDevice(BaseModel):
    id: int
    name: str
    host_api: str
    channels: int
    default_samplerate: float
    is_default: bool = False
    is_vb_cable: bool = False


class StopResponse(BaseModel):
    stopped: bool
    job_id: str | None = None


class MixerStartRequest(BaseModel):
    input_device: int | None
    output_device: int
    mic_gain: float = Field(default=1.0, ge=0, le=3)
    tts_gain: float = Field(default=1.0, ge=0, le=3)
