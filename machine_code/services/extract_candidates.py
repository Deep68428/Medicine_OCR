import re


class PharmaOCRExtractor:
    # ── Month map ─────────────────────────────────────────────────────────

    MONTHS = {
        "JAN": "01",
        "FEB": "02",
        "MAR": "03",
        "APR": "04",
        "MAY": "05",
        "JUN": "06",
        "JUL": "07",
        "AUG": "08",
        "SEP": "09",
        "OCT": "10",
        "NOV": "11",
        "DEC": "12",
    }

    # ── Blacklists ────────────────────────────────────────────────────────

    BLACKLIST_PREFIXES = {
        "EXP",
        "MFG",
        "MFD",
        "MRP",
        "PRICE",
        "TAB",
        "TAX",
        "INCL",
        "DATE",
        "PACK",
        "BLIST",
        "CAPS",
        "STRIP",
        "INCLUS",
        "MAXIMUM",
        "RETAIL",
        "IOTA",
        "PERB",
        "ALL",
        "TAXES",
        "AXES",
        "OFALL",
        "INCLOF",
        "FORIO",
        "FOREO",
        "FORE1",
        "FORLO",
        "FORHLO",
        "FORILO",
        "INCLOE",
        "INCLCE",
    }

    BLACKLIST_EXACT = {
        "MFGDT",
        "MFGDTOCT",
        "MFGDTSEP",
        "MFGDTAUG",
        "MFGDTJUL",
        "EXPIRYDATE",
        "EXPIRY",
        "INCLUSIVE",
        "OFALLTAXES",
        "INCLOEALL",
        "INCLOFALL",
    }

    _MONTH_ABBR = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"

    _MONYR_RE = re.compile(
        r"^(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2,4}$"
    )

    def __init__(self):
        # Label patterns (applied after normalisation / uppercasing)

        self._label_re = re.compile(
            r"(?:"
            r"B[\.\s]*N[O0]"  # B.NO / B.N0 / BNO
            r"|BATCH[\s\.\-]*N[O0]"  # BATCH NO / BATCH N0
            r"|\bLOT[\s\.\-]*N[O0]"  # LOT NO / LOT N0
            r")[\.\s:\-#]*",
            re.IGNORECASE,
        )

        # MFG keyword variants

        self._mfg_kw = r"(?:MFG|MFD|MFC|MF6|ME6|MFGDT|MEGDT)"

        # Suffix to strip from batch+MFG concatenations

        self._mfg_suffix_re = re.compile(
            r"(?:MFG|MFD|MFC|ME6|MF6|MFGDT|MEGDT|MEGD|MGT|MFGT|M1G|FGDT)\w*$"
        )

    # ═════════════════════════════════════════════════════════════════════

    # PUBLIC API

    # ═════════════════════════════════════════════════════════════════════

    def extract(self, ocr_lines) -> dict:
        if not isinstance(ocr_lines, list):
            ocr_lines = [ocr_lines]

        # Normalise every token; build a joined view of the whole label

        norm_lines = [self.normalize(str(t)) for t in ocr_lines if str(t).strip()]

        joined = " ".join(norm_lines)

        # ── Date extraction ─────────────────────────────────────────────

        raw_dates: list = []

        for text in norm_lines + [joined]:
            raw_dates.extend(self._extract_dates(text))

        dates = list(dict.fromkeys(raw_dates))

        # ── Batch extraction ────────────────────────────────────────────

        label_hits, premfg_hits, fallback_hits = [], [], []

        for text in norm_lines + [joined]:
            label_hits.extend(self._batch_from_label(text))

            premfg_hits.extend(self._batch_before_mfg(text))

            fallback_hits.extend(self._batch_fallback(text))

        def keep(lst):
            return [b for b in lst if self._valid_batch(b)]

        batches = self._merge_priority(
            keep(label_hits), keep(premfg_hits), keep(fallback_hits)
        )

        return {"batches": batches, "dates": dates}

    # ═════════════════════════════════════════════════════════════════════

    # NORMALISATION

    # ═════════════════════════════════════════════════════════════════════

    def normalize(self, text: str) -> str:
        """Apply all OCR-error corrections and return uppercased text."""

        text = text.upper().strip()

        # ── Label-prefix repairs ──────────────────────────────────────────

        # 8.NO / 8:NO / 8.N0 → B.NO.

        text = re.sub(r"\b8[\.\s:]*N[O0][\.\s:]*", "B.NO.", text)

        # D.NO / E.NO / F.NO / H.NO → B.NO.  (misread B)

        text = re.sub(r"\b[DEFHG][\.\s]*N[O0][\.\s]*", "B.NO.", text)

        # BNO. / BINO. / BIN0. / BLNO. → B.NO.

        text = re.sub(r"\bB[IL]?N[O0C][\.\s]+", "B.NO.", text)

        # B.N6 / B.NG → B.NO.  (6/G misread as O)

        text = re.sub(r"\bB[\.\s]*N[6G][\.\s]*", "B.NO.", text)

        # ENO. / HNO. / FNO. / BNC. (corrupted B.NO without dot)

        text = re.sub(r"\b[EFHG]N[O0][\.\s]+", "B.NO.", text)

        text = re.sub(r"\bBNC[\.\s]+", "B.NO.", text)

        # ── Month-name OCR fixes ──────────────────────────────────────────

        month_ocr = {
            "5EP": "SEP",
            "5EF": "SEP",
            "AUC": "AUG",
            "AU6": "AUG",
            "4UG": "AUG",
            "JUI": "JUL",
        }

        for bad, good in month_ocr.items():
            text = text.replace(bad, good)

        # Remove trailing dots on month names to simplify date regex

        text = re.sub(
            r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\.", r"\1", text
        )

        # ── Keyword-level fixes ───────────────────────────────────────────

        kw_fixes = [
            ("MFD", "MFG"),  # Manufactured → treat as MFG
            ("M.D.", "MFG."),  # M.D. = Mfg Date abbreviation
            ("E.D.", "EXP."),  # E.D. = Expiry Date abbreviation
            ("M.F.", "MFG."),
            ("MEG.", "MFG."),  # MEG / ME6 / MPG / MFE → MFG
            ("MEG", "MFG"),
            ("ME6.", "MFG."),
            ("ME6", "MFG"),
            ("MEC", "MFG"),
            ("MPG", "MFG"),
            ("MFE", "MFG"),
            ("MEGDT", "MFGDT"),
            ("MECDT", "MFGDT"),
            ("N0 ", "NO "),  # zero→O in standalone NO (trailing space guard)
        ]

        for bad, good in kw_fixes:
            text = text.replace(bad, good)

        return text

    # ═════════════════════════════════════════════════════════════════════

    # BATCH EXTRACTION STRATEGIES

    # ═════════════════════════════════════════════════════════════════════

    def _batch_from_label(self, text: str) -> list:
        """Extract batch codes that appear right after a recognised label."""

        candidates = []

        for m in self._label_re.finditer(text):
            window = text[m.end() : m.end() + 35]

            # Hyphens allowed inside (e.g. ST25-0923)

            hit = re.search(r"[A-Z0-9][A-Z0-9\-]{4,15}", window)

            if hit:
                raw = self._strip_mfg_suffix(hit.group())

                if raw:
                    candidates.append(raw)

        return candidates

    def _batch_before_mfg(self, text: str) -> list:
        """Extract codes that immediately precede a MFG/MFD keyword."""

        return re.findall(
            r"\b([A-Z0-9][A-Z0-9\-]{4,15})\s*(?=" + self._mfg_kw + r")",
            text,
        )

    def _batch_fallback(self, text: str) -> list:
        """Collect all plausible alphanumeric tokens as a last resort."""

        return re.findall(r"\b[A-Z0-9][A-Z0-9\-]{4,15}\b", text)

    def _strip_mfg_suffix(self, raw: str) -> str:
        """Remove trailing MFG/MFGDT/M1G… suffixes OCR merges with batch."""

        raw = self._mfg_suffix_re.sub("", raw)

        # Also strip trailing M.R.P / price concatenation: trailing single

        # letter after a digit (e.g. ST25-0923M → ST25-0923)

        raw = re.sub(r"(\d)[A-Z]$", r"\1", raw)

        return raw.rstrip("-").strip()

    # ═════════════════════════════════════════════════════════════════════

    # BATCH VALIDATION

    # ═════════════════════════════════════════════════════════════════════

    def _valid_batch(self, batch: str) -> bool:
        batch = batch.strip("-").strip()

        clean = batch.replace("-", "")

        # ── Length ────────────────────────────────────────────────────────

        if len(clean) < 3 or len(clean) > 50:
            return False

        # ── Must contain at least one digit ──────────────────────────────

        if not any(c.isdigit() for c in batch):
            return False

        # ── Pure 4-digit year ─────────────────────────────────────────────

        if re.fullmatch(r"\d{4}", clean):
            return False

        # ── Year-only strings 20xx ────────────────────────────────────────

        if re.fullmatch(r"20[2-3]\d", clean):
            return False

        # ── Month+Year tokens: AUG2025, SEP2027 … ────────────────────────

        if self._MONYR_RE.match(batch):
            return False

        # ── Price fragment: RS followed by digits ─────────────────────────

        if re.fullmatch(r"RS\d+", batch):
            return False

        # ── Reject low-entropy strings ────────────────────────────────────

        if len(set(clean)) < 3:
            return False

        # ── Blacklist prefix ──────────────────────────────────────────────

        for prefix in self.BLACKLIST_PREFIXES:
            if batch.startswith(prefix):
                return False

        # ── Exact blacklist ───────────────────────────────────────────────

        if batch in self.BLACKLIST_EXACT:
            return False

        # ── Reject MFG suffix leftovers (including FGDT) ─────────────────

        if re.search(r"(?:MFG|MFGDT|MEGDT|M1G|FGDT)\w*$", batch):
            return False

        # ── Reject month-prefixed tokens (OCT2025, SEP2027M) ─────────────

        if re.match(self._MONTH_ABBR + r"\d", batch):
            return False

        # ── Reject date-formatted fragments: MM-YYYY / MM.YYYY ───────────

        if re.fullmatch(r"\d{2}[\-\.\/]\d{4}", batch):
            return False

        # ── Reject price fragments: NNN-NN or NNNN-NN ────────────────────

        if re.fullmatch(r"\d{2,4}[\-\.]\d{2}", batch):
            return False

        if re.fullmatch(r"\d{1,3}(?:TABS|CAPS|TABLET|BLISTER|STRIP)\w*", batch):
            return False

        return True

    # ═════════════════════════════════════════════════════════════════════

    # DATE EXTRACTION

    # ═════════════════════════════════════════════════════════════════════

    def _extract_dates(self, text: str) -> list:
        dates = []

        # ── MM/YYYY  MM-YYYY  MM.YYYY ─────────────────────────────────────

        for m in re.finditer(r"\b(\d{1,2})[\/\.\-](\d{4})\b", text):
            month = m.group(1).zfill(2)

            year = m.group(2)

            if 1 <= int(month) <= 12 and 2000 <= int(year) <= 2040:
                dates.append(f"{month}/{year}")

        # ── MM/YY (2-digit year) ──────────────────────────────────────────

        for m in re.finditer(r"\b(\d{2})[\/\.\-](\d{2})\b", text):
            month_s, year_s = m.group(1), m.group(2)

            if 1 <= int(month_s) <= 12:
                year_i = int("20" + year_s)

                if 2000 <= year_i <= 2040:
                    dates.append(f"{month_s}/{year_i}")

        # ── MON YYYY / MON.YYYY / MON-YYYY ───────────────────────────────

        mon_pat = r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"

        for m in re.finditer(mon_pat + r"[\.\s\-\/]*(\d{2,4})", text):
            mon_num = self.MONTHS[m.group(1)]

            raw_year = m.group(2)

            year = ("20" + raw_year) if len(raw_year) == 2 else raw_year

            if 2000 <= int(year) <= 2040:
                dates.append(f"{mon_num}/{year}")

        return dates

    # ═════════════════════════════════════════════════════════════════════

    # MERGE / PRIORITISE

    # ═════════════════════════════════════════════════════════════════════

    @staticmethod
    def _merge_priority(label: list, premfg: list, fallback: list) -> list:
        """Return deduplicated batches ordered by extraction priority."""

        seen: dict = {}

        for pri, group in enumerate([label, premfg, fallback]):
            for b in group:
                b = b.strip("-")

                if b and b not in seen:
                    seen[b] = pri

        # Remove shorter strings that are strict prefixes of a longer candidate

        # (e.g. keep "BRF09101A" and drop "BRF09101")

        keys = sorted(seen, key=lambda x: (seen[x], x))

        filtered = []

        for k in keys:
            dominated = any(
                other != k and other.startswith(k)
                for other in keys
                if seen[other] <= seen[k]
            )

            if not dominated:
                filtered.append(k)

        return filtered

    # ═════════════════════════════════════════════════════════════════════

    # LEGACY HELPER — kept for backward compatibility

    # ═════════════════════════════════════════════════════════════════════

    def classify_line(self, line: str) -> str:
        """Classify a single OCR line by its primary content type."""

        line = line.upper()

        if re.search(r"B[\.\s]*N[O0]|BATCH[\s]*N[O0]|\bLOT\b", line):
            return "BATCH"

        if re.search(r"\bMFG\b|\bMFD\b", line):
            return "MFG"

        if re.search(r"\bEXP\b|\bEXPIRY\b", line):
            return "EXP"

        if "MRP" in line or re.search(r"\bRS\b", line):
            return "PRICE"

        return "OTHER"
