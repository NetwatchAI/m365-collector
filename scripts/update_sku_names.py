"""Regenerate src/m365c/sku_names.json (skuId -> product display name).

Source: Microsoft's "Product names and service plan identifiers for licensing" CSV,
https://learn.microsoft.com/entra/identity/users/licensing-service-plan-reference
Download it, then run:

    python scripts/update_sku_names.py <path/to/licensing.csv>
"""

import csv
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "src" / "m365c" / "sku_names.json"

names: dict[str, str] = {}
with open(sys.argv[1], encoding="utf-8-sig", newline="") as fh:
    for row in csv.DictReader(fh):
        guid = row["GUID"].strip().lower()
        name = " ".join(row["Product_Display_Name"].split())
        if guid and name:
            names.setdefault(guid, name)

with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
    json.dump(dict(sorted(names.items())), fh, indent=0, ensure_ascii=False)
    fh.write("\n")
print(len(names), "SKUs")
