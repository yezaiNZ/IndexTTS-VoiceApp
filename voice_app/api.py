from __future__ import annotations

import io
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from . import __version__
from .audio import SAMPLE_RATE, list_input_devices, list_output_devices
from .output_store import LAST_AUDIO_LIMIT, MIB, GeneratedAudioStore
from .presets import preset_summaries
from .reference_audio import ReferencePreview, prepare_reference
from .reference_limits import MAX_UPLOAD_BYTES
from .runtime import ModelRuntime, RuntimeBusyError, RuntimeOptions
from .schemas import (
    DesktopSettings,
    Language,
    MixerStartRequest,
    PresetTrashRequest,
    StopResponse,
    SynthesisRequest,
)
from .settings import DEFAULT_SETTINGS_PATH, SettingsStore


def create_app(
    runtime_options: RuntimeOptions | None = None,
    settings_path: Path = DEFAULT_SETTINGS_PATH,
) -> FastAPI:
    settings = SettingsStore(settings_path)
    output_store = GeneratedAudioStore(
        Path(settings_path).parent / "auto-saved",
        lambda: settings.load().output_limit_mb * MIB,
    )
    runtime = ModelRuntime(runtime_options, output_store=output_store)
    preview = ReferencePreview()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        preview.stop()
        runtime.mixer.stop()

    app = FastAPI(
        title="IndexTTS Voice App",
        version=__version__,
        description="Local IndexTTS 2.5 service with PCM streaming and audio routing.",
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.settings = settings
    app.state.preview = preview

    def check_preset_origin(request: Request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="不允许其他网站操作本机角色预设。")

    async def read_audio_body(request: Request) -> bytes:
        check_preset_origin(request)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            raise HTTPException(status_code=415, detail="请通过桌面导入入口发送音频文件。")
        result = bytearray()
        async for block in request.stream():
            if len(result) + len(block) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="音频不能超过 25 MB，请缩小文件或截取所需片段。")
            result.extend(block)
        return bytes(result)

    def preset_failure(exc):
        code = 409 if isinstance(exc, (FileExistsError, RuntimeBusyError)) else 404 if isinstance(exc, FileNotFoundError) else 422
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    def selection_changed(old_name: str, new_name: str):
        value = settings.load()
        if value.last_preset == old_name:
            value.last_preset = new_name
            value.last_reference_audio = ""
            settings.save(value)

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, **runtime.status()}

    @app.post("/v1/model/load")
    def load_model():
        try:
            runtime.ensure_loaded()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return runtime.status()

    @app.get("/v1/presets")
    def presets():
        return preset_summaries(runtime.resolver.store)

    @app.post("/v1/presets", status_code=201)
    async def import_preset(request: Request, name: str, filename: str, language: Language = "ZH"):
        body = await read_audio_body(request)
        # Decoding/file writes run in a thread, not the ASGI event loop.
        from starlette.concurrency import run_in_threadpool

        def task():
            try:
                reference = prepare_reference(body, filename)
                with runtime.edit_presets():
                    return runtime.resolver.store.create(name, reference, language)
            except (OSError, ValueError, RuntimeBusyError) as exc:
                preset_failure(exc)
        return await run_in_threadpool(task)

    @app.put("/v1/presets/{name}")
    async def update_preset(request: Request, name: str, new_name: str, filename: str = "", language: Language = "ZH"):
        body = await read_audio_body(request)
        from starlette.concurrency import run_in_threadpool

        def task():
            try:
                reference = prepare_reference(body, filename) if body else None
                with runtime.edit_presets():
                    result = runtime.resolver.store.update(name, new_name, reference, language)
                    selection_changed(name, result["name"])
                    return result
            except (OSError, ValueError, RuntimeBusyError) as exc:
                preset_failure(exc)
        return await run_in_threadpool(task)

    @app.post("/v1/presets/{name}/trash")
    def trash_preset(name: str, request: Request, confirmation: PresetTrashRequest):
        check_preset_origin(request)
        try:
            with runtime.edit_presets():
                destination = runtime.resolver.store.trash(name)
                selection_changed(name, "")
            return {"removed": True, "recoverable": True, "trash_path": str(destination)}
        except (OSError, ValueError, RuntimeBusyError) as exc:
            preset_failure(exc)

    @app.post("/v1/preset-preview")
    async def preview_reference(
        request: Request, device_id: int | None = None, filename: str = "", preset: str = ""
    ):
        body = await read_audio_body(request)
        from starlette.concurrency import run_in_threadpool

        def task():
            try:
                if preset and not body:
                    data = runtime.resolver.store.load(preset)
                    if data is None:
                        raise FileNotFoundError("角色不存在，请刷新列表。")
                    content = runtime.resolver.store.reference_bytes(preset)
                    name = data.get("prompt_audio", "")
                else:
                    content, name = body, filename
                try:
                    reference = prepare_reference(content, name)
                except ValueError as exc:
                    raise ValueError(f"音频检查未通过，未播放。{exc}") from exc
                preview.play(reference, device_id)
                return {"played": True, "duration_seconds": reference.seconds}
            except (OSError, ValueError, RuntimeError) as exc:
                preset_failure(exc)
        return await run_in_threadpool(task)

    @app.post("/v1/preset-preview/stop")
    def stop_reference_preview(request: Request):
        check_preset_origin(request)
        preview.stop()
        return {"stopped": True}

    @app.get("/v1/devices")
    def devices():
        try:
            return list_output_devices()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/input-devices")
    def input_devices():
        try:
            return list_input_devices()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/mixer")
    def mixer_status():
        return runtime.mixer.status()

    @app.post("/v1/mixer/start")
    def start_mixer(request: MixerStartRequest):
        try:
            return runtime.mixer.start(
                input_device=request.input_device,
                output_device=request.output_device,
                mic_gain=request.mic_gain,
                tts_gain=request.tts_gain,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/v1/mixer/stop")
    def stop_mixer():
        runtime.mixer.stop()
        return runtime.mixer.status()

    @app.get("/v1/settings", response_model=DesktopSettings)
    def get_settings():
        return settings.load()

    @app.put("/v1/settings", response_model=DesktopSettings)
    def put_settings(value: DesktopSettings):
        return settings.save(value)

    @app.get("/v1/outputs/{job_id}")
    def output_result(job_id: str):
        result = output_store.result(job_id)
        if result is None:
            raise HTTPException(status_code=404, detail="生成结果不存在、尚未完成或已过期")
        return result

    @app.post("/v1/tts")
    def synthesize(request: SynthesisRequest):
        _ensure_saved_mixer(runtime, settings, request)
        session = _start_session(runtime, request)
        output = io.BytesIO()
        try:
            with wave.open(output, "wb") as writer:
                writer.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
                stream = session.iter_pcm()
                try:
                    size = 0
                    for pcm in stream:
                        size += len(pcm)
                        if size > LAST_AUDIO_LIMIT:
                            session.request_stop()
                            raise HTTPException(status_code=413, detail="音频过长，请改用流式接口")
                        writer.writeframesraw(pcm)
                finally:
                    stream.close()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        headers = {"X-Job-Id": session.job_id}
        saved = output_store.result(session.job_id)
        if saved and saved["saved"]:
            headers["X-Output-File"] = saved["path"]
        return Response(
            output.getvalue(),
            media_type="audio/wav",
            headers=headers,
        )

    @app.post("/v1/tts/stream")
    def synthesize_stream(request: SynthesisRequest):
        _ensure_saved_mixer(runtime, settings, request)
        session = _start_session(runtime, request)
        headers = {
            "X-Job-Id": session.job_id,
            "X-Audio-Sample-Rate": str(SAMPLE_RATE),
            "X-Audio-Channels": "1",
            "X-Audio-Format": "s16le",
        }
        if request.save_output:
            headers["X-Output-File"] = str(session.output_path)
        return StreamingResponse(
            session.iter_pcm(),
            media_type=f"audio/pcm;rate={SAMPLE_RATE};channels=1;format=s16le",
            headers=headers,
        )

    @app.post("/v1/stop", response_model=StopResponse)
    def stop():
        stopped, job_id = runtime.stop()
        return StopResponse(stopped=stopped, job_id=job_id)

    return app


def _start_session(runtime: ModelRuntime, request: SynthesisRequest):
    try:
        return runtime.start_session(request)
    except RuntimeBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _ensure_saved_mixer(
    runtime: ModelRuntime, settings: SettingsStore, request: SynthesisRequest
):
    """Restore the persistent mic bridge before a synthesis request starts."""
    saved = settings.load()
    if not saved.microphone_enabled:
        return
    output_device = saved.output_device or request.output_device
    if output_device is None:
        return
    try:
        runtime.mixer.start(
            input_device=saved.microphone_device,
            output_device=output_device,
            mic_gain=saved.microphone_gain,
            tts_gain=saved.tts_gain,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=f"麦克风混音桥启动失败：{exc}",
        ) from exc
