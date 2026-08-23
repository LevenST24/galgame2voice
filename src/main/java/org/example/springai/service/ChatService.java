package org.example.springai.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.RequiredArgsConstructor;
import org.springframework.stereotype.Service;

/**
 * 聊天服务：让 LLM 输出「中文 + 日文」双语内容，
 * 中文用于前端显示，日文用于语音合成。
 */
@Service
@RequiredArgsConstructor
public class ChatService {

    private final AiModelManager aiModelManager;
    private final ObjectMapper objectMapper = new ObjectMapper();
    public record ChatResult(String chinese, String japanese) {}

    /**
     * 发送一条用户消息，返回结构化双语文案。
     */
    public ChatResult chat(String prompt, String apiKey, String baseUrl, String model) {
        String content = aiModelManager.chat(prompt, apiKey, baseUrl, model);
        return parse(content);
    }

    /**
     * 解析 LLM 返回的 JSON。若解析失败则回退为「中文原文 + 空日文」。
     */
    private ChatResult parse(String content) {
        if (content == null || content.isBlank()) {
            return new ChatResult("", "");
        }
        try {
            String json = content.trim();
            if (json.startsWith("```")) {
                int firstNewline = json.indexOf('\n');
                json = json.substring(firstNewline + 1);
                int lastBacktick = json.lastIndexOf("```");
                if (lastBacktick >= 0) {
                    json = json.substring(0, lastBacktick);
                }
                json = json.trim();
            }
            JsonNode node = objectMapper.readTree(json);
            String chinese = node.path("chinese").asText("");
            String japanese = node.path("japanese").asText("");
            return new ChatResult(chinese, japanese);
        } catch (Exception e) {
            return new ChatResult(content.trim(), "");
        }
    }
}