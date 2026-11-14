"""The standalone ``pdfcore`` sidecar (BAR Estimating integration contract §2).

Runs ``python -m pdfcore.sidecar`` always, and the built program too when ``PDFCORE_SIDECAR_BIN`` points at
it (CI sets it after ``tools/build_sidecar.py``)::

    PDFCORE_SIDECAR_BIN=dist/pdfcore-0.1.0-darwin-arm64/pdfcore pytest tests/test_sidecar.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pdfcore import __version__
from pdfcore.words import dumps, schema_path, words_document

ROOT = Path(__file__).resolve().parents[1]
BIN = os.environ.get("PDFCORE_SIDECAR_BIN")
WIN = sys.platform == "win32"

RUNNERS = [pytest.param("module", id="module")]
if BIN:
    RUNNERS.append(pytest.param("binary", id="binary"))
binary_only = pytest.mark.skipif(not BIN, reason="set PDFCORE_SIDECAR_BIN to test the built program")

TEXT_FILES = ["letter", "report_tables", "contract_v1", "drawings_rev0", "slides", "form_fillable", "layers",
              "scanned_letter", "scanned_rotated"]


def _cmd(runner: str, exe: str | None = None) -> list[str]:
    if runner == "binary":
        return [exe or str(Path(BIN).resolve())]
    return [sys.executable, "-m", "pdfcore.sidecar"]


def _env(home: Path) -> dict[str, str]:
    """Child environment whose temp and per-user folders are empty sandboxes we can inspect afterwards."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    for d in ("ostmp", "home", "appdata", "localappdata"):
        (home / d).mkdir(parents=True, exist_ok=True)
    env.update(TMPDIR=str(home / "ostmp"), TEMP=str(home / "ostmp"), TMP=str(home / "ostmp"),
               HOME=str(home / "home"), APPDATA=str(home / "appdata"), LOCALAPPDATA=str(home / "localappdata"))
    env.pop("PDFCORE_TEST_PW", None)
    return env


def run(runner: str, args: list[str], tmp_path: Path, *, env_extra: dict | None = None, exe: str | None = None,
        timeout: float = 120) -> subprocess.CompletedProcess:
    env = _env(tmp_path / "_sandbox")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(_cmd(runner, exe) + [str(a) for a in args], capture_output=True, env=env,
                          cwd=tmp_path, timeout=timeout)


def err_line(r: subprocess.CompletedProcess) -> dict:
    lines = r.stderr.decode("utf-8").splitlines()
    assert len(lines) == 1, f"expected exactly one stderr line, got: {r.stderr!r}"
    obj = json.loads(lines[0])
    assert set(obj) == {"code", "message"} and obj["message"], obj
    assert b"Traceback" not in r.stderr and b"usage:" not in r.stderr
    return obj


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree(p: Path) -> dict[str, str]:
    out = {}
    for f in sorted(p.rglob("*")):
        rel = f.relative_to(p).as_posix()
        out[rel] = sha(f) if f.is_file() else "<dir>"
    return out


@pytest.fixture(scope="module")
def validator():
    import jsonschema

    schema = json.loads(schema_path().read_text(encoding="utf-8"))
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def long_dir(base: Path, min_len: int = 300) -> Path:
    """A nested folder whose absolute path exceeds ``min_len`` characters (Windows MAX_PATH is 260)."""
    d = base
    i = 0
    while len(str(d)) <= min_len:
        d = d / f"deep folder {i:02d} ünïcødé"
        i += 1
    os.makedirs(_os_path(d), exist_ok=True)
    return d


def _os_path(p: Path | str) -> str:
    s = os.path.abspath(str(p))
    if WIN and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s


