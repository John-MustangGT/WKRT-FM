"""Runtime version — prefers installed package metadata, falls back to git describe."""
try:
    from importlib.metadata import version, PackageNotFoundError
    try:
        __version__: str = version("wkrt-fm")
    except PackageNotFoundError:
        import subprocess
        from pathlib import Path
        try:
            __version__ = subprocess.check_output(
                ["git", "describe", "--tags", "--always"],
                stderr=subprocess.DEVNULL,
                cwd=Path(__file__).parent,
                text=True,
            ).strip() or "dev"
        except Exception:
            __version__ = "dev"
except Exception:
    __version__ = "dev"
