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
import json
import re
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
# first names that are also everyday words: matched only as capitalised or upper case
COMMON = set("will mark may june april august grace hope faith joy rose pat bill rob sue art guy ray dawn summer frank "
             "long young little black white brown green king bell hill wood page lane gray grey rich chase hunter "
             "major price love case cash ward stone field park banks miles holly ivy jack sky".split())
TOKEN = re.compile(r"\[([A-Z]+)_([0-9a-f]+)\]")
SEP = r"\s{0,2}[().\-/_]{0,2}\s{0,2}"                                         # between the digits of a written number
CF = dict.fromkeys(c for c in range(0x10000) if unicodedata.category(chr(c)) == "Cf")
MARKS = {**dict.fromkeys(map(ord, "\u2018\u2019\u02bc\uff07\u0060\u00b4"), "'"),        # O’Brien is O'Brien
         **dict.fromkeys(range(0x2010, 0x2016), "-")}


def norm(s):
    """NFKC, invisible format characters removed, a hyphen at a line break joined."""
    return re.sub(r"-\n\s*", "", unicodedata.normalize("NFKC", s).translate(CF).translate(MARKS))


def flat(s):
    return re.sub(r"\s+", " ", s.strip())


def fold(s):
    """Per character: accents dropped, lower case. Same length as s, so offsets carry over."""
    return "".join((("".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c)) or ch)
                    .lower() + "_")[0] if ch.isalpha() else ch for ch in s)


