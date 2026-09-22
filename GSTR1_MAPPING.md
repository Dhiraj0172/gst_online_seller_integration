# GSTR-1 Mapping Reference

## Internal Transaction Model → GSTR-1 Mapping

### B2B (Business to Business)

**Excel Sheet: `b2b`**

| Excel Column | Internal Field | JSON Path | Notes |
|-------------|----------------|-----------|-------|
| GSTIN/UIN of Recipient | customer_gstin | b2b[].ctin | Must be valid 15-char GSTIN |
| Invoice Number | invoice_number | b2b[].inv[].inum | |
| Invoice date | invoice_date | b2b[].inv[].idt | DD-MM-YYYY |
| Invoice Value | invoice_value | b2b[].inv[].val | Total including tax |
| Place Of Supply | place_of_supply | b2b[].inv[].pos | 2-digit state code |
| Reverse Charge | reverse_charge | b2b[].inv[].rchrg | Y or N |
| Invoice Type | invoice_type | b2b[].inv[].inv_typ | R/SEZ WP/SEZ WOP/DE |
| Rate | tax_rate | b2b[].inv[].itms[].itm_det.rt | GST rate |
| Taxable Value | taxable_value | b2b[].inv[].itms[].itm_det.txval | |
| Cess Amount | cess_amount | b2b[].inv[].itms[].itm_det.csamt | |

**JSON also includes (computed):**
- `camt` (CGST amount) = taxable_value × rate / 200
- `samt` (SGST amount) = taxable_value × rate / 200
- `iamt` (IGST amount) = taxable_value × rate / 100 (inter-state only)

### B2CS (B2C Small)

**Excel Sheet: `b2cs`**

| Excel Column | Internal Field | JSON Path |
|-------------|----------------|-----------|
| Type | "OE" (Original) / "A" (Amendment) | b2cs[].typ |
| Place Of Supply | place_of_supply (aggregated) | b2cs[].pos |
| Rate | tax_rate | b2cs[].rt |
| Taxable Value | SUM(taxable_value) per state+rate | b2cs[].txval |
| Cess Amount | SUM(cess_amount) | b2cs[].csamt |

**Aggregation:** B2CS is aggregated by (state_code + tax_rate), not invoice-level.

**JSON also includes:**
- `sply_ty`: "INTRA" or "INTER"
- `camt`, `samt`, `iamt`

### B2CL (B2C Large) — Pre-August 2024 Only

**Applicable when:** Return period < 082024 AND inter-state B2C AND invoice_value > ₹2,50,000

| Excel Column | JSON Path |
|-------------|-----------|
| Invoice Number | b2cl[].inv[].inum |
| Invoice date | b2cl[].inv[].idt |
| Invoice Value | b2cl[].inv[].val |
| Place Of Supply | b2cl[].pos |
| Rate | b2cl[].inv[].itms[].itm_det.rt |
| Taxable Value | b2cl[].inv[].itms[].itm_det.txval |

### CDNR (Credit/Debit Notes — Registered)

| Excel Column | JSON Path |
|-------------|-----------|
| GSTIN/UIN of Recipient | cdnr[].ctin |
| Note Number | cdnr[].nt[].nt_num |
| Note date | cdnr[].nt[].nt_dt |
| Original Invoice Number | cdnr[].nt[].p_gst (linked) |
| Note Value | cdnr[].nt[].val |
| Note Type | cdnr[].nt[].ntty | C=Credit, D=Debit |

### NIL / Exempt / Non-GST

**Excel Sheet: `nil`**

```
Description                           | Nil Rated | Exempted | Non-GST
Inter-State to registered             |     X     |    X     |    X
Intra-State to registered             |     X     |    X     |    X
Inter-State to unregistered           |     X     |    X     |    X
Intra-State to unregistered           |     X     |    X     |    X
```

**JSON:**
```json
{"inv": [
  {"sply_ty": "INTRB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
  {"sply_ty": "INTRAB2B", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
  {"sply_ty": "INTRB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0},
  {"sply_ty": "INTRAB2C", "nil_amt": 0, "expt_amt": 0, "ngsup_amt": 0}
]}
```

### HSN Summary

**Excel Sheet: `hsn` (B2B) and `hsnb2c` (B2C, from May 2025)**

| Excel Column | JSON Path |
|-------------|-----------|
| HSN | hsn.data[].hsn_sc |
| Description | hsn.data[].desc |
| UQC | hsn.data[].uqc |
| Total Quantity | hsn.data[].qty |
| Total Value | hsn.data[].val |
| Taxable Value | hsn.data[].txval |
| Integrated Tax | hsn.data[].iamt |
| Central Tax | hsn.data[].camt |
| State/UT Tax | hsn.data[].samt |
| Cess Amount | hsn.data[].csamt |

## Period-Specific Rules

| Return Period | B2CL | HSN Mode | Schema Version |
|--------------|------|----------|----------------|
| Before 082024 | Applicable (threshold ₹2.5L) | Combined | 1.0 |
| 082024 - 042025 | Eliminated | Combined | 2.0 |
| 052025 onwards | Eliminated | Separate B2B/B2C | 3.0 |

## Date Format

All dates in GSTR-1: **DD-MM-YYYY** (e.g., 15-01-2025)

## Amount Precision

All amounts: 2 decimal places, `ROUND_HALF_UP`

## Validation Rules

1. **B2B**: recipient GSTIN required, must be valid format
2. **B2CS**: no recipient GSTIN, aggregated by state+rate
3. **CDNR**: note type (C/D) required, recipient GSTIN required
4. **HSN**: code must be 2/4/6/8 digits
5. **Tax integrity**: taxable + CGST + SGST + IGST + cess = invoice value (±₹0.02 tolerance)
6. **Intra-state**: IGST must be 0, CGST+SGST > 0
7. **Inter-state**: CGST+SGST must be 0, IGST > 0
