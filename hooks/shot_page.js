// Playwright MCP --init-page module: screenshot after every browser action (opt-in per session).
//
// Playwright MCP require()s this file once per tab and calls the default export with that tab's
// page. hooks/shot.py (a PostToolUse hook) drops "<name>.req" into MT_SHOT_DIR after each browser
// action and waits; this module answers with "<name>.png": the current tab's viewport at its normal
// resolution (CSS pixels, scale 1), written to a temporary name and renamed so it is never read half
// written. Anything that goes wrong becomes "<name>.err", so the hook never waits for nothing.
// Errors are logged to stderr: stdout belongs to the MCP protocol.
'use strict';

const fs = require('fs');
const path = require('path');

const FILE_MODE = 0o600;
const pages = [];          // every tab this MCP server opened, oldest first
let timer = null;
let busy = false;

function warn(what, e) {
  console.error(`[monkeytesting shots] ${what}: ${e && e.message ? e.message : e}`);
}

function shotDir() {
  const raw = process.env.MT_SHOT_DIR || '';
  if (!raw) return null;
  const dir = path.resolve(raw);
  return path.basename(dir) === 'shots' ? dir : null;
}

function currentPage() {
  for (let i = pages.length - 1; i >= 0; i--) {
    if (!pages[i].isClosed()) return pages[i];
  }
  return null;
}

function writePrivate(file, content) {
  const fd = fs.openSync(file, 'w', FILE_MODE);
  try {
    fs.writeSync(fd, content);
  } finally {
    fs.closeSync(fd);
  }
}

async function serve(dir) {
  if (busy) return;
  busy = true;
  try {
    let names;
    try {
      names = fs.readdirSync(dir).filter(f => f.endsWith('.req')).sort();
    } catch (e) {
      warn('cannot list the screenshot folder', e);
      return;
    }
    for (const req of names) {
      const base = path.join(dir, req.slice(0, -4));
      const page = currentPage();
      try {
        if (!page) throw new Error('no open page');
        await page.screenshot({ path: base + '.tmp.png', type: 'png', scale: 'css', animations: 'disabled',
                                caret: 'initial', timeout: 5000 });
        fs.chmodSync(base + '.tmp.png', FILE_MODE);
        fs.renameSync(base + '.tmp.png', base + '.png');
      } catch (e) {
        warn(`screenshot ${req} failed`, e);
        try {
          writePrivate(base + '.err', String(e && e.message || e).slice(0, 500));
        } catch (e2) {
          warn(`cannot write ${req} error file`, e2);
        }
      }
      try {
        fs.unlinkSync(path.join(dir, req));
      } catch (e) {
        warn(`cannot remove ${req}`, e);
      }
    }
  } finally {
    busy = false;
  }
}

exports.default = async function ({ page }) {
  const dir = shotDir();
  if (!dir) return;
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  pages.push(page);
  if (!timer) {
    timer = setInterval(() => { serve(dir); }, 100);
    timer.unref();   // never keeps the MCP server alive on its own
  }
};
