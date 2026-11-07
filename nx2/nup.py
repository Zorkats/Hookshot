"""Read a .nup package (a decrypted Switch 2 dump: titles, each with an exefs/ and a romfs/ tree) and extract it.

    python -I nx2/nup.py <file.nup>                      list the titles and their trees
    python -I nx2/nup.py <file.nup> --extract <dir> [--title <n|version>] [--only exefs|romfs]

The layout (worked out from the file; little-endian):
    0x000  "NUP\\x01" header, u64 file size at +0x40
    0x080  "TOC\\x01", u32 titles, u64 dirs, u64 files, u64 strings offset (from 0x80), u64 strings size
    0x0b0  titles, 0x40 each: u64 title id, u32 root dir, u32 exefs dir, u32 romfs dir, u32 version, ...,
           u64 offset + u64 size of the title's metadata blob at +0x20
    then   dirs, 24 bytes each: u32 name offset, u32 name length, u32 first child, u32 children, u64 0
    then   files, 80 bytes each: u32 name offset, u32 name length, u64 size, u64 data offset, u64 size again,
           32-byte hash, 16 bytes 0
    then   the child list, u32 each: a node index; below the dir count a dir, else (index - dirs) a file
    then   the name strings (not 0-terminated)
The data of a file is stored as is at its data offset, or, in .nup files from newer dumpers, as one zstd frame whose content is the
file. The record says which: its first size is the file's, the second the bytes stored. They differ only for a packed file (on one
package, exactly its 40,588 packed files, each second size reaching the next file's data). The frame header alone cannot tell: three
of the game's own .bcul.zs files state a content size equal to their file size.
"""
import argparse
import os
import shutil
import struct
import sys


ZSTD = b"\x28\xb5\x2f\xfd"


def zstd_decompressor():
    """A streaming zstd decompressor and its error type: the zstandard package, or Python 3.14's own compression.zstd"""
    try:
        import zstandard
        return zstandard.ZstdDecompressor().decompressobj(), zstandard.ZstdError
    except ImportError:
        pass
    try:
        from compression import zstd
        return zstd.ZstdDecompressor(), zstd.ZstdError
    except ImportError:
        raise SystemExit("this .nup keeps files compressed: install the zstandard package (python -m pip install zstandard), or use "
                         "Python 3.14 or newer")


def frame_content_size(head):
    """The content size a zstd frame header states, None when it states none"""
    if head[:4] != ZSTD or len(head) < 18:
        return None
    fhd = head[4]
    single = (fhd >> 5) & 1
    at = 5 + (0 if single else 1) + (0, 1, 2, 4)[fhd & 3]
    n = (1 if single else 0, 2, 4, 8)[fhd >> 6]
    if not n:
        return None
    size = int.from_bytes(head[at:at + n], "little")
    return size + 256 if n == 2 else size


class Nup:
    def __init__(self, path):
        self.path = path
        self.f = open(path, "rb")
        h = self.f.read(0x80)
        if h[:4] != b"NUP\x01":
            raise SystemExit("%s: not a NUP package (magic %r)" % (path, h[:4]))
        self.file_size = struct.unpack_from("<Q", h, 0x40)[0]
        t = self.f.read(0x30)
        if t[:4] != b"TOC\x01":
            raise SystemExit("%s: no TOC at 0x80" % path)
        nt, nd, nf, soff, ssz = struct.unpack_from("<IQQQQ", t, 4)
        self.nd, self.nf = nd, nf
        dirs_at = 0x80 + 0x30 + 0x40 * nt
        files_at = dirs_at + 24 * nd
        kids_at = files_at + 80 * nf
        strings_at = 0x80 + soff
        self.f.seek(0)
        meta = self.f.read(strings_at + ssz)
        self.strings = meta[strings_at:strings_at + ssz]
        self.titles = []
        for i in range(nt):
            o = 0xb0 + 0x40 * i
            tid, root, exefs, romfs, ver = struct.unpack_from("<QIIII", meta, o)
            self.titles.append(dict(index=i, id=tid, root=root, exefs=exefs, romfs=romfs, version=ver))
        self.dirs = [struct.unpack_from("<4I", meta, dirs_at + 24 * i) for i in range(nd)]
        self.files = []
        for i in range(nf):
            no, nl, size, off, size2 = struct.unpack_from("<IIQQQ", meta, files_at + 80 * i)
            self.files.append((no, nl, size, off, size2))
        nk = (strings_at - kids_at) // 4
        self.kids = struct.unpack_from("<%dI" % nk, meta, kids_at)

    def name(self, off, n):
        return self.strings[off:off + n].decode("utf-8")

    def walk(self, d, prefix=""):
        """(relative path, file record) for every file under dir d."""
        no, nl, first, n = self.dirs[d]
        for k in self.kids[first:first + n]:
            if k < self.nd:
                cno, cnl = self.dirs[k][:2]
                yield from self.walk(k, prefix + self.name(cno, cnl) + "/")
            else:
                r = self.files[k - self.nd]
                yield prefix + self.name(r[0], r[1]), r

    def is_packed(self, r, name=""):
        """A file the package keeps compressed: its stored size is not its size. Anything else stored that way is a layout this
        reader does not know, said instead of guessed"""
        if r[4] == r[2]:
            return False
        self.f.seek(r[3])
        head = self.f.read(18)
        size = frame_content_size(head)
        if head[:4] != ZSTD or size not in (None, r[2]):
            raise SystemExit("%s: stored as %d bytes for %d, but not as a zstd frame of the file (it starts %s)" % (
                name or "a file", r[4], r[2], head[:8].hex()))
        return True

    def copy_to(self, r, g, name=""):
        """The file, as itself, into the open file g"""
        packed = self.is_packed(r, name)
        self.f.seek(r[3])
        if packed:
            d, error = zstd_decompressor()
            left = r[4]
            got = 0
            try:
                while left and not d.eof:
                    b = self.f.read(min(left, 8 << 20))
                    if not b:
                        raise SystemExit("short read in the package")
                    left -= len(b)
                    out = d.decompress(b)
                    g.write(out)
                    got += len(out)
            except error as e:
                raise SystemExit("%s: its compressed data is damaged (%s)" % (name or "a file", e))
            if got != r[2] or not d.eof:
                raise SystemExit("%s: came out as %d bytes, %d expected" % (name or "a file", got, r[2]))
            return
        left = r[2]
        while left:
            b = self.f.read(min(left, 8 << 20))
            if not b:
                raise SystemExit("short read in the package")
            g.write(b)
            left -= len(b)

    def read(self, r, n=None):
        import io
        b = io.BytesIO()
        self.copy_to(r, b)
        return b.getvalue() if n is None else b.getvalue()[:n]


