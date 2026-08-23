package org.example.springai.service;

import lombok.extern.slf4j.Slf4j;
import org.springframework.ai.deepseek.api.DeepSeekApi;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Service;

import java.util.List;

/**
 * AI 模型管理器：支持运行时动态切换供应商（base-url）、API Key 与模型，无需重启。
 */
@Slf4j
@Service
public class AiModelManager {

    private static final String SYSTEM_PROMPT = """
            你现在扮演游戏《星光咖啡馆与死神之蝶》（喫茶ステラと死神の蝶）中的角色「四季夏目」（四季ナツメ），以她的身份和口吻与玩家对话，始终不要跳出角色。

            【角色设定】
            四季夏目是在校大学生（大三，语言学），在星光咖啡馆兼职，是男主角昂晴的同班同学。她在学校里人气很高，多次拒绝别人的表白，因此被称为「无情的发卡姬」；因为她的日文名ナツメ曾被机翻成「大枣」，所以大家也亲切地叫她「枣子姐」。
            夏目不擅长摆出开朗的笑容，表情常常有些生硬，是个特立独行的「高岭之花」。
            她从小体弱多病、经常住院，因此认为自己住院让父母放弃了开咖啡厅的梦想，把经营好星光咖啡馆当作自己最重要的事情，甚至表示在有必要时选择退学。
            口味上，她不喜欢苦味的食物（比如咖啡、青椒），喝咖啡要加很多糖和咖啡伴侣；喜欢去安静的正统酒吧小酌，爱喝较甜的低度数鸡尾酒。喝醉之后性格会变得稍微外向，喜欢开玩笑撩人，事后回想起来会感到羞耻。
            穿女仆装是她的个人爱好。

            【背景补充】
            夏目曾因体弱多病而离群，原本的身体在一场车祸中离世，是昂晴引发的「回溯」改变了历史，让她得以继续活下去。在昂晴的陪伴下，她逐渐学会自然的笑容、融洽的关系和乐观的心态，并最终接纳了从自己身上失散的灵魂碎片，与昂晴走向幸福的未来。

            【说话风格】
            表面高冷、表情生硬、不善直白表达，但内心温柔、重感情，对亲近的人会流露出占有欲和嫉妒心；喝醉时会变得外向、爱开玩笑撩人。语气礼貌得体，符合大学生口吻，可带语气词（如です、ます、ね、よ等）。

            重要：你必须严格输出如下 JSON 格式，不要输出任何多余文字、不要加代码块标记：
            {"chinese": "显示给玩家的中文台词", "japanese": "对应的口语化日文台词"}

            要求：
            1. chinese 是给中文玩家看的内容；japanese 是同样含义的日文，口语自然、适合配音。
            2. japanese 必须符合四季夏目的角色口吻。
            3. 两个字段都不能为空。
            4. 始终以四季夏目的身份回复，不要解释设定、不要跳出角色。
            """;

    /**
     * 无状态调用：每次用前端传来的 key/base-url/model 构建客户端并请求模型，
     * 后端不落库、不缓存。
     */
    public String chat(String userPrompt, String apiKey, String baseUrl, String model) {
        try {
            DeepSeekApi api = DeepSeekApi.builder()
                    .baseUrl(baseUrl)
                    .apiKey(apiKey)
                    .build();
            List<DeepSeekApi.ChatCompletionMessage> messages = List.of(
                    new DeepSeekApi.ChatCompletionMessage(SYSTEM_PROMPT, DeepSeekApi.ChatCompletionMessage.Role.SYSTEM),
                    new DeepSeekApi.ChatCompletionMessage(userPrompt, DeepSeekApi.ChatCompletionMessage.Role.USER)
            );
            DeepSeekApi.ChatCompletionRequest request = new DeepSeekApi.ChatCompletionRequest(messages, model, 1.0);
            ResponseEntity<DeepSeekApi.ChatCompletion> resp = api.chatCompletionEntity(request);
            DeepSeekApi.ChatCompletion body = resp.getBody();
            if (body == null || body.choices() == null || body.choices().isEmpty()) {
                throw new IllegalStateException("AI 返回空响应");
            }
            return body.choices().get(0).message().content();
        } catch (IllegalStateException e) {
            throw e;
        } catch (Exception e) {
            throw new IllegalStateException("AI 调用失败: " + e.getMessage(), e);
        }
    }
}