"""Scrub: the middle level, "scrubbed". A model that sees a scope scrubbed gets the same text with
every identifier (names, emails, phones, addresses, account numbers, keys) swapped for a stable
placeholder such as [PERSON_3f9a]: the same person is the same placeholder in every file and message,
so the data still joins up. The vault (identifier -> placeholder) is learned from the private files
before any of them crosses, lives only on this host, and turns placeholders back into the real
values, each only for a model that sees its scope raw.
"""
import csv
import hashlib
import hmac
import io
import base64
import email.header
import html
import json
import quopri
import re
import urllib.parse
import secrets
import unicodedata
from pathlib import Path


def _luhn(s):
    d = [int(c) for c in s if c.isdigit()][::-1]
    return sum(x if i % 2 == 0 else x * 2 - 9 * (x > 4) for i, x in enumerate(d)) % 10 == 0


def _iban(s):
    s = s.replace(" ", "")
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def _token(s):                                                 # random-looking: mixed case and digits, or hex
    return bool(re.fullmatch(r"[0-9a-f]{32,}", s)) or all(re.search(p, s) for p in ("[a-z]", "[A-Z]", "[0-9]"))


# kind: (pattern, check). Patterns are linear (no nested repeats); a check rejects look-alikes.
PATTERNS = {
    "KEY": (r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|\beyJ[\w-]+\.eyJ[\w-]+\.[\w-]*"
            r"|\b(?:[sr]k_(?:live|test)_|sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|xox[abposr]-|AIza|hf_|glpat-)[\w-]{16,}"
            r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", None),
    "SECRET": (r"(?i:passw(?:or)?d|pwd|secret|token|api[_-]?key|access[_-]?key)\s*[=:]\s*[\"']?([^\s\"']{6,})", None),
    "TOKEN": (r"(?<![\w+/=-])[A-Za-z0-9+/_-]{24,}={0,2}", _token),
    "EMAIL": (r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+", None),
    "SSN": (r"\b\d{3}-\d{2}-\d{4}\b", None),
    "CARD": (r"\b(?:\d{4}(?:[ -]?\d{4}){2}[ -]?\d{1,7}|\d{4}[ -]?\d{6}[ -]?\d{4,5})\b", _luhn),
    "IBAN": (r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b", _iban),
    "PHONE": (r"(?:\+\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b|\+\d{1,3}(?:[ -]?\(0?\d{0,4}\)\d{0,4}|[ -]\d{1,8}"
              r"|\.\d{2,5}){2,6}\b|\+\d{8,15}\b|\b0\d{1,4}[ -]?\d{3,4}[ -]?\d{2,4}\b",
              lambda s: 8 <= sum(c.isdigit() for c in s) <= 15),
    "IP": (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", lambda s: all(int(x) < 256 for x in s.split("."))),
    "ADDRESS": (r"\b\d{1,5}[A-Za-z]?,? (?:[A-Z][\w'’]+ ){1,3}(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Mews|Drive|Dr|Way"
                r"|Court|Ct|Place|Pl|Boulevard|Blvd|Terrace|Close|Crescent|Row|Square|Gardens|Hill|Park|Loop|Highway"
                r"|Hwy|Parkway|Pkwy|Circle|Trail|Walk|Grove|Green|Rise|Parade|Quay|Wharf)\b"
                r"|\b\d{1,4},? (?:rue|avenue|boulevard|place|chemin|quai|via|viale|piazza|calle|avenida|plaza)"
                r"(?: (?:de|du|des|la|le|del|di)| [A-ZÀ-Ý][\w'’-]+){1,4}"
                r"|\b[A-ZÄÖÜ][a-zäöüß]+(?:straße|strasse|weg|gasse|platz|allee|ring) \d{1,4}[a-z]?\b", None),
}
COLUMNS = [("EMAIL", r"e ?mail"), ("PHONE", r"phone|mobile|\btel\b|telefon|cell"),
           ("ADDRESS", r"address|street|post ?code|zip|city|town"),
           ("SSN", r"ssn|social|\bni\b|national insurance|tax ?id|\btin\b"), ("DOB", r"birth|\bdob\b"),
           ("ID", r"\bid\b|passport|account|iban|card|sort ?code|(?:employee|staff) (?:no|number)"),
           ("PERSON", r"^(?:full |first |last |given |middle |preferred |legal |sur|fore|employee |contact |manager "
                      r"|patient |client |customer |member )?name$|^(?:manager|employee|patient|client|customer|contact"
                      r"|owner|author|recipient|sender|person|holder|user)$")]
# first names that are also everyday words: kept out of folded and short-name matching and of email-local ids
COMMON = set("will mark may june april august grace hope faith joy rose pat bill rob sue art guy ray dawn summer frank "
             "long young little black white brown green king bell hill wood page lane gray grey rich chase hunter "
             "major price love case cash ward stone field park banks miles holly ivy jack sky".split())
MONTHS = "january february march april may june july august september october november december".split()
TOKEN = re.compile(r"\[([A-Z]+)_([0-9a-f]+)\]")
SEP = r"\s{0,2}[().\-/_,]{0,2}\s{0,2}"                                         # between the digits of a written number
CF = dict.fromkeys(c for c in range(0x10000) if unicodedata.category(chr(c)) == "Cf")
MARKS = {**dict.fromkeys(map(ord, "\u2018\u2019\u02bc\uff07\u0060\u00b4"), "'"),        # O’Brien is O'Brien
         **dict.fromkeys(range(0x2010, 0x2016), "-")}


def norm(s):
    """NFKC, invisible format characters removed, a hyphen at a line break joined, zq(at)corp[dot]com as written."""
    s = unicodedata.normalize("NFKC", s).translate(CF).translate(MARKS)
    s = re.sub(r"-[ \t]*(?:\r\n|[\r\n\x0b\x0c\x85\u2028\u2029])\s*", "", s)   # Quell-\r\nwater too
    s = re.sub(r"(?i)(?<=\w)\s*[\[({<]\s*at\s*[\])}>]\s*(?=\w)", "@", s)
    return re.sub(r"(?i)(?<=\w)\s*[\[({<]\s*dot\s*[\])}>]\s*(?=\w)", ".", s)


def text_of(data):
    """Bytes as text: UTF-8, or UTF-16/32 by their mark or their zero bytes. None when it is not text
    (a binary file cannot be scrubbed, so it never goes out scrubbed)."""
    if isinstance(data, str):
        return data
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            break
    else:
        enc = "utf-16-le" if data[1:200:2].count(0) > 40 else "utf-16-be" if data[:200:2].count(0) > 40 else "utf-8"
    try:
        text = data.decode(enc)
    except UnicodeDecodeError:
        return None
    return None if re.search(r"[\x00-\x08\x0e-\x1f]", text) else text


def flat(s):
    return re.sub(r"\s+", " ", s.strip())


MARK = "\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"   # combining marks NFKC leaves loose (Yoruba tones)
WORD = r"[^\W\d_][^\W\d_" + MARK + r"]*(?:'[^\W\d_" + MARK + r"]+)*"   # a word, its marks and O'
PLAIN = str.maketrans("łøđıðħŧ", "lodidht")                   # Lukasz for Łukasz: letters NFKD leaves whole


def fold(s):
    """Per character: accents dropped, lower case. Same length as s, so offsets carry over."""
    return "".join((("".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)) or ch)
                    .lower() + "_")[0].translate(PLAIN) if ch.isalpha() else ch for ch in s)


def key(s):
    """A whole id as looked up: folded, marks and separators dropped (EMİNE DEMİR, emine_demir: one key)."""
    return re.sub(r"[\s_.\-]+", "", "".join(c for c in fold(s) if not unicodedata.combining(c)))


def spellings(p):
    """How a name part is written without its accents: folded, marks dropped, and Soeren for Søren."""
    f = "".join(c for c in fold(p) if not unicodedata.combining(c))
    wide = p.lower().translate(str.maketrans({"ø": "oe", "æ": "ae", "œ": "oe", "ß": "ss", "þ": "th", "ä": "ae", "ö": "oe",
                                              "ü": "ue", "å": "aa"}))
    return {f, "".join(c for c in fold(wide) if not unicodedata.combining(c))}


class Unscrubbable(ValueError):
    """Known identifiers remain once the text is decoded (entities, %XX, \\uXXXX, quoted-printable, MIME
    header words, RTF \\'xx and \\uN, \\x escapes, short base64)."""


def decoded(text):
    """The text with the common machine encodings undone, as a reader would undo them."""
    t = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
    t = html.unescape(t)
    if re.search(r"%[0-9A-Fa-f]{2}", t):
        t = urllib.parse.unquote(t)
    if re.search(r"=[0-9A-F]{2}|=\r?\n", t):
        t = quopri.decodestring(t.encode("utf-8", "replace")).decode("utf-8", "replace")
    t = re.sub(r"=\?[^?\s]+\?[BbQq]\?[^?\s]*\?=", lambda m: "".join(                # =?UTF-8?B?...?=
        p.decode(c or "utf-8", "replace") if isinstance(p, bytes) else p for p, c in email.header.decode_header(m.group(0))), t)
    t = re.sub(r"(?:\\'[0-9a-fA-F]{2})+", lambda m: bytes.fromhex(m.group(0).replace("\\'", "")).decode("cp1252", "replace"), t)
    t = re.sub(r"\\u(-?\d{1,5})\??", lambda m: chr(int(m.group(1)) % 65536), t)      # RTF \u233?
    t = re.sub(r"(?:\\x[0-9a-fA-F]{2})+", lambda m: bytes.fromhex(m.group(0).replace("\\x", "")).decode("utf-8", "replace"), t)

    def b64(m):                                                # short base64 that is text
        try:
            s = base64.b64decode(m.group(0) + "=" * (-len(m.group(0)) % 4), validate=True).decode("utf-8")
        except ValueError:
            return m.group(0)
        return s if s.isprintable() and re.search(r"[^\W\d_]{2}", s) else m.group(0)
    t = re.sub(r"(?<![\w+/])[A-Za-z0-9+/]{8,}={0,2}(?![\w+/=])", b64, t)
    return norm(t)


class Vault:
    """One table per scope: a scope's placeholders and matchers come from its own files alone, so the view
    of one scope says nothing about another. A placeholder is a keyed hash of scope, kind and value."""
    def __init__(self, path):
        self.path = Path(path)
        self.v = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.v.get("version") != 2:                          # an older vault: relearn
            self.v = {"version": 2}
        for k in ("real", "scope", "files", "by"):
            self.v.setdefault(k, {})
        self.v.setdefault("key", secrets.token_hex(16))
        self.dirty, self._re = False, {}

    def save(self):
        self.path.write_text(json.dumps(self.v))
        self._re, self.dirty = {}, False

    def bind(self, ids):
        """Each scope name with what it is (its directories). A name that now means other data starts
        an empty table, and its old placeholders no longer turn back into anything."""
        of = self.v.setdefault("of", {})
        for s, i in ids.items():
            if of.get(s) != i:
                self.v["by"].pop(s, None)
                for t in [t for t, x in self.v["scope"].items() if x == s]:
                    del self.v["scope"][t], self.v["real"][t]
                self.v["files"], of[s] = {}, i
                self.dirty = True
        if self.dirty:
            self.save()

    def _t(self, scope):
        return self.v["by"].setdefault(scope, {"ids": {}, "parts": {}, "digits": {}})

    def _new(self, real, kind, scope):
        mac = hmac.new(bytes.fromhex(self.v["key"]), f"{self.v.get('of', {}).get(scope, scope)}|{kind}|{real}".encode(),
                       "sha256").hexdigest()
        token = next(t for n in range(10, 65) for t in [f"[{kind}_{mac[:n]}]"]   # fixed length: a clash (2^-40) would
                     if self.v["real"].get(t, real) == real and self.v["scope"].get(t, scope) == scope)  # show, so none
        self.v["real"][token], self.v["scope"][token] = real, scope
        self._re, self.dirty = {}, True
        return token

    def _put(self, table, key, token):
        if key not in table:
            table[key] = token
            self._re, self.dirty = {}, True

    def add(self, value, kind, scopes):
        for scope in scopes:
            self._add(value, kind, scope)

    def _add(self, value, kind, scope):
        value = flat(norm(value)).strip(".'\"")
        check = PATTERNS.get(kind, (None, None))[1]
        if len(value) < 2 or TOKEN.fullmatch(value) or (check and not check(value)) \
                or (kind == "ID" and value.isdigit() and len(value) < 5):   # short numbers are not identifiers
            return
        t = self._t(scope)
        whole = kind != "PERSON" or " " in value                       # one-word names live in "parts"
        if whole:
            token = t["ids"].get(value.lower()) or self._new(value, kind, scope)
            self._put(t["ids"], value.lower(), token)
            bare = re.sub(r"[\W_]+", "", value).lower()          # E-10234 written E10234
            if kind in ("ID", "ADDRESS", "IBAN", "CARD") and bare != value.lower() and len(bare) >= 4 and not bare.isdigit():
                self._put(t["ids"], bare, token)
            digits = re.sub(r"\D", "", value)
            forms = {digits, digits[-10:]} if len(digits) >= 7 else set()   # 4155550134, (415) 555 0134, +1 ...
            if kind == "PHONE" and len(digits) >= 10:
                forms |= {digits[-7:], digits[-8:]}             # 555-0134, 7946 0958
                forms |= {cc + digits[-10:] for cc in ("1", "01", "001", "011", "0")}   # 14155550134, wa.me/1...
            tail = re.search(r"\d{4,}$", bare)
            if kind == "ID" and tail:                           # E-10234 called "staff no. 10234" or "E 102 34"
                forms.add(tail.group(0))
            ymd = re.fullmatch(r"(\d{4})\D(\d{1,2})\D(\d{1,2})|(\d{1,2})\D(\d{1,2})\D(\d{4})", value)
            if kind == "DOB" and ymd:                           # 1985-03-14 = 14/03/1985 = 03.14.1985 = 14/3/85
                y, a, b = ymd.group(1, 2, 3) if ymd.group(1) else (ymd.group(6), ymd.group(4), ymd.group(5))
                forms = {f for a, b in {(a.zfill(2), b.zfill(2)), (str(int(a)), str(int(b)))} for f in (
                    y + a + b, a + b + y, b + a + y, a + b + y[2:], b + a + y[2:])}
            if kind in ("PHONE", "SSN", "CARD", "IBAN", "ID", "DOB"):
                for d in forms:
                    self._put(t["digits"], d, token)
        local = value.split("@")[0].lower() if kind == "EMAIL" else ""
        if len(local) >= 2 and not word(local) and local not in COMMON:   # jdoe1985 at gmail dot com
            self._put(t["ids"], local, token)
        if kind == "ADDRESS":                                          # "41 Larkspur Mews" and "Oakhollow" too
            for part in re.split(r"\s*,\s*", value):
                for p in (part, re.sub(r"^\d{1,5}[A-Za-z]?,?\s+", "", part)):   # "Larkspur Mews" too
                    if len(p) >= 3 and not p.isdigit():
                        self._put(t["ids"], p.lower(), token)
                        street = p.rsplit(" ", 1)[0] if " " in p else ""     # "Larkspur" too
                        if len(street) >= 4 and not street[:1].isdigit():   # its own files: over-scrub, never under
                            self._put(t["ids"], street.lower(), token)
        if kind == "PERSON":                                           # "Alice" or "Nguyen" alone name her too
            for part in {p for sep in (r"[\s,]+", r"[\s,-]+") for p in re.split(sep, value)}:
                part = part.strip(".'\"")
                part = part[:1].upper() + part[1:] if part.islower() else part
                if len(part) < 2 or part == value and whole:
                    continue
                have = t["parts"].get(part)
                if have is None:
                    self._put(t["parts"], part, token if whole else self._new(part, kind, scope))
                elif whole and have != token and self.v["real"].get(have) != part:   # two people of this scope
                    t["parts"][part] = self._new(part, kind, scope)                  # share it: its own token

    def find_in(self, text, scopes):
        for kind, (p, check) in PATTERNS.items():
            for m in re.finditer(p, text):
                self.add(m.group(1) if m.lastindex else m.group(0), kind, scopes)

    def learn(self, name, data, scopes, find=None, also=""):
        """Record every identifier in one private file: patterns, identifier columns of a table, and what
        `find(text)` (a local model trusted with the file) lists as people, addresses and other ids.
        `also`: more text to search the same way, such as the file's path."""
        raw = norm(text_of(data) or "")
        split = re.sub(r"(?<=[a-z])(?=[A-Z])|[^\w]+|_+", " ", also)         # Varnella-Oxtrude, VarnellaOxtrude
        text = also + "\n" + split + "\n" + raw if also else raw          # and as written: a.b@corp.example.md
        self.find_in(text, scopes)
        ext = name.rsplit(".", 1)[-1].lower()
        if ext in ("csv", "tsv"):
            try:
                delim = "\t" if ext == "tsv" else csv.Sniffer().sniff(raw[:4096], ",;\t|").delimiter
            except csv.Error:
                delim = ","
            rows = list(csv.reader(io.StringIO(raw), delimiter=delim))
            for c, head in enumerate(rows[0] if rows else []):
                head = re.sub(r"[_.\-]+", " ", head.strip().lower())
                kind = next((k for k, p in COLUMNS if re.search(p, head)), None)
                for r in rows[1:] if kind else []:
                    if c < len(r) and r[c].strip():
                        self.add(r[c], kind, scopes)
        low = re.sub(r"[\W_]+", "", fold(text))
        for kind, value in (find(text) if find else []):
            if re.sub(r"[\W_]+", "", fold(norm(value))) in low:  # only what is really in the file, however spelt
                self.add(value, kind, scopes)
        self.v["files"][name] = hashlib.sha256(raw.encode()).hexdigest()
        self.save()

    def known(self, name, data):
        text = norm(text_of(data) or "")
        return self.v["files"].get(name) == hashlib.sha256(text.encode()).hexdigest()

    def _pattern(self, scope):
        """Matchers for one scope, from its own table: whole ids, name forms, folded name parts, numbers."""
        if scope not in self._re:
            t = self._t(scope)
            alt = lambda keys: "|".join(re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", r"[\\s_.\\-]*",   # E 10234
                                               re.escape(k).replace(r"\ ", r"[\s_.\-]*"))   # frank_brown, FrankBrown
                                        for k in sorted(keys, key=len, reverse=True))  # noqa: E731
            joined = {key(k): tok for k, tok in t["ids"].items()}   # how a match is looked up
            forms, folded = {}, {}
            for p, tok in t["parts"].items():
                for q in {p, p.replace("'", "")}:              # O'Brien, OBrien
                    for f in {q, q.upper(), q.title(), q.lower()}:   # its own files: "frank" too
                        forms.setdefault(f, tok)
                for q in {p.replace("'", ""), *p.split("'")[1:]}:   # o'brien as obrien, and brien (O Brien)
                    for f in spellings(q) if len(q) >= 4 and q.lower() not in COMMON else ():
                        folded.setdefault(f, tok)
            b, e = r"(?<![A-Za-z0-9])", r"(s?)(?![A-Za-z0-9])"     # '_' and '.' separate words; plurals too
            digits = t["digits"]
            short = {f: tok for p, tok in t["parts"].items() if len(p) < 4 and p.lower() not in COMMON for f in spellings(p)}
            common = {f: tok for p, tok in t["parts"].items() if p.lower() in COMMON for f in spellings(p)}   # brown
            self._re[scope] = (joined, forms, [re.compile(b + "(" + alt(ks) + ")" + e, fl) if ks else None for ks, fl, b, e in
                                               ((t["ids"], re.I, b, e),   # a name next to digits is still a name: park88
                                                (forms, 0, "(?<![A-Za-z])", "(s?)(?![A-Za-z])"))],
                               folded, re.compile("|".join(map(re.escape, sorted(folded, key=len, reverse=True)))) if folded else None,
                               (digits, re.compile(r"(?<!\d)(?:" + "|".join(SEP.join(d) for d in sorted(digits, key=len, reverse=True))
                                                   + r")(?!\d)") if digits else None), short, common)
        return self._re[scope]

    def scrub(self, text, scopes, fresh=None):
        """The text with every known identifier of `scopes` replaced, each scope by its own table. Nothing
        is learned here: a table holds only what its own scope's files held when lego started, so one
        scope's view never depends on another's data. `fresh`: text the caller wrote. See own(). Any
        failure is Unscrubbable, one answer whatever the text held."""
        try:
            return self._view(text, scopes, fresh)
        except Unscrubbable:
            raise
        except Exception:
            raise Unscrubbable() from None

    def _view(self, text, scopes, fresh):
        text = norm(text)
        if fresh is not None:
            return self.own(text, fresh, set(scopes)) if scopes else text
        keep = set(scopes)                                     # [EMP_10234] in a file is text, not a placeholder
        for scope in sorted(scopes):
            text = self._out(text, lambda seg: self._scrub(seg, scope, keep), keep)
        plain = decoded(text)                                  # Jos&eacute;, Jos%C3%A9, Quellw=\nater: refused
        if plain != text and any(self._out(plain, lambda seg: self._scrub(seg, s, keep), keep) != plain for s in scopes):
            raise Unscrubbable()
        return text

    def _out(self, text, f, keep):
        return outside(text, f, lambda t: self.v["scope"].get(t) in keep)

    def _scrub(self, text, scope, keep):
        ids, forms, res, folded, inner, (digits, numbers), short, common = self._pattern(scope)
        def sub(r, look):                                      # Yilmaz for Yılmaz, Agent 47 for Agent47: the same
            return self._out(text, lambda seg: r.sub(lambda m: ((ids.get(key(m.group(1))) if look is ids else look.get(
                re.sub(r"[\s_.\-]+", "", m.group(1)), look.get(m.group(1)))) or "[ID]") + m.group(2), seg), keep)
        if res[0]:                                             # whole values first: E-10234 before its 10234
            text = sub(res[0], ids)
        if digits:                                             # dates written out: the DOB's digits
            text = self._out(text, lambda seg: DATE.sub(lambda m: next((digits[d] for d in date_digits(m) if d in digits),
                                                                     m.group(0)), seg), keep)
        if numbers:                                            # numbers however they are written
            text = self._out(text, lambda seg: numbers.sub(lambda m: digits[re.sub(r"\D", "", m.group(0))], seg), keep)
        if res[1]:
            text = sub(res[1], forms)
        if not inner and not short:
            return text

        def parts(w):                                          # zorbinaQuellwater2, quillonbrimstoke, Jose for José, zOrBiNa
            if not (inner and inner.search(fold(w))):
                return None
            return outside(inner.sub(lambda h: folded[h.group(0)], fold(w)), lambda seg: re.sub(   # zhangwei: wei too
                r"[^\W\d_]+", lambda r: short.get(r.group(0)) or r.group(0), seg))

        most = max(map(len, [*short, *folded, *common]), default=0)
        glued = {**common, **short}                            # inside a word that is no word: abrown, frankq

        def chain(w):                                          # the longest run of known parts w starts with: two
            best, end = {0: []}, 0                             # or more, or one long one (liwei..., zhangwei...)
            for i in range(len(w)):
                for j in range(i + 2, min(len(w), i + most) + 1) if i in best else ():
                    if j not in best and (w[i:j] in glued or w[i:j] in folded):
                        best[j] = best[i] + [glued.get(w[i:j]) or folded[w[i:j]]]
                        end = j if len(best[j]) > 1 or w[i:j] in folded else end
            return best[end], end

        def glue(w):                                           # adminliweiops, devzhangweiops: known parts anywhere
            out, i, hit = [], 0, False
            while i < len(w):
                toks, n = chain(w[i:])
                out, i, hit = (out + toks, i + n, True) if n else (out + [w[i]], i + 1, hit)
            if hit:
                return "".join(out)
            for k in (1, 2):                                   # wli, abrown, frankq, jwhill, parkjw: initials
                if len(w) > k + 1 and w[k:] in glued:          # and a name
                    return w[:k] + glued[w[k:]]
                if len(w) > k + 1 and w[:-k] in glued:
                    return glued[w[:-k]] + w[-k:]
            return None

        def only(r):                                           # dev[P]ops is [P]: letters left around a part
            return "".join(re.findall(r"\[[A-Z]+_[0-9a-f]+\]", r)) if r else None   # would spell it (tran+sfer)

        def known(h):                                          # Baker, bAkEr, BAKERS, QuellWater: a known part
            b = re.sub("['" + MARK + "]", "", fold(h))         # first, even when it is also a word
            for k, s in ((b, ""), (b[:-1], "s") if b.endswith("s") else (None, "")):
                if k in short or k in folded:
                    return (short.get(k) or folded[k]) + s
            return None

        def hump(h):                                           # transfer, planned, let: words, whatever they hold
            return known(h) or (h if word(re.sub("['" + MARK + "]", "", h)) else
                                only(glue(re.sub("['" + MARK + "]", "", fold(h)))) or only(parts(h)) or h)

        def run(m):                                            # MeiNg, TransferMei: hump by hump, the longest
            w = m.group(0)                                     # run of humps that is a known part first
            if not re.fullmatch(r"[A-Za-z][a-z]+(?:[A-Z][a-z]+)+", w):
                return hump(w)                                 # zOrBiNa, O'brien: whole
            hs, out, i = re.split(r"(?<=[a-z])(?=[A-Z])", w), [], 0
            while i < len(hs):
                j = next((j for j in range(len(hs), i + 1, -1) if known("".join(hs[i:j]))), i + 1)
                out.append(known("".join(hs[i:j])) if j > i + 1 else forms.get(hs[i]) or hump(hs[i]))   # TeamGrace
                i = j
            return "".join(out)
        return self._out(text, lambda seg: re.sub(WORD, run, seg), keep)

    def own(self, text, fresh, scopes=()):
        """Text the caller wrote, which a lower model may have asked it to relay: replaced by rules that
        look at the text only, never at the vault, so what is replaced tells the reader nothing about
        what the vault holds. Patterns, dates, runs of capitalised words, and capitalised words that are
        not everyday words each get a placeholder of their own, kept in `fresh` for the way back."""
        def keep(t):                                           # placeholders it was given; any other is text
            return t in fresh or self.v["scope"].get(t) in scopes
        out = lambda text, f: outside(text, f, keep)  # noqa: E731

        def tok(kind, raw):
            key = f"{kind}|{fold(flat(raw))}"
            if key not in fresh:
                fresh[key] = f"[{kind}_{secrets.token_hex(4)}]"
                fresh[fresh[key]] = raw
            return fresh[key]
        for kind, (p, check) in PATTERNS.items():
            text = out(text, lambda seg: re.sub(p, lambda m: m.group(0) if check and not check(
                m.group(1) if m.lastindex else m.group(0)) else tok(kind, m.group(0)), seg))
        text = out(text, lambda seg: DATE.sub(lambda m: tok("DATE", m.group(0)), seg))
        text = out(text, lambda seg: re.sub(r"(?<![\w])\d(?:[\s().\-/_,]{0,3}\d){3,}(?![\w])",   # 4+ digits, any way
                                            lambda m: tok("NUM", m.group(0)), seg))
        text = out(text, lambda seg: re.sub(r"(?<![^\W_])(?=[^\W_]*\d)(?=[^\W_]*[^\W\d_])[^\W_]{3,}",   # e10234
                                            lambda m: tok("ID", m.group(0)), seg))
        start = r"(?:^|(?<=[.!?:;]\s)|(?<=[.!?:;]\s\s)|(?<=\n)|(?<=[-*#>]\s))"   # where a capital means nothing
        def titled(before):                                    # J. Read, Det. Good, Dña. Best, prof. Read
            t = re.sub(r"^[^\w]+", "", (re.findall(r"(\S+)\s{1,2}$", before) or [""])[0])   # (Dr. Best), **Mr. Read**
            t = re.split(r"[/&+,.]+(?=\w)", t)[-1]           # Mr./Mrs. Good, Prof.Dr. Best: the last title
            w = t.rstrip(".")
            return w.lower() in TITLES or t.endswith(".") and 0 < len(w) <= 5 and w.isalpha() and w[0].isupper() and (
                len(w) == 1 or not w.isupper())                # but not an acronym: AWS. The end.
        text = out(text, lambda seg: re.sub(start + r"([A-Z][\w'-]*)", lambda m: m.group(1) if titled(seg[:m.start()])
                                            else "\0" + m.group(1), seg))
        text = out(text, lambda seg: re.sub(r"(?<![\w'])(?:[A-Z][\w'-]*[ \t]+)+[A-Z][\w'-]*(?![\w'])",
                                            lambda m: tok("NAME", m.group(0)), seg))
        text = out(text, lambda seg: re.sub(r"(\0?)((?<![\w'])[\w'-]*[A-Z][\w'-]*)", lambda m: m.group(2) if m.group(1)
                                            and m.group(2).lower() in STARTERS else tok("NAME", m.group(2)), seg))
        return out(text, lambda seg: re.sub(r"[^\W\d_]+", lambda m: m.group(0) if (word(m.group(0), False) or m.group(0).lower()
                                            in TECH) and m.group(0).lower() not in FIRST and not (m.group(0)[0].isupper() and m.group(0).lower() in LOW)
                                            else tok("NAME", m.group(0)), seg).replace("\0", ""))   # zorbina, 王伟; frank

    def scrub_name(self, name, scopes, fresh=None):
        """A file name: whole first (emails, numbers), then piece by piece, `alice_nguyen-review.md` ->
        `[PERSON_1]_[PERSON_1]-review.md`."""
        if fresh is not None:                                  # a name the caller wrote: like its text
            return self.scrub(name, scopes, fresh)
        try:
            name = self.scrub(name, scopes)
        except Unscrubbable:                                   # an encoded name: no name at all
            return "#" + hmac.new(bytes.fromhex(self.v["key"]), name.encode(), "sha256").hexdigest()[:8]
        for scope in sorted(scopes):
            ids, forms = self._pattern(scope)[:2]
            lower = {k.lower(): t for k, t in forms.items()}
            name = re.sub(r"(?<!\[)[A-Za-z0-9]+(?![\w]*\])", lambda m: ids.get(key(m.group(0)))
                          or lower.get(m.group(0).lower()) or m.group(0), name)
        return name

    def unscrub(self, text, allowed, fresh=None):
        """Placeholders back to real values, only those of a scope in `allowed` (and the caller's own,
        from `fresh`). Only a placeholder standing alone: 1[PHONE_x] stays as written, so a later scrub
        cannot miss what it would have become."""
        def back(m):
            t = m.group(0)
            if fresh and t in fresh:
                return fresh[t]
            return self.v["real"][t] if t in self.v["real"] and self.v["scope"].get(t) in set(allowed) else t
        return re.sub(r"(?<![A-Za-z0-9])" + TOKEN.pattern + r"(?![A-Za-z0-9])", back, text)


def outside(text, f, keep=lambda t: True):
    """f applied to the text between placeholders (those `keep` accepts), never inside one."""
    parts = re.split(r"(" + TOKEN.pattern.replace("(", "(?:") + r")", text)
    return "".join(p if i % 2 and keep(p) else f(p) for i, p in enumerate(parts))


MON = "|".join(m[:3] + "(?:" + m[3:] + ")?" for m in MONTHS) + "|sept"
DATE = re.compile(r"(?i)\b(?:(\d{1,2})(?:st|nd|rd|th)?(?:\s+of)?[\s.-]+(" + MON + r")\.?,?[\s.-]+(\d{4}|'?\d{2})"
                  r"|(" + MON + r")\.?[\s.-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s.-]+(\d{4}|'?\d{2}))\b")


def date_digits(m):
    """A written date's digit forms, as the vault keeps a DOB: 14th of March '85 -> 19850314, ..."""
    d, mon, y = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(5), m.group(4), m.group(6))
    mm = next(i + 1 for i, x in enumerate(MONTHS) if x.startswith(mon.lower()[:3]))
    y = y.lstrip("'")
    return [yy + f"{mm:02d}" + d.zfill(2) for yy in ([y] if len(y) == 4 else ["19" + y, "20" + y])] + \
        [f"{mm:02d}" + d.zfill(2) + y[-2:]]


