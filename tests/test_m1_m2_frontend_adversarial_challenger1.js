/**
 * Adversarial Frontend & Controller Stress Harness for M1 & M2.
 * Authored by Challenger 1.
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');

// Mock DOM elements and Web APIs
class MockElement {
    constructor(tagName = 'div') {
        this.tagName = tagName;
        this.classList = {
            classes: new Set(),
            add: (c) => this.classList.classes.add(c),
            remove: (c) => this.classList.classes.delete(c),
            contains: (c) => this.classList.classes.has(c)
        };
        this.listeners = {};
        this.children = [];
        this.innerHTML = '';
        this.textContent = '';
        this.style = {};
        this.attributes = {};
    }

    addEventListener(event, handler) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(handler);
    }

    trigger(event, eventObj = { stopPropagation: () => {} }) {
        if (this.listeners[event]) {
            this.listeners[event].forEach(h => h(eventObj));
        }
    }

    querySelector(selector) {
        if (selector === '.btn-text') {
            return this.btnText || (this.btnText = new MockElement('span'));
        }
        if (selector === '.btn-log-replay') {
            return this.replayBtn || (this.replayBtn = new MockElement('button'));
        }
        return null;
    }

    querySelectorAll(selector) {
        return [];
    }

    setAttribute(name, val) {
        this.attributes[name] = val;
    }

    getAttribute(name) {
        return this.attributes[name];
    }

    appendChild(child) {
        this.children.push(child);
    }
}

global.document = {
    createElement: (tag) => new MockElement(tag),
    getElementById: (id) => new MockElement('div'),
    querySelector: (sel) => new MockElement('div'),
    addEventListener: () => {},
    removeEventListener: () => {}
};
global.window = {
    requestAnimationFrame: (cb) => setTimeout(cb, 16),
    cancelAnimationFrame: (id) => clearTimeout(id)
};

// ----------------------------------------------------------------------------
// Extract and Test Controllers from chat_client.js
// ----------------------------------------------------------------------------

const chatClientPath = path.join(__dirname, '../galgame2voice/static/js/chat_client.js');
const chatClientCode = fs.readFileSync(chatClientPath, 'utf8');

// Evaluate controllers into this test context safely
const controllersContext = {};
const fn = new Function('controllersContext', `
    ${chatClientCode}
    controllersContext.AutoModeController = AutoModeController;
    controllersContext.SkipController = SkipController;
    controllersContext.EmotionManager = EmotionManager;
    controllersContext.LogDrawerController = LogDrawerController;
`);
fn(controllersContext);

const { AutoModeController, SkipController, EmotionManager, LogDrawerController } = controllersContext;

let testsPassed = 0;
let testsFailed = 0;

async function runTest(name, fn) {
    try {
        await fn();
        console.log(`  ✓ ${name}`);
        testsPassed++;
    } catch (err) {
        console.error(`  ✗ ${name}:`, err.message);
        testsFailed++;
    }
}

async function main() {
    console.log('\n=== Challenger 1: Empirical Adversarial Frontend Test Harness ===\n');

    // Test 1: AutoModeController Rapid Clicking
    await runTest('AutoModeController survives 10,000 rapid clicks and maintains state', async () => {
        const btn = new MockElement('button');
        let advanced = false;
        const auto = new AutoModeController({
            defaultDelayMs: 100,
            btnEl: btn,
            onAdvance: () => { advanced = true; }
        });

        for (let i = 0; i < 10000; i++) {
            btn.trigger('click');
        }
        assert.strictEqual(auto.enabled, false, '10,000 toggles should return to false');

        // Toggle to true
        btn.trigger('click');
        assert.strictEqual(auto.enabled, true);
        assert(btn.classList.contains('active'));
    });

    // Test 2: AutoModeController Timer Countdown & Cancel
    await runTest('AutoModeController audio queue finished triggers after delay, canceled on toggle OFF', async () => {
        let count = 0;
        const auto = new AutoModeController({
            defaultDelayMs: 40,
            onAdvance: () => { count++; }
        });
        auto.setEnabled(true);

        auto.onAudioQueueFinished();
        await new Promise(r => setTimeout(r, 60));
        assert.strictEqual(count, 1, 'Auto advance should fire once');

        // Cancel mid countdown
        auto.onAudioQueueFinished();
        await new Promise(r => setTimeout(r, 10));
        auto.setEnabled(false); // Toggle OFF mid countdown
        await new Promise(r => setTimeout(r, 60));
        assert.strictEqual(count, 1, 'Canceled timer should not increment count');
    });

    // Test 3: SkipController Fast Skip
    await runTest('SkipController fast skips typewriter animation and triggers callback', async () => {
        const btn = new MockElement('button');
        let skipped = 0;
        const skip = new SkipController({
            btnEl: btn,
            onSkip: () => { skipped++; }
        });

        for (let i = 0; i < 500; i++) {
            btn.trigger('click');
        }
        assert.strictEqual(skipped, 500);
    });

    // Test 4: EmotionManager 6-Archetype Aura and Fallback
    await runTest('EmotionManager correctly updates aura class, avatar emoji, and badge data', async () => {
        const backdrop = new MockElement('div');
        const avatar = new MockElement('div');
        const badge = new MockElement('div');
        const emoMgr = new EmotionManager({
            backdropEl: backdrop,
            avatarEmojiEl: avatar,
            badgeEl: badge
        });

        const testEmotions = ['gentle', 'shy', 'happy', 'tsundere', 'cool', 'sad'];
        for (const emo of testEmotions) {
            emoMgr.setEmotion(emo);
            assert.strictEqual(emoMgr.getEmotion(), emo);
            assert(backdrop.classList.contains(`emotion-${emo}`));
            assert.strictEqual(badge.getAttribute('data-emotion'), emo);
        }

        // Invalid emotion should be safely ignored
        emoMgr.setEmotion('nonexistent_emotion_xyz');
        assert.strictEqual(emoMgr.getEmotion(), 'sad', 'Invalid emotion should retain previous valid state');
    });

    // Test 5: LogDrawerController HTML Sanitization & XSS Protection
    await runTest('LogDrawerController escapes all HTML/XSS injection attempts in history items', async () => {
        const body = new MockElement('div');
        const drawer = new LogDrawerController({
            bodyEl: body,
            getHistory: () => [
                { role: 'user', content_chinese: '<script>alert("xss")</script>' },
                { role: 'assistant', content_chinese: '"><img src=x onerror=alert(1)>', content_japanese: '<svg/onload=evil()>' }
            ]
        });

        drawer.render();
        assert.strictEqual(body.children.length, 2);

        // Check user item
        const userCard = body.children[0];
        assert(!userCard.innerHTML.includes('<script>'));
        assert(userCard.innerHTML.includes('&lt;script&gt;'));

        // Check assistant item
        const asstCard = body.children[1];
        assert(!asstCard.innerHTML.includes('<img src=x'));
        assert(!asstCard.innerHTML.includes('<svg/onload'));
    });

    // Test 6: LogDrawerController Empty & Huge History Rendering
    await runTest('LogDrawerController handles empty list and 5,000 historical turns smoothly', async () => {
        const body = new MockElement('div');
        let mockHistory = [];
        const drawer = new LogDrawerController({
            bodyEl: body,
            getHistory: () => mockHistory
        });

        // 1. Empty History
        drawer.render();
        assert(body.innerHTML.includes('暂无历史对话记录'));

        // 2. 5,000 Dialogue Items
        mockHistory = [];
        for (let i = 0; i < 5000; i++) {
            mockHistory.push({
                role: i % 2 === 0 ? 'user' : 'assistant',
                content_chinese: `第 ${i} 句对话内容 🌸✨`,
                content_japanese: i % 2 === 0 ? '' : `セリフ #${i}`,
                audio_url: i % 2 === 0 ? '' : `/audio/chunk_${i}.wav`
            });
        }
        drawer.render();
        assert.strictEqual(body.children.length, 5000);
    });

    console.log(`\n========================================`);
    console.log(`Challenger 1 Node Tests Complete: ${testsPassed} passed, ${testsFailed} failed`);
    console.log(`========================================\n`);

    if (testsFailed > 0) {
        process.exit(1);
    }
}

main().catch(err => {
    console.error('Fatal test error:', err);
    process.exit(1);
});