# ----------------------------------------------------------------------------- basics
@pytest.mark.parametrize("runner", RUNNERS)
def test_version(runner, tmp_path):
    r = run(runner, ["--version"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.decode().splitlines() == [f"pdfcore {__version__}", "schemas: pdfcore-words/1"]
    assert r.stderr == b""


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("name", TEXT_FILES)
def test_words_valid_deterministic_and_matches_library(runner, name, corpus, tmp_path, validator):
    src = corpus[name]
    outs = []
    for i in range(2):
        out = tmp_path / f"out{i}.json"
        r = run(runner, ["text", src, "--level", "words", "--format", "json", "-o", out], tmp_path)
        assert r.returncode == 0, r.stderr
        assert r.stdout == b"" and r.stderr == b""
        outs.append(out.read_bytes())
    assert outs[0] == outs[1], "output is not deterministic"
    doc = json.loads(outs[0].decode("utf-8"))
    validator.validate(doc)
    assert not outs[0].startswith(b"\xef\xbb\xbf") and b"\r\n" not in outs[0] and outs[0].endswith(b"\n")
    assert doc["source"]["sha256"] == sha(src)
    # parity with the in-process library: byte-identical
    assert outs[0] == dumps(words_document(src)).encode("utf-8")


@pytest.mark.parametrize("runner", RUNNERS)
def test_pages_selection(runner, corpus, tmp_path, validator):
    out = tmp_path / "o.json"
    r = run(runner, ["text", corpus["report_tables"], "-o", out, "--pages", "2-3"], tmp_path)
    assert r.returncode == 0, r.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    validator.validate(doc)
    assert [p["index"] for p in doc["pages"]] == [1, 2]
    assert {w["page"] for w in doc["words"]} <= {1, 2}


# ----------------------------------------------------------------------------- errors
@pytest.mark.parametrize("runner", RUNNERS)
def test_encrypted_without_password(runner, corpus, tmp_path):
    out = tmp_path / "o.json"
    r = run(runner, ["text", corpus["encrypted"], "-o", out], tmp_path)
    assert r.returncode == 1
    assert err_line(r)["code"] == "Encrypted"
    assert not out.exists()


@pytest.mark.parametrize("runner", RUNNERS)
def test_encrypted_with_password_env(runner, corpus, tmp_path, validator):
    out = tmp_path / "o.json"
    r = run(runner, ["text", corpus["encrypted"], "-o", out, "--password-env", "PDFCORE_TEST_PW"], tmp_path,
            env_extra={"PDFCORE_TEST_PW": "test"})
    assert r.returncode == 0, r.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    validator.validate(doc)
    assert doc["source"]["encrypted"] is True and doc["words"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_damaged(runner, corpus, tmp_path):
    r = run(runner, ["text", corpus["broken"], "-o", tmp_path / "o.json"], tmp_path)
    assert r.returncode == 1
    assert err_line(r)["code"] == "Damaged"


@pytest.mark.parametrize("runner", RUNNERS)
def test_not_a_pdf(runner, tmp_path):
    fake = tmp_path / "notes.pdf"
    fake.write_text("This is a plain text file renamed to .pdf\n", encoding="utf-8")
    r = run(runner, ["text", fake, "-o", tmp_path / "o.json"], tmp_path)
    assert r.returncode == 1
    assert err_line(r)["code"] == "NotPdf"


@pytest.mark.parametrize("runner", RUNNERS)
def test_missing_input(runner, tmp_path):
    r = run(runner, ["text", tmp_path / "nope.pdf", "-o", tmp_path / "o.json"], tmp_path)
    assert r.returncode == 1
    assert err_line(r)["code"] == "NotPdf"


BAD_ARGS = [
    [],
    ["--bogus"],
    ["text"],
    ["text", "{in}"],                                            # no -o
    ["text", "{in}", "-o", "{out}", "--level", "chars"],
    ["text", "{in}", "-o", "{out}", "--format", "xml"],
    ["text", "{in}", "-o", "{out}", "--pages", "0"],
    ["text", "{in}", "-o", "{out}", "--pages", "3-1"],
    ["text", "{in}", "-o", "{out}", "--pages", "a"],
    ["text", "{in}", "-o", "{out}", "--timeout", "-1"],
    ["text", "{in}", "-o", "{out}", "--timeout", "soon"],
    ["text", "{in}", "-o", "{out}", "--password-env", "PDFCORE_UNSET_VAR"],
    ["text", "{in}", "-o", "{in}"],                              # would overwrite the input
    ["text", "{in}", "-o", "{missing}"],                         # output folder doesn't exist
    ["unknown-command", "{in}"],
]


@pytest.mark.parametrize("runner", RUNNERS)
@pytest.mark.parametrize("args", BAD_ARGS, ids=lambda a: " ".join(a) or "<none>")
def test_usage_errors(runner, args, corpus, tmp_path):
    src = tmp_path / "in.pdf"
    shutil.copy(corpus["letter"], src)
    before = sha(src)
    subst = {"{in}": str(src), "{out}": str(tmp_path / "o.json"), "{missing}": str(tmp_path / "no" / "o.json")}
    r = run(runner, [subst.get(a, a) for a in args], tmp_path)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert err_line(r)["code"] == "Usage"
    assert r.stdout == b""
    assert sha(src) == before


@pytest.mark.parametrize("runner", RUNNERS)
def test_timeout(runner, corpus, tmp_path):
    """--timeout tiny on the 50 MB corpus file: exit 1 Timeout, no output, temp folder left empty."""
    out = tmp_path / "o.json"
    tmpd = tmp_path / "scratch"
    tmpd.mkdir()
    r = run(runner, ["text", corpus["image_heavy"], "-o", out, "--tmp", tmpd, "--timeout", "0.000001"], tmp_path)
    assert r.returncode == 1
    assert err_line(r)["code"] == "Timeout"
    assert not out.exists()
    assert list(tmpd.iterdir()) == []


def test_hard_timeout_watchdog(tmp_path):
    """A page that never finishes (simulated) is still stopped: the watchdog fires at --timeout + grace."""
    code = (
        "import time, sys, pdfcore.words as w\n"
        "w.words_document = lambda *a, **k: time.sleep(60)\n"
        "from pdfcore.sidecar import entry\n"
        "sys.argv = ['pdfcore'] + sys.argv[1:]\n"
        "entry()\n"
    )
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    tmpd = tmp_path / "scratch"
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code, "text", str(src), "-o", str(tmp_path / "o.json"), "--tmp", str(tmpd),
                        "--timeout", "0.5"], capture_output=True, env=_env(tmp_path / "_sandbox"), timeout=30)
    took = time.monotonic() - t0
    assert r.returncode == 1
    assert err_line(r)["code"] == "Timeout"
    assert took < 10
    assert not (tmp_path / "o.json").exists()


def test_internal_error_is_one_json_line(tmp_path):
    """An unexpected exception: exit 3, {"code": "Internal"}, no traceback."""
    code = (
        "import sys, pdfcore.words as w\n"
        "def boom(*a, **k):\n    raise RuntimeError('simulated bug')\n"
        "w.words_document = boom\n"
        "from pdfcore.sidecar import entry\n"
        "sys.argv = ['pdfcore'] + sys.argv[1:]\n"
        "entry()\n"
    )
    src = tmp_path / "a.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    r = subprocess.run([sys.executable, "-c", code, "text", str(src), "-o", str(tmp_path / "o.json")],
                       capture_output=True, env=_env(tmp_path / "_sandbox"), timeout=60)
    assert r.returncode == 3
    e = err_line(r)
    assert e["code"] == "Internal" and "simulated bug" in e["message"]