try:
    WORDS = {w.strip() for w in open("/usr/share/dict/words") if w[:1].islower()}
except OSError:
    WORDS = set()
WORDS |= set("paid has does doesn isn wasn didn don won can't email emails json csv tsv md txt py js ts pdf url api hr "
             "cc todo html http https www com org net io dataset vs etc eg ie ok id ids faq ceo cto cfo vp hq usd eur "
             "gbp kb mb gb ssh cli gpu cpu llm".split())   # what a short web2 lacks

TECH = set("""offload offloads offloaded offloading workspace workspaces configs plaintext filename filenames handoff regexes
username usernames timestamp timestamps endpoint endpoints backend frontend metadata codebase localhost subfolder
subfolders subdirectory subdirectories numeric unparseable parseable staging booleans boolean webhook webhooks lookups
untrusted unredacted redacted redaction pseudonymised pseudonymized placeholder placeholders canaries refactor
refactored runtime classify newline newlines hyperlink""".split())   # long lowercase technical words, for caller text only (own); FIRST still
                                 # wins. Short ones (repo, auth, diff, env) are surnames somewhere


STARTERS = set("""a an the this that these those it its i we you he she they my our your their there here what
which who whom whose when where why how if then else so but and or nor not no yes also only just please now next
first second third last finally note see use using write read summarise summarize compute count list find check tell
give make put take get keep send save add remove create update show return answer explain describe compare rank sort
group total sum average calculate draft edit review fix ask call reply open close run start stop do don't is are was
were be been has have had can could would should might must shall all each every any some most many few
none both either neither more less after before during while since until because although though however therefore
thus otherwise instead for from in on at by with without about into onto over under between among per as to of
thanks thank hi hello dear regards best ok okay done good great yes sure result results summary total name names""".split())


