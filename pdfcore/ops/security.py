"""Security: encrypt, decrypt, change passwords, report security settings (pikepdf / qpdf).

Permissions (names accepted everywhere)::

    print         low-resolution printing          print-high    full-quality printing
    modify        change page content              copy          copy / extract text and images
    annotate      add or change comments           forms         fill form fields
    assemble      insert, rotate, delete pages     accessibility text extraction for screen readers

Removing security or changing passwords needs the **owner** password when the file restricts
editing or copying; the user (open) password alone is only enough when the file already allows
both. That mirrors what a conforming PDF reader permits.

``accessibility`` is deprecated in PDF 2.0 and qpdf always grants it for AES files, so denying it
has no effect there.
"""

from __future__ import annotations

import secrets
import warnings
from pathlib import Path
from typing import Any, Iterable

import pikepdf

from pdfcore.gate import GateOptions
from pdfcore.ops import command
from pdfcore.ops._common import add_common, finalize, gate_options_from, output_path
from pdfcore.result import OpResult

__all__ = ["PERMISSIONS", "METHODS", "encrypt", "decrypt", "change_passwords", "info", "permissions_from"]

# our name → pikepdf.Permissions field
PERMISSIONS: dict[str, str] = {
    "print": "print_lowres",
    "print-high": "print_highres",
    "modify": "modify_other",
    "copy": "extract",
    "annotate": "modify_annotation",
    "forms": "modify_form",
    "assemble": "modify_assembly",
    "accessibility": "accessibility",
}
_ALIASES = {"extract": "copy", "print-hi": "print-high", "printhigh": "print-high", "high-print": "print-high",
            "fill-forms": "forms", "form": "forms", "annotations": "annotate", "comment": "annotate",
            "edit": "modify", "assembly": "assemble", "print-low": "print", "all": "*", "none": "-"}
METHODS: dict[str, dict[str, Any]] = {
    "aes256": {"R": 6, "aes": True},  # AES-256, PDF 2.0 / Acrobat X+ (default)
    "aes128": {"R": 4, "aes": True},  # AES-128, Acrobat 7+
}


def _perm_key(name: str) -> str:
    k = name.strip().lower().replace("_", "-")
    k = _ALIASES.get(k, k)
    if k not in PERMISSIONS and k not in ("*", "-"):
        raise ValueError(f"unknown permission {name!r}; use {', '.join(PERMISSIONS)}")
    return k


def permissions_from(allow: Iterable[str] | None = None, deny: Iterable[str] | None = None) -> pikepdf.Permissions:
    """Build pikepdf.Permissions. *allow* None = everything allowed; then *deny* removes items.

    ``allow=[]`` (or ``["none"]``) denies everything. Allowing ``print-high`` implies ``print``;
    denying ``print`` also denies ``print-high``.
    """
    fields = {v: True for v in PERMISSIONS.values()}
    if allow is not None:
        keys = {_perm_key(a) for a in allow}
        if "*" not in keys:
            fields = {v: False for v in PERMISSIONS.values()}
            for k in keys - {"-"}:
                fields[PERMISSIONS[k]] = True
    if fields["print_highres"]:
        fields["print_lowres"] = True
    for d in deny or ():
        k = _perm_key(d)
        if k == "*":
            fields = {v: False for v in PERMISSIONS.values()}
        elif k != "-":
            fields[PERMISSIONS[k]] = False
    if not fields["print_lowres"]:
        fields["print_highres"] = False
    return pikepdf.Permissions(**fields)


def _perm_dict(p: pikepdf.Permissions) -> dict[str, bool]:
    return {k: bool(getattr(p, v)) for k, v in PERMISSIONS.items()}


def _restricted(p: pikepdf.Permissions) -> bool:
    return not all(_perm_dict(p).values())


def _method_name(pdf: pikepdf.Pdf) -> str:
    e = pdf.encryption
    m = str(e.stream_method).split(".")[-1]
    return {"aesv3": "AES-256", "aesv2": "AES-128", "aes": "AES-128", "rc4": f"RC4-{e.bits}", "none": "none"}.get(m, m)


def _open(src: Path, password: str | None) -> pikepdf.Pdf:
    try:
        return pikepdf.open(str(src), password=password or "")
    except pikepdf.PasswordError as exc:
        raise ValueError(f"{src.name}: wrong or missing password") from exc


def _require_owner(pdf: pikepdf.Pdf, action: str) -> None:
    """Refuse to lift restrictions with only the user password."""
    if not pdf.is_encrypted or pdf.owner_password_matched:
        return
    allow = pdf.allow
    if not (allow.modify_other and allow.extract):
        raise ValueError(f"the owner password is needed to {action}: this file restricts editing or copying")


