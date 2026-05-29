#!/usr/bin/env python3
"""Inspect IRS Form 8949 PDF fields using pdfrw."""

import os
import pdfrw

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'f8949.pdf')


def decode_field_name(name):
    """Decode a PDF field name, handling UTF-16 encoding."""
    if not name:
        return ''
    s = str(name)
    # Remove leading /
    if s.startswith('/'):
        s = s[1:]
    # Handle UTF-16 BOM encoded names
    if s.startswith('\xfe\xff'):
        try:
            raw = s.encode('latin-1')
            return raw.decode('utf-16')
        except:
            pass
    return s


def main():
    template = pdfrw.PdfReader(TEMPLATE_PATH)

    print(f"Number of pages: {len(template.pages)}")
    print()

    all_fields = []

    for page_num, page in enumerate(template.pages, 1):
        print(f"=== PAGE {page_num} ===")
        annots = page.get('/Annots')
        if not annots:
            print("  No annotations found")
            continue

        print(f"  Number of annotations: {len(annots)}")
        print()

        fields = []
        for annot in annots:
            if annot.get('/Subtype') != '/Widget':
                continue

            field_name_raw = annot.get('/T')
            field_name = decode_field_name(field_name_raw)
            field_type = annot['/FT'] if '/FT' in annot else ''
            rect = annot.get('/Rect')
            value = annot['/V'] if '/V' in annot else ''

            # Parse rect coordinates
            if rect:
                try:
                    coords = [float(x) for x in rect]
                    x1, y1, x2, y2 = coords
                except:
                    x1 = y1 = x2 = y2 = 0
            else:
                x1 = y1 = x2 = y2 = 0

            fields.append({
                'name': field_name,
                'raw_name': str(field_name_raw),
                'type': str(field_type),
                'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
                'value': str(value),
                'page': page_num,
                'annot': annot,
            })

        # Sort by y position (top to bottom = high y to low y), then x position
        fields.sort(key=lambda f: (-f['y1'], f['x1']))

        for f in fields:
            print(f"  {f['name']:30s}  type={f['type']:5s}  rect=({f['x1']:.0f}, {f['y1']:.0f}, {f['x2']:.0f}, {f['y2']:.0f})")
            all_fields.append(f)

    print("\n\n=== FIELD POSITION ANALYSIS ===")

    for page_num in [1, 2]:
        page_fields = [f for f in all_fields if f['page'] == page_num]
        if not page_fields:
            continue

        print(f"\n--- Page {page_num} ---")

        # Separate text fields and checkboxes
        text_fields = [f for f in page_fields if f['type'] == '/Tx']
        check_fields = [f for f in page_fields if f['type'] == '/Btn']

        print(f"\nCheckboxes ({len(check_fields)}):")
        for f in check_fields:
            print(f"  {f['name']:30s}  y={f['y1']:.0f}  x={f['x1']:.0f}")

        print(f"\nText fields ({len(text_fields)}):")

        # Group text fields by y-position (same row = similar y)
        # Sort by y descending
        text_fields.sort(key=lambda f: (-f['y1'], f['x1']))

        # Group into rows (fields within 5 units of y are same row)
        rows = []
        current_row = []
        current_y = None
        for f in text_fields:
            if current_y is None or abs(f['y1'] - current_y) > 5:
                if current_row:
                    rows.append(current_row)
                current_row = [f]
                current_y = f['y1']
            else:
                current_row.append(f)
        if current_row:
            rows.append(current_row)

        for row_idx, row in enumerate(rows):
            row.sort(key=lambda f: f['x1'])
            y_val = row[0]['y1']
            fields_str = '  '.join(f"{f['name']}(x={f['x1']:.0f})" for f in row)
            print(f"  Row {row_idx}: y={y_val:.0f}  {fields_str}")

    # Print a clean mapping
    print("\n\n=== CLEAN FIELD MAPPING ===")
    print("Column layout for data rows:")
    print("  (a) Description of property")
    print("  (b) Date acquired")
    print("  (c) Date sold or disposed of")
    print("  (d) Proceeds")
    print("  (e) Cost or other basis")
    print("  (f) Code (adjustment)")
    print("  (g) Adjustment amount")
    print("  (h) Gain or (loss)")

    for page_num in [1, 2]:
        page_fields = [f for f in all_fields if f['page'] == page_num]
        text_fields = [f for f in page_fields if f['type'] == '/Tx']
        text_fields.sort(key=lambda f: (-f['y1'], f['x1']))

        # Group into rows
        rows = []
        current_row = []
        current_y = None
        for f in text_fields:
            if current_y is None or abs(f['y1'] - current_y) > 5:
                if current_row:
                    rows.append(current_row)
                current_row = [f]
                current_y = f['y1']
            else:
                current_row.append(f)
        if current_row:
            rows.append(current_row)

        print(f"\nPage {page_num}:")
        for row_idx, row in enumerate(rows):
            row.sort(key=lambda f: f['x1'])
            num_fields = len(row)
            names = [f['name'] for f in row]
            if num_fields == 1:
                # Header field (name or SSN)
                print(f"  Header: {names[0]}")
            elif num_fields >= 5:
                # Data row or totals
                col_labels = ['(a)', '(b)', '(c)', '(d)', '(e)', '(f)', '(g)', '(h)']
                mapping = []
                for i, n in enumerate(names):
                    label = col_labels[i] if i < len(col_labels) else f'(?{i})'
                    mapping.append(f"{label}={n}")
                print(f"  Data row: {', '.join(mapping)}")
            else:
                print(f"  Other ({num_fields} fields): {', '.join(names)}")


if __name__ == '__main__':
    main()
