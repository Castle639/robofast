"""Make KiwiSpec's raw spectrum a legal FITS file, in place, safely.

The spectrograph's camera software (SI Image) writes a file that is not
legal FITS: the primary header says ``SIMPLE = F``, and the pixels follow
the header as 16-bit unsigned big-endian integers that standard readers
will not take. Legacy MINERVA rewrote each raw file on the Linux side
(``fix_fits.py``, ``writeto(clobber=True)``: no check, and a crash in the
middle loses the frame). robofast keeps **one** copy at rest, the legal one,
and gets there without ever risking the raw data (:func:`standardize`):

1. read the raw file and decode its pixels;
2. write the legal file **next to it** under a temporary name (same
   directory, so the final rename is atomic), with FITS ``CHECKSUM`` and
   ``DATASUM``, and fsync it;
3. **verify** the temporary file: it opens as valid FITS, its checksums
   hold, and its pixel array is identical (shape, values, SHA-256) to the
   raw file's;
4. check that the raw file has not changed while this ran (size, mtime,
   SHA-256);
5. ``os.replace`` the raw file with the verified one and fsync the
   directory.

On any failure the temporary file is removed and the raw file is left
exactly as it was; :class:`FitsRewriteError` says why (the KiwiSpec service
raises an alarm). The pixel SHA-256 and FITS ``DATASUM`` are returned so the
spectrum's sidecar records them (docs/kiwispec.md, "The raw file").

What makes the file illegal is taken from legacy ``fix_fits.py`` (which
reads the file with ``ignore_missing_end`` and rebuilds 16-bit pixels from
byte pairs, high byte first); the simulator writes the same form. It has
not been checked against a raw file from the current camera software.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import os
import warnings
from pathlib import Path
from typing import Any

import numpy as np

BLOCK = 2880
CARD = 80
#: Structural keywords the new primary header makes itself.
STRUCTURAL = frozenset(
    {"SIMPLE", "BITPIX", "NAXIS", "NAXIS1", "NAXIS2", "EXTEND", "BZERO", "BSCALE"}
)
#: SI Image's own comment on DATE-OBS, while it is still the PC's local time.
SI_DATE_COMMENT = "DATE-OBS Format is YYYY-MM-DDThh:mm:ss.sss"


class FitsRewriteError(Exception):
    """The raw file was not rewritten (it is untouched)."""


def _header_end(raw: bytes) -> int:
    """Offset of the first byte after the header (its END block)."""
    for i in range(0, len(raw) - CARD + 1, CARD):
        if raw[i : i + 8] == b"END     ":
            return (i // BLOCK + 1) * BLOCK
    raise FitsRewriteError("no END card in the header")


def pixel_sha256(data: np.ndarray) -> str:
    """SHA-256 of a frame's pixels in a fixed form (big-endian uint16, row major)."""
    return hashlib.sha256(np.ascontiguousarray(data, dtype=">u2").tobytes()).hexdigest()


def read_raw(raw: bytes) -> tuple[Any, np.ndarray, bool]:
    """``(header, pixels, legal)`` of an SI raw file's bytes."""
    from astropy.io import fits

    end = _header_end(raw)
    try:
        header = fits.Header.fromstring(raw[:end].decode("ascii", "replace"))
    except Exception as exc:
        raise FitsRewriteError(f"unreadable header: {exc}") from exc
    legal = header.get("SIMPLE") is True
    try:
        nx, ny = int(header["NAXIS1"]), int(header["NAXIS2"])
    except (KeyError, ValueError, TypeError) as exc:
        raise FitsRewriteError(f"no image size in the header: {exc}") from exc
    if nx <= 0 or ny <= 0:
        raise FitsRewriteError(f"image size {nx} x {ny}")
    need = nx * ny * 2
    body = raw[end:]
    if len(body) < need:
        raise FitsRewriteError(f"truncated: {len(body)} bytes of pixels, expected {need}")
    if legal:
        bitpix = header.get("BITPIX")
        if bitpix != 16:
            raise FitsRewriteError(f"legal FITS with BITPIX {bitpix}, expected 16")
        signed = np.frombuffer(body[:need], dtype=">i2").reshape(ny, nx)
        zero = header.get("BZERO", 0)
        pixels = (signed.astype(np.int32) + int(zero)).astype(np.uint16)
    else:
        pixels = np.frombuffer(body[:need], dtype=">u2").reshape(ny, nx).astype(np.uint16)
    return header, pixels, legal


