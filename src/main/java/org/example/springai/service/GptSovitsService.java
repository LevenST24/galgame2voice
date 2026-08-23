package org.example.springai.service;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.example.springai.config.GptSovitsProperties;
import org.springframework.http.*;
import org.springframework.stereotype.Service;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.client.RestClientException;
import org.springframework.web.client.RestTemplate;

import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/**
 * GPT-SoVITS 语音合成服务。
 *
 * <p>通过 HTTP 调用 GPT-SoVITS 项目的 api_v2.py 接口。
 * 注意：新版 api_v2.py 成功时直接返回 WAV 音频流，失败时返回 JSON；
 * GPT 模型（.ckpt）与 SoVITS 模型（.pth）由 tts_infer.yaml 的 custom 段加载。
 * 这里用日文 {@code textLang = "ja"} 合成，实现「显示中文、口播日文」。
 */
@Slf4j
@Service
@RequiredArgsConstructor
public class GptSovitsService {

    private final GptSovitsProperties props;
    private final RestTemplate restTemplate = new RestTemplate();

    /**
     * 将日文文本合成为音频，返回音频文件的公开访问 URL。
     *
     * @param japaneseText 要朗读的日文文本
     * @return 音频文件访问路径（相对 /audio/ 前缀）
     */
    public String synthesize(String japaneseText, TtsOptions options) {
        // 朗读前清洗：去掉括号及其中的舞台指示/动作描写（如「（眉をひそめて）」），避免被读出来
        String text = stripParentheses(japaneseText);
        Map<String, Object> body = buildRequestBody(text, options);

        ResponseEntity<byte[]> response;
        try {
            response = restTemplate.exchange(
                    props.getBaseUrl() + "/tts",
                    HttpMethod.POST,
                    new HttpEntity<>(body, jsonHeaders()),
                    byte[].class
            );
        } catch (HttpClientErrorException e) {
            // api_v2.py 合成失败时返回 HTTP 400 + JSON 错误信息
            byte[] errBody = e.getResponseBodyAsByteArray();
            String err = (errBody != null && errBody.length > 0)
                    ? new String(errBody, StandardCharsets.UTF_8)
                    : e.getStatusText();
            throw new IllegalStateException("语音合成失败: " + err, e);
        } catch (RestClientException e) {
            throw new IllegalStateException("无法连接 GPT-SoVITS 服务（" + props.getBaseUrl() + "），请确认 api_v2.py 已启动", e);
        }

        // 新版 api_v2.py 成功时直接返回 WAV 音频流（HTTP 200），失败时才返回 JSON
        byte[] audioBytes = response.getBody();
        if (audioBytes == null || audioBytes.length == 0) {
            throw new IllegalStateException("GPT-SoVITS 返回空音频");
        }

        try {
            return saveAudio(audioBytes);
        } catch (IOException e) {
            throw new IllegalStateException("保存音频失败", e);
        }
    }

    /**
     * 去除括号及其中的内容（舞台指示/动作描写），只保留真正要朗读的台词。
     * 支持全角（）与半角()，循环处理嵌套括号。
     */
    private String stripParentheses(String text) {
        if (text == null || text.isBlank()) {
            return text;
        }
        String result = text;
        for (int i = 0; i < 5; i++) {
            String prev = result;
            result = result
                    .replaceAll("（[^（）]*）", "")   // 全角括号
                    .replaceAll("\\([^()]*\\)", "");  // 半角括号
            if (result.equals(prev)) {
                break;
            }
        }
        return result.trim();
    }

    private Map<String, Object> buildRequestBody(String japaneseText, TtsOptions options) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("text", japaneseText);
        body.put("text_lang", props.getTextLang());
        body.put("ref_audio_path", props.getRefAudioPath());
        body.put("prompt_text", props.getPromptText());
        body.put("prompt_lang", props.getPromptLang());
        body.put("text_split_method", options.getTextSplitMethod());
        body.put("batch_size", options.getBatchSize());
        body.put("speed_factor", options.getSpeedFactor());
        body.put("fragment_interval", options.getFragmentInterval());
        body.put("top_k", options.getTopK());
        body.put("top_p", options.getTopP());
        body.put("temperature", options.getTemperature());
        body.put("seed", options.getSeed());
        body.put("media_type", "wav");
        // 注意：新版 api_v2.py 的 /tts 不再通过请求体传模型权重，
        // 模型由 GPT_SoVITS/configs/tts_infer.yaml 的 custom 段配置。
        return body;
    }

    private HttpHeaders jsonHeaders() {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        return headers;
    }

    private String saveAudio(byte[] audioBytes) throws IOException {
        String fileName = "tts_" + UUID.randomUUID() + ".wav";
        java.io.File dir = new java.io.File(props.getOutputDir());
        if (!dir.exists() && !dir.mkdirs()) {
            throw new IOException("无法创建音频输出目录: " + props.getOutputDir());
        }
        java.io.File file = new java.io.File(dir, fileName);
        try (FileOutputStream fos = new FileOutputStream(file)) {
            fos.write(audioBytes);
        }
        return "/audio/" + fileName;
    }
}