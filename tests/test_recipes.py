from __future__ import annotations

import hashlib
from pathlib import Path

import pikepdf
import pytest
import yaml

from pdfcore.cli import main
from pdfcore.ops import recipes_op
from pdfcore.ops._common import finalize
from pdfcore.recipes import (
    OpNotAvailable,
    list_examples,
    load_recipe,
    register_op,
    resolve_op,
    run_recipe,
    unregister_op,
)
from pdfcore.result import output_path

CALLS: list[dict] = []


def fake_rotate(src: str | Path, angle: int = 90, pages: list[int] | None = None, out=None, *, quiet: bool = False):
    """Tiny stand-in op: rotate pages (0-based *pages*), write a new file, finalize."""
    CALLS.append({"op": "rotate", "src": Path(src), "angle": angle, "pages": pages, "quiet": quiet})
    dst = output_path(src, "rot", out=out)
    with pikepdf.open(src) as pdf:
        for i in (range(len(pdf.pages)) if pages is None else pages):
            pdf.pages[i].rotate(angle, relative=True)
        pdf.save(dst)
    return finalize(dst, baseline=Path(src), quiet=quiet)


def fake_title(src: str | Path, *, title: str, quiet: bool = False):
    CALLS.append({"op": "title", "src": Path(src), "title": title, "quiet": quiet})
    dst = output_path(src, "titled")
    with pikepdf.open(src) as pdf:
        pdf.docinfo["/Title"] = title
        pdf.save(dst)
    return finalize(dst, baseline=Path(src), quiet=quiet)


def fake_boom(src, **kw):
    raise RuntimeError("kaboom")


@pytest.fixture(autouse=True)
def fakes():
    CALLS.clear()
    register_op("fake.rotate", fake_rotate)
    register_op("fake.title", fake_title)
    register_op("fake.boom", fake_boom)
    yield
    for n in ("fake.rotate", "fake.title", "fake.boom"):
        unregister_op(n)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _rotations(p: Path) -> list[int]:
    with pikepdf.open(p) as pdf:
        return [int(pg.obj.get("/Rotate", 0)) for pg in pdf.pages]


BASIC = {
    "name": "Test Chain",
    "description": "rotate then title",
    "steps": [
        {"op": "fake.rotate", "pages": "2", "degrees": 90},
        {"op": "fake.title", "title": "Chained"},
    ],
}


def test_chain_writes_only_final_file(work, capsys):
    src = work("report_tables")
    before = _sha(src)
    run = run_recipe(BASIC, [src])
    assert run.ok and run.passed == 1
    out = run.files[0].output
    assert out == src.parent / "report_tables.test-chain.pdf"
    assert _rotations(out) == [0, 90, 0]
    with pikepdf.open(out) as pdf:
        assert str(pdf.docinfo["/Title"]) == "Chained"
    # step 2 got step 1's output; both ran quietly in a temp dir
    assert CALLS[0]["pages"] == [1] and CALLS[0]["angle"] == 90 and CALLS[0]["quiet"] is True
    assert CALLS[1]["src"].name == "report_tables.rot.pdf" and CALLS[1]["src"].parent != src.parent
    assert not CALLS[1]["src"].exists()  # intermediates cleaned up
    assert sorted(p.name for p in src.parent.iterdir()) == ["report_tables.pdf", "report_tables.test-chain.pdf"]
    assert _sha(src) == before
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == f"{out}  PASS" and "1 PASS, 0 FAIL" in lines[-1]
    # a second run never overwrites
    again = run_recipe(BASIC, [src], quiet=True)
    assert again.files[0].output.name == "report_tables.test-chain-2.pdf"


