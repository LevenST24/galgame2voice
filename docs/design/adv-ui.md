# ADV UI implementation contract

Status: P0 design contract, 2026-10-19; implementation is delivered in P1–P8.
The approved Chinese plan controls scope, dependencies, and rollout; this contract fixes unspecified interfaces/defaults.
**Provisional** measurements may change after the P3 prototype; protocol/persistence changes require a spec revision.

P7 implementation (2026-10-21): ADV is the only main UI for every session.
`advEnabled` is retired: browser snapshots and backup imports drop it, including
old `false` values. The earlier preview/default-chat references below describe P3–P6.
The parked session sidebar becomes the ADV drawer; LOG owns history paging,
bilingual subtitles, per-line/legacy voice replay, recordings, and failure/retry.
Existing token, session/character/voice and global settings dialogs remain above the stage.

Removed compatibility IDs: `backdrop`, `menuBtn`, `chatTitle`, `modelBadge`, `badgeDot`,
`sessionSettingsBtn`, `voiceModeBtn`, `voiceModeIcon`, `messages`, `composer`, `input`,
`sendBtn`, `stopBtn`, `micBtn`, `portraitToggleBtn`, `stageBackdrop`, `characterStage`,
`stageCharacterName`, `stageEmotionLabel`, `stageCostumeToggleBtn`, `stageCostumeIcon`,
`stageCollapseBtn`, `stageCostumeBar`, `stageViewport`, `stageEmptyState`,
`portraitImgA`, `portraitImgB`, `stageDialogueCard`, `stageDialogueName`, `stageDialogueText`.
`portrait.js`, bubble/typing/composer rendering and arrival-driven sequential autoplay are removed.

### 启动首页（2026-10-24）

首次鉴权与会话同步完成后，`main.js` 调用 `adv/title_hook.js` 的 `showTitle(actions)`。
`createTitleScreen(root, { actions, documentObj, matchMedia })` 保留原入口；
`title.js` / `title.css` 绘制 `.adv-layer-overlay` 内的透明首页，真实背景和立绘仍由舞台绘制。
主角优先使用当前会话，没有当前会话时预览最近更新的会话，使舞台与摘录保持一致。
首页按会话的 `voiceProfileId` 显示角色；未绑定时不借用后端当前音色的立绘，也不从会话标题猜测角色。
没有任何台词的会话（例如第一条消息回复到一半就被中断）摘录显示「从这一刻，开始你们的故事。」，不显示空引号。

- 顶栏为小字标 Gal2Voice、带文字的对话/语音状态胶囊和全局设置齿轮。
  对话状态来自 `getModelStatus` 与模型配置加载状态；语音来自 `/api/system/engine-status`。
  打开时读取，此后每 5 秒刷新，设置弹窗关闭也刷新；关闭首页时注销计时器和订阅。
  模型配置未读完时显示琥珀色「对话 检查中」与正常摘录，模型状态各字段在同一时刻读取。
  轮询也更新晚到的角色名称、头像和背景缩略图；头像沿用真实立绘的 `faceRect` 与分层 `crop`。
  胶囊说明框分别直达全局设置的 `models` / `inference` 页。
  启动、同步、预览及会话设置保存产生的音色切换，均按结果到达时首页是否打开判断（舞台带 `is-home`，或启动同步尚未结束、首页还没来得及打开）：
  首页打开时不弹成功或一般失败提示，由语音胶囊显示状态；404 确认音色被删除并解除绑定时仍提示数据变更。
  从「接着聊 →」「开始聊天」、全部会话卡片或新建对话进入 ADV 时，仅当会话已绑定音色且
  `getActiveProfileId()` 与绑定不同时再切换一次，成功、失败提示沿用 ADV 规则。
  预览后同 ID 会话也执行此判断；已加载目标音色或未绑定时不请求、不提示，新建入口不重复切换。
  首页以外的会话抽屉切换与删除行为保持原样。
- 左侧显示角色名（无绑定角色则显示会话标题）、日期、最近一条有文字的助手消息的最后一句中文、
  同句日语以及该助手消息之前最近的用户消息。没有 `lines` 的旧消息保留完整 `content`。
  中文最多三行，CSS 截断，DOM 保留全文。引导卡期间也保留实际会话开场白，兼容便携版 DOM 冒烟。
  「试听这句」与 LOG 共用 `canReplayLine`、`StreamAudioController.playLine/stopLine`，
  包括七天过期、语音开关及部分音频失败的判定；切换或离开首页时停止试听。
- 底部「最近还聊过」排除主角，最多显示三个会话。圆形头像复用舞台清单缓存、
  `normalizePortrait` / `resolveSpriteState` / `planLayers`，按 `faceRect` 裁脸，失败用姓名首字。
  `previewSession(id)` 复用切换会话但禁止聚焦输入框，保持首页打开；
  「全部会话」保留原读取格子的缩略图、标题、末句、时间与每页六个的分页，标题下仅在有角色时显示角色名。
- 「另开一段对话」显示角色/地点/时间三个下拉列表，默认取主角会话设置。
  角色与背景列表保留 `work`；地点第一项为「默认背景」（值 `''`），其后只列所选角色 `profile.work` 的背景。
  角色未标注作品或该作品没有背景时才列全部背景，规则与 ADV 背景面板一致。
  换角色后，已选地点不属于新角色的作品时重置为「默认背景」，不修改原会话设置。
  `startNewWith({ voiceProfileId, advBackground, advBgTime })` 复用 `newChat` 与角色默认语速。
  草稿不写当前会话；目前不预览草稿舞台，因为现有背景选择器的预览会同时保存设置，角色也没有独立预览入口。
- 模型配置读完且无可用模型时显示三步首次使用引导，语音可在本次首页临时选择「暂时只用文字」，
  角色步骤按主角会话的 `voiceProfileId` 判定；模型可用后立即切换到摘录。
  主按钮仅按会话是否有用户消息决定：有则「接着聊 →」，只有开场白则「开始聊天」。
  引导不额外模糊舞台，避免对真实背景增加全屏滤镜开销。

数据与操作只通过 `actions.js`：保留 `startNew`、`continueLatest`、`loadSession`、
`openCharacters`、`openSettings(page)`、`listSessions`；新增 `startNewWith`、`previewSession`、
`getActiveId`、`getStatus`、`getBackgrounds`、`listCharacters`、`getAvatar`、`playExcerpt`、
`stopExcerpt`、`subscribeSettings`、`voiceEnabled`。
`listSessions` 每次返回独立的 `{ id, title, characterName, lastLine, updatedAt, background, settings, excerpt, hasUserMessages }`；
`background` 是按会话、作品、时间解析的图片 URL，时间戳为毫秒，`excerpt` 不暴露原始可变消息。

首页打开给舞台加 `is-home`：横屏立绘按 `--sprite-cx` 的实际构图中心平移到约 1380u，
竖屏不移动；文本框和快捷菜单隐藏且 inert，首页添加左侧/底部渐变暗角。
进入对话时去掉 class，立绘用 300ms 回到原位、对话层淡入，hook 将首页淡出 300ms；
`prefers-reduced-motion` 时瞬间完成。仅进入对话的 action 包装淡出，预览不包装。
竖屏上部约 55% 留给场景，下部为可滚动深色摘录与整行主按钮，最近会话横向滚动。
打开与预览后焦点放在主按钮，首次使用放在第一个未完成步骤，均使用 `preventScroll`。
Esc 依次关闭说明框、下拉列表、全部会话/新对话页，首页本身不响应；组字按键忽略。

Sources read: the approved `staged-launching-otter.md` plan, local `play-notes.md`,
`yuzusoft-directing.md` plus statistics JSON, `tenshi-assets.md`, stream coordinator,
portrait controller, P1 `conversation.js`, `PORTRAIT_EXPORT.md`, and `scripts/dev_standins.py`.
Local references, extracted files, and screenshots stay untracked; descriptions here are original and contain no dialogue.

## 1. Goals and non-goals

Replace chat bubbles with a readable visual-novel stage: large character art,
a bottom message band, synchronized speaker crop, and one advanceable line.
Chinese text and Japanese voice remain the default content pairing.
Keep FastAPI, vanilla ES modules, Vite, existing authentication, and chat history.
Give the renderer normalized lines regardless of the LLM's reply format.
Separate emotion, concrete face, persistent appearance, and transient motion.
Support missing art, missing voice, interrupted generation, and old histories.

Phase ownership follows the approved plan; P2 has two separately gated parts:

