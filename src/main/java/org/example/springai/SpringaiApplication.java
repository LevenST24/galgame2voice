package org.example.springai;

import org.springframework.ai.model.deepseek.autoconfigure.DeepSeekChatAutoConfiguration;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication(exclude = DeepSeekChatAutoConfiguration.class)
@EnableScheduling
public class SpringaiApplication {

    public static void main(String[] args) {
        SpringApplication.run(SpringaiApplication.class, args);
    }

}
