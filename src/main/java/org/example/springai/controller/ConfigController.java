package org.example.springai.controller;

import org.example.springai.config.ConfigStore;
import org.springframework.beans.BeanUtils;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

/**
 * 配置管理接口：控制台页面按 token 读写「自己那个用户」的配置。
 */
@RestController
@RequestMapping("/api/config")
public class ConfigController {

    private final ConfigStore configStore;

    public ConfigController(ConfigStore configStore) {
        this.configStore = configStore;
    }

    @GetMapping
    public ConfigStore.UserConfig get(@RequestParam String token) {
        String chatId = configStore.resolveToken(token);
        if (chatId == null) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "无效的访问凭证");
        }
        ConfigStore.UserConfig config = configStore.get(chatId);
        // 复制一份并打码 API Key，避免把真实密钥明文下发到前端
        ConfigStore.UserConfig safe = new ConfigStore.UserConfig();
        BeanUtils.copyProperties(config, safe);
        safe.setApiKey(ConfigStore.mask(config.getApiKey()));
        return safe;
    }

    @PostMapping
    public ConfigStore.UserConfig update(@RequestParam String token, @RequestBody ConfigStore.UserConfig updates) {
        String chatId = configStore.resolveToken(token);
        if (chatId == null) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED, "无效的访问凭证");
        }
        // 若提交的 key 为空或仍是打码占位符，说明用户没有修改，保留原值
        ConfigStore.UserConfig existing = configStore.get(chatId);
        String submitted = updates.getApiKey();
        if (submitted == null || submitted.isBlank() || submitted.equals(ConfigStore.mask(existing.getApiKey()))) {
            updates.setApiKey(existing.getApiKey());
        }
        configStore.save(chatId, updates);
        return updates;
    }
}

