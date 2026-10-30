"""Scrub: the middle level, "scrubbed". A model that sees a scope scrubbed gets the same text with
every identifier (names, emails, phones, addresses, account numbers, keys) swapped for a stable
placeholder such as [PERSON_3]: the same person is the same placeholder in every file and message,
so the data still joins up. The vault (identifier -> placeholder) is learned from the private files
before any of them crosses, lives only on this host, and turns placeholders back into the real
values for a model that sees everything.
"""
import csv
import io
import json
import re
from pathlib import Path

PATTERNS = {"EMAIL": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
            "PHONE": r"(?:\+\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b|\+\d[\d .-]{8,}\d",
            "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
            "CARD": r"\b\d{4}(?:[ -]?\d{4}){2}[ -]?\d{1,7}\b",
            "IBAN": r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b",
            "IP": r"\b\d{1,3}(?:\.\d{1,3}){3}\b",
            "ADDRESS": r"\b\d{1,5}[A-Za-z]? (?:[A-Z][a-z]+ ){1,3}(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Mews|Drive|Dr|Way|"
                       r"Court|Ct|Place|Pl|Boulevard|Blvd|Terrace|Close|Crescent|Row|Square|Gardens|Hill|Park)\b",
            "KEY": r"\b(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{24,}\b"}
COLUMNS = [("EMAIL", r"e-?mail"), ("PHONE", r"phone|mobile|tel"), ("ADDRESS", r"address|street"),
           ("SSN", r"ssn|social"), ("ID", r"(^|_)id$|passport|account|iban|card"),
           ("PERSON", r"name|manager|employee|person|contact")]
TOKEN = re.compile(r"\[([A-Z]+)_(\d+)\]")


class Vault:
    def __init__(self, path):
        self.path = Path(path)
        self.v = json.loads(self.path.read_text()) if self.path.exists() else {"ids": {}, "real": {}, "files": []}

    def save(self):
        self.path.write_text(json.dumps(self.v))
        self._re = None

    def add(self, value, kind):
        value = value.strip()
        if len(value) < 3 or TOKEN.fullmatch(value) or value.lower() in self.v["ids"]:
            return
        n = sum(t.startswith(f"[{kind}_") for t in self.v["real"]) + 1
        token = f"[{kind}_{n}]"
        self.v["ids"][value.lower()] = token
        self.v["real"][token] = value
        if kind == "ADDRESS":                                  # "41 Larkspur Mews" and "Oakhollow" alone too
            for part in value.split(","):
                if len(part.strip()) >= 3:
                    self.v["ids"].setdefault(part.strip().lower(), token)
        if kind == "PERSON":                                   # "Alice" and "Nguyen" alone name her too
            for part in value.split():
                if len(part) >= 3 and part[0].isupper():
                    self.v["ids"].setdefault(part.lower(), token)

    def learn(self, name, text, find=None):
        """Record every identifier in one private file: patterns, identifier columns of a CSV, and what
        `find(text)` (a local model trusted with the file) lists as people, addresses and other ids."""
        for kind, p in PATTERNS.items():
            for m in re.findall(p, text):
                self.add(m, kind)
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except csv.Error:
            rows = []
        if len(rows) > 1 and len(rows[0]) > 1:
            for c, head in enumerate(rows[0]):
                kind = next((k for k, p in COLUMNS if re.search(p, head.strip().lower())), None)
                for r in rows[1:] if kind else []:
                    if c < len(r) and r[c].strip():
                        self.add(r[c], kind)
        for kind, value in (find(text) if find else []):
            if value.lower() in text.lower():                 # only what is really in the file
                self.add(value, kind)
        self.v["files"].append(name)
        self.save()

    def known(self, name):
        return name in self.v["files"]

    def scrub(self, text):
        for kind, p in PATTERNS.items():                       # identifiers the files did not hold
            for m in re.findall(p, text):
                if m.lower() not in self.v["ids"]:
                    self.add(m, kind)
                    self.save()
        if not self.v["ids"]:
            return text
        if not getattr(self, "_re", None):
            keys = sorted(self.v["ids"], key=len, reverse=True)
            self._re = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, keys)) + r")(?!\w)", re.I)
        return self._re.sub(lambda m: self.v["ids"][m.group(0).lower()], text)

    def unscrub(self, text):
        return TOKEN.sub(lambda m: self.v["real"].get(m.group(0), m.group(0)), text)