@pytest.mark.parametrize("runner", RUNNERS)
def test_failed_run_leaves_existing_output_untouched(runner, corpus, tmp_path):
    out = tmp_path / "o.json"
    out.write_bytes(b"previous result\n")
    r = run(runner, ["text", corpus["broken"], "-o", out], tmp_path)
    assert r.returncode == 1
    assert out.read_bytes() == b"previous result\n"


# ----------------------------------------------------------------------------- filesystem behaviour
@pytest.mark.parametrize("runner", RUNNERS)
def test_readonly_input_unchanged_and_no_stray_writes(runner, corpus, tmp_path):
    """Input in a read-only folder with spaces/non-ASCII; nothing is written except -o (and temp inside --tmp,
    removed afterwards); the input file is byte-identical; the per-user and temp folders stay empty."""
    ro = tmp_path / "Read only ünïcødé 測試"
    ro.mkdir()
    src = ro / "Estimate #12 – Café.pdf"
    shutil.copy(corpus["letter"], src)
    before_hash, before_tree = sha(src), tree(ro)
    os.chmod(src, stat.S_IREAD)
    os.chmod(ro, stat.S_IREAD | stat.S_IEXEC)
    outdir = tmp_path / "out dir é"
    outdir.mkdir()
    tmpd = tmp_path / "scratch é"
    tmpd.mkdir()
    try:
        r = run(runner, ["text", src, "-o", outdir / "words é.json", "--tmp", tmpd], tmp_path)
        assert r.returncode == 0, r.stderr
    finally:
        os.chmod(ro, stat.S_IRWXU)
        os.chmod(src, stat.S_IREAD | stat.S_IWRITE)
    assert sha(src) == before_hash
    assert tree(ro) == before_tree
    assert sorted(p.name for p in outdir.iterdir()) == ["words é.json"]
    assert list(tmpd.iterdir()) == []
    sandbox = tmp_path / "_sandbox"
    assert [p for p in sandbox.rglob("*") if p.is_file()] == [], "wrote into temp / home / appdata"