def test_yaml_file_input_glob_output_folder_naming(tmp_path, corpus, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for n in ("letter", "contract_v1"):
        (inbox / f"{n}.pdf").write_bytes(corpus[n].read_bytes())
    monkeypatch.setenv("PDFCORE_TEST_TITLE", "From Env")
    rec = tmp_path / "flow.yaml"
    rec.write_text(yaml.safe_dump({
        "name": "Flow",
        "input": "inbox/*.pdf",
        "output": "done",
        "naming": "{n:02d}-{stem}.final.pdf",
        "steps": [{"op": "fake.title", "title": "${PDFCORE_TEST_TITLE}"}],
    }))
    res = recipes_op.recipe(rec, quiet=True)
    assert res.ok and res.data["passed"] == 2
    names = sorted(p.name for p in (tmp_path / "done").iterdir())
    assert names == ["01-contract_v1.final.pdf", "02-letter.final.pdf"]
    assert CALLS[0]["title"] == "From Env"
    assert sorted(p.name for p in inbox.iterdir()) == ["contract_v1.pdf", "letter.pdf"]


def test_dry_run_reports_plan_and_writes_nothing(work, capsys):
    src = work("letter")
    spec = {"name": "Plan", "steps": [{"op": "fake.rotate", "angle": 180},
                                      {"op": "nosuchmodule.func"},
                                      {"op": "fake.title", "title": "x", "colour": "red"}]}
    run = run_recipe(spec, [src], dry_run=True)
    out = capsys.readouterr().out
    assert not run.ok
    assert "1. fake.rotate(angle=180)  [ok]" in out
    assert "pdfcore.ops.nosuchmodule is not available" in out
    assert "unknown parameter 'colour'" in out
    assert str(src.parent / "letter.plan.pdf") in out
    assert CALLS == [] and sorted(p.name for p in src.parent.iterdir()) == ["letter.pdf"]


def test_failing_step_fails_that_file_only(tmp_path, corpus, capsys):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_bytes(corpus["letter"].read_bytes())
    b.write_bytes(corpus["letter"].read_bytes())
    register_op("picky", lambda src, quiet=False: fake_boom(src) if Path(src).stem == "a" else fake_title(src, title="ok", quiet=quiet))
    try:
        res = recipes_op.recipe({"name": "Picky", "steps": ["picky"]}, [a, b])
    finally:
        unregister_op("picky")
    assert not res.ok and res.data["passed"] == 1 and res.data["failed"] == 1
    out = capsys.readouterr().out
    assert f"{a}  FAIL step 1 picky: kaboom" in out
    assert (tmp_path / "b.picky.pdf").exists() and not (tmp_path / "a.picky.pdf").exists()
    assert main(["recipe", str(_write(tmp_path, {"name": "Boom", "steps": ["fake.boom"]})), str(a)]) == 1


def _write(tmp_path: Path, spec: dict) -> Path:
    p = tmp_path / f"{spec['name'].lower()}.yaml"
    p.write_text(yaml.safe_dump(spec))
    return p


def test_side_outputs_from_non_pdf_steps(work):
    src = work("letter")
    spec = {"name": "Proof", "steps": [
        {"op": "fake.rotate", "angle": 90, "pages": "1"},
        {"op": "extract.render", "dpi": 20, "pages": "1"},  # PNG side output; the PDF flows on
        {"op": "fake.title", "title": "after render"},
    ]}
    run = run_recipe(spec, [src], quiet=True)
    oc = run.files[0]
    assert oc.ok and oc.output.name == "letter.proof.pdf"
    assert [p.suffix for p in oc.extra] == [".png"]
    assert oc.extra[0].parent.parent == src.parent and oc.extra[0].exists()


def test_unknown_parameter_fails_with_accepted_list(work):
    run = run_recipe({"name": "Bad", "steps": [{"op": "fake.title", "titel": "x"}]}, [work("letter")], quiet=True)
    assert not run.ok and "unknown parameter 'titel'" in run.files[0].line and "accepts: title" in run.files[0].line


def test_resolution_rules():
    assert resolve_op("fake.rotate") is fake_rotate
    from pdfcore.ops import extract

    assert resolve_op("extract.tables") is extract.tables
    assert resolve_op("tables") is extract.tables  # alias
    assert resolve_op("find_tables") is extract.find_tables  # bare-name search over pdfcore.ops modules
    assert resolve_op("pdfcore.ops.convert:images_to_pdf").__name__ == "images_to_pdf"
    with pytest.raises(OpNotAvailable):
        resolve_op("extract._verify")
    with pytest.raises(OpNotAvailable):
        resolve_op("not_a_real_op_anywhere")


def test_load_validation(tmp_path):
    with pytest.raises(ValueError, match="steps"):
        load_recipe({"name": "x"})
    with pytest.raises(ValueError, match="unknown key"):
        load_recipe({"name": "x", "steps": ["fake.title"], "stepz": []})
    with pytest.raises(ValueError, match="placeholder"):
        load_recipe({"name": "x", "steps": ["fake.title"], "naming": "{nope}.pdf"})
    with pytest.raises(FileNotFoundError):
        load_recipe("no-such-recipe")
    with pytest.raises(ValueError, match="no 'input'"):
        run_recipe({"name": "x", "steps": ["fake.title"]})


def test_examples_load_and_plan(work, capsys):
    ex = {p.stem for p in list_examples()}
    assert {"bid-set-prep", "client-deliverable", "scan-intake"} <= ex
    for name in ex:
        r = load_recipe(name)
        assert r.steps and r.description and r.source is not None
        text = r.source.read_text()
        assert "resolve" in text and "--dry-run" in text
    ops = [s.op for s in load_recipe("bid-set-prep").steps + load_recipe("client-deliverable").steps + load_recipe("scan-intake").steps]
    assert {"organize.rotate", "stamp.stamp", "navigate.auto_bookmarks", "stamp.watermark", "optimize.optimize",
            "security.encrypt", "ocr.ocr"} <= set(ops)
    src = work("letter")
    rc = main(["recipe", "bid-set-prep", str(src), "--dry-run"])
    out = capsys.readouterr().out
    assert "Steps (3)" in out and rc in (0, 1)
    assert main(["recipe", "--list"]) == 0
    assert "scan-intake" in capsys.readouterr().out


def test_rerun_on_folder_skips_own_outputs(tmp_path, corpus):
    (tmp_path / "x.pdf").write_bytes(corpus["letter"].read_bytes())
    spec = {"name": "Again", "steps": [{"op": "fake.title", "title": "t"}]}
    run_recipe(spec, [tmp_path], quiet=True)
    run2 = run_recipe(spec, [tmp_path], quiet=True)
    assert [f.input.name for f in run2.files] == ["x.pdf"]
