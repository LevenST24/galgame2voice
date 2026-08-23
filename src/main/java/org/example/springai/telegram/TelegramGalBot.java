package org.example.springai.telegram;

import lombok.extern.slf4j.Slf4j;
import org.example.springai.config.ConfigStore;
import org.example.springai.service.ChatService;
import org.example.springai.service.GptSovitsService;
import org.example.springai.service.TtsOptions;
import org.springframework.core.io.FileSystemResource;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.RestTemplate;
import org.telegram.telegrambots.bots.DefaultBotOptions;
import org.telegram.telegrambots.bots.TelegramLongPollingBot;
import org.telegram.telegrambots.meta.api.methods.BotApiMethod;
import org.telegram.telegrambots.meta.api.methods.GetFile;
import org.telegram.telegrambots.meta.api.methods.send.SendMessage;
import org.telegram.telegrambots.meta.api.methods.send.SendVoice;
import org.telegram.telegrambots.meta.api.objects.InputFile;
import org.telegram.telegrambots.meta.api.objects.Message;
import org.telegram.telegrambots.meta.api.objects.Update;
import org.telegram.telegrambots.meta.api.objects.Voice;
import org.telegram.telegrambots.meta.exceptions.TelegramApiException;

import java.io.File;
import java.net.ConnectException;
import java.net.InetSocketAddress;
import java.net.Proxy;
import java.net.SocketTimeoutException;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Telegram Bot：接收消息 → DeepSeek 双语台词 → GPT-SoVITS 语音 → 发文本 + 语音条。
 */
@Slf4j
public class TelegramGalBot extends TelegramLongPollingBot {

    private final String botToken;
    private final String botUsername;
    private final ChatService chatService;
    private final GptSovitsService gptSovitsService;
    private final ConfigStore configStore;
    private final String ffmpegPath;
    private final String outputDir;
    private final String proxyHost;
    private final int proxyPort;
    private final String consoleUrl;
    private final ExecutorService ttsExecutor;

    public TelegramGalBot(DefaultBotOptions options,
                          String botToken,
                          String botUsername,
                          ChatService chatService,
                          GptSovitsService gptSovitsService,
                          ConfigStore configStore,
                          String ffmpegPath,
                          String outputDir,
                          String proxyHost,
                          int proxyPort,
                          String consoleUrl) {
        super(options);
        this.botToken = botToken;
        this.botUsername = botUsername;
        this.chatService = chatService;
        this.gptSovitsService = gptSovitsService;
        this.configStore = configStore;
        this.ffmpegPath = ffmpegPath;
        this.outputDir = outputDir;
        this.proxyHost = proxyHost;
        this.proxyPort = proxyPort;
        this.consoleUrl = consoleUrl;
        // 语音合成放后台线程，避免慢速 TTS 阻塞消息消费（daemon 线程，随 JVM 退出）
        this.ttsExecutor = Executors.newFixedThreadPool(2, r -> {
            Thread t = new Thread(r, "tts-worker");
            t.setDaemon(true);
            return t;
        });
    }

    @Override
    public String getBotUsername() {
        return botUsername;
    }

    @Override
    public String getBotToken() {
        return botToken;
    }

    @Override
    public void onUpdateReceived(Update update) {
        if (!update.hasMessage()) {
            return;
        }
        var msg = update.getMessage();
        String chatId = msg.getChatId().toString();

        if (msg.hasText()) {
            String text = msg.getText();
            if (text.startsWith("/")) {
                handleCommand(chatId, text);
            } else {
                handleChat(chatId, text);
            }
        } else if (msg.hasVoice()) {
            handleVoice(chatId, msg.getVoice());
        }
    }