def pick_title(nup, want):
    if want is None:
        return max(nup.titles, key=lambda t: t["version"])   # (the newest: the update over the base)
    for t in nup.titles:
        if str(t["index"]) == want or str(t["version"]) == want or "%016x" % t["id"] == want.lower():
            return t
    raise SystemExit("no title %s" % want)


def extract(nup, t, out, only=None, quiet=False):
    total = n = 0
    for part in ("exefs", "romfs"):
        if only and part != only:
            continue
        for rel, r in nup.walk(t[part]):
            dst = os.path.join(out, part, *rel.split("/"))
            if os.path.exists(dst) and os.path.getsize(dst) == r[2]:
                continue          # (re-runs pick up where they stopped)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst + ".part", "wb") as g:
                nup.copy_to(r, g, rel)
            os.replace(dst + ".part", dst)
            total += r[2]
            n += 1
            if not quiet and n % 5000 == 0:
                print("  %d files, %.1f GB" % (n, total / 1e9), flush=True)
    return n, total


def heal(nup, t, exefs, names):
    """An update's exefs need not hold every module whole: what is not an NSO there is taken from the package's other titles"""
    for m in names:
        p = os.path.join(exefs, m)
        have = b""
        if os.path.exists(p):
            with open(p, "rb") as f:
                have = f.read(16)
        if have[:4] == b"NSO0":
            continue
        for other in reversed(nup.titles):
            if other["index"] == t["index"]:
                continue
            for rel, r in nup.walk(other["exefs"]):
                if rel == m and nup.read(r, 4) == b"NSO0":
                    with open(p, "wb") as g:
                        g.write(nup.read(r))
                    print("  %s: taken from title %d (v%d)" % (m, other["index"], other["version"]))
                    break
            else:
                continue
            break
        else:
            if have:
                print("  %s is not a game module in any title: it starts %s" % (m, have.hex()))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("nup")
    ap.add_argument("--extract", metavar="DIR")
    ap.add_argument("--title", help="index, version or title id (default: the newest)")
    ap.add_argument("--only", choices=("exefs", "romfs"))
    a = ap.parse_args()
    nup = Nup(a.nup)
    print("%s: %.2f GB, %d titles, %d dirs, %d files" % (os.path.basename(a.nup), nup.file_size / 1e9,
                                                          len(nup.titles), nup.nd, nup.nf))
    for t in nup.titles:
        ex = list(nup.walk(t["exefs"]))
        ro = list(nup.walk(t["romfs"]))
        print("title %d: %016x v%d  exefs %d files (%s)  romfs %d files, %.2f GB" % (
            t["index"], t["id"], t["version"], len(ex), ", ".join("%s %d" % (p, r[2]) for p, r in ex),
            len(ro), sum(r[2] for _, r in ro) / 1e9))
        packed = sum(1 for _, r in ex + ro if r[2] != r[4])
        if packed:
            print("  (%d files kept compressed in the package)" % packed)
    if a.extract:
        t = pick_title(nup, a.title)
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(a.extract)) or ".").free
        print("extracting title %d (v%d) to %s (%.0f GB free)" % (t["index"], t["version"], a.extract, free / 1e9))
        n, total = extract(nup, t, a.extract, a.only)
        print("done: %d files written, %.2f GB" % (n, total / 1e9))
        if a.only != "romfs":   # (main is ZBIC-compressed on firmware 22+: made a plain NSO for the tools)
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import unpack
            heal(nup, t, os.path.join(a.extract, "exefs"), unpack.NAMES)
            for m in unpack.NAMES:
                p = os.path.join(a.extract, "exefs", m)
                if os.path.exists(p) and unpack.unpack_nso(p):
                    print("  %s: ZBIC -> plain NSO" % m)


if __name__ == "__main__":
    sys.exit(main())
