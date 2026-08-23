package org.example.springai.config;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import java.io.File;

/**
 * 定期清理过期音频文件，避免 tts_*.wav / tts_*.ogg 越积越多占满磁盘。
 */
@Slf4j
@Component
@RequiredArgsConstructor
public class AudioCleanupTask {

    private final GptSovitsProperties props;

    /** 音频保留时长（分钟），超过则删除。 */
    @Value("${gpt-sovits.audio-retention-minutes:30}")
    private long retentionMinutes;

    /** 清理间隔（毫秒），默认每 10 分钟执行一次。 */
    @Scheduled(fixedDelayString = "${gpt-sovits.cleanup-interval-ms:600000}")
    public void cleanup() {
        File dir = new File(props.getOutputDir());
        if (!dir.exists() || !dir.isDirectory()) {
            return;
        }
        long cutoff = System.currentTimeMillis() - retentionMinutes * 60_000L;
        File[] files = dir.listFiles();
        if (files == null) {
            return;
        }
        int removed = 0;
        for (File f : files) {
            if (f.isFile() && f.lastModified() < cutoff && f.delete()) {
                removed++;
            }
        }
        if (removed > 0) {
            log.info("清理过期音频文件 {} 个（目录 {}）", removed, dir.getAbsolutePath());
        }
    }
}
