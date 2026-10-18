from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        import sys

        argv = sys.argv[1:]
    if not argv or argv[0] == "desktop":
        from .client_main import main as client_main

        return client_main(argv[1:] if argv else [])

    parser = _parser()
    args = parser.parse_args(argv)
    if args.index_tts_root is None:
        parser.error("--index-tts-root is required in server mode")
    # Establish ownership before importing the API, CUDA or opening any device.
    from .lifecycle import watch_desktop_owner

    owner = watch_desktop_owner(
        args.desktop_owner_pid, args.desktop_owner_created, args.desktop_start_event
    )
    try:
        return _run_server(args)
    finally:
        if owner is not None:
            owner.close()


def _run_server(args):
    import uvicorn

    from .api import create_app
    from .runtime import DATA_ROOT, RuntimeOptions

    options = RuntimeOptions(
        index_tts_root=Path(args.index_tts_root),
        model_dir=Path(args.model_dir) if args.model_dir else None,
        presets_dir=Path(args.presets_dir or DATA_ROOT / "presets"),
        device=args.device,
        prefer_half=args.fp16,
        use_deepspeed=args.deepspeed,
        use_cuda_kernel=args.cuda_kernel,
        use_accel=args.accel,
        use_torch_compile=args.torch_compile,
        use_qwen_emo=args.qwen_emo,
    )
    uvicorn.run(
        create_app(options), host=args.host, port=args.port, log_level=args.log_level
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m voice_app",
        description="IndexTTS 2.5 local voice service and desktop client",
    )
    parser.add_argument("mode", choices=("server",))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7862)
    parser.add_argument("--index-tts-root", default=None)
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--presets-dir", default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--fp16", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--deepspeed", action="store_true")
    parser.add_argument("--cuda-kernel", action="store_true")
    parser.add_argument("--accel", action="store_true")
    parser.add_argument("--torch-compile", action="store_true")
    parser.add_argument("--qwen-emo", action="store_true")
    parser.add_argument(
        "--desktop-owner-pid", type=int, default=None, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--desktop-owner-created", type=int, default=None, help=argparse.SUPPRESS
    )
    parser.add_argument("--desktop-start-event", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--log-level", default="info", choices=("debug", "info", "warning", "error")
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