@pytest.mark.parametrize("runner", RUNNERS)
def test_default_tmp_is_cleaned(runner, corpus, tmp_path):
    out = tmp_path / "o.json"
    r = run(runner, ["text", corpus["letter"], "-o", out], tmp_path)
    assert r.returncode == 0, r.stderr
    assert list((tmp_path / "_sandbox" / "ostmp").iterdir()) == []


@pytest.mark.parametrize("runner", RUNNERS)
def test_long_paths(runner, corpus, tmp_path, validator):
    """Input and output paths over 260 characters, plain and (on Windows) with the \\\\?\\ prefix."""
    deep = long_dir(tmp_path)
    src = deep / "input é.pdf"
    shutil.copy(corpus["letter"], _os_path(src))
    variants = [(str(src), str(deep / "out1.json"))]
    if WIN:
        variants.append((_os_path(src), _os_path(deep / "out2.json")))
    for s, o in variants:
        r = run(runner, ["text", s, "-o", o, "--tmp", deep / "tmp"], tmp_path)
        assert r.returncode == 0, r.stderr
        with open(_os_path(o), "rb") as fh:
            data = fh.read()
        validator.validate(json.loads(data))
        assert data == dumps(words_document(corpus["letter"])).encode("utf-8")


# ----------------------------------------------------------------------------- the built program
def _copy_dist(dest: Path) -> str:
    dist = Path(BIN).resolve().parent
    shutil.copytree(_os_path(dist), _os_path(dest))
    return str(dest / Path(BIN).name)


@binary_only
def test_binary_runs_from_path_with_spaces_and_non_ascii(corpus, tmp_path, validator):
    exe = _copy_dist(tmp_path / "Program Files ü" / "BAR Estimating 測試" / "resources" / "pdfcore")
    dist_before = tree(Path(exe).parent)
    r = run("binary", ["--version"], tmp_path, exe=exe)
    assert r.returncode == 0, r.stderr
    out = tmp_path / "o.json"
    r = run("binary", ["text", corpus["letter"], "-o", out], tmp_path, exe=exe)
    assert r.returncode == 0, r.stderr
    validator.validate(json.loads(out.read_bytes()))
    assert tree(Path(exe).parent) == dist_before, "the program wrote into its own folder"


@binary_only
def test_binary_runs_from_deep_path(corpus, tmp_path):
    deep = long_dir(tmp_path, 270)
    exe = _copy_dist(deep / "pdfcore")
    try:
        r = run("binary", ["--version"], tmp_path, exe=_os_path(exe) if WIN else exe)
    except OSError as exc:  # CreateProcess can refuse an image path longer than MAX_PATH
        if WIN:
            pytest.xfail(f"Windows can't start a program stored at a path > 260 chars ({exc}); "
                         "BAR installs the sidecar under a short path")
        raise
    assert r.returncode == 0, r.stderr


@binary_only
def test_binary_startup_time(tmp_path):
    """Contract: under ~2 s cold on Windows. First run of a fresh copy, then a warm run."""
    exe = _copy_dist(tmp_path / "fresh")
    times = []
    for _ in range(2):
        t0 = time.perf_counter()
        r = run("binary", ["--version"], tmp_path, exe=exe)
        times.append(time.perf_counter() - t0)
        assert r.returncode == 0, r.stderr
    print(f"startup: cold {times[0]:.3f} s, warm {times[1]:.3f} s")
    assert times[0] < 2.0 and times[1] < 2.0, times


@binary_only
def test_binary_bundle_contents():
    """No numpy / Pillow / pikepdf etc. in the folder; licences and schema shipped."""
    dist = Path(BIN).resolve().parent
    names = {p.relative_to(dist).as_posix().lower() for p in dist.rglob("*")}
    for banned in ("numpy", "pil/", "pikepdf", "reportlab", "pdfplumber", "libssl", "libcrypto", "sqlite3"):
        assert not any(banned in n for n in names), banned
    assert (dist / "THIRD_PARTY_LICENSES.md").is_file()
    assert (dist / "schemas" / "pdfcore-words-1.schema.json").read_bytes() == schema_path().read_bytes()
    lic = (dist / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8")
    for needle in ("PSF", "pdfium", "Apache License", "MIT"):
        assert needle.lower() in lic.lower(), needle
