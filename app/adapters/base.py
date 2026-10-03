import datetime
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Tuple, Any, Iterable
from dataclasses import dataclass, field
from enum import Enum
from decimal import Decimal
from .canonical import CanonicalTransaction

class ImportRowStatus(Enum):
    SUCCESS = 'SUCCESS'
    WARNING = 'WARNING'
    ERROR = 'ERROR'
    SKIPPED = 'SKIPPED'

@dataclass
class ImportRow:
    row_number: int
    status: ImportRowStatus
    raw_data: Dict[str, Any]
    normalized_data: Optional[Dict[str, Any]] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    sheet_name: str = ''
    canonical_data: Optional[CanonicalTransaction] = None

    def __post_init__(self):
        if self.canonical_data is None and self.normalized_data is not None:
            self.canonical_data = CanonicalTransaction.from_dict(self.normalized_data)
        elif self.normalized_data is None and self.canonical_data is not None:
            self.normalized_data = self.canonical_data.to_dict()

    @property
    def canonical(self) -> Optional[CanonicalTransaction]:
        return self.canonical_data

@dataclass
class ImportResult:
    platform: str
    file_name: str
    total_rows: int = 0
    success_rows: int = 0
    warning_rows: int = 0
    error_rows: int = 0
    skipped_rows: int = 0
    rows: Iterable[ImportRow] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    detected_period: Optional[str] = None
    detected_gstin: Optional[str] = None
    sheet_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

class PlatformAdapter(ABC):
    """Base contract for all marketplace platform adapters.

    An adapter is responsible ONLY for:
    1. Platform identity (PLATFORM_NAME)
    2. Supported file types and detection (detect)
    3. Header and schema recognition (validate)
    4. Parsing rows into ImportResult / ImportRow
    5. Producing canonical transaction representations (normalize / CanonicalTransaction)
    6. Reporting format/row errors and warnings

    Adapters MUST NOT:
    - Implement persistence (database writes)
    - Directly manipulate ImportHistory records
    - Duplicate duplicate-detection logic
    """
    
    PLATFORM_NAME: str = ''
    SUPPORTED_FILE_TYPES: list = []
    EXPECTED_HEADERS: list = []
    INSTRUCTIONS: str = ''
    TEMPLATE_URL: str = ''
    
    @abstractmethod
    def detect(self, workbook_or_data, file_name: str) -> bool:
        """Detect if this adapter can handle the given file."""
        pass

    @abstractmethod
    def validate(self, workbook_or_data) -> Tuple[bool, List[str]]:
        """Validate if the file contains the necessary structure and required headers."""
        pass

    @abstractmethod
    def parse(self, workbook_or_data, file_name: str = '', stream: bool = False) -> ImportResult:
        """Parse the entire file into an ImportResult with canonical rows."""
        pass

    @abstractmethod
    def normalize(self, raw_row: Dict) -> Dict:
        """Normalize a single raw row into a generic canonical dictionary."""
        pass

    @abstractmethod
    def map_taxes(self, raw_row: Dict) -> Dict:
        """Map tax fields from raw row."""
        pass

    @abstractmethod
    def map_customer(self, raw_row: Dict) -> Dict:
        """Map customer details."""
        pass

    @abstractmethod
    def map_invoice(self, raw_row: Dict) -> Dict:
        """Map invoice metadata."""
        pass

    @abstractmethod
    def map_items(self, raw_row: Dict) -> Dict:
        """Map item-level details."""
        pass

    @abstractmethod
    def map_returns(self, raw_row: Dict) -> Dict:
        """Map return/cancellation fields."""
        pass

    @abstractmethod
    def map_notes(self, raw_row: Dict) -> Dict:
        """Map credit/debit note fields."""
        pass

    def get_import_errors(self) -> List[str]:
        """Return general errors if any."""
        return []
