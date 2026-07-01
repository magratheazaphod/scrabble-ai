---
name: woogles-gcg-upload
description: Upload local .gcg tournament game files to woogles.io's Board Editor and group them into a collection. Use this whenever Jesse asks to upload GCG files to Woogles, add tournament rounds to a Woogles collection, or mentions a folder of .gcg files that need to go up on his profile. Covers the finicky UI mechanics of the upload form, the antd Select dropdowns, the Add-to-Collection modal, and the GCG endgame-line gotcha that silently breaks the parser.
---

# Woogles.io GCG Upload

Jesse (woogles.io username `Magrathean`) keeps tournament games as local `.gcg` files (e.g. exported from Quackle) and wants them uploaded to woogles.io's **Board Editor**, then grouped into a **collection** named after the tournament (e.g. "Austin One-Day Aug '23"), one chapter per round.

Woogles.io is a JS-rendered SPA — use the **Claude in Chrome** MCP tools throughout (`navigate`, `computer`, `javascript_tool`, `file_upload`, `read_page`). If Chrome isn't connected, ask Jesse to connect it first.

## Jesse's standing defaults — always apply these

- **Lexicon: Jesse always plays CSW, never NWL.** The GCG files don't encode a lexicon, so you must pick one in the upload form.
- **Use the CSW edition current at the time of the tournament**, not today's. E.g. a tournament from August 2023 should use **CSW21** (the CSW edition in force at that time), not whatever the newest CSW edition is now. Ask Jesse if the right historical edition isn't obvious from context.
- **Challenge rule: always "5 points"** (CSW tournaments use the 5-point challenge rule).
- **The lexicon and challenge rule CANNOT be edited after the game is created.** Both must be set correctly in the upload form *before* clicking "Create new game." If you get this wrong, delete the game and re-upload rather than trying to fix it after the fact.

## Before uploading: check the GCG file for the endgame-line gotcha

Read each `.gcg` file before uploading it. GCG files end one of two ways, and **woogles.io's parser rejects a file if these are confused** (it fails with an opaque "no match found for line" error, or — worse — silently creates a blank game with no board):

1. **Going-out bonus** (one player plays out their rack, the other is left with tiles): the line has an **empty rack field** (two spaces after the colon), then the opponent's leftover tiles in parentheses, then a **positive** score:
   ```
   >Becky_Dyer:  (CFLO) +18 437
   ```
2. **Six-consecutive-scoreless-turns penalty** (no one goes out; the game ends because both players passed/scored zero six times in a row): each player loses points for their own leftover rack. Here the rack field is **populated** (repeated), then the same tiles in parentheses, then a **negative** score:
   ```
   >JD: L (L) -1 524
   >Becky_Dyer: Q (Q) -10 452
   ```

These two formats are NOT interchangeable — emptying the rack field on a penalty line (or vice versa) breaks the upload. If a file ends with several `>Player: X -  +0 <cum>` zero-score lines (six in a row, alternating players), expect it needs the penalty format above for its final two lines. If it ends with a normal play followed by one bonus line per the first pattern, no edit is needed. When in doubt, check the authoritative spec at https://www.poslfit.com/scrabble/gcg/.

If you have to edit a file to fix this, only touch the rack field and parenthetical/sign — don't otherwise alter scores or moves.

## Step 1: Open the upload form

1. Navigate to `https://woogles.io/editor`.
2. Click **"Add an annotated game"**. This opens a small menu (Create a new game from scratch / Upload a GCG file / Annotate from your camera with Scrabblecam).
3. Click **"Upload a GCG file"**.

**Known flakiness:** menu-item clicks frequently don't register on the first try, and the form that should appear sometimes doesn't. Screenshot after each click to confirm the menu/form actually changed state before proceeding. If a coordinate click doesn't work, fall back to a JS click targeting the exact visible node:
```js
const items = Array.from(document.querySelectorAll('li, div, span'))
  .filter(e => e.textContent.trim() === 'Upload a GCG file' && e.offsetParent !== null);
items[items.length - 1].click();
```
Confirm success by checking for the file input:
```js
document.querySelectorAll('input[type="file"]').length
```

## Step 2: Upload the file

Use `file_upload` with the file input's ref and the absolute path to the `.gcg` file. Getting a `ref`:
- Prefer `read_page` over `find` — `find` is backed by a rate-limited model call and can return 429s under load; `read_page`'s accessibility tree usually includes the file input directly (look for an element typed `"file"` inside the upload `<form>`).

After upload, the form should preview the file's raw text and show **Dictionary** and **Challenge rule** fields, plus a "Create new game" button.

