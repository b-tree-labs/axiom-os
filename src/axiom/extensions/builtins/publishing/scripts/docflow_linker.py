#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

import csv
import json
from pathlib import Path

STATE_PATH = Path('.neut/docflow/state.json')
CSV_PATH = Path('tools/publisher/data/doc_links.csv')  # moved into tools/publisher/data


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {'documents': {}}


def save_state(s):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2))


def main():
    state = load_state()
    if not CSV_PATH.exists():
        print(f"❌ CSV not found: {CSV_PATH}")
        return 1

    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            doc_id = row['doc_id'].strip()
            local_path = row['local_path'].strip()
            url = row.get('sharepoint_url', '').strip()
            doc_type = row.get('doc_type', '').strip() or 'prd'
            workflow = row.get('workflow_state', '').strip() or 'drafting'

            entry = state.setdefault('documents', {}).setdefault(doc_id, {})
            entry['local_path'] = local_path
            if url:
                entry['sharepoint_url'] = url
                entry['provider'] = 'sharepoint'
            else:
                entry.pop('sharepoint_url', None)
                entry['provider'] = entry.get('provider', 'local')
            entry['doc_type'] = doc_type
            entry['workflow_state'] = workflow

            print(f"Linked {doc_id} -> {url or '(local only)'} [{doc_type} | {workflow}]")
    save_state(state)
    print('✓ Registry updated.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
