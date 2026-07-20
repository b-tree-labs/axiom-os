# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HTTP ingestion server for Axiom Signal (signals extension).

Dead-simple way to get files into the inbox from any device on the LAN.
Uses only stdlib (http.server) — zero external dependencies for basic operation.

Endpoints:
    POST /upload    Upload a file (auto-routed by extension, optionally processed)
    POST /note      Submit a text note (becomes inbox/raw/note_TIMESTAMP.md)
    GET  /status    Returns inbox counts as JSON
    GET  /inbox     Returns all items with processing status
    GET  /          Minimal HTML upload page (drag-and-drop, text note box)

File routing by extension:
    .m4a, .mp3, .wav, .ogg, .webm  →  inbox/raw/voice/
    .vtt, .srt                       →  inbox/raw/teams/
    .md, .txt                        →  inbox/raw/
    everything else                  →  inbox/raw/other/

Usage:
    axi signal serve [--port 8765] [--host 0.0.0.0] [--process] [--webhook URL]

Options:
    --process       Auto-transcribe voice memos on upload (requires whisper)
    --webhook URL   POST notification to URL when items are processed
"""

from __future__ import annotations

import json
import re
import threading
import urllib.request
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

# Resolve inbox path relative to tools/agents/
from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.branding import get_branding as _get_branding

_RUNTIME_DIR = _REPO_ROOT / "runtime"
INBOX_RAW = _RUNTIME_DIR / "inbox" / "raw"
INBOX_PROCESSED = _RUNTIME_DIR / "inbox" / "processed"

# Extension → subdirectory routing
ROUTE_MAP: dict[str, str] = {
    ".m4a": "voice",
    ".mp3": "voice",
    ".wav": "voice",
    ".ogg": "voice",
    ".webm": "voice",
    ".vtt": "teams",
    ".srt": "teams",
    ".docx": "teams",   # Teams transcript export
    ".md": "",          # root of inbox/raw
    ".txt": "",
}

SETUP_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>" + _get_branding().product_name + " — Voice Memo Upload</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; max-width: 500px; margin: 0 auto; padding: 1.5rem 1rem;
         color: #1a1a1a; line-height: 1.5; background: linear-gradient(180deg, #0EA5E9 0%%, #0284C7 100%%);
         min-height: 100vh; }
  .container { background: white; border-radius: 20px; padding: 2rem 1.5rem; box-shadow: 0 10px 40px rgba(0,0,0,0.2); }

  /* Logo/Brand */
  .brand { text-align: center; margin-bottom: 1.5rem; }
  .logo { height: 64px; width: auto; margin-bottom: 0.5rem; }
  .brand h1 { font-size: 1.5rem; font-weight: 700; color: #1a1a1a; margin-bottom: 0.25rem; }
  .brand .tagline { color: #666; font-size: 0.95rem; }

  /* Upload section */
  .upload-section { background: #f0f9ff; border: 2px solid #0EA5E9; border-radius: 16px; padding: 1.5rem;
                    margin-bottom: 1.5rem; text-align: center; }
  .upload-section h2 { font-size: 1.1rem; color: #0369a1; margin-bottom: 1rem; }
  .upload-section form { width: 100%%; }

  .file-input-wrapper { margin-bottom: 1rem; width: 100%%; }
  .file-input-wrapper input[type="file"] { display: none; }
  .file-btn { display: block; width: 100%%; padding: 1rem; background: #0EA5E9; color: white;
              border-radius: 12px; font-size: 1.1rem; font-weight: 600; cursor: pointer;
              -webkit-tap-highlight-color: transparent; box-sizing: border-box; text-align: center; }
  .file-btn:active { background: #0284C7; }
  .file-name { font-size: 0.9rem; color: #666; margin-top: 0.5rem; min-height: 1.5em; }

  .upload-btn { width: 100%%; padding: 1rem; background: #22c55e; color: white;
                border: none; border-radius: 12px; font-size: 1.1rem; font-weight: 600; cursor: pointer;
                margin-top: 0.75rem; box-sizing: border-box; text-align: center;
                -webkit-appearance: none; appearance: none; display: none; }
  .upload-btn:disabled { background: #94a3b8; cursor: not-allowed; }
  .upload-btn.show { display: block; width: 100%%; }

  .status-msg { margin-top: 1rem; padding: 0.75rem; border-radius: 8px; font-size: 0.9rem; display: none; }
  .status-msg.success { display: block; background: #dcfce7; color: #166534; border: 1px solid #22c55e; }
  .status-msg.error { display: block; background: #fee2e2; color: #991b1b; border: 1px solid #ef4444; }

  /* Instructions */
  .instructions { background: #f8f9fa; border-radius: 12px; padding: 1.25rem; margin-bottom: 1rem; }
  .instructions h3 { font-size: 1rem; color: #1a1a1a; margin-bottom: 0.75rem; }
  .step { display: flex; gap: 0.75rem; margin-bottom: 0.5rem; align-items: center; }
  .step-num { background: #0EA5E9; color: white; width: 24px; height: 24px; border-radius: 50%%;
              display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.8rem; flex-shrink: 0; }
  .step-text { font-size: 0.9rem; color: #4a5568; }

  /* Bookmark tip */
  .tip { background: #fef3c7; border: 1px solid #f59e0b; border-radius: 12px; padding: 1rem; font-size: 0.85rem; color: #92400e; }
  .tip strong { color: #78350f; }

  /* Footer */
  .footer { text-align: center; margin-top: 1.5rem; font-size: 0.8rem; color: #666; display: flex; align-items: center; justify-content: center; gap: 0.5rem; }
  .footer img { height: 24px; width: auto; }
</style>
</head>
<body>
<div class="container">
  <div class="brand">
    <img class="logo" src="/logo.png" alt="{mascot_name}">
    <h1>{product_name}</h1>
    <p class="tagline">Voice Memo Upload</p>
  </div>

  <div class="upload-section">
    <h2>Upload Voice Memo</h2>
    <form id="uploadForm" enctype="multipart/form-data">
      <div class="file-input-wrapper">
        <input type="file" id="fileInput" name="file" accept="audio/*,.m4a,.mp3,.wav">
        <label for="fileInput" class="file-btn">Choose Voice Memo</label>
      </div>
      <div class="file-name" id="fileName"></div>
      <button type="submit" class="upload-btn" id="uploadBtn">Upload to {mascot_name}</button>
    </form>
    <div class="status-msg" id="statusMsg"></div>
  </div>

  <div class="instructions">
    <h3>First: Save memo to Files</h3>
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-text">Open <strong>Voice Memos</strong> app</div>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-text">Tap your recording</div>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-text">Tap <strong>•••</strong> (three dots)</div>
    </div>
    <div class="step">
      <div class="step-num">4</div>
      <div class="step-text">Tap <strong>Save to Files</strong></div>
    </div>
    <div class="step">
      <div class="step-num">5</div>
      <div class="step-text">Choose <strong>iCloud Drive</strong> → <strong>Voice Memos</strong></div>
    </div>
    <div class="step">
      <div class="step-num">6</div>
      <div class="step-text">Tap <strong>Save</strong></div>
    </div>
  </div>

  <div class="instructions" style="background: #e0f2fe;">
    <h3>Then: Upload here</h3>
    <div class="step">
      <div class="step-num">7</div>
      <div class="step-text">Tap <strong>Choose Voice Memo</strong> above</div>
    </div>
    <div class="step">
      <div class="step-num">8</div>
      <div class="step-text">Browse → <strong>iCloud Drive</strong> → <strong>Voice Memos</strong></div>
    </div>
    <div class="step">
      <div class="step-num">9</div>
      <div class="step-text">Select your memo → <strong>Upload to {mascot_name}</strong></div>
    </div>
  </div>

  <div class="tip">
    <strong>Bookmark:</strong> Add this page to your home screen. Tap Share → Add to Home Screen.
  </div>

  <div class="footer">
    <img src="/logo.png" alt="{mascot_name}"> Tell it to {mascot_name}.
  </div>
</div>
<script>
var fileInput = document.getElementById('fileInput');
var fileName = document.getElementById('fileName');
var uploadBtn = document.getElementById('uploadBtn');
var uploadForm = document.getElementById('uploadForm');
var statusMsg = document.getElementById('statusMsg');

fileInput.addEventListener('change', function() {
  if (fileInput.files.length > 0) {
    fileName.textContent = fileInput.files[0].name;
    uploadBtn.classList.add('show');
  } else {
    fileName.textContent = '';
    uploadBtn.classList.remove('show');
  }
  statusMsg.className = 'status-msg';
});

uploadForm.addEventListener('submit', function(e) {
  e.preventDefault();
  if (!fileInput.files.length) return;

  uploadBtn.disabled = true;
  uploadBtn.textContent = 'Uploading...';

  var formData = new FormData();
  formData.append('file', fileInput.files[0]);

  fetch('/upload', { method: 'POST', body: formData })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      statusMsg.className = 'status-msg success';
      statusMsg.textContent = '✓ Uploaded: ' + data.path;
      fileInput.value = '';
      fileName.textContent = '';
      uploadBtn.classList.remove('show');
    })
    .catch(function(err) {
      statusMsg.className = 'status-msg error';
      statusMsg.textContent = 'Upload failed: ' + err.message;
    })
    .finally(function() {
      uploadBtn.disabled = false;
      uploadBtn.textContent = 'Upload to {mascot_name}';
    });
});
</script>
</body>
</html>
"""

