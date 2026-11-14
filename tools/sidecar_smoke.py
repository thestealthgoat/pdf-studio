#!/usr/bin/env python3
"""Smoke-test a built sidecar zip the way BAR uses it (CI, after tools/build_sidecar.py).

    python tools/sidecar_smoke.py dist/pdfcore-<ver>-<target>.zip [--cold-budget 2] [--size-budget 80]

Unzips into a fresh folder whose path has spaces and non-ASCII characters, then measures:
* size of the unpacked folder (fails over --size-budget MB);
* cold start (first ``--version`` of the fresh copy — on Windows this includes Defender's first scan) and a
  warm start (fails if either exceeds --cold-budget seconds);
* a ``text`` run on a corpus file: output byte-identical to the in-process library;
* nothing written outside -o / --tmp: the program folder, the input folder and a sandboxed temp/home/appdata
  are snapshotted before and after.
Prints a JSON summary and appends a table to $GITHUB_STEP_SUMMARY when set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "corpus"))


def tree(p: Path) -> dict[str, str]:
    return {f.relative_to(p).as_posix(): (hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "<dir>")
            for f in sorted(p.rglob("*"))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--cold-budget", type=float, default=2.0)
    ap.add_argument("--size-budget", type=float, default=80.0)
    a = ap.parse_args()

    import build as corpus_build  # corpus/build.py

    from pdfcore.words import dumps, words_document

    corpus = corpus_build.build()
    zpath = Path(a.zip).resolve()
    base = Path(tempfile.mkdtemp(prefix="pdfcore-smoke-"))
    failures: list[str] = []
    try:
        install = base / "Program Files ü" / "BAR Estimating 測試" / "resources"
        install.mkdir(parents=True)
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(install)
            top = zf.namelist()[0].split("/")[0]
            if sys.platform != "win32":  # extractall drops unix modes (the exec bit): restore them
                for zi in zf.infolist():
                    mode = (zi.external_attr >> 16) & 0o777
                    if mode and not zi.is_dir():
                        (install / zi.filename).chmod(mode)
        folder = install / top
        exe = folder / ("pdfcore.exe" if sys.platform == "win32" else "pdfcore")
        size_mb = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) / 1e6

        sandbox = base / "sandbox"
        env = dict(os.environ)
        for d in ("tmp", "home", "appdata", "localappdata"):
            (sandbox / d).mkdir(parents=True)
        env.update(TMPDIR=str(sandbox / "tmp"), TEMP=str(sandbox / "tmp"), TMP=str(sandbox / "tmp"),
                   HOME=str(sandbox / "home"), APPDATA=str(sandbox / "appdata"),
                   LOCALAPPDATA=str(sandbox / "localappdata"))
        env.pop("PYTHONPATH", None)

        folder_before = tree(folder)
        times = []
        for _ in range(2):
            t0 = time.perf_counter()
            r = subprocess.run([str(exe), "--version"], capture_output=True, env=env, cwd=base)
            times.append(round(time.perf_counter() - t0, 3))
            if r.returncode:
                failures.append(f"--version failed: {r.returncode} {r.stderr!r}")
        version_out = r.stdout.decode(errors="replace").strip()

        indir = base / "input ü"
        indir.mkdir()
        src = indir / "Estimate – Café.pdf"
        shutil.copy(corpus["contract_v1"], src)
        in_before = tree(indir)
        out_dir = base / "out"
        out_dir.mkdir()
        tmpd = base / "scratch"
        t0 = time.perf_counter()
        r = subprocess.run([str(exe), "text", str(src), "--level", "words", "--format", "json",
                            "-o", str(out_dir / "o.json"), "--tmp", str(tmpd)], capture_output=True, env=env, cwd=base)
        text_s = round(time.perf_counter() - t0, 3)
        if r.returncode:
            failures.append(f"text failed: {r.returncode} {r.stderr!r}")
        elif (out_dir / "o.json").read_bytes() != dumps(words_document(src)).encode("utf-8"):
            failures.append("text output differs from the in-process library")
        if tree(indir) != in_before:
            failures.append("input folder changed")
        if tree(folder) != folder_before:
            failures.append("program folder changed")
        if sorted(p.name for p in out_dir.iterdir()) != ["o.json"]:
            failures.append(f"unexpected files next to -o: {sorted(p.name for p in out_dir.iterdir())}")
        if tmpd.exists() and any(tmpd.iterdir()):
            failures.append("temp files left in --tmp")
        stray = [str(p.relative_to(sandbox)) for p in sandbox.rglob("*") if p.is_file()]
        if stray:
            failures.append(f"wrote outside -o/--tmp: {stray}")
        if size_mb > a.size_budget:
            failures.append(f"size {size_mb:.1f} MB > {a.size_budget:g} MB")
        if max(times) >= a.cold_budget:
            failures.append(f"startup {times} s ≥ {a.cold_budget:g} s")

        summary = {"zip": zpath.name, "zip_mb": round(zpath.stat().st_size / 1e6, 1), "unpacked_mb": round(size_mb, 1),
                   "cold_start_s": times[0], "warm_start_s": times[1], "text_contract_v1_s": text_s,
                   "version": version_out, "install_path": str(folder), "failures": failures}
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        gss = os.environ.get("GITHUB_STEP_SUMMARY")
        if gss:
            with open(gss, "a", encoding="utf-8") as fh:
                fh.write(f"### {zpath.name}\n\n| metric | value |\n|---|---|\n")
                for k in ("zip_mb", "unpacked_mb", "cold_start_s", "warm_start_s", "text_contract_v1_s"):
                    fh.write(f"| {k} | {summary[k]} |\n")
                fh.write(f"| result | {'FAIL: ' + '; '.join(failures) if failures else 'ok'} |\n\n")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