class Vault:
    def __init__(self, path):
        self.path = Path(path)
        self.v = json.loads(self.path.read_text()) if self.path.exists() else {}
        if "key" not in self.v:                                 # an old vault numbered its placeholders: relearn
            self.v = {}
        self.dirty = False
        for k in ("ids", "parts", "real", "scope", "files", "digits"):
            self.v.setdefault(k, {})
        self.v.setdefault("key", secrets.token_hex(16))         # placeholder tags: nothing about other scopes
        self._re = None

    def save(self):
        self.path.write_text(json.dumps(self.v))
        self._re, self.dirty = None, False

    def _new(self, real, kind, scopes):
        mac = hmac.new(bytes.fromhex(self.v["key"]), f"{kind}|{real}".encode(), "sha256").hexdigest()
        token = next(t for n in range(4, 65) for t in [f"[{kind}_{mac[:n]}]"] if t not in self.v["real"])
        self.v["real"][token], self.v["scope"][token] = real, sorted(scopes)
        self._re, self.dirty = None, True
        return token

    def _tag(self, token, scopes):
        if not set(scopes) <= set(self.v["scope"].get(token, [])):
            self.v["scope"][token] = sorted(set(self.v["scope"].get(token, [])) | set(scopes))
            self._re, self.dirty = None, True                  # the matchers of those scopes now include it

    def add(self, value, kind, scopes):
        value = flat(norm(value)).strip(".'\"")
        check = PATTERNS.get(kind, (None, None))[1]
        if len(value) < 2 or TOKEN.fullmatch(value) or (check and not check(value)) \
                or (kind == "ID" and value.isdigit() and len(value) < 5):   # short numbers are not identifiers
            return
        whole = kind != "PERSON" or " " in value                       # one-word names live in "parts"
        if whole:
            token = self.v["ids"].get(value.lower()) or self._new(value, kind, scopes)
            self.v["ids"][value.lower()] = token
            self._tag(token, scopes)
            bare = re.sub(r"[\W_]+", "", value).lower()          # E-10234 written E10234
            if kind in ("ID", "ADDRESS", "IBAN", "CARD") and bare != value.lower() and len(bare) >= 4 and not bare.isdigit():
                self._tag(self.v["ids"].setdefault(bare, token), scopes)
            digits = re.sub(r"\D", "", value)
            forms = {digits, digits[-10:]}                      # 4155550134, (415) 555 0134, +1 ...
            ymd = re.fullmatch(r"(\d{4})\D(\d{1,2})\D(\d{1,2})|(\d{1,2})\D(\d{1,2})\D(\d{4})", value)
            if kind == "DOB" and ymd:                           # 1985-03-14 = 14/03/1985 = 03.14.1985
                y, a, b = ymd.group(1, 2, 3) if ymd.group(1) else (ymd.group(6), ymd.group(4), ymd.group(5))
                forms = {f for a, b in {(a.zfill(2), b.zfill(2)), (str(int(a)), str(int(b)))} for f in (y + a + b, a + b + y, b + a + y)}
            if kind in ("PHONE", "SSN", "CARD", "IBAN", "ID", "DOB") and len(digits) >= 7:
                for d in forms:
                    self._tag(self.v["digits"].setdefault(d, token), scopes)
        if kind == "ADDRESS":                                          # "41 Larkspur Mews" and "Oakhollow" too
            for part in re.split(r"\s*,\s*", value):
                for p in (part, re.sub(r"^\d{1,5}[A-Za-z]?,?\s+", "", part)):   # "Larkspur Mews" too
                    if len(p) >= 3 and not p.isdigit():
                        self._tag(self.v["ids"].setdefault(p.lower(), token), scopes)
        if kind == "PERSON":                                           # "Alice" or "Nguyen" alone name her too
            for part in {p for sep in (r"[\s,]+", r"[\s,-]+") for p in re.split(sep, value)}:
                part = part.strip(".'\"")
                part = part[:1].upper() + part[1:] if part.islower() else part
                if len(part) < 2 or part == value and whole:
                    continue
                have = self.v["parts"].get(part)
                if have is None:
                    self.v["parts"][part] = token if whole else self._new(part, kind, scopes)
                elif whole and have != token and self.v["real"].get(have) != part:   # two people share it: its own
                    self.v["parts"][part] = self._new(part, kind, set(scopes) | set(self.v["scope"].get(have, [])))  # token,
                                                                   # matched in every scope that matched it before
                self._tag(self.v["parts"][part], scopes)

    def find_in(self, text, scopes):
        for kind, (p, check) in PATTERNS.items():
            for m in re.finditer(p, text):
                self.add(m.group(1) if m.lastindex else m.group(0), kind, scopes)

    def learn(self, name, data, scopes, find=None, also=""):
        """Record every identifier in one private file: patterns, identifier columns of a table, and what
        `find(text)` (a local model trusted with the file) lists as people, addresses and other ids.
        `also`: more text to search the same way, such as the file's path."""
        raw = norm(data.decode(errors="replace") if isinstance(data, bytes) else data)
        also = re.sub(r"(?<=[a-z])(?=[A-Z])|[^\w]+|_+", " ", also)          # Varnella-Oxtrude, VarnellaOxtrude
        text = also + "\n" + raw if also else raw
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
        text = norm(data.decode(errors="replace") if isinstance(data, bytes) else data)
        return self.v["files"].get(name) == hashlib.sha256(text.encode()).hexdigest()

    def _pattern(self, scopes):
        """Matchers for the identifiers of `scopes` only: whole ids, name forms, and folded name parts."""
        key = frozenset(scopes)
        if self._re is None:
            self._re = {}
        if key not in self._re:
            mine = lambda t: set(self.v["scope"].get(t, [])) & key  # noqa: E731
            alt = lambda keys: "|".join(re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", r"[\\s_.\\-]*",   # E 10234
                                               re.escape(k).replace(r"\ ", r"[\s_.\-]*"))   # frank_brown, FrankBrown
                                        for k in sorted(keys, key=len, reverse=True))  # noqa: E731
            ids = {k: t for k, t in self.v["ids"].items() if mine(t)}
            joined = {re.sub(r"[\s_.\-]+", "", k): t for k, t in ids.items()}   # how a match is looked up
            forms, folded = {}, {}
            for p, t in self.v["parts"].items():
                if mine(t):
                    for q in {p, p.replace("'", "")}:          # O'Brien, OBrien
                        for f in {q, q.upper(), q.title()} | ({q.lower()} if q.lower() not in COMMON else set()):
                            forms.setdefault(f, t)
                    if len(p) >= 4 and p.lower() not in COMMON:
                        folded.setdefault(fold(p), t)
            b, e = r"(?<![A-Za-z0-9])", r"(s?)(?![A-Za-z0-9])"     # '_' and '.' separate words; plurals too
            digits = {d: t for d, t in self.v["digits"].items() if mine(t)}
            short = {fold(p): t for p, t in self.v["parts"].items() if mine(t) and len(p) < 4 and p.lower() not in COMMON}
            self._re[key] = (joined, forms, [re.compile(b + "(" + alt(ks) + ")" + e, fl) if ks else None for ks, fl in
                                          ((ids, re.I), (forms, 0))],
                             folded, re.compile("|".join(map(re.escape, sorted(folded, key=len, reverse=True)))) if folded else None,
                             (digits, re.compile(r"(?<!\d)(?:" + "|".join(SEP.join(d) for d in sorted(digits, key=len, reverse=True))
                                                 + r")(?!\d)") if digits else None), short)
        return self._re[key]

    def scrub(self, text, scopes, fresh=None):
        """The text with every known identifier of `scopes` replaced, and only those. Identifiers the
        files did not hold are found by pattern and learned on the spot, tagged with the scopes of the text.

        `fresh` is for text the caller wrote, which a lower model may have asked it to relay: then every
        identifier-looking span, known or not (patterns, "Two Capitalised" words), gets a placeholder of
        its own, kept in `fresh` and never the vault's, so the reader learns neither which of its guesses
        the vault holds nor which row they are. Nothing is learned from such text."""
        text, scopes = norm(text), set(scopes)
        if not scopes:
            return text
        if fresh is None:
            self.find_in(text, scopes)
        if self.dirty:
            self.save()
        ids, forms, res, folded, inner, (digits, numbers), short = self._pattern(scopes)

        def tok(token, raw):
            if fresh is None:
                return token
            kind = token[1:].split("_")[0]
            key = f"{kind}|{fold(flat(raw))}"
            if key not in fresh:
                fresh[key] = f"[{kind}_{secrets.token_hex(4)}]"
                fresh[fresh[key]] = raw
            return fresh[key]
        if numbers:                                            # numbers however they are written
            text = numbers.sub(lambda m: tok(digits[re.sub(r"\D", "", m.group(0))], m.group(0)), text)
        for look, r in zip((ids, forms), res):
            if r:
                text = r.sub(lambda m: tok(look[re.sub(r"[\s_.\-]+", "", m.group(1).lower())] if look is ids
                                           else look[m.group(1)], m.group(1)) + m.group(2), text)
        if fresh is not None:                                  # known or not, they look the same
            for kind, (p, check) in PATTERNS.items():
                text = re.sub(p, lambda m: m.group(0) if TOKEN.fullmatch(m.group(0)) or check and not check(
                    m.group(1) if m.lastindex else m.group(0)) else tok(f"[{kind}_]", m.group(0)), text)
            text = re.sub(r"(?<![\w\[])[A-Z][a-z'-]+(?:[ \t]+[A-Z][a-z'-]+)+(?![\w\]])", lambda m: tok("[PERSON_]", m.group(0)), text)
        if not inner and not short:
            return text

        def hump(w):                                           # MeiNg: short names as whole humps
            if short.get(fold(w)):
                return tok(short[fold(w)], w)
            return inner.sub(lambda h: tok(folded[h.group(0)], h.group(0)), fold(w)) if inner and inner.search(fold(w)) else w

        def run(m):                                            # zorbinaQuellwater2, quillonbrimstoke, Jose for José
            return "".join(hump(w) for w in re.split(r"(?<=[a-z])(?=[A-Z])", m.group(0)))
        return re.sub(r"\[[A-Z]+_[0-9a-f]+\]|[^\W\d_]+", lambda m: m.group(0) if m.group(0)[0] == "[" else run(m), text)

    def scrub_name(self, name, scopes, fresh=None):
        if fresh is not None:                                  # a name the caller wrote: like its text
            return self.scrub(name, scopes, fresh)
        """A file name: whole first (emails, numbers), then piece by piece, `alice_nguyen-review.md` ->
        `[PERSON_1]_[PERSON_1]-review.md`."""
        name = self.scrub(name, scopes)
        if not scopes:
            return name
        ids, forms = self._pattern(scopes)[:2]
        lower = {k.lower(): t for k, t in forms.items()}
        return re.sub(r"(?<!\[)[A-Za-z0-9]+(?![\w]*\])", lambda m: ids.get(m.group(0).lower())
                      or lower.get(m.group(0).lower()) or m.group(0), name)

    def unscrub(self, text, allowed, fresh=None):
        """Placeholders back to real values, only those whose every scope is in `allowed` (and the
        caller's own, from `fresh`). Only a placeholder standing alone: 1[PHONE_x] stays as written,
        so a later scrub cannot miss what it would have become."""
        def back(m):
            t = m.group(0)
            if fresh and t in fresh:
                return fresh[t]
            return self.v["real"][t] if t in self.v["real"] and set(self.v["scope"].get(t, ["?"])) <= set(allowed) else t
        return re.sub(r"(?<![A-Za-z0-9])" + TOKEN.pattern + r"(?![A-Za-z0-9])", back, text)