| Phase | Deliverable |
| --- | --- |
| P1 | Extract the conversation controller without changing the UI; replace source-slicing tests in the same change. |
| P2a | Normalize old replies into line/audio events, persist `lines_json` through six paths, and add line playback; prompts stay unchanged. |
| P2b | Add optional `lines`/`act` output, acting menus, per-line reference audio, new history replay, and non-streaming/Telegram support; enable after measurement. |
| P3 | Add opt-in ADV stage, textbox/crop, line player, acting, signed images, complete old-feature mapping, and split portrait state from rendering. |
| P4 | Turn the message list into LOG with line replay; complete settings and interaction preferences. |
| P5 | Add layered v3 sprites, supported blink/mouth animation, conversion tooling, tags, five Tenshi heroines, and rebuilt Natsume assets. |
| P6 | Add local backgrounds, selection, time variants, and clock following. |
| P7 | Add title/start/continue/load/character/settings screens, make ADV default, and retire the old layout and compatibility IDs. |
| P8 | Refine transitions/preloading, implement portrait-phone layout, and update README/CHANGELOG. |

P0 supplies this spec, local reference capture, asset reconnaissance, a small
format experiment, helper feasibility, and GPU-free development stand-ins.
Dependency order: P0 → P1 plus P2a backend → P2a frontend → P3 plus P2b →
P4 → P5 → P6 → P7 → P8. P2b and P3 can proceed independently on `line`.
The old chat layout was the default through P6 and is removed in P7.
Each implementation phase has its own PR and user acceptance before merge.

Non-goals: scripted branching/rollback, favorite voices, screenshots/gallery tools,
automatic scene/outfit inference, BGM, Live2D, or proprietary UI artwork reproduction.
P3 targets desktop landscape; P8 owns phone portrait composition. Multi-speaker generation is outside P2.
Never infer speakers from quoted names or require commercial assets to boot, test, or ship.

Architecture uses `initX(dom, callbacks)` injection and disposable subscriptions.
`conversation.js` owns send/stop/retry, message construction, persistence, and busy.
It exposes `on(type, listener)` and events `turnstart`, `text`, `line`,
`line_audio`, `done`, `error`, `cancelled`; `audio_chunk` remains forwarded.
`adv/stage.js` owns layout; `textbox.js` owns text/input; `line_player.js` owns
timing; `sprite.js` owns drawing; `quick_menu.js` owns controls.
`backlog.js`, `background.js`, and `title.js` arrive in P4, P6, and P7.
Renderers never call the LLM directly or independently append assistant messages.

## 2. Stage

### 2.1 Logical coordinates and scaling

The desktop stage is a 1920×1080 logical canvas with origin at its top left.
`--u` is a CSS length: one logical pixel at the current viewport scale.
Use CSS dimensions, including font sizes; never scale the stage with a transform.

```css
:root { --u: min(calc(100vw / 1920), calc(100dvh / 1080)); }
.adv-shell { position: fixed; inset: 0; display: grid; place-items: center; background: #000; }
.adv-stage { position: relative; width: calc(1920 * var(--u)); height: calc(1080 * var(--u)); overflow: hidden; }
```

At 1920×1080, `--u = 1px`; at 1366×768 it is about `0.7111px`.
Center the stage and letterbox unused viewport space in black.
Inputs and IME candidate geometry must use their actual layout coordinates.
The shell owns letterbox hit testing; clicks there do not advance the dialogue.
Fullscreen recomputes the same rule; resizing preserves line/page/input state.
P8 may change phone composition, but retains logical art coordinates internally.

### 2.2 Layers, framing, and background

| Bottom to top | Owner | Content |
| --- | --- | --- |
| 0 | shell | Black letterbox and fallback surface. |
| 10 | background | Cached background image or original CSS sky. |
| 20 | sprite | Character state; A/B render buffers for crossfades. |
| 30 | sprite effects | Head-anchored original SVG marks. |
| 40 | textbox | Gradient band, speaker crop, name, text, input, VOICE. |
| 50 | quick menu | Hover/focus menu and model status indicator. |
| 60 | overlays | Drawer, LOG, settings, token dialog, title/load panels. |

The speaker crop appears above the textbox gradient; existing token/settings dialogs remain above the stage.
Overlays trap focus and suppress stage shortcuts; dismissing restores focus.
No chapter/BGM badges are required: the chat has no authored chapters or score.

Default framing is bust-up, with the top of the head near `y=70u`, eyes near
`y=260u`, shoulders near `y=470u`, and torso cropped by the band/stage edge.
The visible head-to-waist region occupies about `900–1100u` vertically;
character width is typically `650–850u`, centered at `x=1100u`.
These are **provisional** art targets, not dimensions of transparent source PNGs.
Manifest stage calibration corrects each outfit/pose; never align by file height.
Close-up uses about `1.35–1.60×` the calibrated bust-up scale, eyes near `y=300u`,
and a visible head about `430–540u` high, filling much of the upper half.
Close-up is a renderer/preview framing preset, not a new per-line LLM field.
Never stretch the character or move facial layers independently to repair alignment.

Give the background a shallow depth-of-field treatment behind sharp sprites.
Default blur is `6u`, configurable `0–12u` in P6; these values are **provisional**.
Use a cached image with `filter: blur(...)` or a pre-blurred variant.
Overscan by at least twice the blur radius to prevent transparent edge halos.
Do not blur the stage container, text, face crop, or overlays.
An absent background renders an original CSS gradient sky immediately.

### 2.3 Loading and performance contract

Decode every replacement image with `await image.decode()` before making it visible.
Retain the previous decoded state until all required next-state layers are ready.
Commit stage and crop from one render revision in the same animation frame.
Late decodes from a previous line, character, or session must not repaint the stage.
Use a `300ms` crossfade by default; reduced motion makes the swap immediate.
Neither network arrival nor a speculative preload may change the displayed face.
On decode failure, keep the last valid state and show a recoverable status.

Preload only the current outfit and pose, plus the next two known line states.
Limit speculative image requests to two concurrent requests and six image URLs.
Limit estimated decoded image memory to 96 MiB, excluding the two active buffers.
Estimate RGBA memory as `width * height * 4`; evict least-recently-used preloads.
Active/pending states are pinned; oversized art skips speculative loading.
Do not preload every emotion pool across all outfits as the old controller does.
P5 should export cropped textures with origins rather than huge transparent rasters.
Use no `backdrop-filter` anywhere on the stage, including the message band.
Animate transforms/opacity rather than layout, and stop animation loops when hidden.
P8 measures load/decode/frame behavior; local recon alone proves no browser FPS claim.

Acceptance: at both desktop sizes, no stretched art, clipped names, blurred text,
IME displacement, flash of an undecoded image, or stale-session repaint occurs.

## 3. Message window

### 3.1 Geometry and appearance

Hands-on notes describe a frameless crop, bottom gradient, aligned name/text,
click-wait glyph, and VOICE display. The following reconstruction is original CSS.
All dimensions in this table are logical multiples of `--u` and **provisional**.

| Element | Rectangle / metric in `u` | Contract |
| --- | --- | --- |
| Gradient band | `x=0, y=840, w=1920, h=240` | Approximately bottom fifth; fade from transparent above to dark blue below. |
| Band fade extension | `y=780–840` | Soft transition, no hard opaque rectangle. |
| Face window | `x=0, y=696, w=450, h=404` | Frameless head/shoulders; lower `20u` bleeds beyond the stage. |
| Name plate | `x=468, y=822, w=1120, h=48` | Left-aligned with the text column; no separate solid plaque. |
| Primary text | `x=468, y=884, w=1154, h=60` | `34u` type, `46u` line height, white with soft shadow. |
| Subtitle | `x=468, y=950, w=1154, h=54` | `30u` type, `42u` line height, slightly dimmer. |
| Click-wait glyph | `x=1640, y=990, w=30, h=36` | Original feather-like SVG, outside the text's hit-free area. |
| VOICE display | `x=1710, y=988, w=160, h=28` | Label plus progress/level segments. |
| Quick menu | `x=450, y=1030, w=1420, h=42` | Compact icon buttons; focusable hit regions at least `40u`. |

The 450×404 crop is grounded in Tenshi's face-thumb rectangle, not a claim that
all Yuzusoft games have an identical final on-screen face rectangle.
The directing report's 1920×1440 UI resource includes auxiliary square-screen
regions; it does not change the approved 1920×1080 application viewport.
Use the table for the prototype, then visually review long names and dual text.
If either language exceeds its space, paginate at punctuation; never clip it.
Keep both languages attached to one logical line through all display pages.

