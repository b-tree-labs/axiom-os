#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Extract Word document comments from a .docx file."""
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def extract_comments(docx_path):
    """Extract and display comments from a Word document."""
    docx_path = Path(docx_path)
    if not docx_path.exists():
        print(f"File not found: {docx_path}")
        return 1

    try:
        with zipfile.ZipFile(docx_path, 'r') as z:
            if 'word/comments.xml' not in z.namelist():
                print("No comments found in this document.")
                return 0

            comments_xml = z.read('word/comments.xml')
    except Exception as e:
        print(f"Error reading {docx_path}: {e}")
        return 1

    try:
        root = ET.fromstring(comments_xml)
    except Exception as e:
        print(f"Error parsing comments.xml: {e}")
        return 1

    # Define namespace
    ns = {
        'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
        'w14': 'http://schemas.microsoft.com/office/word/2010/wordml',
    }

    # Find all comments
    comments = root.findall('.//w:comment', ns)
    print(f"Found {len(comments)} comment(s) in {docx_path.name}\n")
    print("=" * 80)

    for i, comment in enumerate(comments, 1):
        author = comment.get(f'{{{ns["w"]}}}author', 'Unknown')
        initials = comment.get(f'{{{ns["w"]}}}initials', '')
        date_str = comment.get(f'{{{ns["w"]}}}date', '')
        comment_id = comment.get(f'{{{ns["w"]}}}id', '')

        # Extract all text from the comment
        text_parts = []
        for t_elem in comment.findall(f'.//{{{ns["w"]}}}t', ns):
            if t_elem.text:
                text_parts.append(t_elem.text)
        text = ''.join(text_parts).strip()

        print(f"\n[Comment {i}]")
        print(f"  ID: {comment_id}")
        print(f"  Author: {author} ({initials})")
        print(f"  Date: {date_str}")
        print(f"  Text: {text}")

    print("\n" + "=" * 80)
    return 0

if __name__ == '__main__':
    docx = sys.argv[1] if len(sys.argv) > 1 else '.neut/downloads/medical-isotope-prd [DRAFT].docx'
    sys.exit(extract_comments(docx))
