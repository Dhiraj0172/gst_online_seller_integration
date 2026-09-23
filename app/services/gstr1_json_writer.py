import json
from decimal import Decimal
from datetime import datetime
from typing import Dict, Any, List

class GSTR1JsonEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return round(float(obj), 2)
        if isinstance(obj, datetime):
            return obj.strftime("%d-%m-%Y")
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return super().default(obj)

class GSTR1JsonWriter:
    # All official GSTN GSTR-1 tables, including amendments
    TABLES = [
        "b2b", "b2ba", "b2cl", "b2cla", "b2cs", "b2csa",
        "cdnr", "cdnra", "cdnur", "cdnura",
        "exp", "expa",
        "nil", "hsn", "hsnb2c", "doc_issue"
    ]

    @staticmethod
    def generate_json(data: Dict[str, Any], output_path: str) -> str:
        """
        Generates the GSTR-1 JSON file based on the provided data dictionary.
        """
        json_data = {}

        # Top-level fields
        json_data["gstin"] = data.get("gstin", "")
        json_data["fp"] = data.get("fp", "")

        if "gt" in data:
            json_data["gt"] = data["gt"]
        if "cur_gt" in data:
            json_data["cur_gt"] = data["cur_gt"]

        # Tables - include all official GSTN tables plus amendments
        for table in GSTR1JsonWriter.TABLES:
            if table in data and data[table]:
                json_data[table] = data[table]

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(
                json_data,
                f,
                cls=GSTR1JsonEncoder,
                indent=2,
                ensure_ascii=False,
                sort_keys=True
            )

        return output_path

def generate_gstr1_json(data: Dict[str, Any], output_path: str) -> str:
    """Convenience function to generate GSTR-1 JSON."""
    return GSTR1JsonWriter.generate_json(data, output_path)
