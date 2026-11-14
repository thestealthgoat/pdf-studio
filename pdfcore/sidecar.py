"""``pdfcore`` sidecar — the slim standalone program BAR Estimating bundles (integration contract §2).

Interface (stable)::

    pdfcore text <in.pdf> --level words --format json -o <out.json> [--pages 1-3] [--tmp <dir>]
                 [--timeout <s>] [--password-env NAME]
    pdfcore --version          # "pdfcore <semver>" then "schemas: pdfcore-words/1"

Exit codes: 0 ok · 1 the PDF can't be processed · 2 usage · 3 internal. On a non-zero exit exactly one
JSON line goes to stderr: ``{"code": "Encrypted|Damaged|NotPdf|TooLarge|Timeout|Usage|Internal",
"message": "..."}`` — never a traceback, never argparse's usage text.

Guarantees: the input is opened read-only and never modified; the only files written are ``-o`` and one
temp file inside ``--tmp`` (or the OS temp dir), which is always removed; output is written to the temp file
and then moved into place, so ``-o`` is either the complete document or untouched; no network, no telemetry.

This module deliberately imports only what text extraction needs (``pdfcore.words`` → pypdfium2), not
``pdfcore.cli`` / ``pdfcore.ops``, so the frozen build stays small and starts fast.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
import tempfile
import threading

EXIT_OK, EXIT_PDF, EXIT_USAGE, EXIT_INTERNAL = 0, 1, 2, 3
SCHEMAS = ("pdfcore-words/1",)
HARD_TIMEOUT_GRACE = 2.0  # seconds past --timeout before the watchdog ends a run stuck inside one page


class SidecarError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _usage(message: str) -> SidecarError:
    return SidecarError("Usage", message, EXIT_USAGE)


def _emit_error(code: str, message: str) -> None:
    """One JSON line on stderr. ASCII-escaped so it survives any console code page."""
    line = json.dumps({"code": code, "message": message}, ensure_ascii=True) + "\n"
    try:
        stream = sys.__stderr__ or sys.stderr
        if stream is None:
            return
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            buf.write(line.encode("ascii"))
            buf.flush()
        else:
            stream.write(line)
            stream.flush()
    except Exception:
        pass


def _version() -> str:
    from pdfcore import __version__

    return __version__


# ----------------------------------------------------------------------------- arguments
class _Parser(argparse.ArgumentParser):
    """argparse that raises a Usage error instead of printing usage text and exiting 2."""

    def error(self, message: str):  # noqa: D401 - argparse API
        raise _usage(message)


def _parser() -> _Parser:
    ap = _Parser(prog="pdfcore", allow_abbrev=False, add_help=True,
                 description="pdfcore sidecar: word-level text with positions as pdfcore-words/1 JSON.")
    ap.add_argument("--version", action="store_true", help="print the version and the schemas emitted")
    sub = ap.add_subparsers(dest="command", parser_class=_Parser)
    t = sub.add_parser("text", allow_abbrev=False, help="extract words with positions")
    t.add_argument("input", help="input PDF (never modified)")
    t.add_argument("--level", choices=["words"], default="words")
    t.add_argument("--format", choices=["json"], default="json")
    t.add_argument("-o", "--output", required=True, help="output JSON path")
    t.add_argument("--pages", help="1-based pages, e.g. 1-3,5 (default: all)")
    t.add_argument("--tmp", help="folder for the temporary file (default: the OS temp folder)")
    t.add_argument("--timeout", type=float, help="give up after this many seconds (exit 1, Timeout)")
    t.add_argument("--password-env", metavar="NAME", help="read the PDF's user password from this environment variable")
    return ap


def parse_pages(spec: str) -> list[int]:
    """``"1-3,5"`` → ``[0, 1, 2, 4]`` (0-based, sorted, unique). Raises a Usage error on bad input."""
    out: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            raise _usage(f"--pages: empty item in {spec!r}")
        a, sep, b = part.partition("-")
        try:
            lo = int(a)
            hi = int(b) if sep else lo
        except ValueError:
            raise _usage(f"--pages: {part!r} is not a page number or range like 1-3")
        if lo < 1 or hi < lo:
            raise _usage(f"--pages: {part!r} is not a valid range (pages start at 1)")
        out.update(range(lo - 1, hi))
    return sorted(out)


# ----------------------------------------------------------------------------- paths
def fs_path(p: str) -> str:
    """Absolute path usable by the OS. On Windows, long paths get the ``\\\\?\\`` prefix (paths that already
    carry it are kept), so the program works past MAX_PATH whatever the LongPathsEnabled setting is."""
    ap = os.path.abspath(p)
    if os.name != "nt" or ap.startswith("\\\\?\\") or len(ap) < 240:
        return ap
    if ap.startswith("\\\\"):  # UNC \\server\share\...
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(a) == os.path.normcase(b)


# ----------------------------------------------------------------------------- run
class _Run:
    """State shared with the watchdog so a hard timeout can clean up before exiting."""

    def __init__(self):
        self.lock = threading.Lock()
        self.done = False
        self.tmp_file: str | None = None

    def cleanup(self) -> None:
        if self.tmp_file:
            try:
                os.unlink(self.tmp_file)
            except OSError:
                pass
            self.tmp_file = None


def _watchdog(run: _Run, seconds: float) -> threading.Timer:
    def fire():
        with run.lock:
            if run.done:
                return
            run.done = True
            run.cleanup()
            _emit_error("Timeout", f"stopped after {seconds:g} s (no result)")
            os._exit(EXIT_PDF)

    t = threading.Timer(seconds + HARD_TIMEOUT_GRACE, fire)
    t.daemon = True
    t.start()
    return t


def _write_atomic(run: _Run, data: bytes, out: str, tmp_dir: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix="pdfcore-", suffix=".json.part", dir=tmp_dir)
    run.tmp_file = tmp
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.replace(tmp, out)
        run.tmp_file = None
    except OSError as exc:
        # --tmp on another volume: a rename can't cross it, so copy (not atomic, but -o is only touched
        # once the full document exists) and remove the temp file.
        if exc.errno != errno.EXDEV and getattr(exc, "winerror", None) != 17:
            raise
        import shutil

        shutil.copyfile(tmp, out)
        run.cleanup()


def cmd_text(a: argparse.Namespace, run: _Run) -> int:
    from pdfcore.words import WordsError, dumps, words_document

    src = fs_path(a.input)
    out = fs_path(a.output)
    if a.timeout is not None and not (a.timeout > 0):
        raise _usage("--timeout must be a positive number of seconds")
    pages = parse_pages(a.pages) if a.pages else None
    password = None
    if a.password_env:
        if a.password_env not in os.environ:
            raise _usage(f"--password-env: environment variable {a.password_env} is not set")
        password = os.environ[a.password_env]
    if os.path.isdir(out):
        raise _usage("-o points to a folder; give a file path")
    if not os.path.isdir(os.path.dirname(out)):
        raise _usage("the folder for -o does not exist")
    if os.path.exists(src) and _same_file(src, out):
        raise _usage("-o must not be the input file")
    if a.tmp:
        tmp_dir = fs_path(a.tmp)
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except OSError as exc:
            raise _usage(f"--tmp: can't create the folder ({exc.strerror or exc})")
    else:
        tmp_dir = tempfile.gettempdir()
    tempfile.tempdir = tmp_dir  # anything that asks for a temp file lands in --tmp too

    if a.timeout is not None:
        _watchdog(run, a.timeout)
    if os.path.isdir(src):
        raise SidecarError("NotPdf", "the input is a folder, not a PDF file", EXIT_PDF)
    try:
        doc = words_document(src, pages=pages, password=password, timeout=a.timeout)
    except WordsError as exc:
        raise SidecarError(exc.code, exc.message, EXIT_PDF)
    except OSError as exc:
        raise SidecarError("NotPdf", f"can't read the input file ({exc.strerror or exc})", EXIT_PDF)
    data = dumps(doc).encode("utf-8")
    with run.lock:
        if run.done:  # the watchdog already reported a timeout
            return EXIT_PDF
        run.done = True
        try:
            _write_atomic(run, data, out, tmp_dir)
        except OSError as exc:
            raise SidecarError("Internal", f"can't write the output file ({exc.strerror or exc})", EXIT_INTERNAL)
        finally:
            run.cleanup()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    import warnings

    warnings.simplefilter("ignore")
    sys.unraisablehook = lambda *_: None
    threading.excepthook = lambda *_: None
    run = _Run()
    try:
        a = _parser().parse_args(argv)
        if a.version:
            sys.stdout.write(f"pdfcore {_version()}\nschemas: {' '.join(SCHEMAS)}\n")
            sys.stdout.flush()
            return EXIT_OK
        if a.command != "text":
            raise _usage("missing command (expected: text)")
        return cmd_text(a, run)
    except SidecarError as exc:
        run.cleanup()
        _emit_error(exc.code, exc.message)
        return exc.status
    except SystemExit as exc:  # --help (0) or anything else that calls sys.exit
        run.cleanup()
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else EXIT_USAGE)
        if code not in (0, EXIT_USAGE):
            code = EXIT_INTERNAL
        if code:
            _emit_error("Usage", str(exc.code))
        return code
    except BaseException as exc:  # noqa: BLE001 - contract: never a traceback, always one JSON line
        run.cleanup()
        _emit_error("Internal", f"{type(exc).__name__}: {exc}")
        return EXIT_INTERNAL


def entry() -> None:
    """Console-script / frozen entry point."""
    code = main()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os._exit(code)  # skip interpreter teardown: nothing can print after the result line


if __name__ == "__main__":
    entry()
