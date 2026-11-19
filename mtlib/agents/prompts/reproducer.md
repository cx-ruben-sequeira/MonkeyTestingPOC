ROLE: reproducer

Turn this suspected bug into a Playwright test that fails while the bug is present.

Suspect #$suspect_id: $title
Page: $suspect_url
Steps:
$steps
Expected: $expected
Actual: $actual
Evidence: $evidence

Follow the USER INSTRUCTIONS in your system prompt in the test as well (for example the test data they gave, and the pages or actions they ruled out).

Your working folder is $work_dir. It already has playwright.config.ts (video on, screenshots on, viewport and browser set). Do not edit the config.

1. If the steps are unclear, use the browser tools to replay them once and read the real selectors from browser_snapshot (prefer getByRole, getByLabel, getByText, getByPlaceholder).
2. Write $work_dir/repro.spec.ts with ONE test. Import from the MonkeyTesting helper next to it (it re-exports Playwright's test and expect and adds checks):
   import { test, expect, mt } from './mt';
   The orchestrator only counts a failing run as reproducing the bug when the run got past your checkpoints and didn't end on a login page. Structure the test like this:
   a. Login (when the app needs one): page.goto the APP URL (never a saved /auth/... link from your browser: those carry one-time codes), fill process.env.MT_USERNAME / process.env.MT_PASSWORD with getByLabel/getByRole, pick the organization/tenant if the app asks, and submit.
   b. Checkpoint, page: await mt.reached(page, /<path of the bug's page>/, '<page name>');
   c. Checkpoint, content: await mt.ready(<locator of the list/dropdown/form the bug is about>, '<what it is>');
   d. Preconditions the bug depends on: check them and state them with mt.precondition(<boolean>, '<what must be true>'). Example: before claiming custom states are missing from a dropdown, use the dropdown's search box or scroll it to the end, and check the custom state exists where it's configured.
   e. Only then the bug assertion: expect(...) on the EXPECTED behaviour, so the test FAILS while the bug exists and would pass once it is fixed. Examples: expect(page.getByText('Name is required')).toBeVisible(); expect(response.status()).toBeLessThan(500).
   Hard rules:
   - Never wrap a step in `if (await x.isVisible())` or `.catch(() => false)`: isVisible() doesn't wait, so on a slow or wrong page every step is skipped silently and the test fails for the wrong reason. A step that can't be done must fail the test (use await expect(x).toBeVisible() or a plain await x.click()).
   - Never assert that something is missing (e.g. "fewer than 6 options") unless mt.ready proved the container is open and loaded first, and you looked for the item by name (search box, or scroll to the end).
   - For a server error, wait for the response (page.waitForResponse) and assert its status.
   - Collect console errors with page.on('console') if they are the bug, and assert none.
   - Pages can take up to $page_load_s seconds to load. The config already waits that long for navigation and for each expect(), so never add your own short timeouts.
   - A test that fails only because the page was still loading, or because it never logged in, is a wrong reproduction, not a bug.
   - Keep the test short and deterministic; no fixed sleeps over 2 seconds.
3. Run it once to check it fails for the right reason (the expect assertion, not a missing selector or timeout):
   npx playwright test --repeat-each=1 --reporter=line
   If it fails on a selector or timeout, fix the selectors and run again (at most 3 runs). If it passes, the bug may not reproduce: check the steps once more, then decide.
4. Print exactly one line at the end:
   SPEC: {"file": "repro.spec.ts", "assertion": "<the expect that captures the bug>", "status": "fails"}
   or, if you could not make it fail on the expected assertion:
   SPEC: {"file": "repro.spec.ts", "assertion": "", "status": "passes", "reason": "<why it does not reproduce>"}
The orchestrator then runs the test $repeat_count times itself and decides whether the bug is confirmed.