`advWindowOpacity` defaults to 75, range 0–100, applied to the band only.
At 75 the strongest gradient stop has alpha 0.75; text/crop remain fully opaque.
Use original neutral/dark-blue stage styling and existing purple settings accents.
Settings include a live preview with synthetic bilingual content and the crop.
P3 ships original CSS; optional local skin selection is completed in P4.
Skin imagery must not determine text hit testing or language semantics.

### 3.2 Speaker, languages, and line status

Display the current character's name between decorative name brackets.
Narration has an empty name plate and no speech brackets.
Player input replaces the name plate with the configured player name.
Names and lines are inserted as text, never executable markup.
The P2 wire schema has no speaker field: assistant lines belong to the bound
character; user/system messages supply player/narration modes to the renderer.
Reserve `speakerKey` internally for character identity, separate from display name.

Primary defaults to `zh`; subtitle defaults to `ja` and may be `none`.
Primary options are `zh` or `ja`; subtitle options are `zh`, `ja`, or `none`.
The game offers more languages, but this app only possesses these two texts.
Do not offer English or script conversion without an actual content source.
When primary changes to match subtitle, exchange the two existing selections.
X swaps primary/subtitle when both exist; with subtitle `none`, X is disabled.
Changing languages reflows the current line without replaying voice or acting.
An empty selected text falls back to the other nonempty text, with no duplication.
Japanese voice remains Japanese when Chinese is hidden or moved to subtitle.
Use the same font family for both lines; subtitle size remains close to primary.
This refines the plan's small-subtitle wording using the hands-on observation.
Spoken text receives display-only quotation marks; never double-wrap existing ones.
Keep raw stored text unchanged by typography, pagination, and language swapping.

The click-wait glyph appears after the current display page is fully revealed.
It signals advance availability even while voice continues; it is absent in input,
generation-with-no-line, and blocking overlays. Reduced motion makes it static.
VOICE appears only for an actively playing line or explicit LOG replay.
Progress is elapsed/total duration for that line's chunks; unknown duration uses
a non-numeric activity state. Level segments may use analyser amplitude in P5.
`skipped`/`failed`, pause, stop, and final audio end clear its active indication.

### 3.3 Face window

The face window always represents the current speaker, including when her stage
sprite is hidden or a future scene image occupies the stage.
It remains visible for unvoiced character lines; audio presence is not the rule.
Hide it for narration and the player's turn, including player recording/input.
Use an empty corner or original subtle watermark; never ship the game's logo.
If the speaker has no usable portrait, show no crop and keep the name/text usable.
Never substitute the previous speaker, a static avatar, or unrelated expression.
Both views consume the same resolved `{outfit, pose, face, blush, fx}` revision.
Crop the composited sprite using `faceRect`; do not load a separate face asset.
Transient blink/mouth partner selection is shared by both views in P5.
Whole-body stage motion need not shake the textbox crop; its facial state must match.

### 3.4 Quick menu and shortcuts

Reveal the menu on bottom-edge hover or keyboard focus; keep a visible focus cue.
Provide labelled buttons, tooltips, and `aria-pressed` for toggles.
The game has many save/scene/choice controls; only the following map to this app.

| Item | Shortcut | While player types |
| --- | --- | --- |
| AUTO | A / F6 | Disabled; entering input cancels AUTO. |
| SKIP toggle | F / F7; hold Ctrl for temporary skip | Disabled; Ctrl retains native editing behavior. |
| LOG | R / F5; wheel up outside input | Letter shortcut disabled; button/F5 open LOG and preserve draft. |
| Replay voice | F9 | Disabled in player mode; LOG entries have their own replay. |
| Hide window | H / right mouse button | Disabled inside input; right-click keeps the editor context menu. |
| Settings | F8 | Available outside IME composition; preserves draft. |
| Swap languages | X | Disabled in input or with subtitle `none`. |
| Model status | Click status point | Opens existing model settings; accessible name includes model/status. |

Prevent browser defaults only after a recognized, enabled action is handled.
All letter shortcuts are suppressed in any input/textarea/contenteditable.
F5 is an app LOG shortcut only when stage focus owns the event; elsewhere reload
remains native. Do not intercept OS/browser modifier combinations indiscriminately.
Esc closes the top overlay, otherwise stops generation; Ctrl+K creates a session
outside composition, using existing unsaved-draft handling.
There are no dead save/choice/chapter/flowchart/favorite/screenshot menu buttons.

## 4. Interaction

### 4.1 Line state machine

`line_player.js` is DOM-free and accepts injected clock/audio/render adapters.
Its key is `(sessionId, turnId, line.index)`; indexes restart at zero per reply.
States are `idle`, `waiting_line`, `waiting_audio`, `revealing`, `waiting_advance`,
`input`, `suspended`, and `error`. Voice playback is tracked independently.
An overlay suspends progression; closing it restores the same line/page.

| Trigger | Result |
| --- | --- |
| Turn starts, no line yet | Character name, gentle thinking visual if available, ellipsis indicator. |
| Next line available | Resolve acting, wait briefly for audio, then reveal its first page. |
| Audio `ready` | Decode and play only when its matching line is current. |
| Audio `skipped` or `failed` | Reveal without waiting; failure is available in LOG/status. |
| Audio absent after line activation | In voice-priority mode, keep text hidden until playback starts, audio fails/is skipped, or the turn finishes without audio. Manual advance can reveal early. Reading mode retains the 1500ms preview deadline and accepts late audio only while this line is current. |
| Click / Enter / Space during reveal | Complete current page immediately, without advancing or stopping voice. |
| Same action after reveal | Advance one page, or request the next logical line on the final page. |
| Last line exhausted but generation open | Show waiting indicator; do not invent a player turn. |
| Last line exhausted and turn settled | Enter player input mode. |
| Stop / error / session change | Cancel owned timers/sources; retain delivered history; reject stale events. |

An audio wait timeout is a presentation fallback, not a synthetic server terminal.
Do not let an SSE heartbeat extend it or the request's meaningful-progress deadline.
`done` means generation/audio metadata settled, not that the user read every line.
Server completion must not force the line player directly into input.
Default reveal interval is `30ms` per grapheme; zero means instant text.
Use grapheme segmentation where supported, with a Unicode-code-point fallback.
Display pages do not allocate new line indexes or retrigger face/motion/TTS.
Prefer punctuation boundaries; if none fits, break at a grapheme boundary.
Reflow after resize keeps an offset into the raw line rather than losing content.

### 4.2 Hide, LOG, SKIP, and AUTO

Right-click on the noninteractive stage or H hides the band/crop/menu.
The next stage click or Esc restores them and consumes that action without advance.
Hiding pauses AUTO progression; voice may finish; no unread line is silently lost.
Wheel up outside editors/scrolling overlays opens LOG; it never rewinds the model.
P3 may open the existing message-list overlay; P4 adds normalized per-line entries.
LOG shows speaker, primary/subtitle, and available replay controls, with older-page
loading preserved. While a turn is still being presented, LOG lists only the lines
the stage has shown; queued lines appear once shown or skipped, or when the turn
ends. Expired seven-day audio cache entries have disabled replay.
Opening LOG cancels SKIP, pauses AUTO and voice, and preserves the current page.
Only one voice source group, including replay, may own output at a time.

Hold Ctrl to skip or use the SKIP toggle; release Ctrl/blur clears temporary skip.
SKIP completes pages and advances queued lines at a maximum of one every `80ms`.
It suppresses voice, body motion, and transient fx; state and LOG still update.
It waits when the next line has not arrived and stops on input/error/overlay.
No read-only skip filter is required before a persistent read-state model exists.
Do not mark skipped text as actually read for a future such feature.

AUTO uses the displayed primary text's grapheme count, excluding decorative marks.
For each display page, `D = clamp(baseMs + perCharMs * count, 1200, 6000)`.
Defaults are `baseMs=1500`, `perCharMs=45`; these follow the approved plan.
`normal` starts D when the page finishes revealing; voice can extend the wait when
interrupt-on-advance is off. `voice-priority` starts D after both reveal and the
line's voice end; this is the default and matches the plan's post-voice wait.
With no voice, skipped/failed audio, or voice mode off, both use reveal end + D.
For nonfinal display pages, use text timing; do not wait for/replay the full voice
on every page. Voice-priority gates the final page on voice completion.
AUTO never sends player text and never proceeds through an error or token dialog.
Manual advance resets the page timer; it does not silently enable AUTO.
The notes' game defaults (2.00 seconds plus 25 units) are observations, not app defaults.

`advInterruptVoice` defaults true: advancing to another logical line fades/stops
only current-line sources over `40ms` and resets audio scheduling to current time.
When false, record the advance request and retain the current line until its voice
ends; then advance once. Repeated clicks do not queue many advances or overlap voices.
Typing, explicit stop, session change, and SKIP always interrupt, regardless of this setting.
Completing reveal or changing display pages never interrupts the line's voice.