# ------------------------------------------------------------------ ops
def encrypt(
    src: str | Path,
    *,
    user_password: str = "",
    owner_password: str | None = None,
    allow: Iterable[str] | None = None,
    deny: Iterable[str] | None = None,
    method: str = "aes256",
    encrypt_metadata: bool = True,
    out: str | Path | None = None,
    password: str | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Encrypt a copy. AES-256 (R6) by default.

    *user_password* opens the file ("" = opens without a password but permissions still apply).
    *owner_password* lifts restrictions; when omitted and permissions are restricted (or no user
    password is set) a random one is generated and returned in ``data["owner_password"]``.
    """
    src = Path(src)
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; use {', '.join(METHODS)}")
    perms = permissions_from(allow, deny)
    generated = False
    if not owner_password:
        if _restricted(perms) or not user_password:
            owner_password = secrets.token_urlsafe(18)
            generated = True
        else:
            owner_password = user_password
    if owner_password == user_password and _restricted(perms):
        raise ValueError("owner password must differ from the user password, or the restrictions mean nothing")
    if not user_password and not _restricted(perms):
        raise ValueError("nothing to protect: give a user password and/or restrict permissions")
    dst = output_path(src, "encrypted", out=out)
    with _open(src, password) as pdf:
        if pdf.is_encrypted:
            _require_owner(pdf, "re-encrypt it")
        enc = pikepdf.Encryption(user=user_password, owner=owner_password, allow=perms, metadata=encrypt_metadata,
                                 **METHODS[method])
        pdf.save(str(dst), encryption=enc)
    data: dict[str, Any] = {"method": "AES-256" if method == "aes256" else "AES-128", "permissions": _perm_dict(perms),
                            "user_password_set": bool(user_password)}
    if generated:
        data["owner_password"] = owner_password
    return _finalize_with_base(dst, src, options, user_password or None, password, data, quiet)


def _finalize_with_base(dst: Path, src: Path, options, out_pw: str | None, in_pw: str | None, data, quiet) -> OpResult:
    """finalize() opens baseline and output with one password; when they differ use a decrypted temp baseline."""
    import tempfile

    with warnings.catch_warnings():
        # An unencrypted baseline opens with any password, so one password serves both files.
        warnings.filterwarnings("ignore", message=".*no password was needed.*")
        if not in_pw or (out_pw or None) == in_pw:
            return finalize(dst, baseline=src, options=options, password=out_pw or None, data=data, quiet=quiet)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "baseline.pdf"
            with pikepdf.open(str(src), password=in_pw) as pdf:
                pdf.save(str(base))  # decrypted copy of the input, for comparison only
            return finalize(dst, baseline=base, options=options, password=out_pw or None, data=data, quiet=quiet)


def decrypt(
    src: str | Path,
    password: str = "",
    *,
    out: str | Path | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """Write an unencrypted copy. Needs the owner password if the file restricts editing or copying."""
    src = Path(src)
    dst = output_path(src, "decrypted", out=out)
    with _open(src, password) as pdf:
        if not pdf.is_encrypted:
            raise ValueError(f"{src.name} is not encrypted")
        _require_owner(pdf, "remove security")
        was = {"method": _method_name(pdf), "permissions": _perm_dict(pdf.allow)}
        pdf.save(str(dst), encryption=False)
    return _finalize_with_base(dst, src, options, None, password, {"was": was}, quiet)


def change_passwords(
    src: str | Path,
    password: str,
    *,
    user_password: str | None = None,
    owner_password: str | None = None,
    allow: Iterable[str] | None = None,
    deny: Iterable[str] | None = None,
    method: str | None = None,
    out: str | Path | None = None,
    options: GateOptions | None = None,
    quiet: bool = False,
) -> OpResult:
    """New user and/or owner password (``None`` = keep, ``""`` user = no open password).

    Permissions are kept unless *allow*/*deny* are given; the method is kept unless *method* is given
    (RC4 files are upgraded to AES-256). Needs the owner password for restricted files.
    """
    src = Path(src)
    dst = output_path(src, "repassword", out=out)
    with _open(src, password) as pdf:
        if not pdf.is_encrypted:
            raise ValueError(f"{src.name} is not encrypted; use encrypt")
        _require_owner(pdf, "change its passwords")
        cur_user = pdf.encryption.user_password.decode("latin-1") if pdf.user_password_matched else None
        if user_password is None:
            if cur_user is None:
                raise ValueError("opened with the owner password: give the new (or same) user password explicitly")
            user_password = cur_user
        perms = permissions_from(allow, deny) if (allow is not None or deny is not None) else pdf.allow
        if owner_password is None:
            owner_password = password if pdf.owner_password_matched else secrets.token_urlsafe(18)
        if owner_password == user_password and _restricted(perms):
            raise ValueError("owner password must differ from the user password when permissions are restricted")
        m = method or {"AES-256": "aes256", "AES-128": "aes128"}.get(_method_name(pdf), "aes256")
        if m not in METHODS:
            raise ValueError(f"unknown method {m!r}; use {', '.join(METHODS)}")
        enc = pikepdf.Encryption(user=user_password, owner=owner_password, allow=perms, **METHODS[m])
        pdf.save(str(dst), encryption=enc)
    data = {"method": "AES-256" if m == "aes256" else "AES-128", "permissions": _perm_dict(perms)}
    return _finalize_with_base(dst, src, options, user_password or None, password, data, quiet)


def info(src: str | Path, password: str | None = None) -> OpResult:
    """Report security settings. No output file; everything is in ``data``."""
    src = Path(src)
    d: dict[str, Any] = {"file": str(src)}
    try:
        pdf = pikepdf.open(str(src), password=password or "")
    except pikepdf.PasswordError:
        d.update(encrypted=True, needs_password=True, opened=False)
        return OpResult(None, None, [], d)
    with pdf:
        d["opened"] = True
        d["encrypted"] = pdf.is_encrypted
        d["needs_password"] = pdf.is_encrypted and not _opens_without_password(src)
        if pdf.is_encrypted:
            e = pdf.encryption
            d.update(
                method=_method_name(pdf), revision=e.R, version=e.V, key_bits=e.bits, P=e.P,
                user_password_matched=pdf.user_password_matched, owner_password_matched=pdf.owner_password_matched,
                permissions=_perm_dict(pdf.allow),
            )
        else:
            d["permissions"] = {k: True for k in PERMISSIONS}
    return OpResult(None, None, [], d)


def _opens_without_password(src: Path) -> bool:
    try:
        pikepdf.open(str(src)).close()
        return True
    except pikepdf.PasswordError:
        return False


def format_info(d: dict[str, Any]) -> str:
    if not d.get("opened"):
        return f"{Path(d['file']).name}: encrypted — a password is needed to open it"
    if not d["encrypted"]:
        return f"{Path(d['file']).name}: not encrypted (all permissions allowed)"
    rows = [f"{Path(d['file']).name}: encrypted, {d['method']} (R{d['revision']}, {d['key_bits']}-bit)",
            f"  open password: {'required' if d['needs_password'] else 'none'}",
            f"  opened as: {'owner' if d['owner_password_matched'] else 'user'}"]
    perms = d["permissions"]
    rows.append("  allowed: " + (", ".join(k for k, v in perms.items() if v) or "nothing"))
    rows.append("  denied:  " + (", ".join(k for k, v in perms.items() if not v) or "nothing"))
    return "\n".join(rows)


# ------------------------------------------------------------------ CLI
_PERM_HELP = "comma list of: " + ", ".join(PERMISSIONS) + " (or all / none)"


def _split(s: str | None) -> list[str] | None:
    if s is None:
        return None
    return [t for t in (x.strip() for x in s.split(",")) if t]


@command("encrypt", "Password-protect a PDF (AES-256) and set permissions", group="security")
def _cli_encrypt(p):
    p.add_argument("pdf")
    p.add_argument("--user-password", "--open-password", dest="user_password", default="",
                   help="password to open the file (default none)")
    p.add_argument("--owner-password", help="password to change security (default: generated and printed)")
    p.add_argument("--allow", help=f"only these permissions; {_PERM_HELP}")
    p.add_argument("--deny", help=f"remove these permissions; {_PERM_HELP}")
    p.add_argument("--method", default="aes256", choices=sorted(METHODS))
    p.add_argument("--plain-metadata", action="store_true", help="leave XMP metadata unencrypted (searchable)")
    add_common(p)

    def run(ns):
        res = encrypt(ns.pdf, user_password=ns.user_password, owner_password=ns.owner_password, allow=_split(ns.allow),
                      deny=_split(ns.deny), method=ns.method, encrypt_metadata=not ns.plain_metadata, out=ns.out,
                      password=ns.password, options=gate_options_from(ns))
        if "owner_password" in res.data:
            print(f"owner password (generated, keep it safe): {res.data['owner_password']}")
        return res

    return run


@command("decrypt", "Remove password protection (needs the owner password for restricted files)", group="security")
def _cli_decrypt(p):
    add_common(p)
    p.add_argument("pdf")
    return lambda ns: decrypt(ns.pdf, ns.password or "", out=ns.out, options=gate_options_from(ns))


@command("passwords", "Change the open and/or owner password of an encrypted PDF", group="security")
def _cli_pw(p):
    p.add_argument("pdf")
    p.add_argument("--new-user-password", help="new open password ('' for none; default keep)")
    p.add_argument("--new-owner-password", help="new owner password (default keep)")
    p.add_argument("--allow", help=f"replace permissions; {_PERM_HELP}")
    p.add_argument("--deny", help=f"remove permissions; {_PERM_HELP}")
    p.add_argument("--method", choices=sorted(METHODS))
    add_common(p)
    return lambda ns: change_passwords(ns.pdf, ns.password or "", user_password=ns.new_user_password,
                                       owner_password=ns.new_owner_password, allow=_split(ns.allow),
                                       deny=_split(ns.deny), method=ns.method, out=ns.out, options=gate_options_from(ns))


@command("security", "Show encryption method and permissions", group="security")
def _cli_info(p):
    p.add_argument("pdf")
    p.add_argument("--password")
    p.add_argument("--json", action="store_true")

    def run(ns):
        res = info(ns.pdf, ns.password)
        if ns.json:
            import json

            print(json.dumps(res.data, indent=2))
        else:
            print(format_info(res.data))
        return res

    return run
