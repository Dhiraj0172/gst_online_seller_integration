import os
from typing import List, Dict, Any
from app.validators.transaction_validator import ValidationResult

def validate_upload(file_path: str, max_size_mb: int = 10) -> ValidationResult:
    result = ValidationResult()
    
    if not file_path or not os.path.exists(file_path):
        result.add_error("file", file_path, "File does not exist")
        return result
        
    size_bytes = os.path.getsize(file_path)
    if size_bytes == 0:
        result.add_error("file_size", 0, "File is empty")
    elif size_bytes > max_size_mb * 1024 * 1024:
        result.add_error("file_size", size_bytes, f"File size exceeds {max_size_mb}MB limit")
        
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in ('.xlsx', '.csv'):
        result.add_error("file_extension", ext, "Only .xlsx and .csv files are supported")
        
    return result

def validate_workbook(wb_or_df) -> ValidationResult:
    result = ValidationResult()
    if wb_or_df is None:
        result.add_error("workbook", None, "Workbook or DataFrame is empty")
        return result
    # In a real impl, we would check openpyxl workbook structure here
    # For now, we assume structural checks are delegated to headers/rows validation
    return result

def validate_headers(headers: List[str], expected_headers: List[str]) -> ValidationResult:
    result = ValidationResult()
    missing_headers = []
    
    h_lower = [h.lower().strip() for h in headers if h]
    
    for expected in expected_headers:
        if expected.lower().strip() not in h_lower:
            missing_headers.append(expected)
            
    if missing_headers:
        result.add_error("headers", missing_headers, f"Missing required headers: {', '.join(missing_headers)}")
        
    return result

def detect_duplicates(rows: List[Dict[str, Any]], key_fields: List[str]) -> List[Dict[str, Any]]:
    duplicates = []
    seen = set()
    
    for i, row in enumerate(rows):
        key = tuple(row.get(k) for k in key_fields)
        if key in seen:
            duplicates.append({
                "row_index": i,
                "key": key,
                "message": f"Duplicate found based on {key_fields}"
            })
        else:
            seen.add(key)
            
    return duplicates