def _utc_date_obs(header: Any, utc_offset_hours: float) -> None:
    """DATE-OBS from the PC's local time to UTC, while it still carries SI
    Image's own comment (the same test as legacy ``fix_fits``)."""
    if "DATE-OBS" not in header or header.comments["DATE-OBS"] != SI_DATE_COMMENT:
        return
    fmt = "%Y-%m-%dT%H:%M:%S.%f"
    local = datetime.datetime.strptime(str(header["DATE-OBS"]), fmt)
    utc = local - datetime.timedelta(hours=utc_offset_hours)
    header["DATE-OBS"] = (utc.strftime(fmt)[:-3], "UTC at exposure start")


def _fsync_dir(path: Path) -> None:
    with contextlib.suppress(OSError):  # not every file system allows it
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _stamp(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


def standardize(path: str | os.PathLike[str], *, utc_offset_hours: float = -7.0) -> dict[str, Any]:
    """Rewrite the raw file at ``path`` as legal FITS (module doc). Returns
    what was done: ``rewritten`` (False if it was legal already),
    ``pixel_sha256``, ``datasum``, ``raw_sha256`` (the file as the camera
    wrote it), ``shape``. Raises :class:`FitsRewriteError` (the raw file
    untouched)."""
    from astropy.io import fits
    from astropy.utils.exceptions import AstropyUserWarning

    path = Path(path)
    try:
        stamp = _stamp(path)
        raw = path.read_bytes()
    except OSError as exc:
        raise FitsRewriteError(f"cannot read {path.name}: {exc}") from exc
    raw_sha = hashlib.sha256(raw).hexdigest()
    header, pixels, legal = read_raw(raw)
    digest = pixel_sha256(pixels)
    out: dict[str, Any] = {
        "file": path.name,
        "raw_sha256": raw_sha,
        "pixel_sha256": digest,
        "shape": list(pixels.shape),
    }
    if legal:
        return {**out, "rewritten": False, "legal": True}
    if "PARAM0" in header:
        # SI Image's untranslated header: the spectrograph server never
        # finished the frame (legacy fix_fits skips these too).
        raise FitsRewriteError("the header was never written (SI PARAM keywords still present)")

    hdu = fits.PrimaryHDU(pixels)
    for card in header.cards:
        if card.keyword in STRUCTURAL or card.keyword == "":
            continue
        hdu.header.append(card, end=True)
    _utc_date_obs(hdu.header, utc_offset_hours)
    hdu.header["RAWSHA"] = (digest[:64], "SHA-256 of the pixels (big-endian uint16)")

    tmp = path.with_name(f".{path.name}.rewrite.tmp")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", fits.verify.VerifyWarning)
            with open(tmp, "wb") as f:
                hdu.writeto(f, output_verify="fix", checksum=True)
                f.flush()
                os.fsync(f.fileno())
        with warnings.catch_warnings():
            # A checksum mismatch is only a warning in astropy: make it fatal.
            warnings.simplefilter("error", AstropyUserWarning)
            with fits.open(tmp, checksum=True, memmap=False) as hdul:
                hdul.verify("exception")
                if len(hdul) != 1:
                    raise FitsRewriteError(f"rewritten file has {len(hdul)} HDUs")
                back = hdul[0].data
                datasum = hdul[0].header.get("DATASUM")
                if back is None or back.shape != pixels.shape:
                    raise FitsRewriteError("rewritten pixels have the wrong shape")
                if back.dtype.kind != "u" or not np.array_equal(back, pixels):
                    raise FitsRewriteError("rewritten pixels differ from the raw pixels")
                if pixel_sha256(back) != digest:
                    raise FitsRewriteError("rewritten pixels' SHA-256 differs")
        # The camera software must not still be writing the raw file.
        if _stamp(path) != stamp or hashlib.sha256(path.read_bytes()).hexdigest() != raw_sha:
            raise FitsRewriteError("the raw file changed while it was being rewritten")
        os.replace(tmp, path)
    except FitsRewriteError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    except Exception as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise FitsRewriteError(f"{type(exc).__name__}: {exc}") from exc
    _fsync_dir(path.parent)
    return {**out, "rewritten": True, "datasum": datasum}


def write_si_raw(
    path: str | os.PathLike[str],
    pixels: np.ndarray,
    cards: dict[str, Any] | None = None,
) -> None:
    """Write ``pixels`` (uint16) the way SI Image does (module doc): for the
    simulator and the tests."""
    from astropy.io import fits

    ny, nx = pixels.shape
    h = fits.Header()
    h["SIMPLE"] = (False, "SI Image raw file")
    h["BITPIX"] = 16
    h["NAXIS"] = 2
    h["NAXIS1"] = nx
    h["NAXIS2"] = ny
    for k, v in (cards or {}).items():
        h[k] = v
    text = h.tostring(sep="", endcard=True, padding=True).encode("ascii")
    body = np.ascontiguousarray(pixels, dtype=">u2").tobytes()
    pad = (-len(body)) % BLOCK
    Path(path).write_bytes(text + body + b"\0" * pad)
