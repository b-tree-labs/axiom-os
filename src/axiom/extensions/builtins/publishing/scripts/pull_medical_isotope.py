#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pull medical-isotope-prd from SharePoint, convert with pandoc --extract-media, and show diffs.

Run interactively; device-code auth will prompt in terminal.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

from ..providers.sharepoint import SharePointProvider


def main():
    state_path = Path('.neut/docflow/state.json')
    if not state_path.exists():
        print('Registry not found at .neut/docflow/state.json')
        return 1
    state = json.loads(state_path.read_text())
    doc = state['documents'].get('medical-isotope-prd')
    if not doc:
        print('medical-isotope-prd not in registry')
        return 1
    url = doc.get('sharepoint_url')
    if not url:
        print('No sharepoint_url for medical-isotope-prd')
        return 1

    p = SharePointProvider()
    downloads = Path('.neut/downloads')
    downloads.mkdir(parents=True, exist_ok=True)
    docx = downloads / 'medical-isotope-prd.docx'
    print('Downloading to', docx)
    p._download_via_graph(url, docx)

    pandoc = shutil.which('pandoc')
    md = Path('docs/prds/prd-medical-isotope.md')
    media = Path('docs/prds/media/prd_medical-isotope')
    if not pandoc:
        print('pandoc not found; saved .docx at', docx)
        return 0

    media.mkdir(parents=True, exist_ok=True)
    cmd = [pandoc, '-f', 'docx', '-t', 'gfm', '--extract-media=' + str(media), '-o', str(md), str(docx)]
    print('Running:', ' '.join(cmd))
    subprocess.run(cmd, check=True)
    print('Converted to', md)

    print('\n--- git diff for Markdown ---')
    subprocess.run(['git', '--no-pager', 'diff', '--', str(md)])
    print('\n--- git status for media files ---')
    subprocess.run(['git', 'status', '--porcelain', str(media)])
    return 0


if __name__ == '__main__':
    sys.exit(main())
