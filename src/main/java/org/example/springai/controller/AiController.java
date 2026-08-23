package org.example.springai.controller;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.example.springai.service.ChatService;
import org.example.springai.service.GptSovitsService;
import org.example.springai.service.TtsOptions;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.ModelAttribute;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@Slf4j
@RestController
@RequiredArgsConstructor
@RequestMapping("/ai")
public class AiController {

    private final ChatService chatService;
    private final GptSovitsService gptSovitsService;

    @GetMapping("/chat")
    public Map<String, String> chat(
            @RequestParam String prompt,
            @ModelAttribute TtsOptions options,
            @RequestHeader(value = "X-Api-Key", required = false) String apiKey,
            @RequestHeader(value = "X-Base-Url", required = false) String baseUrl,
            @RequestHeader(value = "X-Model", required = false) String model) {

        if (apiKey == null || apiKey.isBlank()) {
            return Map.of("chinese", "请先在右侧「AI 设置」里填写 API Key", "japanese", "", "audioUrl", "");
        }
        if (baseUrl == null || baseUrl.isBlank()) {
            baseUrl = "https://api.deepseek.com";
        }
        if (model == null || model.isBlank()) {
            model = "deepseek-v4-pro";
        }

        ChatService.ChatResult result;
        try {
            result = chatService.chat(prompt, apiKey, baseUrl, model);
        } catch (Exception e) {
            log.warn("AI 调用失败: {}", e.getMessage());
            return Map.of("chinese", "AI 出错：" + e.getMessage(), "japanese", "", "audioUrl", "");
        }

        String audioUrl = "";
        if (result.japanese() != null && !result.japanese().isBlank()) {
            try {
                audioUrl = gptSovitsService.synthesize(result.japanese(), options);
            } catch (Exception e) {
                // 语音合成失败不应阻塞聊天主流程，只返回空的 audioUrl
                log.warn("语音合成失败，audioUrl 置空: {}", e.getMessage());
            }
        }

        return Map.of(
                "chinese", result.chinese(),
                "japanese", result.japanese(),
                "audioUrl", audioUrl
        );
    }
}