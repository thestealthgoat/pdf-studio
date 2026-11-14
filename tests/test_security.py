from __future__ import annotations

import pikepdf
import pytest

from pdfcore.ops import security as sec


def test_info_corpus_encrypted(work):
    src = work("encrypted")
    locked = sec.info(src)
    assert locked.data["encrypted"] and locked.data["needs_password"] and not locked.data["opened"]
    d = sec.info(src, "test").data
    assert d["encrypted"] and d["method"] == "AES-256" and d["revision"] == 6 and d["key_bits"] == 256
    assert d["needs_password"] and d["user_password_matched"] and not d["owner_password_matched"]
    assert d["permissions"]["assemble"] is False and d["permissions"]["print"] is True
    assert "AES-256" in sec.format_info(d)
    plain = sec.info(work("letter")).data
    assert plain["encrypted"] is False and all(plain["permissions"].values())


def test_decrypt_corpus_with_user_password(work):
    src = work("encrypted")
    res = sec.decrypt(src, "test", quiet=True)
    assert res.ok, res.gate.details()
    assert res.output != src
    with pikepdf.open(res.output) as pdf:  # opens with no password
        assert not pdf.is_encrypted and len(pdf.pages) == 3
    with pytest.raises(ValueError, match="password"):
        sec.decrypt(src, "wrong", quiet=True)


def test_encrypt_aes256_permissions_roundtrip(work):
    src = work("letter")
    res = sec.encrypt(src, user_password="open", owner_password="boss", deny=["copy", "modify", "print-high"], quiet=True)
    assert res.ok, res.gate.details()
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(res.output)
    with pikepdf.open(res.output, password="open") as pdf:
        e = pdf.encryption
        assert e.R == 6 and e.bits == 256 and str(e.stream_method).endswith("aesv3")
        a = pdf.allow
        assert not a.extract and not a.modify_other and not a.print_highres
        assert a.print_lowres and a.modify_annotation and a.modify_form and a.accessibility
    with pikepdf.open(res.output, password="boss") as pdf:
        assert pdf.owner_password_matched
    # restricted file: the user password alone cannot strip security
    with pytest.raises(ValueError, match="owner password"):
        sec.decrypt(res.output, "open", quiet=True)
    ok = sec.decrypt(res.output, "boss", quiet=True)
    assert ok.ok and not sec.info(ok.output).data["encrypted"]


def test_encrypt_allow_list_and_generated_owner(work):
    src = work("form_fillable")
    res = sec.encrypt(src, allow=["print", "forms"], method="aes128", quiet=True)  # no open password
    assert res.ok, res.gate.details()
    owner = res.data["owner_password"]
    assert owner and res.data["method"] == "AES-128"
    d = sec.info(res.output).data
    assert d["encrypted"] and not d["needs_password"] and d["method"] == "AES-128"
    perms = dict(d["permissions"])
    perms.pop("accessibility")  # PDF 2.0 deprecates this bit; qpdf always grants it
    assert perms == {"print": True, "print-high": False, "modify": False, "copy": False,
                     "annotate": False, "forms": True, "assemble": False}
    with pikepdf.open(res.output, password=owner) as pdf:
        assert pdf.owner_password_matched


def test_permissions_from():
    p = sec.permissions_from(allow=["print-high"])
    assert p.print_highres and p.print_lowres and not p.extract
    p = sec.permissions_from(deny=["print"])
    assert not p.print_lowres and not p.print_highres and p.extract
    p = sec.permissions_from(allow=["none"])
    assert not any([p.print_lowres, p.extract, p.modify_other, p.accessibility])
    with pytest.raises(ValueError):
        sec.permissions_from(allow=["teleport"])


def test_encrypt_refuses_meaningless(work):
    src = work("letter")
    with pytest.raises(ValueError):
        sec.encrypt(src, quiet=True)
    with pytest.raises(ValueError):
        sec.encrypt(src, user_password="x", owner_password="x", deny=["copy"], quiet=True)


def test_change_passwords(work):
    src = work("encrypted")
    res = sec.change_passwords(src, "owner-test", user_password="new-user", owner_password="new-owner", quiet=True)
    assert res.ok, res.gate.details()
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(res.output, password="test")
    with pikepdf.open(res.output, password="new-user") as pdf:
        assert pdf.user_password_matched and not pdf.allow.modify_assembly  # permissions kept
        assert pdf.encryption.R == 6
    with pikepdf.open(res.output, password="new-owner") as pdf:
        assert pdf.owner_password_matched
    # keep the user password, change only the owner password, using the user password (file allows edits)
    res2 = sec.change_passwords(src, "test", owner_password="o3", quiet=True)
    with pikepdf.open(res2.output, password="test") as pdf:
        assert pdf.user_password_matched
    with pikepdf.open(res2.output, password="o3") as pdf:
        assert pdf.owner_password_matched


def test_cli(work, capsys):
    from pdfcore.cli import main

    src = work("letter")
    assert main(["encrypt", str(src), "--user-password", "pw", "--deny", "copy"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "owner password (generated" in out
    enc = src.with_name("letter.encrypted.pdf")
    assert main(["security", str(enc), "--password", "pw"]) == 0
    out = capsys.readouterr().out
    assert "AES-256" in out and "denied:  copy" in out
    assert main(["decrypt", str(work("encrypted")), "--password", "test"]) == 0
    assert "PASS" in capsys.readouterr().out