### 4.3 Player input and recovery

Player input lives in the text column, with player name and microphone button.
Use a real textarea with visible caret; do not simulate an editor on a canvas.
Enter sends nonempty text, Shift+Enter adds a newline, and Space types a space.
An empty Enter does nothing in input; send is disabled while the controller is busy.
Speech recognition inserts into the same draft; retain existing recording metadata.
Player recordings remain playable on their user message in LOG.

Check `event.isComposing`, tracked composition state, and `event.keyCode === 229`
before send, advance, skip, new-session, or shortcut handling.
Do not send on the Enter that commits an IME candidate, including a browser where
`compositionend` precedes that key event; retain a commit-key guard until keyup.
`compositionstart` must not erase the draft or advance dialogue.
Synthetic key tests cover guards, but actual Chinese IME acceptance is manual.

Allow the user to focus/type into the textbox before queued assistant lines finish.
On the first actual edit/composition start, cancel AUTO/SKIP, stop current voice,
complete/log the current delivered line, discard its unread presentation pages,
and mark all remaining delivered lines as skipped for presentation only.
Keep all their text/acting/audio metadata in LOG and persistence.
If generation continues, append future lines to LOG without returning to playback.
Keep the draft editable; enable sending after generation settles, or after explicit
Esc/Stop cancels it. Do not silently abort generation or submit overlapping turns.
Programmatic focus, language changes, and IME navigation alone do not submit text.

Errors show a short reason and Retry / Settings actions in the band.
Retry delegates to `conversation.retry` and preserves the failed user's recording.
It must not add a second user message or duplicate already persisted partial text.
Show partial replies in LOG with interrupted/failed status; never claim completion.
P1's controller currently stores replies based on delivered Chinese deltas;
P2a must also preserve meaningful normalized lines, including Japanese-only lines.

### 4.4 Accessibility and old-feature mapping

Subscribe in JS to `matchMedia('(prefers-reduced-motion: reduce)')` and its change event.
When true, immediately finish the typewriter, cancel body/fx animations, and disable
blink/lip animation; apply subsequent states without crossfade or glyph motion.
CSS media queries are complementary; they cannot cancel JS timers themselves.
Use one offscreen `aria-live="polite"`, `aria-atomic="true"` region for speaker plus
complete primary line when activated; never announce each typewriter character.
Keep the visible typewriter non-live and subtitle optional for screen-reader reading.
Do not move focus on every line; errors use a concise alert; overlays restore focus.
Controls are keyboard reachable and disabled states are semantic, not just dimmed.

| Existing feature | ADV destination / acceptance |
| --- | --- |
| Session list and history browsing | ADV session drawer; title Load uses the P7 actions hook; LOG pages older history and disables paging during generation. |
| Microphone / speech input | Microphone inside player textbox with current permissions/recovery. |
| Disable automatic voice | Existing voice mode off: typewriter and advance work; AUTO uses text count. |
| Failure note / resend | Textbox reason with Retry and Settings; LOG retains recovery actions after reopening a failed session, preserving controller retry semantics. |
| Model badge | Accessible status point next to quick menu; opens existing settings. |
| Japanese translation panel | Subtitle selection and LOG bilingual lines. |
| Player recording bar | Playback button on the matching player LOG message, including failed sends. |
| Token and settings modals | Existing dialogs over the stage; retain cancellation/authentication behavior. |
| Hint/toast bars | Anchor above the message band instead of assuming a bottom chat input. |

Acceptance: every row is reachable in the P3 preview without returning to bubbles;
P4/P7 may improve its destination but may not temporarily remove the capability.

## 5. Line protocol

### 5.1 P2a server events: old prompt, normalized lines

Keep `{"tts":{...},"chinese":"...","japanese":"..."}` prompting unchanged.
Keep existing `text`, `audio_chunk`, error, and `done` consumers functioning.
All new identifiers below describe required P2a changes, not current server output.
SSE names are in the `event:` field; JSON objects below are their `data:` payloads.

```typescript
type Emotion = 'gentle' | 'happy' | 'shy' | 'tsundere' | 'cool' | 'sad' | 'angry';
type Motion = 'nod' | 'bounce' | 'startle' | 'sigh' | 'fidget' | 'head-shake' | 'lean-in' | 'step-back';
type Fx = 'sweat' | 'anger' | 'heart' | 'sparkle' | 'question' | 'exclamation';
type Act = { face?: string; pose?: string; motion?: Motion | null;
             blush?: boolean; fx?: Fx | null };
type Line = { index: number; zh: string; ja: string; emotion: Emotion; act: Act };
type Chunk = { index: number; line: number; audio_url: string; sentence: string };
// event: line
// data: Line                           // act is {} throughout P2a
// event: audio_chunk
// data: Chunk & { emotion: Emotion }    // existing fields plus line
// event: line_audio
// data: { line: number; status: 'ready' | 'skipped' | 'failed'; chunks: Chunk[] }
```

`index` on `line` is zero-based, contiguous, immutable, and local to one reply.
`index` on an audio chunk remains its existing reply-wide chunk ID, possibly sparse.
`line` is the foreign key; neither array length nor chunk index is a line number.
One display line may own zero, one, or several Japanese audio chunks.
`ready` means its nonempty ordered chunk list has usable audio URLs.
`skipped` means no synthesis is needed/available by policy; `chunks` is empty.
`failed` means synthesis could not finish; chunks may contain successful partials.
On partial failure, play available chunks, indicate incomplete voice in LOG, and
never keep waiting for a missing chunk. Status describes server production, not
browser decoding: download/decode failure is a local playback failure.
Existing `audio_chunk_error` may remain for diagnostics; `line_audio` is authoritative.

Ordering guarantees within one live, normally completed reply:

1. Emit `line` indexes in increasing order exactly once; text/acting never mutate.
2. Emit a line before any `audio_chunk` or `line_audio` that references it.
3. Emit its successful chunks in synthesis order, then exactly one `line_audio`.
4. Preserve increasing line order in the serial TTS worker's terminal outcomes.
5. Emit `done` only after all announced lines have terminal audio outcomes.
6. `done.lines` contains the same normalized lines and audio outcomes, in index order.

Define persisted records as `Line & {audio:{status:'ready'|'skipped'|'failed',chunks:Chunk[]}}`.
`done.lines`, frontend `message.lines`, and decoded `lines_json` use this record array.
It is a snapshot for reconciliation, not permission to replay duplicate live events.
On cancellation/error, retain complete announced lines; mark unfinished audio failed
in the persisted snapshot. A disconnected stream cannot guarantee terminal delivery.
`done.truncated` and transport errors release all client waits; late callbacks are ignored.
A stream ending without `done` is interrupted, never silently considered successful.

Old-format normalization splits Chinese and Japanese on sentence-ending punctuation
and explicit paragraph boundaries, retaining punctuation and ellipsis-only units.
Do not split inside quoted JSON strings until decoded; commas are not display boundaries.
Equal sentence counts pair one-to-one; unequal counts group adjacent units in order
using cumulative character-length proportions, never reorder, duplicate, or lose text.
Use at most `min(zhCount, jaCount)` groups when both sides exist; a single-language
reply keeps its own units and supplies empty strings for the missing language.
Tie boundaries choose the earlier punctuation boundary; tests fix deterministic results.
This is approximate translation alignment, not a claim of semantic equivalence.
The current first-comma Japanese TTS split may produce several chunks for one group.
Track original sentence/chunk IDs through grouping; preserve the audio-to-line map.
P2a may buffer normalized events until grouping is stable, including finalization;
do not revise an emitted line after learning final sentence counts.
Continue early TTS work and legacy text streaming; do not promise P2b latency in P2a.
Use the existing per-sentence cue detector with reply emotion fallback, then gentle.

Add nullable `messages.lines_json TEXT` with `_add_column_if_missing`.
Do not increment schema v7: downgrade protection must not block opening the DB.
Null means legacy/unavailable data, while `[]` is an intentionally empty line list.
Validate arrays and nested types at boundaries; malformed JSON falls back to old text.
Retain aggregate Chinese/Japanese/audio fields for older consumers.
Avoid a destructive migration or rewriting all old messages during startup.

The plan's six persistence surfaces must round-trip `lines`:

| Surface | Required change |
| --- | --- |
| Browser localStorage | Preserve complete/partial normalized message lines during save/reload. |
| `mapBackendMessage` | Map decoded backend lines without dropping acting or indexes. |
| `portableSnapshot` | Keep line text/acting/indexes; remove cache-specific audio references. |
| Import API and history API | Validate/store/return line arrays, retaining old imports. |
| Backup query | Select `lines_json` and export portable line records. |
| `add_message` | Write line records on successful and interrupted assistant persistence paths. |

