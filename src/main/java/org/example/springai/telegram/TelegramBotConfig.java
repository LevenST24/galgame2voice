package org.example.springai.telegram;

import lombok.extern.slf4j.Slf4j;
import org.apache.http.client.config.RequestConfig;
import org.example.springai.config.ConfigStore;
import org.example.springai.service.ChatService;
import org.example.springai.service.GptSovitsService;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.telegram.telegrambots.bots.DefaultBotOptions;
import org.telegram.telegrambots.meta.TelegramBotsApi;
import org.telegram.telegrambots.updatesreceivers.DefaultBotSession;

/**
 * Telegram Bot 配置与注册。
 */
@Slf4j
@Configuration
public class TelegramBotConfig {

    @Value("${galgame.telegram-bot-token}")
    private String botToken;
    @Value("${galgame.telegram-bot-username}")
    private String botUsername;
    @Value("${galgame.telegram-proxy-host}")
    private String proxyHost;
    @Value("${galgame.telegram-proxy-port}")
    private int proxyPort;
    @Value("${galgame.ffmpeg-path}")
    private String ffmpegPath;
    @Value("${galgame.console-url}")
    private String consoleUrl;
    @Value("${gpt-sovits.output-dir:audio}")
    private String outputDir;

    @Bean
    public ApplicationRunner telegramBotRunner(ChatService chatService, GptSovitsService gptSovitsService, ConfigStore configStore) {
        return args -> {
            try {
                DefaultBotOptions options = new DefaultBotOptions();
                options.setProxyHost(proxyHost);
                options.setProxyPort(proxyPort);
                options.setProxyType(DefaultBotOptions.ProxyType.HTTP);
                // 显式设置超时：走代理时网络偶尔抖动，避免默认 75s 读超时才失败。
                // 注意 socketTimeout 必须大于长轮询 getUpdatesTimeout(默认 50s)，否则会误伤长轮询。
                options.setRequestConfig(RequestConfig.custom()
                        .setRedirectsEnabled(true)
                        .setConnectTimeout(10_000)
                        .setSocketTimeout(90_000)
                        .setConnectionRequestTimeout(10_000)
                        .build());

                TelegramGalBot bot = new TelegramGalBot(
                        options, botToken, botUsername, chatService, gptSovitsService,
                        configStore, ffmpegPath, outputDir, proxyHost, proxyPort, consoleUrl
                );

                TelegramBotsApi api = new TelegramBotsApi(DefaultBotSession.class);
                api.registerBot(bot);
                log.info("Telegram Bot 已注册: @{}", botUsername);
            } catch (Exception e) {
                log.error("Telegram Bot 注册失败", e);
            }
        };
    }
}
