package org.example.springai.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.Data;
import lombok.extern.slf4j.Slf4j;
import org.example.springai.service.TtsOptions;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import jakarta.annotation.PostConstruct;
import java.io.File;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 多租户配置存储：每个 Telegram 用户（chatId）一份独立配置，token 用于控制台鉴权。
 * 全局默认配置兜底，用户修改后持久化到 configs/<chatId>.json。
 */
@Slf4j
@Component
public class ConfigStore {

    @Value("${galgame.api-key}")
    private String defaultApiKey;

    private static final String CONFIG_DIR = "configs";
    private final ObjectMapper objectMapper = new ObjectMapper();
    private final Map<String, UserConfig> cache = new ConcurrentHashMap<>();
    private final Map<String, String> tokenToChatId = new ConcurrentHashMap<>();

    @PostConstruct
    public void init() {
        File dir = new File(CONFIG_DIR);
        if (!dir.exists()) {
            dir.mkdirs();
        }
    }

    /** 获取某个用户的配置（没有则返回默认配置）。 */
    public synchronized UserConfig get(String chatId) {
        UserConfig cached = cache.get(chatId);
        if (cached != null) {
            return cached;
        }
        UserConfig loaded = loadFromFile(chatId);
        if (loaded != null) {
            cache.put(chatId, loaded);
            return loaded;
        }
        UserConfig def = new UserConfig();
        def.setApiKey(defaultApiKey);
        return def;
    }

    /** 保存某个用户的配置到内存 + 磁盘。 */
    public synchronized void save(String chatId, UserConfig config) {
        cache.put(chatId, config);
        try {
            File dir = new File(CONFIG_DIR);
            if (!dir.exists()) {
                dir.mkdirs();
            }
            objectMapper.writeValue(new File(dir, chatId + ".json"), config);
        } catch (Exception e) {
            log.warn("保存配置失败 chatId={}: {}", chatId, e.getMessage());
        }
    }

    /** 生成（或复用）一个控制台访问 token。 */
    public synchronized String tokenFor(String chatId) {
        for (Map.Entry<String, String> e : tokenToChatId.entrySet()) {
            if (e.getValue().equals(chatId)) {
                return e.getKey();
            }
        }
        String token = UUID.randomUUID().toString().replace("-", "");
        tokenToChatId.put(token, chatId);
        return token;
    }

    /** 用 token 解析出 chatId（校验控制台访问）。 */
    public String resolveToken(String token) {
        return token == null ? null : tokenToChatId.get(token);
    }

    private UserConfig loadFromFile(String chatId) {
        try {
            File f = new File(CONFIG_DIR, chatId + ".json");
            if (f.exists()) {
                return objectMapper.readValue(f, UserConfig.class);
            }
        } catch (Exception e) {
            log.warn("加载配置失败 chatId={}: {}", chatId, e.getMessage());
        }
        return null;
    }

    /** 打码 API Key，避免把真实密钥明文下发到控制台/前端。 */
    public static String mask(String key) {
        if (key == null || key.isBlank()) return "(未设置)";
        if (key.length() <= 8) return "***";
        return key.substring(0, 6) + "…" + key.substring(key.length() - 4);
    }

    /** 单个用户的配置。 */
    @Data
    public static class UserConfig {
        private String apiKey = "";
        private String apiBaseUrl = "https://api.deepseek.com";
        private String chatModel = "deepseek-chat";
        private String sttModel = "";
        private double speedFactor = 1.0;
        private double temperature = 1.0;
        private int topK = 15;
        private double topP = 1.0;
        private int seed = -1;
        private int batchSize = 1;
        private String textSplitMethod = "cut1";
        private double fragmentInterval = 0.3;

        public TtsOptions toTtsOptions() {
            TtsOptions options = new TtsOptions();
            options.setSpeedFactor(speedFactor);
            options.setTemperature(temperature);
            options.setTopK(topK);
            options.setTopP(topP);
            options.setSeed(seed);
            options.setBatchSize(batchSize);
            options.setTextSplitMethod(textSplitMethod);
            options.setFragmentInterval(fragmentInterval);
            return options;
        }
    }
}