Portable line audio is normalized to `{status:'skipped',chunks:[]}` because exported
JSON does not carry cached WAVs; do not export local paths or signed image URLs.
Line text and acting survive audio expiry/import. History replay failure is recoverable.
Frontend ADV buffers audio by explicit line; old chat continues sequential playback.
P2a must prevent the controller from immediately autoplaying ADV's future lines.

### 5.2 P2b reply format and acting semantics

The browser requests this format by default (`advReplyFormat='lines'`). A session set to
`legacy` requests the old whole-reply format instead. A stored `old`, the default before
this format became the default, reads as `lines`. API clients that omit `reply_format`
still get `old`.
The LLM's JSON has this shape; placeholder strings below are not dialogue examples.

```json
{
  "tts": {"speed": 1.05, "temp": 0.95, "emotion": "gentle"},
  "lines": [
    {"zh": "<Chinese text>", "ja": "<Japanese text>", "emotion": "gentle",
     "act": {"face": "smile", "pose": "b", "motion": "nod", "blush": false, "fx": null}}
  ]
}
```

Require string `zh`/`ja`, at least one nonempty, and one of the seven emotions.
`act` and every member are optional; never require padding fields on every line.
The server assigns `index`; the model supplies neither indexes nor URLs.
`tts` supplies reply defaults subject to existing numeric clamps and user overrides;
line emotion chooses reference audio and visual fallback independently of its speed.
Keep `temp` in the LLM format; existing `tts_params.temperature` may retain its API name.

Evidence is from Café Stella's 69 compiled scenarios across authored branches:
41,590 text records, 20,009 voiced records, and 18,247 comparable speaker-line pairs.
It is static authored frequency, not Tenshi runtime probability or an LLM quota.

| Field | Semantics and evidence |
| --- | --- |
| `face` | Resolve every line, even if omitted. Raw face changes in 16,879/18,247 pairs (92.50%); pose+face changes in 95.02%. Request concrete semantic face tags on nearly every generated line when assets support them. |
| `pose` | Optional sticky pose/arm/orientation choice. All-character changes are 40.15%; five heroines are about 44–54%. A pose change is not a body animation. |
| `motion` | Optional one-shot enum. Only 1,203 ordinary-character object updates match body motions; their event count is not a per-line probability. Omission never repeats the prior motion. |
| `blush` | Optional sticky boolean; explicit false clears added blush. Explicit cheek layers occur on 1,111/18,956 known voiced states (5.86%); observed runs average 3.027 speaker turns. |
| `fx` | Optional rare one-shot mark. Omission/null means none for this line. No verified general heart/anger layer axis exists in the directing corpus; original SVG is an application feature. |
| `outfit` | Excluded from `act`; selected outside generation and sticky across lines. Dress changes in only 315/18,247 within-scene pairs (1.73%); automatic outfit inference is deferred. |

These rates justify distinct axes and subtle face variety, not forced emotional
changes or a body bounce on every sentence. Missing-state lines are not zero changes.
Stage/face co-visibility is common: 16,169/20,009 voiced records show both.
The face window therefore follows speaker identity, not whether a stage sprite exists.

Canonical motion values are `nod`, `bounce`, `startle`, `sigh`, `fidget`,
`head-shake`, `lean-in`, `step-back`. `bounce` means a light laugh-like bob.
Do not send `laugh`, `head-tilt`, free-form CSS, or arbitrary animation parameters.
The broader plan examples are narrowed to this fixed P2b vocabulary.
Canonical fx values are `sweat`, `anger`, `heart`, `sparkle`, `question`, `exclamation`.
Both fields accept null; arbitrary arrays, screen shake, and flashes are invalid.

Default state resolution, in order: validated explicit field, retained sticky field
where applicable, emotion-derived fallback, then first valid manifest default.
For `face`, use emotion pools with existing micro-drift/recent-three anti-repeat.
For `pose`, retain the current valid pose; initialize to the outfit's default.
For `blush`, explicit values override everything and remain sticky until explicitly
changed/session reset; before any explicit value, preserve existing affection logic.
That fallback enables blush at affection ≥2 for shy/tsundere/angry, ≥3 otherwise,
only when compatible art exists. No new per-line affection mutation is introduced.
For motion/fx, every emotion defaults to null; do not infer frequent effects.
Face fallback tags prefer gentle→neutral/smile, happy→smile, shy→embarrassed,
tsundere→pout, cool→neutral, sad→sad, angry→angry, only when offered by the pack.
`thinking`/`surprised` are existing frontend presentation keys, not new wire emotions.
The stand-in includes `surprised`; it tests normalization to the seven-key taxonomy.

Build an acting menu from the active character manifest and selected outfit.
Include semantic face tags, compatible poses/faces, defaults, blush capability,
and the canonical motion/fx lists. Whole-image packs advertise only usable choices.
Example menu metadata: `{characterId, outfit, poses:[{id,faces:[tag]}], motions, fx}`.
Validate pose first, then face against the effective pose/outfit combination.
Unknown values/wrong types are dropped individually and logged with character,
line index, field, and sanitized value/reason; do not discard a whole valid line.
Reject additional `act` keys, including `outfit`; never treat identifiers as paths.
Invalid/missing emotion uses the existing classifier then gentle; record a metric.
Resolve supported fallback choices deterministically; do not expose raw model HTML.

Use a quote/escape/nesting-aware incremental scanner to extract complete line objects.
It must tolerate chunk boundaries inside escaped quotes and multibyte characters.
Deliver via a side channel without changing `feed_chunk`/`finalize` signatures or
the `text_pipeline.py` return tuple. Emit a complete first line before the full reply.
`finalize` and `parse_full_text` both aggregate `lines` into Chinese/Japanese text.
Handle fences, preambles, truncation, and invalid acts without executing extra text.
Keep complete validated objects on truncation; never fabricate an unfinished string.
Telegram and non-streaming endpoints receive nonempty aggregate text and usable audio.

TTS work items become `{line, ja, emotion}`; synthesize once per line unless engine
limits require splitting. All child chunks retain that line foreign key.
Select compatible `refs/<emotion>.ogg` per line; fall back to the profile's normal
reference if absent, logging the fallback. Do not fail valid text for a missing variant.
Preserve user TTS options/adaptive-voice precedence; face tags never select file paths.
Reference emotion and synthesis options belong in cache identity to prevent wrong reuse.

When lines mode is on, `_format_assistant_turn` replays stored lines in the new
`{"tts":{...},"lines":[...]}` format; strip server indexes/audio from model context.
Convert old history temporarily through P2a normalization; do not rewrite its DB rows.
Strip the known old format template at runtime, then append the new contract.
Cover character-manifest prompts, database prompts, and `apply_character_fixes.py`.
Local `system_prompt.txt` updates are a separate backed-up migration, never implicit.
With lines mode off, retain old prompting and history behavior.

Acceptance before default enablement: exercise the usual model with all 13 profiles
for 20 turns each; report raw format compliance, valid acts before sanitization,
motion/fx frequency, output length, and first-complete-line/first-audio latency.
Proposed enablement gates: ≥95% raw format compliance, ≥98% legal provided act
fields, 100% recoverable text, and no unexplained latency regression above 20%.
These gates are product thresholds, not measurements already obtained in P0.
Until visual review accepts acting frequency, keep P2b opt-in regardless of syntax rate.

## 6. Sprite system

### 6.1 Shared state and rendering modes

`SpriteState = {outfit:string, pose:string, face:string, blush:boolean, fx:Fx|null}`.
State is per character; `motion` is an event and must not become a sticky member.
`fx` holds the current line's mark, cleared on line exit/expiry, not inherited.
The reducer resolves intent to a valid state; drawing consumes it without reselection.
P3 splits current `portrait.js` into pure selection/state and injected render logic,
retaining a compatibility wrapper for the old stage through P7.
Preserve character/paint revision guards and abort superseded manifest requests.

Whole-image mode supports `expressions.json` and older `manifest.json`, using existing
costume→face pools, default pose when absent, and paired blush images where available.
Calibrate `faceRect` once per source/pose; a missing calibration disables the crop
until a pack default is supplied, rather than pretending a random crop is correct.
P3 should use locally rebuilt high-resolution Natsume whole images where available.
The existing 351×1080 exports can prove behavior but not close-up visual quality.

