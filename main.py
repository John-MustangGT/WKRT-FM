#!/usr/bin/env python3
"""
WKRT-FM 104.7 — Retro 80s Radio Engine
Entry point.

Usage:
    python main.py                    # Run with default config
    python main.py --config path.toml # Custom config
    python main.py --scan             # Scan library and exit
    python main.py --test-dj          # Generate one DJ clip and exit
"""
import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="WKRT-FM Retro Radio Engine")
    parser.add_argument("--config", type=str, default=None, help="Path to settings.toml")
    parser.add_argument("--scan", action="store_true", help="Scan library and print stats")
    parser.add_argument("--test-dj", action="store_true", help="Generate one DJ clip and play it")
    parser.add_argument("--test-tts", type=str, default=None, help="Synthesize a test phrase")
    args = parser.parse_args()

    # Ensure we can import from this directory
    sys.path.insert(0, str(Path(__file__).parent))

    from wkrt.config import load, resolve_paths
    base = Path(__file__).parent
    cfg = load()
    cfg = resolve_paths(cfg, base)

    if args.scan:
        _cmd_scan(cfg)
    elif args.test_dj:
        _cmd_test_dj(cfg)
    elif args.test_tts:
        _cmd_test_tts(cfg, args.test_tts)
    else:
        _cmd_run(cfg)


def _cmd_scan(cfg):
    from wkrt.playlist import scan_library
    from rich.console import Console
    from rich.table import Table

    console = Console()
    library = scan_library(cfg["paths"]["music_dir"])

    if not library:
        console.print(f"[red]No music found in {cfg['paths']['music_dir']}[/red]")
        return

    table = Table(title="WKRT Library Scan", show_header=True)
    table.add_column("Year", style="cyan", width=6)
    table.add_column("Tracks", justify="right", style="yellow")
    table.add_column("Sample", style="dim")

    total = 0
    for year in sorted(library.keys()):
        tracks = library[year]
        total += len(tracks)
        sample = f"{tracks[0].artist} — {tracks[0].title}" if tracks else ""
        table.add_row(str(year), str(len(tracks)), sample)

    table.add_section()
    table.add_row("TOTAL", str(total), "")
    console.print(table)


def _cmd_test_dj(cfg):
    """Generate a test DJ clip using placeholder tracks."""
    from wkrt.playlist import Track
    from wkrt.dj import DJEngine
    from wkrt.tts import TTSEngine
    from rich.console import Console
    import subprocess, shutil

    console = Console()

    prev = Track(
        path=Path("/dev/null"), year=1987,
        artist="Bon Jovi", title="Livin' on a Prayer"
    )
    next_t = Track(
        path=Path("/dev/null"), year=1984,
        artist="Van Halen", title="Jump"
    )

    console.print("[cyan]Generating DJ script via Claude API...[/cyan]")
    dj = DJEngine(cfg)
    script = dj.generate(prev_track=prev, next_track=next_t)
    console.print(f"\n[magenta]Script:[/magenta]\n{script.text}\n")

    console.print("[cyan]Synthesizing with Piper TTS...[/cyan]")
    tts = TTSEngine(cfg)
    clip_path = tts.synthesize(script.text)
    console.print(f"[green]Clip:[/green] {clip_path}")

    ffplay = shutil.which("ffplay")
    if ffplay:
        console.print("[cyan]Playing...[/cyan]")
        subprocess.run([ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(clip_path)])
    else:
        console.print("[yellow]ffplay not found — clip saved but not played[/yellow]")


def _cmd_test_tts(cfg, text: str):
    """Quick TTS test without Claude API."""
    from wkrt.tts import TTSEngine
    from rich.console import Console
    import subprocess, shutil

    console = Console()
    tts = TTSEngine(cfg)
    console.print(f"[cyan]Synthesizing:[/cyan] {text}")
    clip = tts.synthesize(text)
    console.print(f"[green]Output:[/green] {clip}")

    ffplay = shutil.which("ffplay")
    if ffplay:
        subprocess.run([ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(clip)])


def _cmd_run(cfg):
    from wkrt.engine import WKRTEngine
    import signal

    engine = WKRTEngine()

    def _sig(sig, frame):
        print("\nStopping WKRT...")
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    engine.run()


if __name__ == "__main__":
    main()
