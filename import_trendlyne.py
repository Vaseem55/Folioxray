"""
Run this script to import Trendlyne CSV files into fund_holdings_db.py

Usage:
  python import_trendlyne.py "C:\Users\vasee\Downloads\ICICI_Prudential_Large_Cap_Gr.csv" "icici prudential large cap fund"
  python import_trendlyne.py "C:\Users\vasee\Downloads\Axis_Bluechip_Direct.csv" "axis bluechip fund"
"""

import csv
import sys
import json
import os

def parse_trendlyne_csv(filepath: str, min_weight: float = 0.1) -> list:
    holdings = []
    with open(filepath, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Find the % column (header contains "% of Total Holding")
            pct_key = next((k for k in row if "%" in k), None)
            name_key = next((k for k in row if "Invested In" in k or k == "Invested In"), None)
            if not pct_key or not name_key:
                continue
            name = row[name_key].strip()
            pct_str = row[pct_key].strip()
            if not name or not pct_str:
                continue
            try:
                pct = float(pct_str)
            except:
                continue
            if pct >= min_weight and name:
                holdings.append({"stock_name": name, "weight_percent": round(pct, 2)})

    holdings.sort(key=lambda x: x["weight_percent"], reverse=True)
    return holdings[:20]  # top 20 holdings


def update_db(fund_key: str, holdings: list):
    db_path = os.path.join(os.path.dirname(__file__), "fund_holdings_db.py")
    with open(db_path, encoding="utf-8") as f:
        content = f.read()

    # Format the holdings as Python list
    lines = ['    "' + fund_key + '": [']
    for h in holdings:
        lines.append(f'        {{"stock_name": "{h["stock_name"]}", "weight_percent": {h["weight_percent"]}}},')
    lines.append("    ],")
    new_block = "\n".join(lines)

    # Check if fund already exists in DB
    if f'"{fund_key}":' in content:
        # Replace existing entry
        import re
        pattern = rf'"{re.escape(fund_key)}":\s*\[.*?\],'
        new_content = re.sub(pattern, new_block, content, flags=re.DOTALL)
        if new_content == content:
            print(f"WARNING: Could not replace existing entry for '{fund_key}'")
            return False
        with open(db_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"Updated existing entry: '{fund_key}' ({len(holdings)} holdings)")
    else:
        # Insert before the closing brace of FUND_HOLDINGS
        insert_marker = "\n}"
        insert_pos = content.rfind("\n    \"quant flexi cap fund\"")
        if insert_pos < 0:
            insert_pos = content.rfind("}")
            content = content[:insert_pos] + "    " + new_block + "\n" + content[insert_pos:]
        else:
            # Insert after the quant entry
            end_of_quant = content.find("],\n", insert_pos) + 3
            content = content[:end_of_quant] + "    " + new_block + "\n" + content[end_of_quant:]
        with open(db_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Added new entry: '{fund_key}' ({len(holdings)} holdings)")

    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python import_trendlyne.py <csv_path> <fund_key>")
        print('Example: python import_trendlyne.py "ICICI_Large_Cap.csv" "icici prudential large cap fund"')
        sys.exit(1)

    csv_path = sys.argv[1]
    fund_key = sys.argv[2].lower().strip()

    print(f"Parsing: {csv_path}")
    holdings = parse_trendlyne_csv(csv_path)

    if not holdings:
        print("ERROR: No holdings found in CSV")
        sys.exit(1)

    print(f"Found {len(holdings)} holdings:")
    for h in holdings[:5]:
        print(f"  {h['stock_name']}: {h['weight_percent']}%")
    print("  ...")

    update_db(fund_key, holdings)
    print("\nDone! Commit and push to Railway to go live.")