Layered mode reads `portrait/layers.json`, `version:3`; legacy packs remain valid.
Recon confirms merged facial partials plus body/hair/blush/effect layers, not a
general eye/brow/mouth compositor. A/B sources often mean poses 1/2 versus 3.
Pose/outfit support is not a Cartesian product; validate actual recipes.
Scope remains the five planned Tenshi heroines plus Natsume; the extra heroine
found by recon does not expand P5 asset delivery automatically.

### 6.2 v3 manifest schema (`portrait/layers.json`)

The layered portrait pack contract between the asset exporter and the app.
All paths are relative to the character's `portrait/` folder. Coordinate values are integers.

```json
{
  "version": 3,
  "defaultOutfit": "uniform",
  "defaultPose": "a",
  "canvases": {"A": {"width": 1875, "height": 2656}},
  "outfits": {"uniform": {"name": "校服", "icon": "🎒"}},
  "poses": {"a": {"name": "站姿1", "canvas": "A"}},
  "faces": {
    "01": {"label": "平常", "jp": "1通常１", "emotion": "gentle", "weights": {"gentle": 3}}
  },
  "looks": {
    "a/uniform": {
      "pose": "a", "outfit": "uniform",
      "crop": {"left": 540, "top": 865, "width": 792, "height": 1623},
      "faceRect": {"x": 175, "y": 0, "width": 362, "height": 325},
      "headAnchor": {"x": 356, "y": 2},
      "body": ["A/1278", "A/1301"],
      "blush": ["A/1490"],
      "fx": {"sweat": ["A/1600"]},
      "defaultFace": "01",
      "faces": {
        "01": {"layers": ["A/1511", "A/1402"], "eyesClosed": false,
               "blink": {"half": "16", "closed": "14"},
               "mouth": {"closed": "01", "open": "02"}},
        "02": {"layers": ["A/1512", "A/1402"], "eyesClosed": false, "frameOnly": true},
        "14": {"layers": ["A/1531", "A/1402"], "eyesClosed": true, "frameOnly": true},
        "16": {"layers": ["A/1533", "A/1402"], "eyesClosed": false, "frameOnly": true}
      }
    }
  },
  "layers": {
    "A/1278": {"canvas": "A", "file": "layers/A/1278.webp",
               "rect": {"x": 600, "y": 900, "width": 700, "height": 1500},
               "z": 0, "opacity": 1}
  }
}
```

- **`canvases`:** source canvases. A pose draws on exactly one canvas, and a layer belongs to exactly one canvas. A look may only reference layers on its pose's canvas.
- **`outfits`:** display data for every outfit id used by any look: a Chinese `name`, and an optional one-emoji `icon`.
- **`poses`:** pose ids are short ASCII strings (`a`, `a2`, `b`). `name` is a short Chinese display name.
- **`faces`:** shared metadata for every selectable face id used by any look: `label` (Chinese act tag), `emotion` (one of the 7 primary emotions), `weights` (primary 3, secondaries 1-2), and `jp`. Partner-only faces need no metadata entry.
- **`looks`:** keyed by `"<pose>/<outfit>"`. Pose and outfit support is not a Cartesian product.
  - **`crop`:** the drawing box in canvas pixels. The renderer treats the box like a v2 sprite of `crop.width × crop.height`. A layer is drawn at `rect.x - crop.left`, `rect.y - crop.top`.
  - **`faceRect`, `headAnchor`, and optional `stage`:** relative to the crop, exactly as in v2. Stage placement and the 450×404 face window reuse the v2 code paths unchanged.
  - **`body`:** the dress and pose layers.
  - **`blush`:** optional overlay for `blush: true`. A face may override it with its own `blush` array, or turn it off with `"blush": []`.
  - **`fx`:** optional source effect layers keyed by the P2b fx names. Drawn instead of the SVG mark when present.
  - **`faces`:** face recipes. Blush layers are listed separately and never in face `layers`. `eyesClosed` must be true for faces whose eyes are closed.
  - **`faces[id].frameOnly`:** optional boolean, default `false`. When `true`, this recipe is only an animation partner in this look; tags, emotion pools, drift, animation preferences, defaults and first-face fallbacks cannot select it as an expression. Top-level metadata, if present, is ignored for selection in this look. The same id may remain selectable in another look. Blink, mouth and `combinedFrames` references still render it. Non-boolean values and a `frameOnly` `defaultFace` are errors. An unreferenced partner (including one referenced only by itself) produces a warning, not an error.
- **Draw order:** collect `body` + current face `layers` + active blush layers + active fx layers, then draw in ascending `z`. Equal `z` keeps list order. `opacity` is 0–1 with normal alpha blending.
- **Partners:** optional `blink:{half?, closed}` and `mouth:{closed, half?, open}` referencing compatible face IDs in the same look.
  - Blink and mouth frames keep the blush of the face they animate (its override, otherwise the look's blush).
  - Only faces with `eyesClosed: false` may have `blink`; the `closed` partner must have `eyesClosed: true`.
  - `combinedFrames: {"<eye>:<mouth>": faceId}` maps reviewed combinations during overlapping blink and speech. Without an entry, the blink frame wins over the mouth frame.
  - Missing partners disable that animation for the face.
- **Paths:** canvas ids match `^[A-Za-z0-9_-]{1,32}$`. Each layer's `file` is exactly `layers/<canvas>/<name>`, using that layer's own `canvas`, with `<name>` matching `^[A-Za-z0-9._-]+\.(webp|png)$` and no `..`. Resolved files stay inside `portrait/layers/<canvas>/`.
- **Layer routes:** `GET /api/media/portrait/{character_id}/layers/{canvas}/{file_name}` serves signed, expiring media URLs; `GET /api/characters/{character_id}/portrait/layers/{canvas}/{file_name}` is the console-token fallback. Both use fixed segments and enforce the same path and canvas boundaries.
- **Validation & Precedence:** finite positive dimensions, bounded rectangles, valid references, unique IDs, and the layer path rule above. A valid `layers.json` (v3) takes precedence over `expressions.json` (v2) and `manifest.json` (v1); an invalid `layers.json` logs the reason and falls back to v2/v1.
- **Chat compatibility:** a v3 payload carries a normalized `legacy` payload for the chat UI when valid `expressions.json` (v2), or otherwise `manifest.json` (v1), is available. Its whole-image sprite URLs are signed like existing sprites. Without either, `legacy` is absent; v3 top-level `sprites` remains `{}` and ADV consumes the layered data.

### 6.3 Motion, blink, and lip sync

CSS keyframes implement motions on an outer stage wrapper, preserving base layout.
Initial **provisional** recipes use logical `u`, ease-in-out, and return to origin:

| Motion | Recipe | Total duration |
| --- | --- | --- |
| `nod` | Translate Y `0 → 10 → 0`. | 550ms |
| `bounce` | Two light Y bobs `0 → -10 → 0 → -10 → 0`. | 600ms |
| `startle` | One quick Y hop `0 → -14 → 0`. | 220ms |
| `sigh` | Settle Y `0 → 6 → 6 → 0`. | 1400ms |
| `fidget` | Translate X `0 → -10 → 10 → 0`. | 1200ms |
| `head-shake` | Translate X `0 → -18 → 18 → 0`. | 1000ms |
| `lean-in` | Scale `1 → 1.06 → 1`, Y `0 → 12 → 0`. | 900ms |
| `step-back` | Scale `1 → 0.94 → 1`. | 900ms |

No same motion on consecutive lines; minimum start-to-start interval is 2000ms.
FX expire after 1200ms or line exit; suppress identical fx inside 3000ms.
These are frequency caps, not encouragement to fill every permitted interval.
Cancellation restores the wrapper's base transform; never accumulate relative offsets.
Apply acting when a line activates, not when it arrives over the network.
SKIP, hidden tab, reduced motion, and player input suppress decorative motion.

**Layered rendering (`frontend/src/adv/sprite.js`):**
A v3 line commits one frame set per A/B buffer. The buffers for layers are `<div>`
boxes of the look's `crop` size, created when a layered pack first draws, placed
with the whole-image framing code, and clipped like a whole image.
- The frame set holds the body, one group per face the line's face can show (itself,
  its blink, mouth and combined frames), the blush, and source fx layers.
- Blush belongs to the line's face (its override, else the look's), so blink and
  mouth frames keep it.
- Every layer is decoded (`img.decode()`) before the swap, twice: once for the stage
  and once for the face window. The decoded images become the layer elements.
- Layers get the rank of their `z` as z-index and are appended in list order, so
  equal `z` keeps list order in every group. `isolation: isolate` keeps them inside.
- Blink and mouth frames only toggle which group is visible, in the stage and the
  face window together. They never create, remove or reload an element.
