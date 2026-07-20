#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path

STATE_PATH = Path('.neut/docflow/state.json')


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {'documents': {}}


def main():
    state = load_state()
    docs = state.get('documents', {})
    print('\n🔍 Dry-run sync actions:')
    print('-' * 60)
    for doc_id, doc in docs.items():
        local = doc.get('local_path')
        remote = doc.get('sharepoint_url')
        provider = doc.get('provider')
        if provider == 'sharepoint' and remote:
            print(f"  Would pull remote -> local: {doc_id}\n    Remote: {remote}\n    Local:  {local}\n")
    print('Done.')


if __name__ == '__main__':
    main()
