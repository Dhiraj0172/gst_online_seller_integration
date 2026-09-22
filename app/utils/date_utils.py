from datetime import datetime, date
import re

def parse_date(date_str, formats=None) -> date:
    """Parse string to date using multiple formats."""
    if not date_str:
        return None
    if isinstance(date_str, date) and not isinstance(date_str, datetime):
        return date_str
    if isinstance(date_str, datetime):
        return date_str.date()
        
    if formats is None:
        formats = [
            '%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d', '%d-%b-%Y', '%d/%b/%Y',
            '%d-%m-%y', '%d/%m/%y', '%Y/%m/%d'
        ]
        
    date_str = str(date_str).strip()
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            pass
            
    return None

def format_date_gst(d: date) -> str:
    """Format date to DD-MM-YYYY."""
    if d is None:
        return ""
    return d.strftime('%d-%m-%Y')

def format_date_json(d: date) -> str:
    """Format date for JSON output (DD-MM-YYYY)."""
    return format_date_gst(d)

def get_return_period(month: int, year: int) -> str:
    """Get MMYYYY format from month and year."""
    return f"{month:02d}{year}"

def get_financial_year(d: date) -> str:
    """Get financial year like 2024-25 from date."""
    if not d:
        return ""
    year = d.year
    if d.month < 4:
        return f"{year-1}-{str(year)[-2:]}"
    return f"{year}-{str(year+1)[-2:]}"

def validate_return_period(period: str) -> tuple[bool, str]:
    """Validate MMYYYY return period string."""
    if not period or not isinstance(period, str):
        return False, "Return period must be a string"
    if not re.match(r'^(0[1-9]|1[0-2])[0-9]{4}$', period):
        return False, "Invalid return period format. Must be MMYYYY."
    return True, "Valid return period"

def get_month_year_from_period(period: str) -> tuple[int, int]:
    """Extract month and year integers from MMYYYY string."""
    if not period or len(period) != 6:
        raise ValueError("Invalid period format, expected MMYYYY")
    return int(period[:2]), int(period[2:])

def is_date_in_period(d: date, period: str) -> bool:
    """Check if date falls within given MMYYYY period."""
    if not d or not period:
        return False
    try:
        month, year = get_month_year_from_period(period)
        return d.month == month and d.year == year
    except ValueError:
        return False