- A stale or failed decode never paints; the previous complete frame stays.
- Preloading decodes the frame sets of the look's next two faces that the current
  frame set does not already hold, in the whole-image queue and budget.
- Source fx layers replace the SVG mark for that fx, never both, and follow the same
  acting gate. They stay for the line instead of expiring after 1200ms.

**Blink scheduler:**
Runs per mounted sprite view for faces with `eyesClosed: false` and a `blink` partner, when `advBlink` is on and reduced motion is off.
- Waits a random interval uniformly sampled between 2 and 6 seconds.
- Sequence: `half` (60ms) → `closed` (90ms) → `half` (60ms) → base face. Without `half`, shows `closed` (90ms) → base face.
- Timers cancel immediately on face, pose, look, or character change, hidden tab, unmount, `advBlink` off, or reduced motion on.
  A new line opens the eyes of the old frame as soon as it starts decoding.
- The stage keeps blinking during player input, while the face window is hidden.
- The resolved base face remains separate from the transient frame, so line and state transitions always resolve against the base.

**Lip sync:**
Routes `chunkGain → masterGain → AnalyserNode → destination` in `frontend/src/audio_player.js`, eliminating duplicate destination connections.
The analyser uses `fftSize=256` and smoothing `0.6`, exposing time-domain RMS.
- Sampled at ≤30Hz in the ADV, active only while a character line's voice is playing (per-line playback and stage F9 replay; user recordings and LOG replay never drive the mouth).
- Levels: RMS `<0.012` is closed, `<0.045` is half, otherwise open, with 40ms hysteresis. When `half` is missing, `open` is used for the half level.
- While speaking, the closed level shows the `mouth.closed` partner.
- Applies only to faces with a `mouth` partner when `advLipSync` is on and reduced motion is off.
  The sampler keeps running through faces without one, because a line's voice can
  start before its face commits.
- Blink overlap: uses `combinedFrames` when matched; otherwise the blink frame wins.
- `startLipSync` returns a token; the end of an older line cannot stop a newer one.
  An interrupt or skip stops whichever line is playing.
- On end, interrupt, or skip, it returns to the base face and stops polling. A paused
  player reads as silent, because a suspended context keeps its last analyser buffer.
  With voice off no line plays, so the mouth never moves.
- `setAnimationOptions({ advBlink, advLipSync })` applies setting changes, previews
  and reverts at once. Turning one off, reduced motion, or a hidden tab shows the
  base face; turning it back on resumes.

## 7. Assets and copyright

Extracted images, voices, UI skins, and source metadata stay in gitignored local
`characters/`, `backgrounds/`, and `skins/`; no source archive or reference capture
enters git, Docker contexts/images, source zips, or ordinary portable releases.
The repository ships original CSS/SVG interface art and synthetic test fixtures only.
Do not reproduce game text or ship its logo, UI bitmaps, sprite sheets, or screenshots.
Update ignore rules and add archive-content regression checks in the relevant phase.
`scripts/package_release.py` must exclude these three folders entirely, not merely
model weights: the plan identified its current character-image packaging gap.
Document local installation separately; absence of assets remains a supported mode.

P3 signs sprite/layer/skin images; P6 uses the same facility for backgrounds.
Authenticated manifest APIs return same-origin expiring image URLs that `<img>`
can request without an Authorization header. Never put the console token in a URL.
Proposed route: `/api/assets/{kind}/{asset_id}?v={revision}&exp={unix}&sig={mac}`.
Kinds are `portrait`, `layer`, `background`, `skin`; IDs resolve server-side entries.
Sign kind, canonical asset ID, revision, and expiry with HMAC-SHA256; TTL is 900s.
Constant-time verification rejects expiry, tampering, unknown kinds, and wrong paths.
Resolve files under the allowed local root; reject traversal, symlink escape, and
encoded separator bypasses before reading. Do not accept arbitrary disk filenames.
Refresh the manifest 60s before expiry, or once after an image authentication error.
Refresh/decode failure keeps the current valid visual and offers retry without loops.
Signed links are scoped bearer access to an image, never API-write authorization.
Do not persist them in exports; revision keys preserve existing cache-busting behavior.
Keep fonts/images same-origin and retain CSP; no `blob:` image allowance is needed.

## 8. Settings and persistence

Every new session setting must be added in the same phase to all three boundaries:
`store.js` defaults, `session_data.js` `pickSettings`, and backend `chat_backup.py`
`SESSION_SETTINGS`. `pickSettings` currently iterates default keys and checks types;
extend it for enums/ranges and nullable values rather than assuming it deep-validates.
The names below are new contract names; do not invent alternate spellings per module.
All are session-scoped so preview/background/acting choices travel with the session.
Keep existing `state.global.voiceMode` and TTS preset behavior; do not duplicate them.

| Phase | Setting | Default / allowed values |
| --- | --- | --- |
| P1 | None | Refactor only. |
| P2a | None | Audio mode is selected by renderer, not persisted as another preference. |
| P2b | `advReplyFormat` | `lines`; enum `lines`, `legacy`. A stored `old` reads as `lines`. |
| P3–P6 only | `advEnabled` (removed) | Ignored/dropped in P7; all sessions use ADV. |
| P3 | `advPlayerName` | `Player`; nonempty string, maximum 40 graphemes. |
| P3 | `advPrimaryLanguage` | `zh`; enum `zh`, `ja`. |
| P3 | `advSubtitleLanguage` | `ja`; enum `zh`, `ja`, `none`, distinct from primary. |
| P3 | `advWindowOpacity` | 75; integer 0–100. |
| P3 | `advTextIntervalMs` | 30; integer 0–100. |
| P3 | `advInterruptVoice` | true; boolean. |
| P3 | `advAutoBaseMs` | 1500; integer 0–6000; final delay still clamped. |
| P3 | `advAutoPerCharMs` | 45; integer 0–200. |
| P3 | `advAutoMode` | `voice-priority`; enum `normal`, `voice-priority`. |
| P3 | `advOutfit` | Empty string means character-pack default; otherwise valid outfit ID. |
| P4 | `advSkin` | Empty string means original CSS; otherwise installed local skin ID. |
| P5 | `advBlink` | true; effective only with compatible v3 partners. |
| P5 | `advLipSync` | true; effective only with compatible v3 partners. |
| P6 | `advBackground` | Empty string means CSS sky; otherwise local manifest ID. |
| P6 | `advBackgroundTime` | `clock`; enum `clock`, `day`, `evening`, `night`. |
| P6 | `advBackgroundBlur` | 6; finite number 0–12 logical units. |
| P7 | No additional keys | Title reads sessions through `advActions`; the preview toggle is removed. |
| P8 | No required keys | Responsive composition and preload policy are automatic. |

P3 owns the behavior/defaults; P4 completes the consolidated preference panel.
P7 retires the preview switch; ignore/drop old stored values for backup compatibility
and route all sessions through ADV.
AUTO/SKIP active flags, drafts, hidden-window state, timers, signed links, analyser
state, and decoded-image caches are transient and must not be restored as playback.
OS reduced motion is evaluated live and overrides animation preference switches.
Unknown/missing settings fall back without deleting unrelated existing settings.
Missing installed assets preserve the selected ID but render a fallback/status.

P6 background manifests map ID/name/source attribution to day/evening/night files.
`GET /api/backgrounds` returns available choices and signed variant URLs.
Initial clock boundaries are **provisional**: day 06:00–16:59, evening 17:00–19:59,
night otherwise, using browser local time. Recheck once a minute and on tab resume.
Manual time selection overrides clock; missing variants fall back to day then CSS.
Store background and time per session; defer automatic dialogue-driven scene changes.
Use the planned Background / Time / Size selection layout; size is a preview-fit
control in P6 unless a later spec adds a persisted crop/zoom setting.

Acceptance: set every new value, switch sessions, reload, export/import through
browser and backend backups, and verify it survives; test invalid enum/range input.
Use primitive defaults or explicitly teach `pickSettings` any new object shape.

## 9. Testing strategy per phase

### 9.1 Shared gates and fixtures

Each phase runs `npm --prefix frontend test` and the full Python suite with the
locked development environment: `uv run --frozen --extra dev python -m pytest -q`.
Integration uses `npm --prefix frontend run deploy` and verifies tracked static
output matches source; never manually copy `dist/*` over preserved static scripts.
Helpers leave `galgame2voice/static` for the phase integrator's single rebuild.
All six required CI checks must pass before merge; update broken legacy assertions
in the phase that changes their contract, not in a later cleanup.
This P0 documentation change does not build/deploy or modify any other file.