    private void handleCommand(String chatId, String text) {
        try {
            String[] parts = text.trim().split("\\s+");
            String cmd = parts[0].toLowerCase();
            ConfigStore.UserConfig c = configStore.get(chatId);
            switch (cmd) {
                case "/start" -> executeWithRetry(new SendMessage(chatId,
                        "你好，我是四季夏目。直接发消息（文字或语音）和我聊天吧。\n\n" + helpText()));
                case "/console" -> {
                    String token = configStore.tokenFor(chatId);
                    executeWithRetry(new SendMessage(chatId, "你的专属控制台（只控制你自己的参数）：\n" + consoleUrl + "/settings.html?token=" + token));
                }
                case "/setkey" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/setkey <你的 API Key>")); return; }
                    c.setApiKey(parts[1]); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ API Key 已更新"));
                }
                case "/seturl" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/seturl <接口地址>")); return; }
                    c.setApiBaseUrl(parts[1]); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ 接口地址已更新"));
                }
                case "/setmodel" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/setmodel <对话模型>")); return; }
                    c.setChatModel(parts[1]); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ 对话模型已更新"));
                }
                case "/setsttmodel" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/setsttmodel <语音识别模型>")); return; }
                    c.setSttModel(parts[1]); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ 语音识别模型已更新"));
                }
                case "/setbatch" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/setbatch <1-8>，当前 " + c.getBatchSize())); return; }
                    c.setBatchSize(Integer.parseInt(parts[1])); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ 批量大小已设为 " + parts[1]));
                }
                case "/settopk" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/settopk <值>，当前 " + c.getTopK())); return; }
                    c.setTopK(Integer.parseInt(parts[1])); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ top_k 已设为 " + parts[1]));
                }
                case "/settopp" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/settopp <0-1.0>，当前 " + c.getTopP())); return; }
                    c.setTopP(Double.parseDouble(parts[1])); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ top_p 已设为 " + parts[1]));
                }
                case "/setseed" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/setseed <整数>，-1 表示随机，当前 " + c.getSeed())); return; }
                    c.setSeed(Integer.parseInt(parts[1])); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ seed 已设为 " + parts[1]));
                }
                case "/setsplit" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/setsplit <cut0-cut5>，当前 " + c.getTextSplitMethod())); return; }
                    c.setTextSplitMethod(parts[1]); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ 切分方式已设为 " + parts[1]));
                }
                case "/speed" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/speed <0.5-2.0>，当前 " + c.getSpeedFactor())); return; }
                    c.setSpeedFactor(Double.parseDouble(parts[1])); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ 语速已设为 " + parts[1]));
                }
                case "/temp" -> {
                    if (parts.length < 2) { executeWithRetry(new SendMessage(chatId, "用法：/temp <0-2.0>，当前 " + c.getTemperature())); return; }
                    c.setTemperature(Double.parseDouble(parts[1])); configStore.save(chatId, c);
                    executeWithRetry(new SendMessage(chatId, "✅ 温度已设为 " + parts[1]));
                }
                case "/model" -> executeWithRetry(new SendMessage(chatId, modelText(c)));
                case "/voice" -> executeWithRetry(new SendMessage(chatId, voiceText(c)));
                case "/help" -> executeWithRetry(new SendMessage(chatId, helpText()));
                default -> executeWithRetry(new SendMessage(chatId, "未知命令，发 /help 查看帮助"));
            }
        } catch (Exception e) {
            log.error("处理命令失败: {}", text, e);
            try { executeWithRetry(new SendMessage(chatId, "命令出错：" + e.getMessage())); } catch (TelegramApiException ignored) {}
        }
    }

    private void handleVoice(String chatId, Voice voice) {
        try {
            executeWithRetry(new SendMessage(chatId, "🎤 正在识别你的语音…"));
            File ogg = downloadFile(execute(new GetFile(voice.getFileId())));
            File wav = new File(ogg.getParentFile(), ogg.getName().replaceFirst("\\.[^.]+$", "") + ".wav");
            runFfmpeg(ffmpegPath, "-y", "-i", ogg.getAbsolutePath(), "-ar", "16000", "-ac", "1", wav.getAbsolutePath());
            String text = stt(wav, configStore.get(chatId));
            handleChat(chatId, text);
        } catch (Exception e) {
            log.error("识别语音失败", e);
            try { executeWithRetry(new SendMessage(chatId, "识别语音失败：" + e.getMessage())); } catch (TelegramApiException ignored) {}
        }
    }

    private void handleChat(String chatId, String prompt) {
        try {
            ConfigStore.UserConfig c = configStore.get(chatId);
            // 1. 生成「中文 + 日文」双语台词
            ChatService.ChatResult result = chatService.chat(prompt, c.getApiKey(), c.getApiBaseUrl(), c.getChatModel());

            // 2. 发中文文本（同步，保证先看到文字）
            if (result.chinese() != null && !result.chinese().isBlank()) {
                executeWithRetry(new SendMessage(chatId, result.chinese()));
            }

            // 3. GPT-SoVITS 合成日文语音 → 放后台线程，避免慢速 TTS 阻塞消息处理
            if (result.japanese() != null && !result.japanese().isBlank()) {
                final String japanese = result.japanese();
                final TtsOptions ttsOptions = c.toTtsOptions();
                ttsExecutor.submit(() -> synthesizeAndSendVoice(chatId, japanese, ttsOptions));
            }
        } catch (Exception e) {
            log.error("处理 Telegram 消息失败", e);
            try {
                executeWithRetry(new SendMessage(chatId, "出错了：" + e.getMessage()));
            } catch (TelegramApiException ignored) {
            }
        }
    }

    /** 在后台线程执行：TTS 合成 → 转 ogg/opus → 发语音条。 */
    private void synthesizeAndSendVoice(String chatId, String japanese, TtsOptions ttsOptions) {
        try {
            String audioUrl = gptSovitsService.synthesize(japanese, ttsOptions);
            File wav = new File(outputDir, audioUrl.substring("/audio/".length()));
            File ogg = convertToOgg(wav);
            execute(new SendVoice(chatId, new InputFile(ogg)));
        } catch (Exception e) {
            log.error("语音合成失败 chatId={}: {}", chatId, e.getMessage());
            try {
                executeWithRetry(new SendMessage(chatId, "语音合成失败：" + e.getMessage()));
            } catch (TelegramApiException ignored) {
            }
        }
    }

    private static final int MAX_SEND_RETRIES = 3;

    /**
     * 发送 Telegram API 方法，带有限次重试。
     * 走代理时网络偶尔抖动，会出现瞬时 SocketTimeoutException / ConnectException，
     * 直接失败会让用户收不到消息，这里做退避重试。
     */
    private Message executeWithRetry(BotApiMethod<Message> method) throws TelegramApiException {
        TelegramApiException last = null;
        for (int attempt = 1; attempt <= MAX_SEND_RETRIES; attempt++) {
            try {
                return execute(method);
            } catch (TelegramApiException e) {
                last = e;
                if (attempt < MAX_SEND_RETRIES && isTransientNetworkError(e)) {
                    log.warn("Telegram API 瞬时网络错误，{}s 后重试 ({}/{}): {}",
                            attempt, attempt, MAX_SEND_RETRIES, e.getMessage());
                    try {
                        Thread.sleep(1000L * attempt);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        throw e;
                    }
                } else {
                    throw e;
                }
            }
        }
        throw last;
    }

    /** 判断是否为可重试的瞬时网络错误（读取超时 / 连接失败）。 */
    private boolean isTransientNetworkError(Throwable t) {
        for (Throwable c = t; c != null; c = c.getCause()) {
            if (c instanceof SocketTimeoutException || c instanceof ConnectException) {
                return true;
            }
        }
        return false;
    }

    /** 调用 OpenAI 兼容的语音识别接口。 */
    private String stt(File wav, ConfigStore.UserConfig c) {
        if (c.getSttModel() == null || c.getSttModel().isBlank()) {
            throw new IllegalStateException("未设置语音识别模型，请用 /setsttmodel 设置（如果当前 API 不支持语音识别，请换支持的服务）");
        }
        RestTemplate rt = new RestTemplate();
        SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
        factory.setProxy(new Proxy(Proxy.Type.HTTP, new InetSocketAddress(proxyHost, proxyPort)));
        rt.setRequestFactory(factory);

        MultiValueMap<String, Object> body = new LinkedMultiValueMap<>();
        body.add("file", new FileSystemResource(wav));
        body.add("model", c.getSttModel());

        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.MULTIPART_FORM_DATA);
        headers.set("Authorization", "Bearer " + c.getApiKey());

        String url = c.getApiBaseUrl().replaceAll("/+$", "") + "/audio/transcriptions";
        try {
            Map<?, ?> resp = rt.postForObject(url, new HttpEntity<>(body, headers), Map.class);
            return resp != null ? String.valueOf(resp.get("text")) : "";
        } catch (Exception e) {
            throw new IllegalStateException("当前 API 不支持语音识别（或识别失败）：" + e.getMessage() + "。请换支持语音识别的 API，或检查 /setsttmodel 模型名");
        }
    }

    /** wav 转 ogg/opus（Telegram 语音条要求 ogg/opus）。 */
    private File convertToOgg(File wav) throws Exception {
        File ogg = new File(wav.getParentFile(), wav.getName().replace(".wav", ".ogg"));
        runFfmpeg(ffmpegPath, "-y", "-i", wav.getAbsolutePath(), "-c:a", "libopus", "-b:a", "64k", ogg.getAbsolutePath());
        return ogg;
    }

    private void runFfmpeg(String... args) throws Exception {
        Process process = new ProcessBuilder(args).start();
        int exit = process.waitFor();
        if (exit != 0) {
            throw new IllegalStateException("ffmpeg 执行失败，exit=" + exit);
        }
    }

    private String modelText(ConfigStore.UserConfig c) {
        return "【模型设置】\n" +
                "• API 地址: " + c.getApiBaseUrl() + "\n" +
                "• API Key: " + ConfigStore.mask(c.getApiKey()) + "\n" +
                "• 对话模型: " + c.getChatModel() + "\n" +
                "• 语音识别模型: " + (c.getSttModel().isBlank() ? "(未设置)" : c.getSttModel());
    }

    private String voiceText(ConfigStore.UserConfig c) {
        return "【语音设置】\n" +
                "• 批量大小 batch_size: " + c.getBatchSize() + "\n" +
                "• 语速 speed: " + c.getSpeedFactor() + "\n" +
                "• 温度 temperature: " + c.getTemperature() + "\n" +
                "• top_k: " + c.getTopK() + "\n" +
                "• top_p: " + c.getTopP() + "\n" +
                "• seed: " + c.getSeed() + "\n" +
                "• 切分方式: " + c.getTextSplitMethod() + "\n" +
                "• 句间停顿: " + c.getFragmentInterval() + "s";
    }

    private String helpText() {
        return "【模型设置】\n" +
                "/model 查看模型设置\n" +
                "/console 获取你的专属控制台链接\n" +
                "/setkey <key> API Key\n" +
                "/seturl <url> 接口地址\n" +
                "/setmodel <model> 对话模型\n" +
                "/setsttmodel <model> 语音识别模型\n" +
                "\n【语音设置】\n" +
                "/voice 查看语音设置\n" +
                "/setbatch <1-8> 批量大小\n" +
                "/speed <0.5-2.0> 语速\n" +
                "/temp <0-2.0> 温度\n" +
                "/settopk <值> top_k\n" +
                "/settopp <0-1.0> top_p\n" +
                "/setseed <整数> seed\n" +
                "/setsplit <cut0-cut5> 切分方式\n" +
                "\n/help 帮助";
    }

}
