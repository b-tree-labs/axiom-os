#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

STATE_PATH = Path('.neut/docflow/state.json')
DOCS_DIR = Path('docs')


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {'documents': {}}


def main():
    state = load_state()
    tracked = {v.get('local_path') for v in state.get('documents', {}).values()}

    print('\n📄 Untracked local documents:')
    print('-' * 60)
    found = False
    for md in DOCS_DIR.rglob('*.md'):
        rel = str(md)
        if rel not in tracked:
            print('  ', rel)
            found = True
    if not found:
        print('  (none)')

    print('\n🔗 Documents without SharePoint link:')
    print('-' * 60)
    found = False
    for doc_id, doc in state.get('documents', {}).items():
        if not doc.get('sharepoint_url'):
            print('  ', doc_id, doc.get('local_path'))
            found = True
    if not found:
        print('  (none)')


if __name__ == '__main__':
    main()
