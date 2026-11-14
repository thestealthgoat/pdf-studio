"""Signatures: sign / certify / timestamp / verify with throwaway test PKI (no network)."""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("pyhanko")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.serialization import pkcs12  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

from pdfcore.ops import sign as sg  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEST_PASS = "throwaway-test-passphrase"  # protects a key generated inside this test session only


# ---------------------------------------------------------------- test PKI (generated per session)
def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PDF Studio Test PKI")])


def _ku(**on) -> x509.KeyUsage:
    keys = ["digital_signature", "content_commitment", "key_encipherment", "data_encipherment", "key_agreement",
            "key_cert_sign", "crl_sign", "encipher_only", "decipher_only"]
    return x509.KeyUsage(**{k: on.get(k, False) for k in keys})


@pytest.fixture(scope="session")
def pki(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("pki")
    now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = (x509.CertificateBuilder().subject_name(_name("Test Root CA")).issuer_name(_name("Test Root CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now).not_valid_after(now + dt.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(_ku(digital_signature=True, key_cert_sign=True, crl_sign=True), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    crl_dp = x509.CRLDistributionPoints([x509.DistributionPoint(
        [x509.UniformResourceIdentifier("http://crl.invalid/test-root.crl")], None, None, None)])

    def leaf(cn: str, eku=None):
        k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        b = (x509.CertificateBuilder().subject_name(_name(cn)).issuer_name(ca.subject).public_key(k.public_key())
             .serial_number(x509.random_serial_number()).not_valid_before(now)
             .not_valid_after(now + dt.timedelta(days=10))
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             .add_extension(_ku(digital_signature=True, content_commitment=eku is None), critical=True)
             .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
             .add_extension(crl_dp, critical=False))
        if eku:
            b = b.add_extension(x509.ExtendedKeyUsage(eku), critical=True)
        return k, b.sign(ca_key, hashes.SHA256())

    sk, sc = leaf("Test Signer")
    tk, tc = leaf("Test TSA", [ExtendedKeyUsageOID.TIME_STAMPING])
    crl = (x509.CertificateRevocationListBuilder().issuer_name(ca.subject).last_update(now)
           .next_update(now + dt.timedelta(days=7))
           .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
           .add_extension(x509.CRLNumber(1), critical=False).sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    p8 = serialization.PrivateFormat.PKCS8
    out = {
        "ca": d / "ca.pem", "cert": d / "signer.pem", "key": d / "signer.key", "key_enc": d / "signer-enc.key",
        "tsa_cert": d / "tsa.pem", "tsa_key": d / "tsa.key", "crl": d / "root.crl", "p12": d / "signer.p12",
    }
    out["ca"].write_bytes(ca.public_bytes(pem))
    out["cert"].write_bytes(sc.public_bytes(pem))
    out["key"].write_bytes(sk.private_bytes(pem, p8, serialization.NoEncryption()))
    out["key_enc"].write_bytes(sk.private_bytes(pem, p8, serialization.BestAvailableEncryption(TEST_PASS.encode())))
    out["tsa_cert"].write_bytes(tc.public_bytes(pem))
    out["tsa_key"].write_bytes(tk.private_bytes(pem, p8, serialization.NoEncryption()))
    out["crl"].write_bytes(crl.public_bytes(serialization.Encoding.DER))
    out["p12"].write_bytes(pkcs12.serialize_key_and_certificates(
        b"signer", sk, sc, [ca], serialization.BestAvailableEncryption(TEST_PASS.encode())))
    return out


@pytest.fixture(scope="session")
def tsa(pki):
    """In-process RFC 3161 TSA (pyHanko's DummyTimeStamper) — no network."""
    from pyhanko.keys import load_cert_from_pemder, load_private_key_from_pemder
    from pyhanko.sign.timestamps.dummy_client import DummyTimeStamper

    return DummyTimeStamper(tsa_cert=load_cert_from_pemder(str(pki["tsa_cert"])),
                            tsa_key=load_private_key_from_pemder(str(pki["tsa_key"]), None),
                            certs_to_embed=[load_cert_from_pemder(str(pki["ca"]))])


def _ident(pki) -> dict:
    return dict(key=pki["key"], cert=pki["cert"], chain=[pki["ca"]])


def _verify(path, pki, **kw):
    return sg.verify(path, trust_roots=[pki["ca"]], quiet=True, **kw)


def _has_verapdf() -> bool:
    return shutil.which("verapdf") is not None


# ---------------------------------------------------------------- basic signing
def test_sign_bb_invisible_is_incremental_and_valid(work, pki):
    src = work("letter")
    before = src.read_bytes()
    res = sg.sign(src, level="b-b", quiet=True, **_ident(pki))
    assert res.ok, res.line()
    assert res.output != src and res.output.exists()
    assert src.read_bytes() == before  # input untouched
    assert res.output.read_bytes().startswith(before.rstrip(b"\n"))  # incremental update
    assert res.data["visible"] is False and res.data["field"] == "Signature1"
    v = _verify(res.output, pki)
    assert v.ok
    s = v.data["signatures"][0]
    assert s["intact"] and s["trusted"] and s["covers_whole_file"]
    assert s["signer"] == "Test Signer"
    assert s["pades_level"] == "PAdES B-B"
    assert s["verdict"] == "valid and trusted"


def test_verify_untrusted_root_is_reported_but_intact(work, pki):
    res = sg.sign(work("letter"), quiet=True, **_ident(pki))
    v = sg.verify(res.output, trust_roots=[], quiet=True)
    s = v.data["signatures"][0]
    assert s["intact"] and not s["trusted"]
    assert s["verdict"] == "valid but untrusted root"
    assert "trusted root" in (s["trust_problem"] or "")
    assert v.ok  # exit 0: all intact
    strict = sg.verify(res.output, trust_roots=[], require_trust=True, quiet=True)
    assert not strict.ok


def test_visible_signature_uses_embedded_inter(work, pki):
    import pikepdf

    res = sg.sign(work("letter"), rect=(330, 650, 540, 710), reason="Approved", location="Springfield",
                  quiet=True, **_ident(pki))
    assert res.ok and res.data["visible"]
    with pikepdf.open(res.output) as pdf:
        annots = [a for a in pdf.pages[-1].obj.get("/Annots", []) if a.get("/FT") == "/Sig"]
        assert annots
        a = annots[0]
        assert int(a.F) & 4  # Print flag
        fonts = []

        def walk(xobj, depth=0):
            res_ = xobj.get("/Resources", {})
            for _, f in (res_.get("/Font", {}) or {}).items():
                fonts.append(f)
            for _, x in (res_.get("/XObject", {}) or {}).items():
                if depth < 6:
                    walk(x, depth + 1)

        walk(a.AP.N)
        assert fonts, "appearance draws no text"
        for f in fonts:
            assert "Inter" in str(f.BaseFont)
            fd = f.get("/FontDescriptor") or f.DescendantFonts[0].FontDescriptor
            assert "/FontFile2" in fd or "/FontFile3" in fd or "/FontFile" in fd
    s = _verify(res.output, pki).data["signatures"][0]
    assert s["intact"]


def test_visible_signature_with_image(work, pki, tmp_path):
    from PIL import Image

    img = tmp_path / "sig.png"
    Image.new("RGBA", (120, 40), (20, 40, 160, 200)).save(img)
    res = sg.sign(work("letter"), rect=(330, 650, 540, 710), image=img, quiet=True, **_ident(pki))
    assert res.ok
    assert _verify(res.output, pki).ok


def test_bt_needs_tsa(work, pki):
    with pytest.raises(ValueError, match="time-stamping authority"):
        sg.sign(work("letter"), level="b-t", quiet=True, **_ident(pki))


def test_bt_timestamp(work, pki, tsa):
    res = sg.sign(work("letter"), level="b-t", timestamper=tsa, quiet=True, **_ident(pki))
    assert res.ok
    s = _verify(res.output, pki).data["signatures"][0]
    assert s["pades_level"] == "PAdES B-T"
    assert s["timestamp"]["tsa"] == "Test TSA" and s["timestamp"]["trusted"]


def test_blt_embeds_revocation_info(work, pki, tsa):
    res = sg.sign(work("letter"), level="b-lt", timestamper=tsa, trust_roots=[pki["ca"]], crls=[pki["crl"]],
                  allow_fetching=False, quiet=True, **_ident(pki))
    assert res.ok
    assert res.data["dss"]["crls"] >= 1 and "warnings" not in res.data
    v = _verify(res.output, pki)
    s = v.data["signatures"][0]
    assert s["ltv"], s["ltv_reason"]
    assert s["pades_level"] == "PAdES B-LT"
    assert v.ok


def test_blta_adds_document_timestamp(work, pki, tsa):
    res = sg.sign(work("letter"), level="b-lta", timestamper=tsa, trust_roots=[pki["ca"]], crls=[pki["crl"]],
                  allow_fetching=False, quiet=True, **_ident(pki))
    assert res.ok
    v = _verify(res.output, pki)
    kinds = [s["kind"] for s in v.data["signatures"]]
    assert kinds == ["approval", "timestamp"]
    sig = v.data["signatures"][0]
    assert sig["pades_level"] == "PAdES B-LTA"
    assert not sig["covers_whole_file"] and sig["modification_level"] == "LTA_UPDATES"
    assert v.ok


def test_blt_without_revocation_info_warns(work, pki, tsa):
    res = sg.sign(work("letter"), level="b-lt", timestamper=tsa, trust_roots=[pki["ca"]], allow_fetching=False,
                  quiet=True, **_ident(pki))
    assert res.output.exists()
    assert any("LTV" in w for w in res.data.get("warnings", []))


# ---------------------------------------------------------------- key material
def test_encrypted_key_passphrase_env_and_argument(work, pki, monkeypatch):
    src = work("letter")
    monkeypatch.delenv(sg.PASSPHRASE_ENV, raising=False)
    with pytest.raises(ValueError) as ei:
        sg.sign(src, key=pki["key_enc"], cert=pki["cert"], quiet=True)
    assert "passphrase" in str(ei.value)
    with pytest.raises(ValueError) as ei:
        sg.sign(src, key=pki["key_enc"], cert=pki["cert"], passphrase="not-it", quiet=True)
    assert "not-it" not in str(ei.value)
    monkeypatch.setenv("MY_SIGN_PW", TEST_PASS)
    res = sg.sign(src, key=pki["key_enc"], cert=pki["cert"], passphrase_env="MY_SIGN_PW", quiet=True)
    assert res.ok and TEST_PASS not in json.dumps(res.data, default=str)
    res2 = sg.sign(src, key=pki["key_enc"], cert=pki["cert"], passphrase=TEST_PASS, quiet=True)
    assert res2.ok and res2.output != res.output


def test_pkcs12(work, pki):
    res = sg.sign(work("letter"), pkcs12=pki["p12"], passphrase=TEST_PASS, quiet=True)
    assert res.ok
    assert _verify(res.output, pki).data["signatures"][0]["trusted"]


def test_missing_identity_is_clear(work):
    with pytest.raises(ValueError, match="--key and --cert"):
        sg.sign(work("letter"), quiet=True)


# ---------------------------------------------------------------- fields
def test_sign_existing_empty_field(work, pki, tmp_path):
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields

    src = work("letter")
    with_field = tmp_path / "with-field.pdf"
    with open(src, "rb") as f:
        w = IncrementalPdfFileWriter(f)
        fields.append_signature_field(w, fields.SigFieldSpec("ApproverSig", on_page=0, box=(320, 80, 540, 140)))
        with open(with_field, "wb") as o:
            w.write(o)
    res = sg.sign(with_field, quiet=True, **_ident(pki))  # picks the empty field
    assert res.ok
    assert res.data["field"] == "ApproverSig" and not res.data["new_field"] and res.data["visible"]
    assert res.data["box"] == [320, 80, 540, 140]
    v = _verify(res.output, pki)
    assert [s["field"] for s in v.data["signatures"]] == ["ApproverSig"]
    with pytest.raises(ValueError, match="already signed"):
        sg.sign(res.output, field="ApproverSig", quiet=True, **_ident(pki))


def test_second_signature_keeps_first_valid(work, pki):
    first = sg.sign(work("letter"), quiet=True, **_ident(pki))
    second = sg.sign(first.output, reason="Second approval", quiet=True, **_ident(pki))
    assert second.data["field"] == "Signature2"
    v = _verify(second.output, pki)
    a, b = v.data["signatures"]
    assert a["intact"] and not a["covers_whole_file"] and a["modification_level"] == "FORM_FILLING"
    assert b["covers_whole_file"]
    assert v.ok


# ---------------------------------------------------------------- certify + tampering
def _append_content(src: Path, dst: Path) -> None:
    """Incremental update that draws on page 1 (a real content change)."""
    from pyhanko.pdf_utils import generic
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    with open(src, "rb") as f:
        w = IncrementalPdfFileWriter(f)
        page_ref, _ = w.find_page_for_modification(0)
        page = page_ref.get_object()
        st = w.add_object(generic.StreamObject(stream_data=b"q 1 0 0 rg 20 20 60 60 re f Q"))
        old = page.raw_get("/Contents")
        arr = generic.ArrayObject(list(old.get_object()) if isinstance(old.get_object(), generic.ArrayObject) else [old])
        arr.append(st)
        page["/Contents"] = arr
        w.mark_update(page_ref)
        with open(dst, "wb") as o:
            w.write(o)


def test_certify_no_changes_then_modified(work, pki, tmp_path):
    res = sg.certify(work("letter"), docmdp="no-changes", quiet=True, **_ident(pki))
    assert res.ok and res.data["docmdp"] == "no-changes"
    v = _verify(res.output, pki)
    s = v.data["signatures"][0]
    assert s["kind"] == "certification" and s["docmdp"] == "no-changes" and v.ok
    tampered = tmp_path / "changed.pdf"
    _append_content(res.output, tampered)
    v2 = _verify(tampered, pki)
    s2 = v2.data["signatures"][0]
    assert s2["intact"]  # the signed bytes are fine …
    assert s2["verdict"] == "modified after signing"  # … but the document changed afterwards
    assert not v2.ok


def test_certify_form_fill_allows_signature(work, pki):
    cert = sg.certify(work("letter"), docmdp="form-fill", quiet=True, **_ident(pki))
    appr = sg.sign(cert.output, quiet=True, **_ident(pki))
    v = _verify(appr.output, pki)
    assert v.ok and v.data["signatures"][0]["docmdp_ok"] is not False


def test_certify_must_be_first(work, pki):
    signed = sg.sign(work("letter"), quiet=True, **_ident(pki))
    with pytest.raises(ValueError, match="first signature"):
        sg.certify(signed.output, quiet=True, **_ident(pki))


def test_tampered_bytes_not_intact(work, pki, tmp_path):
    res = sg.sign(work("letter"), quiet=True, **_ident(pki))
    data = res.output.read_bytes()
    assert b"Sample letter" in data
    bad = tmp_path / "tampered.pdf"
    bad.write_bytes(data.replace(b"Sample letter", b"Simple letter", 1))
    v = _verify(bad, pki)
    s = v.data["signatures"][0]
    assert not s["intact"] and s["verdict"].startswith("BROKEN")
    assert not v.ok


def test_verify_unsigned_fails(work):
    v = sg.verify(work("letter"), trust_roots=[], quiet=True)
    assert not v.ok and "no signatures" in v.line()


def test_verify_corpus_signed(corpus):
    v = sg.verify(corpus["signed"], trust_roots=[ROOT / "corpus/generated/testkeys/signer.cert.pem"], quiet=True)
    s = v.data["signatures"][0]
    assert s["field"] == "Signature1" and s["intact"] and v.ok


# ---------------------------------------------------------------- timestamp
def test_document_timestamp(work, pki, tsa):
    res = sg.timestamp(work("letter"), timestamper=tsa, quiet=True)
    assert res.ok
    v = _verify(res.output, pki)
    s = v.data["signatures"][0]
    assert s["kind"] == "timestamp" and s["pades_level"] == "document timestamp"
    assert s["intact"] and s["trusted"]


def test_timestamp_needs_tsa(work):
    with pytest.raises(ValueError, match="time-stamping authority"):
        sg.timestamp(work("letter"), quiet=True)


# ---------------------------------------------------------------- CLI
def test_cli_sign_and_verify_exit_codes(work, pki, tmp_path, capsys):
    from pdfcore.cli import main

    src = work("letter")
    out = tmp_path / "cli-signed.pdf"
    assert main(["sign", str(src), "--key", str(pki["key"]), "--cert", str(pki["cert"]), "--rect", "330,650,540,710",
                 "--reason", "CLI", "-o", str(out)]) == 0
    assert out.exists()
    assert main(["verify", str(out), "--trust-root", str(pki["ca"])]) == 0
    assert main(["verify", str(out), "--trust-root", str(pki["ca"]), "--json"]) == 0
    printed = capsys.readouterr().out
    js = json.loads(printed[printed.index("{\n"):])
    assert js["signatures"][0]["intact"]
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(out.read_bytes().replace(b"Sample letter", b"Simple letter", 1))
    assert main(["verify", str(bad), "--trust-root", str(pki["ca"])]) == 1
    assert main(["sign", str(src), "--key", str(pki["key"]), "--cert", str(pki["cert"]), "--level", "b-t"]) == 2


def test_cli_commands_registered():
    from pdfcore.ops import COMMANDS, load_all

    load_all()
    for c in ("sign", "certify", "verify", "timestamp"):
        assert c in COMMANDS


# ---------------------------------------------------------------- Phase 4 done-when
@pytest.mark.skipif(not _has_verapdf(), reason="veraPDF not installed")
def test_signed_pdfa_validates_in_verapdf(tmp_path, pki, tsa):
    """typeset PDF/A-2b letter → PAdES B-T signature → veraPDF PDF/A-2b PASS, and verify says intact."""
    pytest.importorskip("typst")
    from pdfcore.ops import standards, typeset

    made = typeset.make("letter", ROOT / "templates/examples/letter.yaml", standard="pdfa-2b",
                        out=tmp_path / "letter.pdf", quiet=True)
    assert made.ok, made.line()
    assert standards.declared_standard(made.output) == "pdfa-2b"
    signed = sg.sign(made.output, level="b-t", timestamper=tsa, rect=(330, 640, 540, 700), reason="Approved",
                     location="Springfield", quiet=True, **_ident(pki))
    assert signed.ok, signed.line()  # gate ran veraPDF too (input declares PDF/A-2b)
    vr = standards.run_verapdf(signed.output, "pdfa-2b")
    assert vr.compliant, [r.rule_id for r in vr.rules]
    v = _verify(signed.output, pki)
    s = v.data["signatures"][0]
    assert v.ok and s["intact"] and s["trusted"] and s["pades_level"] == "PAdES B-T"


@pytest.mark.skipif(not _has_verapdf(), reason="veraPDF not installed")
def test_signed_blta_pdfa_validates(tmp_path, pki, tsa):
    pytest.importorskip("typst")
    from pdfcore.ops import standards, typeset

    made = typeset.make("letter", ROOT / "templates/examples/letter.yaml", standard="pdfa-2b",
                        out=tmp_path / "letter.pdf", quiet=True)
    signed = sg.sign(made.output, level="b-lta", timestamper=tsa, trust_roots=[pki["ca"]], crls=[pki["crl"]],
                     allow_fetching=False, quiet=True, **_ident(pki))
    assert standards.run_verapdf(signed.output, "pdfa-2b").compliant
    assert _verify(signed.output, pki).data["signatures"][0]["pades_level"] == "PAdES B-LTA"


def test_sign_encrypted_input(work, pki):
    src = work("encrypted")  # corpus: AES-256, owner password "owner-test"
    with pytest.raises(ValueError, match="encrypted"):
        sg.sign(src, quiet=True, **_ident(pki))
    res = sg.sign(src, password="owner-test", rect=(300, 600, 500, 660), quiet=True, **_ident(pki))
    assert res.ok, res.line()
    v = sg.verify(res.output, trust_roots=[pki["ca"]], password="owner-test", quiet=True)
    assert v.ok and v.data["signatures"][0]["intact"]