TITLES = set("mr ms mrs mx dr prof sir capt col gen sgt rev hon st lt maj cpt fr dear".split())
LOW = {"list", "no", "real", "old", "part", "space"}             # List, No: surnames; list, no: words
try:                                                            # first names, from a fixed list: never the vault
    FIRST = {w.strip().lower() for w in open("/usr/share/dict/propernames")} - {
        "will", "mark", "art", "bill", "guy", "ray", "joy", "hope", "case", "page", "lane", "rich", "sky", "major"} - set(
        """the think those trying plastic sanity tolerant rodent stagger shatter panacea""".split()) - LOW
    # macOS's list: not names, or (LOW) names only when capitalised
except OSError:
    FIRST = set()


def word(w, doubled=True):
    """In the dictionary, as written or inflected (salaries, reviewed, offloading). `doubled`: planned from
    plan too, which caller text does without (ritter is no word for it: rit is one)."""
    w = w.lower()
    return w in WORDS or any(w.endswith(a) and w[:-len(a)] + b in WORDS and len(w) - len(a) >= n for a, b, n in (
        ("s", "", 1), ("ies", "y", 1), ("ed", "", 1), ("ed", "e", 1), ("ing", "", 3), ("ing", "e", 1), ("ly", "", 1),
        ("er", "", 4), ("ers", "", 4), ("'s", "", 1), ("ising", "ize", 1), ("ise", "ize", 1), ("ised", "ize", 1),
        ("est", "", 1), ("iest", "y", 1))) or bool(re.search(r"(s|x|z|ch|sh|o)es$", w)) and w[:-2] in WORDS or \
        doubled and bool(d := re.fullmatch(r"(\w*([b-df-hj-np-tv-z]))\2(?:ed|ing|er|est)", w)) and d.group(1) in WORDS
                                                               # deeper, not huber; catches, not hayes
