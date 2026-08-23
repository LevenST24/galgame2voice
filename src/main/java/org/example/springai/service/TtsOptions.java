package org.example.springai.service;

import lombok.Data;

/**
 * 语音合成可调参数（由前端控制台传入，每次请求可变）。
 */
@Data
public class TtsOptions {
    /** 文本切分方式：cut0不切 / cut1凑四句一切 / cut2凑50字一切 / cut3按中文句号 / cut4按英文句号 / cut5按标点 */
    private String textSplitMethod = "cut1";

    /** 语速，越大越快（1.0 为正常） */
    private double speedFactor = 1.0;

    /** 句间停顿秒数 */
    private double fragmentInterval = 0.3;

    /** GPT 采样 top_k */
    private int topK = 15;

    /** GPT 采样 top_p */
    private double topP = 1.0;

    /** GPT 采样温度 */
    private double temperature = 1.0;

    /** 随机种子，-1 表示随机；固定值可复现相同音色（防止随机性） */
    private int seed = -1;

    /** 推理批处理大小：一次合成几个文本片段，越大越快但越占显存 */
    private int batchSize = 1;
}