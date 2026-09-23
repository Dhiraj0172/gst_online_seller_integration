"""CSV reading helpers shared by the import adapters and the import pipeline.

Marketplace exports arrive either as Excel workbooks or as CSV. The CSV side
must behave exactly like the Excel side: BOM-safe decoding, delimiter
detection, the same header-driven detection, and explicit errors for files that
cannot be read instead of silently empty results.

No marketplace-specific CSV layout is assumed here: the columns are matched by
the adapters' own header maps / generic alias table.
"""
import csv
import io
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Any

# Encodings tried in order. ``utf-8-sig`` strips a UTF-8 BOM when present and
# decodes plain UTF-8 otherwise, so it is always tried first.
CANDIDATE_ENCODINGS = ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1')

# Delimiters a marketplace export may use. Comma is the default.
CANDIDATE_DELIMITERS = ',;\t|'

_DELIMITER_LABELS = {',': 'comma', ';': 'semicolon', '\t': 'tab', '|': 'pipe'}

# Magic bytes of the formats that are NOT text CSV.
_BINARY_SIGNATURES = (
    (b'PK\x03\x04', 'an Excel (.xlsx) workbook'),
    (b'\xd0\xcf\x11\xe0', 'a legacy Excel (.xls) workbook'),
    (b'%PDF', 'a PDF document'),
)


@dataclass
class CsvReadResult:
    """Result of reading one CSV file."""

    rows: List[List[str]] = field(default_factory=list)
    encoding: str = ''
    delimiter: str = ','
    errors: List[str] = field(default_factory=list)

    @property
    def delimiter_label(self) -> str:
        return _DELIMITER_LABELS.get(self.delimiter, repr(self.delimiter))


def delimiter_label(delimiter: str) -> str:
    return _DELIMITER_LABELS.get(delimiter, repr(delimiter))


def sniff_delimiter(sample: str) -> str:
    """Pick the delimiter of a CSV sample, preferring comma on any doubt."""
    lines = [line for line in sample.splitlines() if line.strip()]
    if not lines:
        return ','
    header = lines[0]
    try:
        return csv.Sniffer().sniff('\n'.join(lines[:5]), delimiters=CANDIDATE_DELIMITERS).delimiter
    except csv.Error:
        pass
    # Sniffer is easily confused; fall back to the most frequent candidate in
    # the header line, defaulting to comma.
    counts = {delimiter: header.count(delimiter) for delimiter in CANDIDATE_DELIMITERS}
    best = max(counts, key=lambda delimiter: counts[delimiter])
    return best if counts[best] > 0 else ','


def decode_csv_bytes(data: bytes) -> (Optional[str], str, List[str]):
    """Decode CSV bytes; return (text, encoding, errors)."""
    if not data.strip():
        return None, '', ['CSV file is empty (no bytes to read).']
    for signature, description in _BINARY_SIGNATURES:
        if data.startswith(signature):
            return None, '', [
                f'File is not text CSV: it is {description}. Upload it with its own '
                f'file extension so the matching reader is used.'
            ]
    if b'\x00' in data[:4096]:
        return None, '', ['File is not text CSV: it contains NUL bytes (binary content).']

    for encoding in CANDIDATE_ENCODINGS:
        try:
            return data.decode(encoding), encoding, []
        except UnicodeDecodeError:
            continue
    return None, '', [
        'CSV file could not be decoded with any supported encoding '
        f'({", ".join(CANDIDATE_ENCODINGS)}). Re-export it as UTF-8.'
    ]


def read_csv_rows(path: str) -> CsvReadResult:
    """Read a CSV file into a grid of rows (header row included).

    Returns a ``CsvReadResult``: ``errors`` is non-empty when the file cannot be
    read at all, in which case ``rows`` is empty.
    """
    if not path or not os.path.exists(path):
        return CsvReadResult(errors=[f'File not found: {path}'])
    try:
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError as exc:
        return CsvReadResult(errors=[f'Cannot read file {path}: {exc}'])

    text, encoding, errors = decode_csv_bytes(data)
    if errors:
        return CsvReadResult(errors=errors)

    delimiter = sniff_delimiter(text)
    result = CsvReadResult(encoding=encoding, delimiter=delimiter)
    try:
        reader = csv.reader(io.StringIO(text, newline=''), delimiter=delimiter)
        for row in reader:
            if not row or not any(str(cell).strip() for cell in row):
                continue  # blank line
            result.rows.append([str(cell).strip() for cell in row])
    except csv.Error as exc:
        return CsvReadResult(
            encoding=encoding, delimiter=delimiter,
            errors=[f'Malformed CSV file {os.path.basename(path)}: {exc}'],
        )

    if not result.rows:
        result.errors.append(
            f'CSV file {os.path.basename(path)} contains no data rows '
            f'(encoding {encoding}, delimiter {result.delimiter_label}).'
        )
    return result


# Characters that can trigger formula execution in spreadsheet software (Excel, LibreOffice)
FORMULA_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def sanitize_csv_value(val: Any) -> Any:
    """Sanitize a value for safe CSV export, preventing formula injection.

    If the value is a string whose leading character (ignoring leading spaces)
    starts with '=', '+', '-', '@', '\\t', or '\\r', it is prefixed with a single
    quote (') so spreadsheet software treats it as literal text rather than an
    executable formula.

    Pure numeric types (int, float, Decimal) and non-string types are preserved
    without modification to avoid corrupting financial/tax calculations.
    """
    if val is None:
        return ''
    if isinstance(val, (int, float, Decimal)):
        return val
    s = str(val)
    if not s:
        return ''
    stripped = s.lstrip(' ')
    if stripped and stripped[0] in FORMULA_PREFIXES:
        return f"'{s}"
    return s
