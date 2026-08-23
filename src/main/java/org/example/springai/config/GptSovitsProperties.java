package org.example.springai.config;

import lombok.Data;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

@Data
@Component
@ConfigurationProperties(prefix = "gpt-sovits")
public class GptSovitsProperties {
    private String baseUrl;
    private String refAudioPath;
    private String promptText;
    private String promptLang;
    private String textLang;

    /** 生成音频的本地输出目录（相对项目运行目录） */
    private String outputDir = "audio";
}