## Step 3: Set Dictionary and Challenge rule (before creating the game)

These are antd `Select` components and are the least reliable part of the whole flow. Plain coordinate clicks on dropdown options frequently fail to register (the field stays empty even though the dropdown visually closes). Use this JS pattern instead — it has been reliable every time:

```js
async function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
function setReactInputValue(el, value) {
  const proto = window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

// Open the select (e.g. selects[0] = Dictionary, selects[1] = Challenge rule)
const selects = Array.from(document.querySelectorAll('.ant-select'));
const dictSelect = selects[0];
dictSelect.querySelector('.ant-select-selector')
  .dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
await sleep(300);
```

**The historical CSW edition (e.g. CSW21) often isn't in the default visible option list** — the unfiltered dropdown only shows a handful of current options (CSW 24, NWL23, NWL20, NWL18, etc.). To reveal it, type into the search input to filter:
```js
const input = dictSelect.querySelector('input.ant-select-selection-search-input');
setReactInputValue(input, '21'); // filters down to "CSW21"
await sleep(300);
```
Then select the option, again via JS (visibility-filtered, since closed/hidden dropdown items can share the same class):
```js
const items = Array.from(document.querySelectorAll('.ant-select-item-option'))
  .filter(i => i.offsetParent !== null);
const item = items.find(i => i.textContent === 'CSW21');
item.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
item.click();
```
Repeat the same pattern for the Challenge rule select, picking the option with exact text `'5 points'`. Verify both fields actually took the value before moving on:
```js
document.querySelectorAll('.ant-select-selection-item')[0].textContent // Dictionary
document.querySelectorAll('.ant-select-selection-item')[1].textContent // Challenge rule
```
Take a screenshot too — sometimes the dropdown list visually overlaps the form in a way that's only obvious from a screenshot, even though the underlying value is set correctly.

## Step 4: Create the game

Click **"Create new game"**. On success you land on `/editor/<gameId>` with the full board rendered and final scores shown in the right-hand panel, and the header should read something like "Annotated • Classic • CSW21" / "5 point challenge • Unrated". A blank/empty board on this page means the GCG didn't parse — re-check the endgame-line format (Step "Before uploading").

## Step 5: Add the game to the collection

In the editor controls panel (scroll down if needed), expand **Collections** and click **Add**.

**Known flakiness:** the very first click on "Add" frequently does nothing visible — click it again to actually open the "Add Game to Collection" modal. Likewise, clicking inside the modal's "Select Collection" field with a plain coordinate click can intermittently close the whole modal (it resets back to the lobby/game view) instead of opening the dropdown. The reliable approach is, once the modal is open, to do the whole fill-in via JS in one shot:

```js
async function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
function setReactInputValue(el, value) {
  const proto = window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}
const modal = document.querySelector('.ant-modal-content');
const selector = modal.querySelector('.ant-select-selector');
selector.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
await sleep(300);
const items = Array.from(document.querySelectorAll('.ant-select-item-option')).filter(i => i.offsetParent !== null);
const item = items.find(i => i.textContent.includes('<tournament collection name>'));
item.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
item.click();
await sleep(300);
const titleInput = modal.querySelector('input[placeholder*="Round 1"]');
setReactInputValue(titleInput, 'Round N - <Player A> vs <Player B>');
```
Verify both fields with a screenshot (the "Select Collection" and "Chapter Title" inputs should show the values you set — they tend to *retain* JS-set values across the modal's re-renders even when an immediately-following coordinate click looked like it cleared them).

Use a consistent chapter-title naming convention across the tournament, e.g. `Round 4 - JD vs Becky Dyer`.

Then click **"Add to Collection"**. Confirm via the green "Game added to collection" toast — don't assume success just because the modal closed.

## Step 6: Verify

After all rounds are uploaded, navigate to `https://woogles.io/collections/<collectionId>` and check the **Chapters** list in the left sidebar: it should show one chapter per round, correctly numbered and named, matching the chapter count shown next to the collection title (e.g. "Collection by magrathean • 6 chapters"). Spot check a couple of chapters' boards render fully (not blank).

## Notes / open questions to flag to Jesse if encountered

- A GCG file with an endgame line format Claude hasn't seen before (not a clean going-out or six-scoreless-turns ending) — don't guess, ask Jesse or check the spec.
- A file whose lexicon/era isn't obvious (tournament may have used a lexicon other than CSW, or an edition Jesse hasn't specified) — confirm before creating the game, since it can't be changed afterward.
- The Claude in Chrome `find` tool can hit session-level rate limits during a long multi-file upload run; prefer `read_page` + raw JS DOM queries over `find` when possible to avoid stalling mid-task.