UPLOAD_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{product_name} — Signal Messenger</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #f5f5f7; color: #1a1a1a; line-height: 1.5; }

  /* Header */
  .header { background: #f5f5f7; padding: 1.5rem 2rem; border-bottom: 1px solid #e0e0e0; }
  .header-inner { max-width: 900px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; }
  .brand { display: flex; align-items: center; gap: 0.75rem; }
  .brand .logo { height: 48px; width: auto; }
  .brand h1 { font-size: 1.25rem; font-weight: 700; color: #1a1a1a; }
  .brand .tagline { font-size: 0.9rem; color: #666; }
  .header-status { text-align: right; font-size: 0.9rem; color: #666; }
  .header-status .count { font-size: 1.5rem; font-weight: 700; display: block; color: #1a1a1a; }

  /* Main */
  .main { max-width: 900px; margin: 0 auto; padding: 2rem; display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
  @media (max-width: 700px) { .main { grid-template-columns: 1fr; } }

  /* Cards */
  .card { background: white; border-radius: 16px; padding: 1.5rem; box-shadow: 0 2px 8px rgba(0,0,0,0.1); border: 1px solid #e0e0e0; }
  .card h2 { font-size: 1rem; color: #1a1a1a; font-weight: 700; margin-bottom: 1rem; }

  /* QR Card */
  .qr-card { grid-column: 1 / -1; display: flex; align-items: center; gap: 2rem; background: white; }
  .qr-card img { width: 140px; height: 140px; border-radius: 12px; background: #f8f9fa; padding: 8px; border: 2px solid #e0e0e0; }
  .qr-info h3 { font-size: 1.2rem; font-weight: 700; margin-bottom: 0.5rem; color: #1a1a1a; }
  .qr-info p { color: #4a5568; font-size: 0.95rem; margin-bottom: 1rem; }
  .qr-info .platform-note { font-size: 0.85rem; color: #718096; margin-top: 0.75rem; }
  .qr-info .setup-btn { display: inline-block; padding: 0.75rem 1.5rem;
                        background: #5a67d8;
                        color: white; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 1rem; }
  .qr-info .setup-btn:hover { background: #4c51bf; }

  /* Drop zone */
  .drop-zone { border: 3px dashed #5a67d8; border-radius: 12px; padding: 2rem 1rem; text-align: center;
               cursor: pointer; transition: all 0.2s; background: #eef2ff; }
  .drop-zone:hover { border-color: #4c51bf; background: #e0e7ff; }
  .drop-zone.active { border-color: #4c51bf; background: #c7d2fe; }
  .drop-zone input { display: none; }
  .drop-zone p { color: #1a1a1a; font-size: 1rem; font-weight: 600; margin-bottom: 0.5rem; }
  .drop-zone .formats { font-size: 0.9rem; color: #4a5568; }

  /* Note input */
  .note-form { display: flex; flex-direction: column; }
  textarea { width: 100%%; height: 100px; border: 2px solid #cbd5e0; border-radius: 10px; padding: 0.75rem;
             font-family: inherit; font-size: 0.95rem; resize: vertical; background: #f7fafc; }
  textarea:focus { outline: none; border-color: #5a67d8; background: white; }
  .send-btn { margin-top: 0.75rem; padding: 0.75rem 1.5rem; width: 100%%;
              background: #5a67d8;
              color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 1rem; font-weight: 600; }
  .send-btn:hover { background: #4c51bf; }
  .send-btn:disabled { background: #a0aec0; cursor: not-allowed; }

  /* Feedback */
  .feedback { margin-top: 1rem; }
  .msg { padding: 0.75rem 1rem; border-radius: 8px; font-size: 0.9rem; }
  .msg.ok { background: #d4edda; color: #155724; }
  .msg.err { background: #f8d7da; color: #721c24; }

  /* Pipeline info */
  .pipeline-card { grid-column: 1 / -1; }
  .pipeline { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }
  .pipeline-step { flex: 1; text-align: center; padding: 1rem 0.5rem; background: #f8f9fa; border-radius: 10px; border: 1px solid #e0e0e0; }
  .step-badge { display: block; width: 44px; height: 44px; margin: 0 auto 0.5rem; background: #5a67d8;
              border-radius: 10px; color: white; font-weight: 700; font-size: 1.25rem; line-height: 44px; text-align: center; }
  .step-badge.count { background: #e53e3e; }
  .step-badge.icon { font-size: 1.5rem; line-height: 44px; }
  .pipeline-step .label { font-size: 0.9rem; color: #1a1a1a; font-weight: 600; }
  .pipeline-arrow { color: #5a67d8; font-size: 1.5rem; font-weight: 700; }
  .pipeline-step.clickable { cursor: pointer; transition: background 0.2s; }
  .pipeline-step.clickable:hover { background: #e8f4fc; border-color: #0EA5E9; }
  .file-list { display: none; margin-top: 1rem; background: #f8f9fa; border-radius: 10px; padding: 1rem; text-align: left; max-height: 200px; overflow-y: auto; }
  .file-list.show { display: block; }
  .file-list h4 { margin: 0 0 0.5rem; font-size: 0.9rem; color: #666; }
  .file-list ul { margin: 0; padding: 0; list-style: none; }
  .file-list li { padding: 0.4rem 0; border-bottom: 1px solid #e0e0e0; font-size: 0.85rem; color: #333; display: flex; justify-content: space-between; }
  .file-list li:last-child { border-bottom: none; }
  .file-list .file-date { color: #888; font-size: 0.75rem; }

  /* Footer */
  .footer { max-width: 900px; margin: 0 auto; padding: 1rem 2rem 2rem; text-align: center; }
  .footer p { font-size: 0.8rem; color: #666; display: inline-flex; align-items: center; gap: 0.5rem; }
  .footer img { height: 20px; width: auto; }
  .footer a { color: #0EA5E9; text-decoration: none; }
</style>
</head>
<body>

<div class="header">
  <div class="header-inner">
    <div class="brand">
      <img class="logo" src="/logo.png" alt="{mascot_name}">
      <div>
        <h1>{product_name}</h1>
        <div class="tagline">Signal Messenger</div>
      </div>
    </div>
    <div class="header-status">
      <span class="count" id="totalCount">-</span>
      items pending
    </div>
  </div>
</div>

<div class="main">

  <!-- QR Code Card -->
  <div class="card qr-card">
    <img src="/qr.svg" alt="QR Code" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
    <div class="qr-fallback" style="display:none;width:140px;height:140px;background:#667eea;border-radius:12px;align-items:center;justify-content:center;color:white;font-size:2rem;font-weight:700;">QR</div>
    <div class="qr-info">
      <h3>Set Up Your Phone</h3>
      <p>Scan this QR code with your camera to install the voice memo shortcut. Takes 30 seconds.</p>
      <a class="setup-btn" href="/setup">Open Setup Page →</a>
      <p class="platform-note">🍏 iPhone: Uses Shortcuts app &nbsp;•&nbsp; 🤖 Android: Use the web page directly</p>
    </div>
  </div>

  <!-- Voice Upload Card -->
  <div class="card">
    <h2>🎙️ Voice Memos</h2>
    <div class="drop-zone" id="voiceDropZone">
      <p>Drop voice memo or click to browse</p>
      <div class="formats">.m4a, .mp3, .wav</div>
      <input type="file" id="voiceFileInput" accept=".m4a,.mp3,.wav,.ogg,.webm,audio/*">
    </div>
    <div class="feedback" id="voiceFeedback"></div>
    <details style="margin-top: 1rem; font-size: 0.85rem; color: #666;">
      <summary style="cursor: pointer; color: #5a67d8;">📱 How to upload from iPhone</summary>
      <ol style="margin: 0.5rem 0 0 1.25rem;">
        <li>Open Voice Memos app</li>
        <li>Tap recording → ••• → Save to Files</li>
        <li>Save to iCloud Drive → Voice Memos</li>
        <li>Tap above to browse to that folder</li>
      </ol>
    </details>
  </div>

  <!-- Teams Upload Card -->
  <div class="card">
    <h2>📺 Teams Transcripts</h2>
    <div class="drop-zone" id="teamsDropZone">
      <p>Drop Teams transcript or click to browse</p>
      <div class="formats">.vtt, .srt, .docx, .txt</div>
      <input type="file" id="teamsFileInput" accept=".vtt,.srt,.docx,.txt">
    </div>
    <div class="feedback" id="teamsFeedback"></div>
    <details style="margin-top: 1rem; font-size: 0.85rem; color: #666;">
      <summary style="cursor: pointer; color: #5a67d8;">💻 How to download from Teams</summary>
      <ol style="margin: 0.5rem 0 0 1.25rem;">
        <li>Open the Teams meeting recording</li>
        <li>Click the <strong>Transcript</strong> tab (right side)</li>
        <li>Click <strong>⋮</strong> → <strong>Download</strong></li>
        <li>Choose .vtt or .docx format</li>
        <li>Drop the file here</li>
      </ol>
    </details>
  </div>

  <!-- Note Card -->
  <div class="card">
    <h2>📝 Quick Note</h2>
    <div class="note-form">
      <textarea id="noteText" placeholder="Type a thought, observation, or decision..."></textarea>
      <button class="send-btn" onclick="submitNote()" id="sendBtn">Send Note</button>
    </div>
    <div class="feedback" id="noteFeedback"></div>
  </div>

  <!-- Pipeline Card -->
  <div class="card pipeline-card">
    <h2>⚡ What Happens Next</h2>
    <div class="pipeline">
      <div class="pipeline-step clickable" id="inboxStep" onclick="toggleFileList()">
        <span class="step-badge count" id="inboxCount">0</span>
        <div class="label">In Sent</div>
      </div>
      <div class="pipeline-arrow">→</div>
      <div class="pipeline-step">
        <span class="step-badge icon">🎙️</span>
        <div class="label">Transcribed</div>
      </div>
      <div class="pipeline-arrow">→</div>
      <div class="pipeline-step">
        <span class="step-badge icon">🔍</span>
        <div class="label">Analyzed</div>
      </div>
      <div class="pipeline-arrow">→</div>
      <div class="pipeline-step">
        <span class="step-badge icon">📋</span>
        <div class="label">Changelog</div>
      </div>
    </div>
    <div class="file-list" id="fileList">
      <h4>Sent Items</h4>
      <ul id="fileListItems"><li>Loading...</li></ul>
    </div>
  </div>

</div>

<div class="footer">
  <p><img src="/logo.png" alt="{mascot_name}"> Tell it to {mascot_name}. • <a href="#">{product_name}</a></p>
</div>

<script>
// Voice upload zone
const voiceDz = document.getElementById('voiceDropZone');
const voiceFi = document.getElementById('voiceFileInput');
const voiceFb = document.getElementById('voiceFeedback');

voiceDz.addEventListener('click', () => voiceFi.click());
voiceDz.addEventListener('dragover', e => { e.preventDefault(); voiceDz.classList.add('active'); });
voiceDz.addEventListener('dragleave', () => voiceDz.classList.remove('active'));
voiceDz.addEventListener('drop', e => {
  e.preventDefault(); voiceDz.classList.remove('active');
  uploadFiles(e.dataTransfer.files, voiceFb);
});
voiceFi.addEventListener('change', () => uploadFiles(voiceFi.files, voiceFb));

// Teams upload zone
const teamsDz = document.getElementById('teamsDropZone');
const teamsFi = document.getElementById('teamsFileInput');
const teamsFb = document.getElementById('teamsFeedback');

teamsDz.addEventListener('click', () => teamsFi.click());
teamsDz.addEventListener('dragover', e => { e.preventDefault(); teamsDz.classList.add('active'); });
teamsDz.addEventListener('dragleave', () => teamsDz.classList.remove('active'));
teamsDz.addEventListener('drop', e => {
  e.preventDefault(); teamsDz.classList.remove('active');
  uploadFiles(e.dataTransfer.files, teamsFb);
});
teamsFi.addEventListener('change', () => uploadFiles(teamsFi.files, teamsFb));

async function uploadFiles(files, feedbackEl) {
  for (const f of files) {
    const form = new FormData();
    form.append('file', f);
    try {
      const r = await fetch('/upload', { method: 'POST', body: form });
      const j = await r.json();
      feedbackEl.innerHTML = '<div class="msg ' + (r.ok ? 'ok' : 'err') + '">' + (j.message || j.error) + '</div>';
    } catch(e) {
      feedbackEl.innerHTML = '<div class="msg err">' + e.message + '</div>';
    }
  }
  refreshStatus();
}

async function submitNote() {
  const text = document.getElementById('noteText').value.trim();
  const fb = document.getElementById('noteFeedback');
  const btn = document.getElementById('sendBtn');
  if (!text) return;

  btn.disabled = true;
  btn.textContent = 'Sending...';

  try {
    const r = await fetch('/note', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'text=' + encodeURIComponent(text),
    });
    const j = await r.json();
    fb.innerHTML = '<div class="msg ' + (r.ok ? 'ok' : 'err') + '">' + (j.message || j.error) + '</div>';
    if (r.ok) document.getElementById('noteText').value = '';
  } catch(e) {
    fb.innerHTML = '<div class="msg err">' + e.message + '</div>';
  }

  btn.disabled = false;
  btn.textContent = 'Send Note';
  refreshStatus();
}

async function refreshStatus() {
  try {
    const r = await fetch('/status');
    const j = await r.json();
    const counts = j.counts || {};
    const total = Object.values(counts).reduce((a,b) => a + b, 0);
    document.getElementById('totalCount').textContent = total;
    document.getElementById('inboxCount').textContent = total;
  } catch(e) {}
}

refreshStatus();
setInterval(refreshStatus, 5000);

async function toggleFileList() {
  const list = document.getElementById('fileList');
  list.classList.toggle('show');
  if (list.classList.contains('show')) {
    await loadFileList();
  }
}

async function loadFileList() {
  const ul = document.getElementById('fileListItems');
  try {
    const r = await fetch('/files');
    const j = await r.json();
    if (j.files && j.files.length > 0) {
      ul.innerHTML = j.files.map(f =>
        '<li><span>' + f.name + '</span><span class="file-date">' + f.date + '</span></li>'
      ).join('');
    } else {
      ul.innerHTML = '<li>No items yet</li>';
    }
  } catch(e) {
    ul.innerHTML = '<li>Error loading files</li>';
  }
}
</script>
</body>
</html>
"""


def _parse_multipart(body: bytes, boundary: str) -> tuple[str, bytes]:
    """Parse a multipart/form-data body and extract the first file.

    Returns:
        (filename, file_data) or ("", b"") if no file found.
    """
    boundary_bytes = boundary.encode("utf-8")
    # Parts are separated by --boundary
    parts = body.split(b"--" + boundary_bytes)

    for part in parts:
        if b"Content-Disposition" not in part:
            continue
        # Split headers from body at the double CRLF
        header_end = part.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        headers_section = part[:header_end].decode("utf-8", errors="replace")
        file_body = part[header_end + 4:]  # Skip \r\n\r\n

        # Remove trailing \r\n
        if file_body.endswith(b"\r\n"):
            file_body = file_body[:-2]

        # Extract filename
        match = re.search(r'filename="([^"]+)"', headers_section)
        if match:
            return match.group(1), file_body

    return "", b""


def _generate_shortcut_plist(server_url: str, share_sheet: bool = False) -> bytes:
    """Generate an iOS Shortcut file (.shortcut) as a property list.

    Args:
        server_url: The base URL of the sense server (e.g., http://192.168.1.10:8765)
        share_sheet: If True, creates a share sheet shortcut. If False, uses "Get Latest Voice Memo".

    Returns:
        Binary plist data that iOS can import as a shortcut.
    """
    import plistlib
    import uuid

    shortcut_name = (_get_branding().mascot_name + " Signal") if share_sheet else "Upload Latest Memo"

    # Build the workflow actions
    actions = []

    if share_sheet:
        # Share sheet shortcut: expects input from share sheet
        actions.append({
            "WFWorkflowActionIdentifier": "is.workflow.actions.getitemfromlist",
            "WFWorkflowActionParameters": {
                "WFItemSpecifier": "First Item",
            }
        })
    else:
        # Standalone shortcut: get latest voice memo
        actions.append({
            "WFWorkflowActionIdentifier": "is.workflow.actions.getlatestvoicememo",
            "WFWorkflowActionParameters": {}
        })

    # URL action
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.url",
        "WFWorkflowActionParameters": {
            "WFURLActionURL": f"{server_url}/upload"
        }
    })

    # Get Contents of URL (POST with file)
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "WFHTTPMethod": "POST",
            "WFHTTPBodyType": "Form",
            "WFFormValues": {
                "Value": {
                    "WFDictionaryFieldValueItems": [
                        {
                            "WFItemType": 0,
                            "WFKey": {
                                "Value": {"string": "file"},
                                "WFSerializationType": "WFTextTokenString"
                            },
                            "WFValue": {
                                "Value": {
                                    "Type": "ActionOutput",
                                    "OutputName": "Voice Memo" if not share_sheet else "Item from List",
                                    "OutputUUID": str(uuid.uuid4()).upper()
                                },
                                "WFSerializationType": "WFTokenAttachmentParameterState"
                            }
                        }
                    ]
                },
                "WFSerializationType": "WFDictionaryFieldValue"
            }
        }
    })

    # Show result notification
    actions.append({
        "WFWorkflowActionIdentifier": "is.workflow.actions.showresult",
        "WFWorkflowActionParameters": {
            "Text": {
                "Value": {"string": "✓ Sent to {product_name} Signal"},
                "WFSerializationType": "WFTextTokenString"
            }
        }
    })

    shortcut = {
        "WFWorkflowClientVersion": "1177.2",
        "WFWorkflowClientRelease": "4.0",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 463140863,  # Blue
            "WFWorkflowIconGlyphNumber": 61440  # Microphone
        },
        "WFWorkflowActions": actions,
        "WFWorkflowInputContentItemClasses": [
            "WFAVAssetContentItem",
            "WFGenericFileContentItem"
        ] if share_sheet else [],
        "WFWorkflowTypes": ["WatchKit", "ActionExtension"] if share_sheet else ["WatchKit"],
        "WFWorkflowName": shortcut_name,
    }

    return plistlib.dumps(shortcut, fmt=plistlib.FMT_BINARY)


class InboxHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the inbox ingestion server."""

    inbox_root: Path = INBOX_RAW
    process_enabled: bool = False
    webhook_url: str | None = None

    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        elif self.path in ("/", ""):
            self._serve_upload_page()
        elif self.path == "/setup":
            self._serve_setup_page()
        elif self.path == "/qr.svg":
            self._serve_qr_code()
        elif self.path == "/shortcut.shortcut":
            self._serve_shortcut(share_sheet=False)
        elif self.path == "/shortcut-share.shortcut":
            self._serve_shortcut(share_sheet=True)
        elif self.path == "/logo.png":
            self._serve_logo_image()
        elif self.path == "/files":
            self._handle_files()
        elif self.path == "/inbox":
            self._handle_inbox()
        elif self.path.startswith("/feedback/"):
            self._serve_feedback_page()
        elif self.path == "/feedback":
            self._serve_feedback_status()
        else:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/upload":
            self._handle_upload()
        elif self.path == "/note":
            self._handle_note()
        elif self.path.startswith("/feedback/"):
            self._handle_feedback_submit()
        else:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_status(self):
        """Return inbox file counts as JSON."""
        counts: dict[str, int] = {}
        root = self.inbox_root
        if root.exists():
            for child in root.iterdir():
                if child.is_dir():
                    n = sum(1 for f in child.rglob("*") if f.is_file() and f.name != ".gitkeep")
                    if n:
                        counts[child.name] = n
                elif child.is_file() and child.name != ".gitkeep":
                    counts["root"] = counts.get("root", 0) + 1
        self._respond(HTTPStatus.OK, {"counts": counts})

    def _handle_files(self):
        """Return list of files in inbox as JSON."""
        from datetime import datetime
        files = []
        root = self.inbox_root
        if root.exists():
            for child in root.iterdir():
                if child.is_dir():
                    for f in child.rglob("*"):
                        if f.is_file() and f.name != ".gitkeep":
                            mtime = datetime.fromtimestamp(f.stat().st_mtime)
                            files.append({
                                "name": f.name,
                                "path": str(f.relative_to(root)),
                                "date": mtime.strftime("%b %d, %H:%M"),
                                "timestamp": f.stat().st_mtime
                            })
                elif child.is_file() and child.name != ".gitkeep":
                    mtime = datetime.fromtimestamp(child.stat().st_mtime)
                    files.append({
                        "name": child.name,
                        "path": child.name,
                        "date": mtime.strftime("%b %d, %H:%M"),
                        "timestamp": child.stat().st_mtime
                    })
        # Sort by timestamp, newest first
        files.sort(key=lambda x: x["timestamp"], reverse=True)
        self._respond(HTTPStatus.OK, {"files": files})

    def _handle_inbox(self):
        """Return all inbox items with processing status."""
        from datetime import datetime

        items = []
        raw_root = self.inbox_root
        processed_root = INBOX_PROCESSED

        # Build set of processed file stems for lookup
        processed_stems = set()
        if processed_root.exists():
            for f in processed_root.rglob("*_transcript.md"):
                # Extract original filename stem from transcript name
                stem = f.stem.replace("_transcript", "")
                processed_stems.add(stem)

        # Scan raw inbox
        if raw_root.exists():
            for child in raw_root.iterdir():
                if child.is_dir():
                    for f in child.rglob("*"):
                        if f.is_file() and f.name != ".gitkeep":
                            mtime = datetime.fromtimestamp(f.stat().st_mtime)
                            is_processed = f.stem in processed_stems
                            items.append({
                                "name": f.name,
                                "path": str(f.relative_to(raw_root)),
                                "folder": child.name,
                                "status": "processed" if is_processed else "pending",
                                "date": mtime.strftime("%Y-%m-%d %H:%M"),
                                "timestamp": f.stat().st_mtime,
                                "size": f.stat().st_size,
                            })
                elif child.is_file() and child.name != ".gitkeep":
                    mtime = datetime.fromtimestamp(child.stat().st_mtime)
                    is_processed = child.stem in processed_stems
                    items.append({
                        "name": child.name,
                        "path": child.name,
                        "folder": "root",
                        "status": "processed" if is_processed else "pending",
                        "date": mtime.strftime("%Y-%m-%d %H:%M"),
                        "timestamp": child.stat().st_mtime,
                        "size": child.stat().st_size,
                    })

        # Sort by timestamp, newest first
        items.sort(key=lambda x: x["timestamp"], reverse=True)

        # Summary stats
        pending = sum(1 for i in items if i["status"] == "pending")
        processed = sum(1 for i in items if i["status"] == "processed")

        self._respond(HTTPStatus.OK, {
            "items": items,
            "summary": {
                "total": len(items),
                "pending": pending,
                "processed": processed,
            },
            "process_enabled": self.process_enabled,
        })

    def _serve_upload_page(self):
        """Serve the minimal HTML upload interface."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        _branding = _get_branding()
        page = (UPLOAD_PAGE
                .replace("{product_name}", _branding.product_name)
                .replace("{mascot_name}", _branding.mascot_name)
                ).encode("utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def _serve_logo_image(self):
        """Serve the Neut mascot image."""
        img_path = Path(__file__).parent / "logo_light_background.png"
        if img_path.exists():
            self.send_response(HTTPStatus.OK)
            # File is JPEG despite .png extension
            self.send_header("Content-Type", "image/jpeg")
            data = img_path.read_bytes()
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._respond(HTTPStatus.NOT_FOUND, {"error": "Image not found"})

    def _get_server_url(self) -> str:
        """Get the server URL, preferring LAN IP over localhost."""
        host = self.headers.get("Host", "localhost:8765")
        port = host.split(":")[-1] if ":" in host else "8765"

        # If accessed via localhost, replace with LAN IP for iPhone compatibility
        if host.startswith("localhost") or host.startswith("127."):
            import socket
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
                return f"http://{ip}:{port}"
            except Exception:
                pass
        return f"http://{host}"

    def _serve_setup_page(self):
        """Serve the iPhone setup page with auto-detected server URL."""
        server_url = self._get_server_url()

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        page = SETUP_PAGE.replace("{{SERVER_URL}}", server_url).encode("utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def _serve_shortcut(self, share_sheet: bool = False):
        """Serve a downloadable iOS Shortcut file."""
        server_url = self._get_server_url()

        try:
            shortcut_data = _generate_shortcut_plist(server_url, share_sheet=share_sheet)

            filename = (f"Send-to-{_get_branding().mascot_name}-Share.shortcut") if share_sheet else "Upload-Latest-Memo.shortcut"

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-shortcut")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(shortcut_data)))
            self.end_headers()
            self.wfile.write(shortcut_data)
        except Exception as e:
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})

    def _serve_qr_code(self):
        """Generate and serve a QR code SVG for the setup URL."""
        setup_url = f"{self._get_server_url()}/setup"

        try:
            import qrcode
            import qrcode.image.svg

            qr = qrcode.QRCode(version=1, box_size=10, border=2)
            qr.add_data(setup_url)
            qr.make(fit=True)

            factory = qrcode.image.svg.SvgPathImage
            img = qr.make_image(image_factory=factory)

            from io import BytesIO
            buffer = BytesIO()
            img.save(buffer)
            svg_data = buffer.getvalue()

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg_data)))
            self.end_headers()
            self.wfile.write(svg_data)
        except ImportError:
            # Fallback: simple text response
            self._respond(HTTPStatus.OK, {"url": setup_url, "note": "Install qrcode: pip install qrcode"})

    def _handle_upload(self):
        """Handle multipart file upload, route by extension, optionally process."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "Expected multipart/form-data"})
            return

        # Extract boundary from Content-Type
        match = re.search(r"boundary=(.+)", content_type)
        if not match:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "No boundary in Content-Type"})
            return
        boundary = match.group(1).strip()

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Parse multipart manually (cgi module removed in Python 3.13)
        filename, file_data = _parse_multipart(body, boundary)

        if not filename:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "No file provided"})
            return

        filename = Path(filename).name  # Sanitize
        ext = Path(filename).suffix.lower()
        subdir = ROUTE_MAP.get(ext, "other")

        dest_dir = self.inbox_root / subdir if subdir else self.inbox_root
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename

        # Avoid overwriting — append timestamp if exists
        if dest.exists():
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            stem = dest.stem
            dest = dest_dir / f"{stem}_{ts}{ext}"

        dest.write_bytes(file_data)

        route_label = subdir if subdir else "root"
        response = {
            "message": f"Saved {filename} → inbox/raw/{route_label}/ ({len(file_data)} bytes)",
            "path": str(dest.relative_to(self.inbox_root)),
            "status": "saved",
        }

        # Auto-process if enabled and it's a voice file
        if self.process_enabled and subdir == "voice":
            result = self._process_voice_file(dest)
            response.update(result)

            # Send webhook notification
            if self.webhook_url:
                self._send_webhook({
                    "event": "processed",
                    "file": filename,
                    "transcript": result.get("transcript_preview", ""),
                    "signals": result.get("signal_count", 0),
                })

        self._respond(HTTPStatus.OK, response)

    def _process_voice_file(self, path: Path) -> dict:
        """Process a voice file through the extractor."""
        try:
            from axiom.infra.gateway import Gateway

            from .correlator import Correlator
            from .extractors.voice import VoiceExtractor

            extractor = VoiceExtractor()
            gateway = Gateway()
            correlator = Correlator()

            print(f"  Processing {path.name}...", flush=True)
            extraction = extractor.extract(
                path,
                model_size="base",
                gateway=gateway if gateway.available else None,
                correlator=correlator,
            )

            # Find transcript file
            transcript_path = None
            transcript_preview = ""
            if extraction.signals:
                meta = extraction.signals[0].metadata
                if "transcript_path" in meta:
                    transcript_path = meta["transcript_path"]
                    try:
                        content = Path(transcript_path).read_text(encoding="utf-8")
                        # Extract just the transcript text (after "## Full Transcript")
                        if "## Full Transcript" in content:
                            transcript_preview = content.split("## Full Transcript")[-1].strip()[:500]
                        else:
                            transcript_preview = content[:500]
                    except Exception:
                        pass

            result = {
                "status": "processed",
                "signal_count": len(extraction.signals),
                "errors": extraction.errors,
            }

            if transcript_path:
                result["transcript_path"] = transcript_path
            if transcript_preview:
                result["transcript_preview"] = transcript_preview

            print(f"  → {len(extraction.signals)} signal(s) extracted", flush=True)
            return result

        except ImportError as e:
            return {
                "status": "saved",
                "processing_error": f"Missing dependency: {e}. Install with: pip install openai-whisper",
            }
        except Exception as e:
            return {
                "status": "saved",
                "processing_error": str(e),
            }

    def _send_webhook(self, payload: dict):
        """POST notification to webhook URL (non-blocking)."""
        if not self.webhook_url:
            return

        webhook = self.webhook_url  # narrowed to str by guard above

        def send():
            try:
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    webhook,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                print(f"  Webhook failed: {e}", flush=True)

        # Fire and forget
        threading.Thread(target=send, daemon=True).start()

    def _handle_note(self):
        """Handle text note submission."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "Empty request"})
            return

        body = self.rfile.read(content_length).decode("utf-8")
        params = parse_qs(body)
        text = params.get("text", [""])[0].strip()

        if not text:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "No text provided"})
            return

        ts = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        self.inbox_root.mkdir(parents=True, exist_ok=True)
        dest = self.inbox_root / f"note_{ts}.md"
        dest.write_text(f"# Note — {ts}\n\n{text}\n", encoding="utf-8")

        self._respond(HTTPStatus.OK, {
            "message": f"Note saved as {dest.name}",
            "path": dest.name,
        })

    def _serve_feedback_page(self):
        """Serve the feedback form for a specific signal."""
        from .feedback import FEEDBACK_TYPES, FeedbackCollector

        # Extract request_id from path: /feedback/{request_id}
        parts = self.path.split("/")
        if len(parts) < 3:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "Missing request ID"})
            return

        request_id = parts[2].split("?")[0]  # Strip query params

        collector = FeedbackCollector()
        request = collector.get_request(request_id)

        if not request:
            self._send_html(HTTPStatus.NOT_FOUND, """
<!DOCTYPE html>
<html><head><title>Not Found</title></head>
<body style="font-family: system-ui; max-width: 600px; margin: 40px auto; padding: 20px;">
<h1>Request Not Found</h1>
<p>This feedback request may have expired or already been completed.</p>
</body></html>
""")
            return

        # Build feedback type options
        feedback_options = "\n".join([
            f'<option value="{ft}">{desc}</option>'
            for ft, desc in FEEDBACK_TYPES.items()
        ])

        # Build suggested PRD options
        prd_options = ""
        if request.suggested_prds:
            prd_options = "<h3>LLM-Suggested Relevance</h3><ul>"
            for prd in request.suggested_prds:
                prd_options += f'<li><label><input type="checkbox" name="confirm_prd" value="{prd}"> {prd}</label></li>'
            prd_options += "</ul>"

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Signal Feedback — Neut Sense</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: system-ui, -apple-system, sans-serif;
            max-width: 700px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
            color: #1a1a1a;
        }}
        .card {{
            background: white;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        }}
        h1 {{ color: #0EA5E9; margin-top: 0; }}
        h2 {{ color: #333; font-size: 1.1em; }}
        h3 {{ color: #666; font-size: 0.95em; margin-top: 20px; }}
        pre {{
            background: #f8f9fa;
            padding: 16px;
            border-radius: 8px;
            overflow-x: auto;
            font-size: 0.9em;
            line-height: 1.5;
        }}
        .routing {{
            background: #e8f4f8;
            padding: 12px 16px;
            border-radius: 8px;
            margin-top: 12px;
        }}
        form {{ margin-top: 24px; }}
        label {{ display: block; margin-bottom: 8px; font-weight: 500; }}
        select, textarea, input[type="text"] {{
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 8px;
            font-size: 1em;
            margin-bottom: 16px;
        }}
        textarea {{ min-height: 100px; resize: vertical; }}
        .btn {{
            background: #0EA5E9;
            color: white;
            border: none;
            padding: 14px 28px;
            border-radius: 8px;
            font-size: 1em;
            font-weight: 600;
            cursor: pointer;
            margin-right: 10px;
        }}
        .btn:hover {{ background: #0284c7; }}
        .btn-secondary {{
            background: #10b981;
        }}
        .btn-secondary:hover {{ background: #059669; }}
        .btn-dismiss {{
            background: #6b7280;
        }}
        .actions {{ margin-top: 24px; }}
        ul {{ padding-left: 20px; }}
        li {{ margin-bottom: 8px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>📡 Signal Receipt</h1>
        <p>We captured a signal from <strong>{request.originator}</strong> and extracted:</p>

        <pre>{request.signal_summary}</pre>

        {f'<div class="routing"><strong>Routing to:</strong> {", ".join(request.routed_to)}</div>' if request.routed_to else ''}
    </div>

    <div class="card">
        <h2>Your Feedback Welcome!</h2>
        <p>Help us improve by confirming, correcting, or adding context.</p>

        {prd_options}

        <form method="POST" action="/feedback/{request_id}">
            <input type="hidden" name="request_id" value="{request_id}">
            <input type="hidden" name="signal_id" value="{request.signal_id}">
            <input type="hidden" name="originator" value="{request.originator}">

            <label for="feedback_type">What would you like to do?</label>
            <select name="feedback_type" id="feedback_type" required>
                <option value="">— Select —</option>
                {feedback_options}
            </select>

            <label for="content">Details (optional)</label>
            <textarea name="content" id="content" placeholder="Add any clarification, corrections, or additional context..."></textarea>

            <label for="person">Suggest another person who should see this</label>
            <input type="text" name="person" id="person" placeholder="Name or email (optional)">

            <label for="initiative">Add relevance to another initiative</label>
            <input type="text" name="initiative" id="initiative" placeholder="Initiative name (optional)">

            <div class="actions">
                <button type="submit" class="btn">Submit Feedback</button>
                <button type="submit" name="feedback_type" value="approve" class="btn btn-secondary">✓ Looks Good</button>
                <button type="submit" name="feedback_type" value="dismiss" class="btn btn-dismiss">✗ Noise/Ignore</button>
            </div>
        </form>
    </div>

    <p style="text-align: center; color: #888; font-size: 0.85em;">
        Neut Sense — keeping humans in the loop
    </p>
</body>
</html>
"""
        self._send_html(HTTPStatus.OK, html)

    def _handle_feedback_submit(self):
        """Handle feedback form submission."""
        from .feedback import FeedbackCollector, SignalFeedback

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "Empty request"})
            return

        body = self.rfile.read(content_length).decode("utf-8")
        params = parse_qs(body)

        signal_id = params.get("signal_id", [""])[0]
        feedback_type = params.get("feedback_type", [""])[0]
        content = params.get("content", [""])[0]
        originator = params.get("originator", [""])[0]
        person = params.get("person", [""])[0]
        initiative = params.get("initiative", [""])[0]

        if not signal_id or not feedback_type:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "Missing required fields"})
            return

        feedback = SignalFeedback(
            signal_id=signal_id,
            feedback_type=feedback_type,
            content=content,
            originator=originator,
            person=person,
            initiative=initiative,
        )

        collector = FeedbackCollector()
        success = collector.submit_feedback(feedback)

        if success:
            # Show thank you page
            html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Thank You — Neut Sense</title>
    <style>
        body {
            font-family: system-ui, -apple-system, sans-serif;
            max-width: 600px;
            margin: 80px auto;
            padding: 20px;
            text-align: center;
        }
        .check { font-size: 4em; }
        h1 { color: #10b981; }
        p { color: #666; }
    </style>
</head>
<body>
    <div class="check">✓</div>
    <h1>Thank You!</h1>
    <p>Your feedback has been recorded and will help improve signal processing.</p>
    <p style="margin-top: 30px;">
        <a href="/" style="color: #0EA5E9;">← Back to Inbox</a>
    </p>
</body>
</html>
"""
            self._send_html(HTTPStatus.OK, html)
        else:
            self._respond(HTTPStatus.BAD_REQUEST, {"error": "Invalid feedback type"})

    def _serve_feedback_status(self):
        """Show feedback system status."""
        from .feedback import FeedbackCollector

        collector = FeedbackCollector()
        status = collector.status()
        pending = list(collector.pending.values())

        pending_html = ""
        if pending:
            pending_html = "<h3>Pending Requests</h3><ul>"
            for req in pending[:20]:
                pending_html += f'<li><a href="/feedback/{req.request_id}">{req.originator}</a> — {req.signal_summary[:50]}...</li>'
            pending_html += "</ul>"

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Feedback Status — Neut Sense</title>
    <style>
        body {{ font-family: system-ui; max-width: 700px; margin: 40px auto; padding: 20px; }}
        .stat {{ display: inline-block; background: #f0f0f0; padding: 15px 25px; border-radius: 8px; margin: 5px; text-align: center; }}
        .stat-num {{ font-size: 2em; font-weight: bold; color: #0EA5E9; }}
        .stat-label {{ font-size: 0.85em; color: #666; }}
        h1 {{ color: #333; }}
        ul {{ padding-left: 20px; }}
        li {{ margin-bottom: 8px; }}
        a {{ color: #0EA5E9; }}
    </style>
</head>
<body>
    <h1>📊 Feedback Status</h1>

    <div class="stat">
        <div class="stat-num">{status['pending_requests']}</div>
        <div class="stat-label">Pending</div>
    </div>
    <div class="stat">
        <div class="stat-num">{status['total_feedback']}</div>
        <div class="stat-label">Received</div>
    </div>
    <div class="stat">
        <div class="stat-num">{status['unapplied_feedback']}</div>
        <div class="stat-label">Unapplied</div>
    </div>

    {pending_html}

    <p><a href="/">← Back to Inbox</a></p>
</body>
</html>
"""
        self._send_html(HTTPStatus.OK, html)

    def _send_html(self, status: HTTPStatus, html: str):
        """Send an HTML response."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        payload = html.encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _respond(self, status: HTTPStatus, data: dict):
        """Send a JSON response."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        payload = json.dumps(data).encode("utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        """Override to use a cleaner log format."""
        print(f"  [{self.log_date_time_string()}] {format % args}")


def create_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    inbox_root: Path | None = None,
    process_enabled: bool = False,
    webhook_url: str | None = None,
) -> HTTPServer:
    """Create and return a configured HTTP server (not yet started).

    Args:
        host: Bind address (0.0.0.0 for LAN access).
        port: Port number.
        inbox_root: Override inbox path (for testing).
        process_enabled: Auto-transcribe voice memos on upload.
        webhook_url: URL to POST notifications to.
    """
    if inbox_root is not None:
        InboxHandler.inbox_root = inbox_root
    else:
        InboxHandler.inbox_root = INBOX_RAW

    InboxHandler.process_enabled = process_enabled
    InboxHandler.webhook_url = webhook_url

    server = HTTPServer((host, port), InboxHandler)
    return server


def _get_local_urls(port: int) -> list[str]:
    """Get all URLs where the server can be reached."""
    urls = []

    # Get LAN IP
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        urls.append(f"http://{ip}:{port}")
    except Exception:
        pass

    # Get .local hostname (Bonjour)
    try:
        import subprocess
        result = subprocess.run(["scutil", "--get", "LocalHostName"],
                                capture_output=True, text=True)
        if result.returncode == 0:
            hostname = result.stdout.strip()
            urls.append(f"http://{hostname}.local:{port}")
    except Exception:
        pass

    return urls


NEWT_BANNER = r"""
                 .  * .
              *   \|/   *
            .  ----●----  .        ___
              *  /|\  *        .-'   '-.
                 .|.       .-'  ^   ^  '-.
           ╔═════╪═══════/      ◡        \
           ║≋≋≋≋≋╪≋≋≋≋≋≋|    '------'    |
           ╚═════╪═══════\              _/
                 .|.       '-._    _.-'
              *  \|/  *        '---'
            .  ----●----  .
              *   |   *
                 .  .
"""


def run_server(
    host: str = "0.0.0.0",
    port: int = 8765,
    process: bool = False,
    webhook: str | None = None,
):
    """Start the inbox server (blocking).

    Args:
        host: Bind address.
        port: Port number.
        process: Auto-transcribe voice memos on upload.
        webhook: URL to POST notifications to.
    """
    INBOX_RAW.mkdir(parents=True, exist_ok=True)

    server = create_server(host, port, process_enabled=process, webhook_url=webhook)
    urls = _get_local_urls(port)

    print(NEWT_BANNER)
    print(f"{_get_branding().cli_name} signal serve — inbox ingestion server")
    print("─" * 50)
    print()
    if urls:
        print("  📱 iPhone setup (scan QR or open in Safari):")
        for url in urls:
            print(f"     {url}/setup")
        print()
    print(f"  Inbox:   {INBOX_RAW}")
    if process:
        print("  Process: enabled (auto-transcribe voice memos)")
    if webhook:
        print(f"  Webhook: {webhook}")
    print()
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="signal inbox server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8765, help="Port number")
    parser.add_argument("--process", action="store_true", help="Auto-transcribe voice memos")
    parser.add_argument("--webhook", type=str, help="URL to POST notifications to")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, process=args.process, webhook=args.webhook)