Use synthetic sessions/portraits and test credentials, never personal chats or keys.
Fake clocks test timing; DOM/browser checks test rendering and focus separately.
Capture local visual QA at 1920×1080, 1366×768, and a narrow window without
embedding or linking commercial reference captures in tracked documentation.
Each phase ends with a real-model plus GPT-SoVITS turn and user acceptance;
stand-in success cannot prove model compliance, voice quality, or actual IME behavior.

`scripts/dev_standins.py` starts two loopback-only services with no GPU/model/key.
Run from the repo root, for example:

```powershell
uv run --frozen --extra dev python scripts/dev_standins.py --llm-format mixed --seed 7
```

| Stand-in option / endpoint | Implemented behavior |
| --- | --- |
| LLM port 18080 | OpenAI-compatible `/v1/models` and `/v1/chat/completions`; model `standin`. |
| TTS port 19880 | GPT-SoVITS health/control/configuration and `/tts`; control never restarts real processes. |
| `--llm-format old/lines/mixed/broken` | Old JSON, lines JSON, alternating formats, or cycling malformed replies. |
| `--first-token-ms 800` | Initial response delay; configurable. |
| `--tokens-per-sec 35` | Approximate character rate, not actual tokenizer throughput; pieces contain 1–4 characters. |
| `--tts-base-ms 150` | Fixed synthesis delay before duration-dependent delay. |
| `--tts-latency-factor 0.3` | Extra delay proportional to generated duration. |
| `--tts-fail-rate 0..1` | Seeded synthesis failures with HTTP 500. |
| `--tts-offline` | TTS routes return HTTP 503. |
| `--seed` | Reproducible splitting/failure/envelope sequence for a fixed request sequence. |

Audio is mono 32 kHz, signed 16-bit PCM WAV; duration is `max(0.4, 0.13*len(text))`
seconds, with 4–7Hz amplitude bursts and punctuation pauses. It is synthetic sound,
not intelligible Japanese; it can exercise RMS mouth switching and timing.
The app still validates an active voice profile and reference audio.
Use an isolated test DB/profile; allow private LLM endpoints only in that test setup.
Configure provider `openai`, base `http://127.0.0.1:18080/v1`, key/model `standin`,
and activate it through settings; provider configuration lives in SQLite, not env.
Point `GALGAME2VOICE_GPT_SOVITS_BASE_URL` at `http://127.0.0.1:19880` for the app.
The script returns upstream LLM chunks, not the app's normalized `line` SSE directly.
Both listeners bind before startup; an occupied port fails without half-starting.
Its current implementation is self-contained, with injectable sleeps/factories;
the plan's intended fixture reuse is not an implementation prerequisite.

### 9.2 Phase acceptance matrix

| Phase | Required checks and observable acceptance |
| --- | --- |
| P1 | Import controller directly; assert event order, busy rejection, pre-request cancellation, retry identity, failed-user marking, partial reply persistence, and origin-session isolation. Rewrite both source-slicing tests and import badge logic directly. Old UI/audio remain usable. |
| P2a | Test equal/unequal/single-language pairing, ellipses, punctuation, and lossless aggregation; verify line-before-audio and one terminal per line on ready/skipped/failed, including partial failure. Exercise additive migration twice and reopening with old code. Round-trip all six persistence paths and use fake clocks for line audio ownership/interruption. |
| P2b | Feed scanner every split point around escapes/nesting, fenced/prefaced/truncated arrays, trailing text, unknown acts, and missing emotion. Test `parse_full_text`, Telegram/non-streaming aggregates, menu generation, pose/face legality, sticky false, one-shot omission, per-line refs/cache identity, all prompt sources, and new-format history. Run the 13×20-turn measurement before proposing default enablement. |
| P3 | Test line-player waits/timeouts, late audio, pages, AUTO modes, skip, typing-away queues, hidden window, JS reduced-motion changes, and once-per-line live announcements. Test shared crop/state and stale decode guards. Verify signed image expiry/tamper/traversal and token-on `<img>` loading. Exercise every old-feature row, missing art, and no-voice mode. |
| P4 | LOG pages all history, retains skipped lines, replays only selected line, disables expired replay, and preserves interrupted drafts. Check settings persistence, opacity preview, focus return, keyboard/editor exclusions, and retry/settings recovery. |
| P5 | Validate v3 dimensions/recipes, hair/blush order, crop-origin alignment, unsupported combinations, and whole-image fallback. Test blink timer cancellation, eyes-closed suppression, partner-only mouth frames, analyser silence/end/reset, animation cancellation, and shared crop frames. Visually inspect each of the six planned local packs. |
| P6 | Test manifest/path validation, absent variants, signed URLs, clock boundaries/tab resume, blur edge handling, and background/time round-trip through all three settings boundaries. Missing assets must show CSS sky. |
| P7 | Review two clickable title prototypes from each planned helper before selection; verify Start/Continue/Load/Character/Settings, session thumbnails, newest-session selection, empty history, and character binding. Switch default to ADV, remove obsolete layout IDs, and update their tests in the same phase. |
| P8 | Measure preload budgets/cancellation, transitions, resize/fullscreen, phone portrait with software keyboard, safe-area controls, and reduced-motion behavior. Update README/CHANGELOG and verify source/Docker/portable archives exclude all local assets. |

Broken stand-in mode cycles `fence`, `preamble`, `truncated_string`,
`truncated_lines`, `unknown_act`, `missing_emotion`, and `trailing_text`.
Run at least seven turns to cover that cycle; assert retention of complete lines,
no infinite audio wait, sanitization logs, and useful text on supported fallbacks.
Offline/fail-rate tests need explicit no-deadlock assertions and a successful retry.
Use long TTS delay to verify that voice-priority waits for playback start, that reading mode keeps the 1500ms text fallback, and that stale audio is suppressed.
Use voice mode off to prove line progression needs neither engine nor analyser.
Test session switching during generation/decode and reopen after partial persistence.
Manual Chinese IME tests include candidate commit, Enter, Shift+Enter, Space,
composition cancellation, and Ctrl editing shortcuts while lines are queued.
Real audio/portrait testing remains local; do not add extracted fixtures to CI.

## 10. Open questions and recorded source resolutions

The following refinements are resolved for implementation by this contract:

| Source tension | Resolution |
| --- | --- |
| Plan says small Japanese subtitle; hands-on notes say nearly equal text sizes. | Use 34u primary / 30u subtitle provisionally; retain selectable languages. |
| Notes expose Japanese/English/Simplified/Traditional choices. | App exposes only available zh/ja text and subtitle none; no invented translations. |
| Plan's component wording suggests independently animated eyes/mouth. | Recon found merged facial partials; v3 partner faces are optional and reviewed. |
| Plan mentions roughly 3467px Natsume source height. | That describes a body slice; actual full source canvas is 3750×5313. Preserve origins. |
| Recon inventories six Tenshi heroines; plan targets five. | Keep five plus Natsume in scope; extra inventory is future material. |
| Notes show different AUTO defaults from the plan. | App keeps 1500ms + 45ms/character, clamped, with default voice-priority. |
| Frontend and stand-ins include surprised/thinking beyond seven server emotions. | Keep seven wire emotions; presentation tags remain available; normalize malformed fixture values. |
| Directing report finds no universal symbol-effect axis. | Rare original SVG fx are app features; do not claim they replicate verified game commands. |
| Current P1 controller autoplays chunks; ADV needs queued playback. | P2a adds renderer-selected line ownership while preserving old chat continuous mode. |

Questions requiring prototype/measurement evidence, with safe defaults already set:

1. P3: does bilingual pagination need a taller band at 1366×768 or closer font sizes? Adjust marked provisional geometry after review.
2. P2a: can old-format grouping release stable prefixes without changing indexes? Measure buffering cost before optimizing.
3. P2b: do 13-profile compliance/latency results and acting frequency pass review? Keep the new prompt opt-in meanwhile.
4. P5: which faces have compatible blink/mouth partners across outfit exceptions? No reviewed mapping means static faces.
5. P5: which source blend types need preprocessing? Reject unsupported layers until local comparisons prove equivalence.
6. P6: should clock boundaries or background size become preferences? Ship fixed boundaries and fit-to-stage first.
7. P7: which title prototype and local/original thumbnail art are selected? Store session references, not extracted thumbnails in git.
8. P8: what decoded-image budget suits actual portable/phone devices? Tune the initial caps from memory/frame measurements.

Future multi-speaker generation needs an explicit speaker/kind protocol version; crop identity already works independently of stage visibility.
Source analysis establishes assets/layouts/static frequency; browser quality, latency, IME, and model compliance require phase evidence.
