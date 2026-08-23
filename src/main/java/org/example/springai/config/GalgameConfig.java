package org.example.springai.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.Data;
import lombok.extern.slf4j.Slf4j;
import org.example.springai.service.TtsOptions;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import jakarta.annotation.PostConstruct;
import java.io.File;

/**
 * 运行时可变的 galgame 配置：一套 API（对话 + 语音识别共用）+ TTS 参数，持久化到 galgame-config.json。
 */
@Data
@Slf4j
@Component
public class GalgameConfig {

    @Value("${galgame.api-key}")
    private String defaultApiKey;

    /** 一套 API 的 Key（LLM 对话 + 语音识别共用） */
    private String apiKey;
    /** 一套 API 的地址（OpenAI 兼容格式），各家可换 */
    private String apiBaseUrl = "https://api.deepseek.com";
    /** 对话模型 */
    private String chatModel = "deepseek-v4-flash";
    /** 语音识别模型（空=未设置，识别前需配置） */
    private String sttModel = "";
    /** TTS 语速（越大越快） */
    private double speedFactor = 1.0;
    /** GPT 采样温度 */
    private double temperature = 1.0;
    private int topK = 15;
    private double topP = 1.0;
    private int seed = -1;
    /** 推理批处理大小（越大越快但越占显存） */
    private int batchSize = 1;
    /** 文本切分方式：cut0不切/cut1四句/cut2五十字/cut3中文句号/cut4英文句号/cut5标点 */
    private String textSplitMethod = "cut1";
    /** 句间停顿秒数 */
    private double fragmentInterval = 0.3;

    private static final String CONFIG_FILE = "galgame-config.json";
    private final ObjectMapper objectMapper = new ObjectMapper();

    @PostConstruct
    public void init() {
        apiKey = defaultApiKey;
        load();
    }

    public synchronized void load() {
        try {
            File f = new File(CONFIG_FILE);
            if (f.exists()) {
                GalgameConfig loaded = objectMapper.readValue(f, GalgameConfig.class);
                if (loaded.getApiKey() != null && !loaded.getApiKey().isBlank()) {
                    this.apiKey = loaded.getApiKey();
                }
                if (loaded.getApiBaseUrl() != null && !loaded.getApiBaseUrl().isBlank()) {
                    this.apiBaseUrl = loaded.getApiBaseUrl();
                }
                if (loaded.getChatModel() != null && !loaded.getChatModel().isBlank()) {
                    this.chatModel = loaded.getChatModel();
                }
                if (loaded.getSttModel() != null) {
                    this.sttModel = loaded.getSttModel();
                }
                this.speedFactor = loaded.getSpeedFactor();
                this.temperature = loaded.getTemperature();
                this.topK = loaded.getTopK();
                this.topP = loaded.getTopP();
                this.seed = loaded.getSeed();
                if (loaded.getBatchSize() > 0) {
                    this.batchSize = loaded.getBatchSize();
                }
                if (loaded.getTextSplitMethod() != null && !loaded.getTextSplitMethod().isBlank()) {
                    this.textSplitMethod = loaded.getTextSplitMethod();
                }
                if (loaded.getFragmentInterval() > 0) {
                    this.fragmentInterval = loaded.getFragmentInterval();
                }
            }
        } catch (Exception e) {
            log.warn("加载 galgame 配置失败: {}", e.getMessage());
        }
    }

    public synchronized void save() {
        try {
            objectMapper.writeValue(new File(CONFIG_FILE), this);
        } catch (Exception e) {
            log.warn("保存 galgame 配置失败: {}", e.getMessage());
        }
    }

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

