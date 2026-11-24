# Voice still generating after the reply text is complete (r67 R6)

Status: proposal, 2026-11-24. Not implemented. Needs a decision on the open question below.

## Problem

A turn stays open until every sentence's voice job settles. When GPT-SoVITS stalls,
that can take a long time after the last line of text has already arrived and been read:
`TTS_TIMEOUT.read` is 300 s and `TTS_JOB_TIMEOUT_SECONDS` is 600 s
(`2 * SWITCH_TIMEOUT.read + TTS_TIMEOUT.read + 60`).

During that tail the stage shows only the `……` dots:

- `line_player.moveToNextLine()` runs out of lines, sees `turnSettled === false`,
  enters `waiting_line` and calls `render.thinking(true)`, which hides the input.
- `advance()` returns early in `waiting_line`, so clicking does nothing.
- The #566 quiet hint (「语音还在准备，点击可先看文字」) only shows in `waiting_audio`,
  for a line that has text but no audio yet. It never shows once all lines are shown.
- The user can open the input (quick menu 「输入」, typing, or the mic), but
  `onSend` is `!controller.busy && controller.send(text)`, so the draft cannot be sent
  until `done` arrives.
- The only way out is quick menu 「停止」. That cancels the stream, so the `done`
  payload is `truncated: true` (`cancel_flagged`) and the voice still in flight is lost,
  even though the text was complete.

The client cannot tell "the model is still writing" from "only voice is left":
no SSE event says the text is complete. `voice_job` started/finished frames exist,
but only feed `ai.js`'s idle deadline.

## Proposal

Keep #566's quiet style: no modal, no spinner, no countdown.

1. **Backend: say when only voice is left.** At the end of `_llm_producer`, after
   `text_pipe.finalize()` succeeds and the lines are queued, emit one
   `progress` frame with `stage: "voice"`, bypassing the 5 s throttle in
   `_note_reply_progress`. It reuses an existing event, so older clients keep treating
   it as plain progress. `adv-ui.md` says protocol changes need a spec revision, so
   this note is that revision for the `progress` stage values.
2. **Frontend: carry it to the player.** `ai.js` passes the frame through `onEvent`;
   the controller emits `text_complete` for the turn; `adv_app` calls a new
   `player.textComplete(turnId)`.
3. **Frontend: open the input in the voice tail.** In `waiting_line` with
   `textComplete` set and every line shown, the player enters `input` with
   `canSend: false` instead of the dots, and the textbox shows the muted note
   「语音还在准备，可以先写下一句」 in the existing `adv-wait-hint` slot.
   A late `line_audio` still lands in LOG for replay. When `done` arrives,
   `setCanSend(true)` enables the send button and the draft stays as typed.

## Open question (needs a decision)

What should pressing Send do while only voice is left?

- **A. Stay disabled until `done`** (as above). Smallest change: frontend plus one
  progress frame. The user can write but still waits up to the TTS timeout to send.
- **B. Send = skip the remaining voice.** The client asks the server to stop the voice
  jobs only; the server persists the reply as complete (`truncated: false`, because the
  text was complete) and releases the turn lock (`serialize_stream_turn`), then the new
  message goes out. This removes the wait but needs a new "skip voice" cancel mode in
  the stream coordinator and persist policy, plus tests for the lock hand-over.

Recommendation: ship A first; it is low risk and fixes "nothing tells me what is
happening". Do B as its own PR if the wait still bites.

## Tests to add with the implementation

- Backend: a stream whose last TTS job hangs emits `progress` `voice` after the last
  `line` and before `done`; a stream that fails in the model does not.
- Frontend (`test_adv_p*.mjs` style): after all lines are shown and `text_complete`
  arrives, the input is visible with send disabled and the note is shown; `done` enables
  send without clearing the draft; without `text_complete` the dots stay as today.
